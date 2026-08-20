"""Tailoring Service.

Pipeline:
1. JD Analysis (hybrid LLM + deterministic)
2. Compute target character count per bullet (from layout measurer)
3. Tailoring Engine (single LLM pass)
4. Safety Net (fabrication check only — revert overflows to original)
5. Skills fitting + page fitting
"""

from __future__ import annotations

import copy
import logging
from typing import Optional

from backend.analysis.job_analyzer import JobAnalyzer
from backend.models.job_description import JobAnalysis
from backend.models.resume_content import ResumeContent, SectionType
from backend.models.resume_ir import ResumeIR
from backend.models.tailoring import (
    BulletChange,
    KeywordCoverage,
    ResumeBank,
    TailoringResult,
)
from backend.services.resume_bank_service import generate_bank_from_ir
from backend.tailoring.claim_validator import ClaimValidator
from backend.tailoring.tailoring_engine import TailoringEngine

logger = logging.getLogger(__name__)


class TailoringService:
    """Orchestrate resume tailoring with unified LLM pipeline."""

    def __init__(self, use_llm: bool = True):
        self._use_llm = use_llm

    def analyze_jd(self, jd_text: str) -> JobAnalysis:
        """Step 1: Analyze a job description (hybrid LLM + deterministic)."""
        analyzer = JobAnalyzer(use_llm=self._use_llm)
        return analyzer.analyze(jd_text)

    def tailor(
        self,
        ir: ResumeIR,
        jd: JobAnalysis,
        bank: Optional[ResumeBank] = None,
        max_bullets_per_entry: int = 4,
        one_line_bullets: bool = True,
        enforce_single_line: bool = True,
        max_bullet_chars: int = 115,
    ) -> TailoringResult:
        """Run the tailoring pipeline."""
        if bank is None:
            bank = generate_bank_from_ir(ir)

        # === STAGE 1: Set up calibrated measurer ===
        # Calibrates against original bullets — the widest one that
        # fits on one line defines the real line capacity.
        from backend.tailoring.bullet_measurer import BulletMeasurer
        measurer = None
        try:
            measurer = BulletMeasurer(ir.layout, content=ir.content)
        except Exception:
            pass

        # === STAGE 2: Unified LLM tailoring pass ===
        result = TailoringResult(resume_id="", job_title=jd.job_title)

        if self._use_llm:
            # Get calibrated max chars from measurer
            max_chars = measurer.max_chars_for_line() if measurer else 120
            result.debug_log.append(
                f"Line capacity: {max_chars} chars"
                + (f", calibrated width: {measurer._calibrated_width_pt:.0f}pt"
                   f", font: {measurer.font_name}" if measurer else "")
            )

            engine = TailoringEngine()
            engine_result = engine.tailor(
                content=ir.content,
                jd_raw_text=jd.raw_text,
                max_chars_per_line=max_chars,
                max_bullets_per_entry=max_bullets_per_entry,
                available_width_pt=measurer.raw_width_pt if measurer else 0,
                font_name=measurer.font_name if measurer else "",
                font_size=measurer.font_size if measurer else 0,
            )

            if engine_result.llm_used:
                result.bullet_changes = engine_result.bullet_changes
                result.planning_used = True
                result.planning_duration_ms = engine_result.duration_ms

                # Log what the LLM returned
                llm_rewrites = sum(
                    1 for c in result.bullet_changes if c.action == "rewrite"
                )
                llm_keeps = sum(
                    1 for c in result.bullet_changes if c.action == "keep"
                )
                llm_removes = sum(
                    1 for c in result.bullet_changes if c.action == "remove"
                )
                result.debug_log.append(
                    f"LLM returned: {llm_rewrites} rewrites, "
                    f"{llm_keeps} keeps, {llm_removes} removes "
                    f"({engine_result.duration_ms}ms)"
                )

                for cat, skills in engine_result.skill_reorders.items():
                    result.reordered_skills[cat] = skills
                for cat, skills in engine_result.skill_additions.items():
                    result.added_skills[cat] = skills
            else:
                result.planning_error = engine_result.llm_error
                result.debug_log.append(
                    f"LLM FAILED: {engine_result.llm_error}"
                )
                logger.warning(
                    f"Tailoring engine failed: {engine_result.llm_error}"
                )

        # If LLM didn't produce results, fall back to keep-all
        if not result.bullet_changes:
            result.bullet_changes = self._deterministic_fallback(ir.content)

        # === STAGE 3: Safety net (fabrication + overflow revert) ===
        try:
            from backend.config import get_config
            config = get_config()
            validator = ClaimValidator(config.validation)
            self._run_safety_net(result, bank, ir, validator, measurer)
        except Exception as e:
            # Graceful degradation: if validation fails, keep original bullets
            result.debug_log.append(
                f"SAFETY NET FAILED (using original bullets): {str(e)[:100]}"
            )
            logger.warning(f"Safety net error (graceful fallback): {e}")
            # Revert all rewrites to keeps
            for change in result.bullet_changes:
                if change.action == "rewrite":
                    change.tailored_text = change.original_text
                    change.action = "keep"
                    change.reason = "Safety net error; using original"

        # === STAGE 4: Build coverage report ===
        try:
            from backend.tailoring.matcher import Matcher
            matcher = Matcher(jd, ir.content, bank)
            match_result = matcher.match()
            self._build_coverage_report(result, ir, jd, match_result)
        except Exception as e:
            # Graceful degradation: coverage report failure doesn't block result
            result.debug_log.append(
                f"COVERAGE REPORT FAILED: {str(e)[:100]}"
            )
            logger.warning(f"Coverage report error (continuing anyway): {e}")
            # Still return the result; coverage metrics just won't be populated

        return result

    # ------------------------------------------------------------------
    # Safety net
    # ------------------------------------------------------------------

    def _run_safety_net(
        self,
        result: TailoringResult,
        bank: ResumeBank,
        ir: ResumeIR,
        validator: ClaimValidator,
        measurer,
    ):
        """Fabrication check + overflow revert. No shortening pass."""
        # Build full resume text so validator can check skills section too
        full_resume_text = self._build_full_resume_text(ir.content)

        for change in result.bullet_changes:
            if change.action != "rewrite":
                continue

            # Identical to original
            if change.tailored_text.strip() == change.original_text.strip():
                change.tailored_text = change.original_text
                change.action = "keep"
                change.reason = "No meaningful change"
                result.debug_log.append(
                    f"REVERTED {change.bullet_id}: identical to original"
                )
                continue

            # Rewrite only removed words without adding anything
            orig_words = set(change.original_text.lower().split())
            new_words = set(change.tailored_text.lower().split())
            if not (new_words - orig_words):
                change.tailored_text = change.original_text
                change.action = "keep"
                change.reason = "Rewrite only removed words"
                result.debug_log.append(
                    f"REVERTED {change.bullet_id}: only removed words, "
                    f"added nothing new"
                )
                continue

            # Validate claimed keywords are actually present
            if change.target_keywords:
                text_lower = change.tailored_text.lower()
                change.target_keywords = [
                    kw for kw in change.target_keywords
                    if kw.lower() in text_lower
                ]

            # Fabrication check — include full resume as context
            # so skills listed in the Skills section count as evidence
            facts = self._get_facts_for_bullet(
                change.bullet_id, bank, ir.content,
            )
            facts_with_resume = facts + [
                {"id": "_resume", "text": full_resume_text},
            ]
            validation = validator.validate(change, facts_with_resume)
            if not validation.valid:
                result.debug_log.append(
                    f"REVERTED {change.bullet_id}: {'; '.join(validation.issues)}"
                    f" | new_text: \"{change.tailored_text[:60]}...\""
                )
                change.tailored_text = change.original_text
                change.action = "keep"
                change.reason = (
                    f"Rewrite rejected: {'; '.join(validation.issues)}"
                )
                continue

            # Metric preservation warning
            if validation.metric_warnings:
                change.reason = (
                    (change.reason or "")
                    + f" | WARNING: {'; '.join(validation.metric_warnings)}"
                )

            # Passed all checks (overflow handled in batch below)
            if change.action == "rewrite":
                result.debug_log.append(
                    f"VALIDATED {change.bullet_id}: "
                    f"\"{change.tailored_text[:60]}...\""
                )

        # === REJECTION FEEDBACK LOOP ===
        # Collect bullets that failed validation and retry them
        rejected_bullets = [
            (change, change.reason.split(": ", 1)[1].split("; ") if "Rewrite rejected:" in change.reason else [])
            for change in result.bullet_changes
            if change.action == "keep" and "Rewrite rejected:" in (change.reason or "")
        ]

        if rejected_bullets and len(rejected_bullets) <= 5:
            result.debug_log.append(
                f"RETRY LOOP: Showing model why {len(rejected_bullets)} bullets failed validation"
            )
            from backend.tailoring.tailoring_engine import TailoringEngine

            engine = TailoringEngine()
            retry_result = engine.retry_rejected_bullets(rejected_bullets, ir.content)

            if retry_result.llm_used:
                # Merge retry results back into main results
                retry_map = {c.bullet_id: c for c in retry_result.bullet_changes}
                for change in result.bullet_changes:
                    if change.bullet_id in retry_map:
                        retry_change = retry_map[change.bullet_id]
                        if retry_change.action == "rewrite":
                            # Re-validate the retry before accepting it
                            facts = self._get_facts_for_bullet(
                                change.bullet_id, bank, ir.content,
                            )
                            facts_with_resume = facts + [
                                {"id": "_resume", "text": full_resume_text},
                            ]
                            validation = validator.validate(retry_change, facts_with_resume)
                            if validation.valid:
                                change.tailored_text = retry_change.tailored_text
                                change.action = "rewrite"
                                change.reason = (
                                    retry_change.reason + " (retry after feedback)"
                                )
                                result.debug_log.append(
                                    f"RETRY ACCEPTED {change.bullet_id}: "
                                    f"\"{change.tailored_text[:60]}...\""
                                )
                            else:
                                result.debug_log.append(
                                    f"RETRY REJECTED {change.bullet_id}: "
                                    f"still failed: {'; '.join(validation.issues)}"
                                )
                        else:
                            result.debug_log.append(
                                f"RETRY ACTION={retry_change.action} {change.bullet_id}"
                            )

        # === Batch overflow trimming ===
        if measurer:
            self._batch_trim_overflows(result, measurer, validator, bank, ir)

        # Dedup pass: check for repeated adjectives/descriptors
        self._dedup_adjectives(result)

    def _batch_trim_overflows(
        self,
        result: TailoringResult,
        measurer,
        validator: ClaimValidator,
        bank: ResumeBank,
        ir: ResumeIR,
    ):
        """Batch-trim all overflowing bullets in 1-2 LLM calls.

        Re-validates trimmed text against claim validator to prevent
        fabricated terms from being introduced during trimming.
        """
        from backend.tailoring.tailoring_engine import (
            TailoringEngine,
            _compute_char_cap,
        )

        # Collect overflowing rewrites
        overflow_changes: list[BulletChange] = []
        for change in result.bullet_changes:
            if change.action != "rewrite":
                continue
            measurement = measurer.measure(change.tailored_text)
            if not measurement.fits_one_line:
                overflow_changes.append(change)

        if not overflow_changes:
            return

        trimmer = TailoringEngine()
        full_resume_text = self._build_full_resume_text(ir.content)

        # Up to 3 batch rounds
        for round_num in range(3):
            # Build batch request
            batch_items: list[dict] = []
            for change in overflow_changes:
                break_idx = measurer.find_line_break(change.tailored_text)
                char_cap = _compute_char_cap(
                    change.tailored_text,
                    measurer.raw_width_pt,
                    measurer.font_name,
                    measurer.font_size,
                )
                overflow_text = change.tailored_text[break_idx:]
                measurement = measurer.measure(change.tailored_text)

                result.debug_log.append(
                    f"TRIMMING {change.bullet_id} (round {round_num + 1}): "
                    f"{len(change.tailored_text)} chars, "
                    f"{measurement.rendered_width_pt:.0f}pt "
                    f"> {measurement.available_width_pt:.0f}pt "
                    f"(char cap: {char_cap}, "
                    f"overflow: \"{overflow_text[:30]}\")"
                )

                batch_items.append({
                    "bullet_id": change.bullet_id,
                    "text": change.tailored_text,
                    "break_index": break_idx,
                    "max_chars": char_cap,
                    "keywords": change.target_keywords or [],
                })

            # Single LLM call for all overflowing bullets
            trim_results = trimmer.batch_trim_bullets(
                batch_items, is_retry=(round_num > 0),
            )

            # Validate each result individually
            still_overflowing: list[BulletChange] = []
            for change in overflow_changes:
                trimmed = trim_results.get(change.bullet_id)

                is_final = round_num == 2

                if not trimmed:
                    result.debug_log.append(
                        f"TRIM FAIL {change.bullet_id}: "
                        f"round {round_num + 1} returned nothing"
                    )
                    if is_final:
                        result.debug_log.append(
                            f"REVERTED {change.bullet_id}: trim failed"
                        )
                        change.tailored_text = change.original_text
                        change.action = "keep"
                        change.reason = "Rewrite too long, trim failed"
                    else:
                        still_overflowing.append(change)
                    continue

                # === Re-validate trimmed text ===
                original_text = change.tailored_text
                change.tailored_text = trimmed
                facts = self._get_facts_for_bullet(change.bullet_id, bank, ir.content)
                facts_with_resume = facts + [{"id": "_resume", "text": full_resume_text}]
                validation = validator.validate(change, facts_with_resume)

                if not validation.valid:
                    result.debug_log.append(
                        f"TRIMMED TEXT FAILED VALIDATION {change.bullet_id}: "
                        f"{'; '.join(validation.issues)}"
                    )
                    change.tailored_text = change.original_text
                    change.action = "keep"
                    change.reason = "Rewrite too long, trim failed"
                    continue

                trim_m = measurer.measure(trimmed)
                if trim_m.fits_one_line:
                    change.reason = (
                        (change.reason or "") + " (trimmed to fit)"
                    )
                    result.debug_log.append(
                        f"TRIMMED {change.bullet_id}: "
                        f"{len(trimmed)} chars, "
                        f"{trim_m.rendered_width_pt:.0f}pt "
                        f"(round {round_num + 1})"
                    )
                elif len(trimmed) >= len(original_text):
                    # Stuck — no progress
                    result.debug_log.append(
                        f"TRIM STUCK {change.bullet_id}: "
                        f"{len(trimmed)} chars (no progress)"
                    )
                    if is_final:
                        result.debug_log.append(
                            f"REVERTED {change.bullet_id}: trim failed"
                        )
                        change.tailored_text = change.original_text
                        change.action = "keep"
                        change.reason = "Rewrite too long, trim failed"
                    else:
                        still_overflowing.append(change)
                else:
                    # Made progress but still overflows — use shorter
                    # text for retry
                    result.debug_log.append(
                        f"RETRIM {change.bullet_id}: "
                        f"{len(trimmed)} chars, "
                        f"{trim_m.rendered_width_pt:.0f}pt"
                    )
                    if is_final:
                        result.debug_log.append(
                            f"REVERTED {change.bullet_id}: "
                            f"still overflows after 3 rounds"
                        )
                        change.tailored_text = change.original_text
                        change.action = "keep"
                        change.reason = "Rewrite too long, trim failed"
                    else:
                        still_overflowing.append(change)

            # Prepare next round with only the failures
            overflow_changes = still_overflowing
            if not overflow_changes:
                break

        # Log accepted rewrites
        for change in result.bullet_changes:
            if change.action == "rewrite":
                result.debug_log.append(
                    f"ACCEPTED {change.bullet_id}: "
                    f"\"{change.tailored_text[:60]}...\""
                )

    @staticmethod
    def _dedup_adjectives(result: TailoringResult):
        """Flag rewrites that overuse the same adjective/descriptor.

        If the same descriptive word appears in 3+ rewritten bullets,
        log a warning. Common verbs and tech terms are excluded.
        """
        from collections import Counter

        # Words that are fine to repeat (tech terms, common verbs)
        EXEMPT = {
            "python", "react", "javascript", "typescript", "sql",
            "fastapi", "node.js", "docker", "mongodb", "postgresql",
            "api", "apis", "rest", "data", "system", "application",
            "developed", "built", "designed", "implemented", "created",
            "tested", "deployed", "integrated", "automated", "led",
            "using", "with", "for", "and", "the", "across", "from",
            "processing", "tracking", "managing", "handling",
            "full-stack", "frontend", "backend",
        }

        word_counts: Counter = Counter()
        word_bullets: dict[str, list[str]] = {}

        for change in result.bullet_changes:
            if change.action != "rewrite":
                continue
            # Find words that are NEW (not in original)
            orig_words = set(change.original_text.lower().split())
            new_words = set(change.tailored_text.lower().split())
            added = new_words - orig_words
            for word in added:
                clean = word.strip(",.;:()")
                if clean in EXEMPT or len(clean) <= 3:
                    continue
                word_counts[clean] += 1
                word_bullets.setdefault(clean, []).append(change.bullet_id)

        for word, count in word_counts.most_common(10):
            if count >= 3:
                bullets = word_bullets[word]
                result.debug_log.append(
                    f"REPEATED '{word}' added to {count} bullets: "
                    f"{', '.join(bullets)}"
                )

    # ------------------------------------------------------------------
    # Deterministic fallback
    # ------------------------------------------------------------------

    def _deterministic_fallback(
        self, content: ResumeContent,
    ) -> list[BulletChange]:
        """When LLM is unavailable, keep all bullets unchanged."""
        changes: list[BulletChange] = []
        for section in content.sections:
            for entry in section.experience_entries:
                for b in entry.bullets:
                    changes.append(BulletChange(
                        bullet_id=b.id,
                        original_text=b.text,
                        tailored_text=b.text,
                        action="keep",
                        reason="LLM not available",
                    ))
            for entry in section.project_entries:
                for b in entry.bullets:
                    changes.append(BulletChange(
                        bullet_id=b.id,
                        original_text=b.text,
                        tailored_text=b.text,
                        action="keep",
                        reason="LLM not available",
                    ))
        return changes

    # ------------------------------------------------------------------
    # Coverage report
    # ------------------------------------------------------------------

    def _build_coverage_report(
        self,
        result: TailoringResult,
        ir: ResumeIR,
        jd: JobAnalysis,
        match_result,
    ):
        """Build keyword coverage report."""
        all_jd_keywords = jd.all_keywords()
        resume_text = " ".join(
            c.tailored_text.lower()
            for c in result.bullet_changes
            if c.action != "remove"
        )
        for section in ir.content.sections:
            for cat in section.skill_categories:
                resume_text += " " + " ".join(s.lower() for s in cat.skills)

        for kw in all_jd_keywords[:30]:
            skill_info = next(
                (s for s in jd.all_skills_flat()
                 if s.name.lower() == kw.lower()),
                None,
            )
            importance = skill_info.importance if skill_info else 0.3

            if kw.lower() in resume_text:
                orig_text = " ".join(
                    c.original_text.lower() for c in result.bullet_changes
                )
                for section in ir.content.sections:
                    for cat in section.skill_categories:
                        orig_text += " " + " ".join(
                            s.lower() for s in cat.skills
                        )
                status = "matched" if kw.lower() in orig_text else "added"
                source = (
                    "present in resume"
                    if status == "matched"
                    else "added via rewrite"
                )
            else:
                status = "missing"
                source = "not in resume bank"

            result.keyword_coverage.append(KeywordCoverage(
                keyword=kw,
                importance=importance,
                status=status,
                source=source,
            ))

        result.required_skill_coverage = match_result.required_coverage
        result.technical_keyword_coverage = match_result.technical_coverage
        result.responsibility_coverage = match_result.responsibility_coverage

    # ------------------------------------------------------------------
    # Apply tailoring
    # ------------------------------------------------------------------

    def apply_tailoring(
        self,
        ir: ResumeIR,
        result: TailoringResult,
        fit_skills: bool = True,
    ) -> ResumeIR:
        """Apply tailoring result to produce a new ResumeIR."""
        new_ir = copy.deepcopy(ir)

        accepted_changes: dict[str, BulletChange] = {}
        for c in result.bullet_changes:
            if c.action == "rewrite" and c.accepted:
                accepted_changes[c.bullet_id] = c
            elif c.action == "remove":
                accepted_changes[c.bullet_id] = c

        for section in new_ir.content.sections:
            if section.type in (SectionType.EXPERIENCE, SectionType.VOLUNTEER):
                for entry in section.experience_entries:
                    new_bullets = []
                    for bullet in entry.bullets:
                        change = accepted_changes.get(bullet.id)
                        if change is None:
                            new_bullets.append(bullet)
                        elif change.action == "rewrite":
                            bullet.original_text = bullet.text
                            bullet.text = change.tailored_text
                            bullet.source_fact_ids = change.source_fact_ids
                            bullet.target_keywords = change.target_keywords
                            new_bullets.append(bullet)
                        elif change.action == "remove":
                            continue
                    entry.bullets = new_bullets

            elif section.type == SectionType.PROJECTS:
                for entry in section.project_entries:
                    new_bullets = []
                    for bullet in entry.bullets:
                        change = accepted_changes.get(bullet.id)
                        if change is None:
                            new_bullets.append(bullet)
                        elif change.action == "rewrite":
                            bullet.original_text = bullet.text
                            bullet.text = change.tailored_text
                            bullet.source_fact_ids = change.source_fact_ids
                            bullet.target_keywords = change.target_keywords
                            new_bullets.append(bullet)
                        elif change.action == "remove":
                            continue
                    entry.bullets = new_bullets

            elif section.type == SectionType.SKILLS:
                for cat in section.skill_categories:
                    if cat.category in result.added_skills:
                        existing = {s.lower() for s in cat.skills}
                        for new_skill in result.added_skills[cat.category]:
                            key = f"{cat.category}:{new_skill}"
                            accepted = result.additions_accepted.get(key, True)
                            if accepted and new_skill.lower() not in existing:
                                # === CHANGE: Mark LLM-added skills with internal marker ===
                                # When skills fitting trims the row, it can see which skills
                                # came from the LLM (JD-matched) vs original resume.
                                # Trim the original skills first; protect the LLM additions.
                                marked_skill = f"[LLM]{new_skill}"
                                cat.skills.append(marked_skill)
                                existing.add(new_skill.lower())

                    if cat.category in result.reordered_skills:
                        accepted = result.reorder_accepted.get(
                            cat.category, True,
                        )
                        if accepted:
                            reordered = list(
                                result.reordered_skills[cat.category]
                            )
                            existing_reordered = {
                                s.lower() for s in reordered
                            }
                            for s in cat.skills:
                                if s.lower() not in existing_reordered:
                                    reordered.append(s)
                            cat.skills = reordered

        if fit_skills:
            self._fit_skills_to_line(new_ir, result)

        self._sync_elements_with_content(
            new_ir, accepted_changes, result.reordered_skills,
        )
        return new_ir

    def _fit_skills_to_line(
        self,
        ir: ResumeIR,
        result: Optional[TailoringResult] = None,
    ):
        """Trim skills rows so each fits on a single rendered line.

        === PRIORITY CHANGE ===
        When trimming overflowing skills rows, PROTECT skills marked with [LLM].
        These are JD-matched additions from the tailoring engine.
        We trim original resume skills first; only trim [LLM] skills if absolutely necessary.
        """
        from backend.tailoring.bullet_measurer import BulletMeasurer

        try:
            measurer = BulletMeasurer(ir.layout, content=ir.content)
        except Exception:
            return

        for section in ir.content.sections:
            if section.type != SectionType.SKILLS:
                continue
            for cat in section.skill_categories:
                row_text = f"{cat.category}: {', '.join(cat.skills)}"
                m = measurer.measure_line(row_text)
                if m.fits_one_line:
                    # === Clean [LLM] markers even if row fits ===
                    cat.skills = [s.replace("[LLM]", "").strip() for s in cat.skills]
                    continue

                # === Trim with protection for [LLM]-marked skills ===
                while len(cat.skills) > 1:
                    # Find the last non-[LLM] skill to remove
                    removed_idx = None
                    for i in range(len(cat.skills) - 1, -1, -1):
                        if not cat.skills[i].startswith("[LLM]"):
                            removed_idx = i
                            break

                    # If all remaining are [LLM], stop
                    if removed_idx is None:
                        break

                    removed = cat.skills.pop(removed_idx)
                    if result:
                        # Clean the marker for the result tracking
                        clean_removed = removed.replace("[LLM]", "").strip()
                        key = f"{cat.category}:{clean_removed}"
                        if key not in result.additions_accepted:
                            result.additions_accepted[key] = False

                    row_text = f"{cat.category}: {', '.join(cat.skills)}"
                    m = measurer.measure_line(row_text)
                    if m.fits_one_line:
                        break

                # === Clean [LLM] markers from final result ===
                cat.skills = [s.replace("[LLM]", "").strip() for s in cat.skills]

    def _sync_elements_with_content(
        self,
        ir: ResumeIR,
        changes: dict[str, BulletChange],
        reordered_skills: dict[str, list[str]],
    ):
        """Update layout element run text to match content changes."""
        from backend.models.resume_layout import ElementType, RunFormat

        skills_texts: dict[str, str] = {}
        for section in ir.content.sections:
            for cat in section.skill_categories:
                # === Clean any [LLM] markers (safety measure) ===
                clean_skills = [s.replace("[LLM]", "").strip() for s in cat.skills]
                skills_texts[cat.category] = (
                    f"{cat.category}: " + ", ".join(clean_skills)
                )

        new_elements = []
        for el in ir.layout.elements:
            if el.element_type == ElementType.BULLET:
                orig_text = "".join(
                    r.text for r in el.paragraph_format.runs if not r.is_tab
                ).strip()

                matched_change = None
                for change in changes.values():
                    clean_orig = orig_text.lstrip("• ").strip()
                    if (
                        clean_orig
                        and change.original_text
                        and clean_orig[:30] == change.original_text[:30]
                    ):
                        matched_change = change
                        break

                if matched_change and matched_change.action == "remove":
                    continue

                if matched_change and matched_change.action == "rewrite":
                    new_text = "• " + matched_change.tailored_text
                    if el.paragraph_format.runs:
                        first_run = el.paragraph_format.runs[0]
                        el.paragraph_format.runs = [RunFormat(
                            text=new_text,
                            font_family=first_run.font_family,
                            font_size_half_pt=first_run.font_size_half_pt,
                            bold=first_run.bold,
                            bold_cs=first_run.bold_cs,
                            italic=first_run.italic,
                            italic_cs=first_run.italic_cs,
                            color=first_run.color,
                        )]
                    else:
                        el.paragraph_format.runs = [RunFormat(text=new_text)]

                new_elements.append(el)

            elif el.element_type == ElementType.SKILLS_ROW:
                orig_text = "".join(
                    r.text for r in el.paragraph_format.runs if not r.is_tab
                ).strip()

                matched_cat = None
                for cat_name in (
                    list(reordered_skills.keys()) + list(skills_texts.keys())
                ):
                    if orig_text.startswith(cat_name + ":"):
                        matched_cat = cat_name
                        break

                if matched_cat and matched_cat in skills_texts:
                    new_text = skills_texts[matched_cat]
                    if ":" in new_text:
                        label, value = new_text.split(":", 1)
                        first_run = (
                            el.paragraph_format.runs[0]
                            if el.paragraph_format.runs
                            else RunFormat()
                        )
                        el.paragraph_format.runs = [
                            RunFormat(
                                text=label + ": ",
                                font_family=first_run.font_family,
                                font_size_half_pt=first_run.font_size_half_pt,
                                bold=True,
                                bold_cs=first_run.bold_cs,
                            ),
                            RunFormat(
                                text=value.strip(),
                                font_family=first_run.font_family,
                                font_size_half_pt=first_run.font_size_half_pt,
                                bold=False,
                            ),
                        ]
                new_elements.append(el)
            else:
                new_elements.append(el)

        ir.layout.elements = new_elements

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_full_resume_text(content: ResumeContent) -> str:
        """Build full resume text for validator context."""
        parts: list[str] = []
        for section in content.sections:
            for entry in section.experience_entries:
                parts.append(f"{entry.company} {entry.role}")
                for b in entry.bullets:
                    parts.append(b.text)
            for entry in section.project_entries:
                parts.append(entry.name)
                for b in entry.bullets:
                    parts.append(b.text)
            for cat in section.skill_categories:
                parts.append(f"{cat.category}: {' '.join(cat.skills)}")
        return " ".join(parts)

    def _get_facts_for_bullet(
        self,
        bullet_id: str,
        bank: ResumeBank,
        content: ResumeContent,
    ) -> list[dict]:
        """Get source facts for a bullet from the bank."""
        entry_id = ""
        for section in content.sections:
            for entry in section.experience_entries:
                for b in entry.bullets:
                    if b.id == bullet_id:
                        entry_id = entry.id
                        break
            for entry in section.project_entries:
                for b in entry.bullets:
                    if b.id == bullet_id:
                        entry_id = entry.id
                        break

        for source in [
            bank.find_experience(entry_id),
            bank.find_project(entry_id),
        ]:
            if source:
                for ab in source.approved_bullets:
                    if ab.id == bullet_id:
                        matched = [
                            {"id": f.id, "text": f.text}
                            for f in source.facts
                            if f.id in ab.source_fact_ids
                        ]
                        return matched or [
                            {"id": f.id, "text": f.text}
                            for f in source.facts
                        ]
                return [
                    {"id": f.id, "text": f.text} for f in source.facts
                ]
        return []
