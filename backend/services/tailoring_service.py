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

        # === STAGE 1: Compute target char count per bullet ===
        from backend.tailoring.bullet_measurer import BulletMeasurer
        measurer = None
        try:
            measurer = BulletMeasurer(ir.layout)
            line_capacity = self._find_line_capacity(measurer)
        except Exception:
            line_capacity = max_bullet_chars

        # Target = line capacity for all bullets (fill the line)
        target_chars: dict[str, int] = {}
        for section in ir.content.sections:
            for entry in section.experience_entries:
                for b in entry.bullets:
                    target_chars[b.id] = line_capacity
            for entry in section.project_entries:
                for b in entry.bullets:
                    target_chars[b.id] = line_capacity

        # === STAGE 2: Unified LLM tailoring pass ===
        result = TailoringResult(resume_id="", job_title=jd.job_title)

        if self._use_llm:
            engine = TailoringEngine()
            engine_result = engine.tailor(
                content=ir.content,
                jd=jd,
                bank=bank,
                target_chars=target_chars,
                max_bullets_per_entry=max_bullets_per_entry,
            )

            if engine_result.llm_used:
                result.bullet_changes = engine_result.bullet_changes
                result.planning_used = True
                result.planning_duration_ms = engine_result.duration_ms

                for cat, skills in engine_result.skill_reorders.items():
                    result.reordered_skills[cat] = skills
                for cat, skills in engine_result.skill_additions.items():
                    result.added_skills[cat] = skills
            else:
                result.planning_error = engine_result.llm_error
                logger.warning(
                    f"Tailoring engine failed: {engine_result.llm_error}"
                )

        # If LLM didn't produce results, fall back to keep-all
        if not result.bullet_changes:
            result.bullet_changes = self._deterministic_fallback(ir.content)

        # === STAGE 3: Safety net (fabrication + overflow revert) ===
        validator = ClaimValidator()
        self._run_safety_net(result, bank, ir, validator, measurer)

        # === STAGE 4: Build coverage report ===
        from backend.tailoring.matcher import Matcher
        matcher = Matcher(jd, ir.content, bank)
        match_result = matcher.match()
        self._build_coverage_report(result, ir, jd, match_result)

        return result

    # ------------------------------------------------------------------
    # Line capacity
    # ------------------------------------------------------------------

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
        measurer,
    ):
        """Fabrication check + overflow revert. No shortening pass."""
        for change in result.bullet_changes:
            if change.action != "rewrite":
                continue

            # Identical to original
            if change.tailored_text.strip() == change.original_text.strip():
                change.tailored_text = change.original_text
                change.action = "keep"
                change.reason = "No meaningful change"
                continue

            # Rewrite only removed words without adding anything
            orig_words = set(change.original_text.lower().split())
            new_words = set(change.tailored_text.lower().split())
            if not (new_words - orig_words):
                change.tailored_text = change.original_text
                change.action = "keep"
                change.reason = "Rewrite only removed words"
                continue

            # Validate claimed keywords are actually present
            if change.target_keywords:
                text_lower = change.tailored_text.lower()
                change.target_keywords = [
                    kw for kw in change.target_keywords
                    if kw.lower() in text_lower
                ]

            # Fabrication check
            facts = self._get_facts_for_bullet(
                change.bullet_id, bank, ir.content,
            )
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
                    (change.reason or "")
                    + f" | WARNING: {'; '.join(validation.metric_warnings)}"
                )

            # Overflow check — revert to original, don't shorten
            if measurer:
                measurement = measurer.measure(change.tailored_text)
                if not measurement.fits_one_line:
                    change.tailored_text = change.original_text
                    change.action = "keep"
                    change.reason = (
                        f"Rewrite overflows line "
                        f"({measurement.line_count} lines) — reverted"
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
                                cat.skills.append(new_skill)
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
