"""Tailoring Service.

Orchestrates the full tailoring pipeline:
JD analysis → matching → selection → rewriting → validation

Keeps LLM calls modular and validates every rewritten bullet.
"""

from __future__ import annotations

import copy
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
from backend.tailoring.selector import Selector


class TailoringService:
    """Orchestrate resume tailoring."""

    def __init__(self, use_llm: bool = True):
        self._use_llm = use_llm
        self._rewriter = None  # lazy init

    def _get_rewriter(self):
        if self._rewriter is None:
            from backend.tailoring.rewriter import BulletRewriter
            self._rewriter = BulletRewriter()
        return self._rewriter

    def analyze_jd(self, jd_text: str) -> JobAnalysis:
        """Step 1: Analyze a job description."""
        analyzer = JobAnalyzer()
        return analyzer.analyze(jd_text)

    def match(
        self,
        ir: ResumeIR,
        jd: JobAnalysis,
        bank: Optional[ResumeBank] = None,
    ) -> dict:
        """Step 2: Match resume against JD."""
        if bank is None:
            bank = generate_bank_from_ir(ir)

        matcher = Matcher(jd, ir.content, bank)
        match_result = matcher.match()

        return {
            "match_result": match_result,
            "bank": bank,
        }

    def tailor(
        self,
        ir: ResumeIR,
        jd: JobAnalysis,
        bank: Optional[ResumeBank] = None,
        max_bullets_per_entry: int = 4,
    ) -> TailoringResult:
        """Run the full tailoring pipeline."""
        if bank is None:
            bank = generate_bank_from_ir(ir)

        # Step 1: Match
        matcher = Matcher(jd, ir.content, bank)
        match_result = matcher.match()

        # Step 2: Select
        selector = Selector(jd, ir.content, match_result, bank, max_bullets_per_entry)
        selection = selector.select()

        # Step 3: Rewrite (with LLM if available)
        result = TailoringResult(
            resume_id="",
            job_title=jd.job_title,
        )

        validator = ClaimValidator()

        for sel in selection.bullet_selections:
            if sel.action == "keep":
                result.bullet_changes.append(BulletChange(
                    bullet_id=sel.bullet_id,
                    original_text=sel.text,
                    tailored_text=sel.text,
                    action="keep",
                    reason="High relevance to JD",
                ))
                continue

            if sel.action == "remove":
                result.bullet_changes.append(BulletChange(
                    bullet_id=sel.bullet_id,
                    original_text=sel.text,
                    tailored_text=sel.text,
                    action="remove",
                    reason="Low relevance, exceeds bullet limit",
                ))
                continue

            if sel.action == "rewrite" and self._use_llm:
                # Get facts for this bullet
                facts = self._get_facts_for_bullet(sel.bullet_id, sel.entry_id, bank)

                try:
                    rewriter = self._get_rewriter()
                    change = rewriter.rewrite_bullet(
                        bullet_id=sel.bullet_id,
                        bullet_text=sel.text,
                        facts=facts,
                        jd=jd,
                        target_keywords=sel.target_keywords[:10],
                    )

                    # Validate
                    validation = validator.validate(change, facts)
                    if not validation.valid:
                        # Reject: keep original
                        change.tailored_text = change.original_text
                        change.action = "keep"
                        change.reason = f"Rewrite rejected: {'; '.join(validation.issues)}"

                    result.bullet_changes.append(change)
                except Exception as e:
                    # LLM failed — keep original
                    result.bullet_changes.append(BulletChange(
                        bullet_id=sel.bullet_id,
                        original_text=sel.text,
                        tailored_text=sel.text,
                        action="keep",
                        reason=f"Rewrite failed: {str(e)[:100]}",
                    ))
            else:
                # No LLM — keep with note
                result.bullet_changes.append(BulletChange(
                    bullet_id=sel.bullet_id,
                    original_text=sel.text,
                    tailored_text=sel.text,
                    action="keep",
                    reason="Rewrite suggested but LLM not available",
                    target_keywords=sel.target_keywords[:5],
                ))

        # Step 4: Keyword coverage report
        all_jd_keywords = jd.all_keywords()
        resume_text = " ".join(
            c.tailored_text.lower() for c in result.bullet_changes
            if c.action != "remove"
        )
        # Add skills
        for section in ir.content.sections:
            for cat in section.skill_categories:
                resume_text += " " + " ".join(s.lower() for s in cat.skills)

        for kw in all_jd_keywords[:30]:
            skill_info = next(
                (s for s in jd.all_skills_flat() if s.name.lower() == kw.lower()),
                None,
            )
            importance = skill_info.importance if skill_info else 0.3

            if kw.lower() in resume_text:
                # Check if it was in original or added
                orig_text = " ".join(c.original_text.lower() for c in result.bullet_changes)
                for section in ir.content.sections:
                    for cat in section.skill_categories:
                        orig_text += " " + " ".join(s.lower() for s in cat.skills)

                if kw.lower() in orig_text:
                    status = "matched"
                    source = "present in resume"
                else:
                    status = "added"
                    source = "added via rewrite"
            else:
                status = "missing"
                source = "not in resume bank"

            result.keyword_coverage.append(KeywordCoverage(
                keyword=kw,
                importance=importance,
                status=status,
                source=source,
            ))

        # Coverage metrics
        result.required_skill_coverage = match_result.required_coverage
        result.technical_keyword_coverage = match_result.technical_coverage
        result.responsibility_coverage = match_result.responsibility_coverage

        # Skills reordering
        for reorder in selection.skill_reorders:
            result.reordered_skills[reorder.category] = reorder.suggested_order

        return result

    def apply_tailoring(self, ir: ResumeIR, result: TailoringResult) -> ResumeIR:
        """Apply a tailoring result to produce a new ResumeIR."""
        new_ir = copy.deepcopy(ir)

        # Build bullet change lookup
        changes = {c.bullet_id: c for c in result.bullet_changes if c.accepted}

        for section in new_ir.content.sections:
            if section.type in (SectionType.EXPERIENCE, SectionType.VOLUNTEER):
                for entry in section.experience_entries:
                    new_bullets = []
                    for bullet in entry.bullets:
                        change = changes.get(bullet.id)
                        if change is None or change.action == "keep":
                            new_bullets.append(bullet)
                        elif change.action == "rewrite":
                            bullet.original_text = bullet.text
                            bullet.text = change.tailored_text
                            bullet.source_fact_ids = change.source_fact_ids
                            bullet.target_keywords = change.target_keywords
                            new_bullets.append(bullet)
                        elif change.action == "remove":
                            continue  # skip
                    entry.bullets = new_bullets

            elif section.type == SectionType.PROJECTS:
                for entry in section.project_entries:
                    new_bullets = []
                    for bullet in entry.bullets:
                        change = changes.get(bullet.id)
                        if change is None or change.action == "keep":
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
                    if cat.category in result.reordered_skills:
                        cat.skills = result.reordered_skills[cat.category]

        return new_ir

    def _get_facts_for_bullet(
        self, bullet_id: str, entry_id: str, bank: ResumeBank
    ) -> list[dict]:
        """Get source facts for a bullet from the bank."""
        # Check experiences
        exp = bank.find_experience(entry_id)
        if exp:
            # Find the approved bullet
            for ab in exp.approved_bullets:
                if ab.id == bullet_id:
                    # Return the facts referenced by this bullet
                    return [
                        {"id": f.id, "text": f.text}
                        for f in exp.facts
                        if f.id in ab.source_fact_ids
                    ] or [{"id": f.id, "text": f.text} for f in exp.facts]
            # Fallback: return all facts for the experience
            return [{"id": f.id, "text": f.text} for f in exp.facts]

        # Check projects
        proj = bank.find_project(entry_id)
        if proj:
            for ab in proj.approved_bullets:
                if ab.id == bullet_id:
                    return [
                        {"id": f.id, "text": f.text}
                        for f in proj.facts
                        if f.id in ab.source_fact_ids
                    ] or [{"id": f.id, "text": f.text} for f in proj.facts]
            return [{"id": f.id, "text": f.text} for f in proj.facts]

        return []
