"""Tailoring Service.

Simplified pipeline:
1. JD Analysis (hybrid LLM + deterministic)
2. Matcher (deterministic coverage report)
3. Tailoring Engine (single LLM pass — rewrites, removals, skill changes)
4. Safety Net (fabrication check + layout fit)
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
from backend.tailoring.matcher import Matcher
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
        self._one_line_bullets = one_line_bullets
        self._max_bullets = max_bullets_per_entry

        if bank is None:
            bank = generate_bank_from_ir(ir)

        # === STAGE 1: Deterministic matching (coverage report) ===
        matcher = Matcher(jd, ir.content, bank)
        match_result = matcher.match()

        # === STAGE 2: Compute per-bullet character budgets ===
        from backend.tailoring.bullet_measurer import BulletMeasurer
        try:
            measurer = BulletMeasurer(ir.layout)
            char_budgets = self._compute_char_budgets(ir.content, measurer)
        except Exception:
            # Fallback: use max_bullet_chars
            char_budgets = self._fallback_char_budgets(ir.content, max_bullet_chars)

        # === STAGE 3: Unified LLM tailoring pass ===
        result = TailoringResult(resume_id="", job_title=jd.job_title)

        if self._use_llm:
            engine = TailoringEngine()
            engine_result = engine.tailor(
                content=ir.content,
                jd=jd,
                bank=bank,
                covered_keywords=match_result.matched_keywords,
                missing_keywords=match_result.missing_keywords,
                char_budgets=char_budgets,
                max_bullets_per_entry=max_bullets_per_entry,
            )

            if engine_result.llm_used:
                result.bullet_changes = engine_result.bullet_changes
                result.planning_used = True
                result.planning_duration_ms = engine_result.duration_ms

                # Skill changes from engine
                for cat, skills in engine_result.skill_reorders.items():
                    result.reordered_skills[cat] = skills
                for cat, skills in engine_result.skill_additions.items():
                    result.added_skills[cat] = skills
            else:
                result.planning_error = engine_result.llm_error
                logger.warning(
                    f"Tailoring engine failed: {engine_result.llm_error}"
                )

        # If LLM didn't produce results, fall back to deterministic keep-all
        if not result.bullet_changes:
            result.bullet_changes = self._deterministic_fallback(ir.content)

        # === STAGE 4: Safety net ===
        validator = ClaimValidator()
        self._run_safety_net(result, bank, ir, validator)

        # === STAGE 5: Layout fitting ===
        if one_line_bullets:
            try:
                measurer_obj = BulletMeasurer(ir.layout)
                self._enforce_line_fit(result, measurer_obj, bank)
            except Exception as e:
                logger.warning(f"Layout fitting skipped: {e}")

        # === STAGE 6: Keyword coverage report ===
        self._build_coverage_report(result, ir, jd, match_result)

        return result

    # ------------------------------------------------------------------
    # Character budgets
    # ------------------------------------------------------------------

    def _compute_char_budgets(
        self,
        content: ResumeContent,
        measurer,
    ) -> dict[str, tuple[int, int]]:
        """Compute per-bullet (floor, ceiling) character budgets.

        Floor = 80% of original length (don't lose content).
        Ceiling = max chars that fit on one rendered line.
        """
        # Find single-line capacity via binary search
        max_line_chars = self._find_line_capacity(measurer)

        budgets: dict[str, tuple[int, int]] = {}
        for section in content.sections:
            for entry in section.experience_entries:
                for b in entry.bullets:
                    floor = int(len(b.text) * 0.80)
                    ceiling = max_line_chars
                    budgets[b.id] = (floor, ceiling)
            for entry in section.project_entries:
                for b in entry.bullets:
                    floor = int(len(b.text) * 0.80)
                    ceiling = max_line_chars
                    budgets[b.id] = (floor, ceiling)
        return budgets

    def _fallback_char_budgets(
        self,
        content: ResumeContent,
        max_chars: int,
    ) -> dict[str, tuple[int, int]]:
        """Fallback budgets when measurer is unavailable."""
        budgets: dict[str, tuple[int, int]] = {}
        for section in content.sections:
            for entry in section.experience_entries:
                for b in entry.bullets:
                    floor = int(len(b.text) * 0.80)
                    budgets[b.id] = (floor, max_chars)
            for entry in section.project_entries:
                for b in entry.bullets:
                    floor = int(len(b.text) * 0.80)
                    budgets[b.id] = (floor, max_chars)
        return budgets

    def _find_line_capacity(self, measurer) -> int:
        """Binary search for how many chars fit on one rendered line."""
        sample = (
            "Developed and deployed a full-stack Python software application "
            "with RESTful API data pipeline processing and automated testing "
            "across multiple production environments for enterprise clients "
        )
        low, high = 80, 200
        while low < high:
            mid = (low + high + 1) // 2
            test_text = (sample * 3)[:mid]
            m = measurer.measure(test_text)
            if m.fits_one_line:
                low = mid
            else:
                high = mid - 1
        return low

    # ------------------------------------------------------------------
    # Safety net
    # ------------------------------------------------------------------

    def _run_safety_net(
        self,
        result: TailoringResult,
        bank: ResumeBank,
        ir: ResumeIR,
        validator: ClaimValidator,
    ):
        """Check for fabrication and metric loss. Revert bad rewrites."""
        for change in result.bullet_changes:
            if change.action != "rewrite":
                continue

            # Get source facts for this bullet
            facts = self._get_facts_for_bullet(
                change.bullet_id, bank, ir.content,
            )

            # Fabrication check
            validation = validator.validate(change, facts)
            if not validation.valid:
                change.tailored_text = change.original_text
                change.action = "keep"
                change.reason = (
                    f"Rewrite rejected: {'; '.join(validation.issues)}"
                )
                continue

            # Metric preservation warning
            if validation.metric_warnings:
                change.reason = (
                    (change.reason or "") +
                    f" | WARNING: {'; '.join(validation.metric_warnings)}"
                )

    # ------------------------------------------------------------------
    # Layout fitting
    # ------------------------------------------------------------------

    def _enforce_line_fit(
        self,
        result: TailoringResult,
        measurer,
        bank: ResumeBank,
    ):
        """Check each rewritten bullet fits on one line. Shorten if needed."""
        for change in result.bullet_changes:
            if change.action not in ("rewrite", "keep"):
                continue

            text = change.tailored_text
            measurement = measurer.measure(text)

            if measurement.fits_one_line:
                continue

            # Try shortening via LLM
            if change.action == "rewrite":
                try:
                    from backend.tailoring.rewriter import BulletRewriter
                    rewriter = BulletRewriter()
                    facts = [{"id": "original", "text": change.original_text}]
                    shortened = rewriter.shorten_bullet(
                        bullet_id=change.bullet_id,
                        bullet_text=text,
                        facts=facts,
                        target_lines=1,
                        chars_per_line=len(text) - 10,  # rough target
                    )
                    new_m = measurer.measure(shortened.tailored_text)
                    if new_m.fits_one_line:
                        change.tailored_text = shortened.tailored_text
                        change.reason = (
                            (change.reason or "") + " | Shortened to fit line"
                        )
                        continue
                except Exception:
                    pass

                # LLM shortening failed — revert to original
                change.tailored_text = change.original_text
                change.action = "keep"
                change.reason = (
                    f"Rewrite overflows line ({measurement.line_count} lines)"
                    " — reverted"
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
    # Keyword coverage report
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
                    # Add new skills
                    if cat.category in result.added_skills:
                        existing = {s.lower() for s in cat.skills}
                        for new_skill in result.added_skills[cat.category]:
                            key = f"{cat.category}:{new_skill}"
                            accepted = result.additions_accepted.get(key, True)
                            if accepted and new_skill.lower() not in existing:
                                cat.skills.append(new_skill)
                                existing.add(new_skill.lower())

                    # Reorder skills
                    if cat.category in result.reordered_skills:
                        accepted = result.reorder_accepted.get(
                            cat.category, True
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

        # Ensure skills rows fit on one line
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
        """Trim skills rows so each fits on a single rendered line."""
        from backend.tailoring.bullet_measurer import BulletMeasurer

        try:
            measurer = BulletMeasurer(ir.layout)
        except Exception:
            return

        for section in ir.content.sections:
            if section.type != SectionType.SKILLS:
                continue
            for cat in section.skill_categories:
                row_text = f"{cat.category}: {', '.join(cat.skills)}"
                m = measurer.measure_line(row_text)
                if m.fits_one_line:
                    continue

                while len(cat.skills) > 1:
                    removed = cat.skills.pop()
                    if result:
                        key = f"{cat.category}:{removed}"
                        if key not in result.additions_accepted:
                            result.additions_accepted[key] = False
                    row_text = f"{cat.category}: {', '.join(cat.skills)}"
                    m = measurer.measure_line(row_text)
                    if m.fits_one_line:
                        break

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
                skills_texts[cat.category] = (
                    f"{cat.category}: " + ", ".join(cat.skills)
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

    def _get_facts_for_bullet(
        self,
        bullet_id: str,
        bank: ResumeBank,
        content: ResumeContent,
    ) -> list[dict]:
        """Get source facts for a bullet from the bank."""
        # Find which entry this bullet belongs to
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
