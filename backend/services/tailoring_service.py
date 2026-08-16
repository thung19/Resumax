"""Tailoring Service.

Two-stage LLM pipeline:
1. Planning pass: Full resume + JD → per-bullet decisions (keep/rewrite/remove)
2. Rewrite pass: Focused per-bullet calls with planning context

Deterministic matcher/selector run first as safety baseline.
LLM scores are merged with deterministic scores, never replace them.
Full fallback to deterministic if LLM unavailable.
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
from backend.tailoring.planner import BulletPlan, PlanResult, ResumePlanner
from backend.tailoring.selector import Selector

logger = logging.getLogger(__name__)


class TailoringService:
    """Orchestrate resume tailoring with two-stage LLM pipeline."""

    def __init__(self, use_llm: bool = True):
        self._use_llm = use_llm
        self._rewriter = None
        self._plan_result: Optional[PlanResult] = None

    def _get_rewriter(self):
        if self._rewriter is None:
            from backend.tailoring.rewriter import BulletRewriter
            self._rewriter = BulletRewriter()
        return self._rewriter

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
        """Run the full two-stage tailoring pipeline."""
        self._one_line_bullets = one_line_bullets
        self._enforce_single_line = enforce_single_line
        self._max_bullet_chars = max_bullet_chars
        self._max_bullets = max_bullets_per_entry

        if bank is None:
            bank = generate_bank_from_ir(ir)

        # === STAGE 0: Deterministic matching (always runs) ===
        matcher = Matcher(jd, ir.content, bank)
        match_result = matcher.match()

        # === STAGE 1: LLM planning pass (full resume evaluation) ===
        plan = PlanResult()
        if self._use_llm:
            try:
                planner = ResumePlanner()
                plan = planner.plan(ir.content, jd, bank)
                self._plan_result = plan
                if plan.llm_error:
                    logger.warning(f"Planning pass error: {plan.llm_error}")
            except Exception as e:
                plan.llm_error = str(e)[:200]
                logger.warning(f"Planning pass failed: {e}")

        # === STAGE 2: Merge deterministic + LLM scores, select bullets ===
        selector = Selector(jd, ir.content, match_result, bank, max_bullets_per_entry)
        selection = selector.select()

        # Merge planning decisions with deterministic selections
        merged_selections = self._merge_plan_with_selection(selection, plan, match_result)

        # Store plan for rewriter access
        self._plan = plan

        # === STAGE 3: Rewrite selected bullets ===
        result = TailoringResult(resume_id="", job_title=jd.job_title)
        validator = ClaimValidator()

        # Create measurer once — all rewrites use it for layout-accurate limits
        from backend.tailoring.bullet_measurer import BulletMeasurer
        self._measurer = BulletMeasurer(ir.layout)

        # Collect nearby bullets per entry for context
        entry_bullets = self._collect_entry_bullets(ir.content)

        for sel in merged_selections:
            if sel["action"] == "keep":
                result.bullet_changes.append(BulletChange(
                    bullet_id=sel["bullet_id"],
                    original_text=sel["text"],
                    tailored_text=sel["text"],
                    action="keep",
                    reason=sel.get("reason", "High relevance"),
                ))
                continue

            if sel["action"] == "remove":
                result.bullet_changes.append(BulletChange(
                    bullet_id=sel["bullet_id"],
                    original_text=sel["text"],
                    tailored_text=sel["text"],
                    action="remove",
                    reason=sel.get("reason", "Low relevance, exceeds bullet limit"),
                ))
                continue

            # Rewrite
            if self._use_llm:
                change = self._rewrite_with_planning(
                    sel, bank, jd, validator, entry_bullets
                )
                result.bullet_changes.append(change)
            else:
                result.bullet_changes.append(BulletChange(
                    bullet_id=sel["bullet_id"],
                    original_text=sel["text"],
                    tailored_text=sel["text"],
                    action="keep",
                    reason="LLM not available",
                    target_keywords=sel.get("target_keywords", [])[:5],
                ))

        # === STAGE 4: Enforce bullet line constraints ===
        if self._one_line_bullets:
            from backend.tailoring.bullet_fitter import BulletFitter
            fitter = BulletFitter(
                layout=ir.layout,
                bank=bank,
                use_llm=self._use_llm,
            )
            fit_report = fitter.fit_bullets(result)
            result.fitting_report = {
                "total": fit_report.total_bullets,
                "overflows": fit_report.overflows_detected,
                "fitted": fit_report.successfully_fitted,
                "failed": fit_report.failed_to_fit,
                "unchanged": fit_report.unchanged,
                "all_fit": fit_report.all_fit,
            }

            # Final hard constraint validation
            violations = fitter.validate_all_fit(result)
            if violations:
                result.fitting_violations = violations
                logger.warning(f"Bullet fitting violations: {violations}")
        elif self._enforce_single_line:
            # Legacy character-based enforcement (fallback)
            self._enforce_bullet_length(result, bank, jd)

        # === STAGE 5: Keyword coverage report ===
        self._build_coverage_report(result, ir, jd, match_result)

        # === STAGE 6: Skills reordering + additions ===
        for reorder in selection.skill_reorders:
            result.reordered_skills[reorder.category] = reorder.suggested_order
        for addition in selection.skill_additions:
            cat = addition.category
            if cat not in result.added_skills:
                result.added_skills[cat] = []
            result.added_skills[cat].append(addition.skill)

        # Add planning metadata
        result.planning_used = plan.llm_used
        result.planning_error = plan.llm_error
        result.planning_duration_ms = plan.duration_ms

        return result

    # ------------------------------------------------------------------
    # Score merging
    # ------------------------------------------------------------------

    def _merge_plan_with_selection(
        self,
        selection,
        plan: PlanResult,
        match_result,
    ) -> list[dict]:
        """Merge deterministic selections with LLM planning decisions.

        Combined score: deterministic * 0.6 + llm_job_relevance * 0.4
        High strategic_value bullets are protected from removal.
        Removals only happen when an entry exceeds max_bullets_per_entry.
        """
        det_scores: dict[str, dict] = {}
        for sel in selection.bullet_selections:
            det_scores[sel.bullet_id] = {
                "bullet_id": sel.bullet_id,
                "entry_id": sel.entry_id,
                "text": sel.text,
                "action": sel.action,
                "det_score": sel.relevance_score,
                "target_keywords": sel.target_keywords,
                "reason": "",
            }

        # First pass: determine ideal action for each bullet
        merged: list[dict] = []

        for bid, det in det_scores.items():
            llm_plan = plan.bullet_plans.get(bid)

            if llm_plan:
                det_score = det["det_score"]
                combined = det_score * 0.6 + llm_plan.job_relevance * 0.4

                if llm_plan.decision == "keep" and det["action"] == "keep":
                    action = "keep"
                    reason = llm_plan.reason or "High relevance"
                elif llm_plan.decision == "rewrite":
                    action = "rewrite"
                    reason = llm_plan.reason
                elif llm_plan.decision == "keep" and det["action"] == "rewrite":
                    action = "keep"
                    reason = llm_plan.reason or "LLM: no improvement needed"
                elif llm_plan.decision == "remove":
                    # Mark as removal CANDIDATE — actual removal enforced below
                    if llm_plan.strategic_value >= 0.6:
                        action = "keep"
                        reason = f"Protected: strategic_value={llm_plan.strategic_value:.1f}"
                    else:
                        action = "remove_candidate"
                        reason = llm_plan.reason
                else:
                    action = det["action"]
                    reason = llm_plan.reason or ""

                merged.append({
                    "bullet_id": bid,
                    "entry_id": det["entry_id"],
                    "text": det["text"],
                    "action": action,
                    "combined_score": combined,
                    "strategic_value": llm_plan.strategic_value,
                    "reason": reason,
                    "target_keywords": llm_plan.supported_target_keywords or det["target_keywords"],
                    "rewrite_guidance": llm_plan.rewrite_guidance,
                    "planning_reason": llm_plan.reason,
                })
            else:
                merged.append({
                    "bullet_id": bid,
                    "entry_id": det["entry_id"],
                    "text": det["text"],
                    "action": det["action"],
                    "combined_score": det["det_score"],
                    "strategic_value": 0.5,
                    "reason": "Deterministic only",
                    "target_keywords": det["target_keywords"],
                    "rewrite_guidance": "",
                    "planning_reason": "",
                })

        # Second pass: enforce per-entry removal limits
        # Only actually remove if the entry has MORE bullets than max_bullets_per_entry
        from collections import Counter
        entry_counts: Counter = Counter()
        for m in merged:
            if m["action"] != "remove_candidate":
                entry_counts[m["entry_id"]] += 1

        entry_totals: Counter = Counter(m["entry_id"] for m in merged)

        for m in merged:
            if m["action"] == "remove_candidate":
                entry_id = m["entry_id"]
                non_removed = entry_counts[entry_id]
                total = entry_totals[entry_id]
                # Only remove if entry still has more bullets than needed
                if total > self._max_bullets and non_removed > self._max_bullets:
                    m["action"] = "remove"
                else:
                    # Downgrade to keep — entry doesn't have excess bullets
                    m["action"] = "keep"
                    m["reason"] = f"Kept (entry has {total} bullets, limit is {self._max_bullets})"

        return merged

    # ------------------------------------------------------------------
    # Rewriting with planning context
    # ------------------------------------------------------------------

    def _rewrite_with_planning(
        self,
        sel: dict,
        bank: ResumeBank,
        jd: JobAnalysis,
        validator: ClaimValidator,
        entry_bullets: dict[str, list[str]],
    ) -> BulletChange:
        """Rewrite a single bullet using full strategic context."""
        bullet_id = sel["bullet_id"]
        entry_id = sel.get("entry_id", "")
        text = sel["text"]
        target_kws = sel.get("target_keywords", [])
        planning_reason = sel.get("planning_reason", "")
        rewrite_guidance = sel.get("rewrite_guidance", "")

        # Get source facts
        facts = self._get_facts_for_bullet(bullet_id, entry_id, bank)

        # Get nearby bullets for context
        nearby = [b for b in entry_bullets.get(entry_id, []) if b != text][:3]

        # Build strategic context from planner
        strategy_context = ""
        entry_context = ""
        covered_keywords: list[str] = []

        plan = getattr(self, "_plan", None)
        if plan and plan.llm_used:
            s = plan.strategy
            strategy_parts = []
            if s.jd_narrative:
                strategy_parts.append(f"What they want: {s.jd_narrative}")
            if s.candidate_strengths:
                strategy_parts.append(f"Your strengths: {s.candidate_strengths}")
            if s.gaps_to_address:
                strategy_parts.append(f"Gaps to fill: {s.gaps_to_address}")
            if s.tone_guidance:
                strategy_parts.append(f"Tone: {s.tone_guidance}")
            strategy_context = "\n".join(strategy_parts)

            covered_keywords = s.keywords_already_covered or []

            # Entry-specific context
            ec = plan.entry_contexts.get(entry_id)
            if ec:
                entry_context = f"{ec.company} — {ec.role}\n{ec.entry_purpose}"

        try:
            rewriter = self._get_rewriter()

            # Measure original bullet with layout engine to get real capacity
            measurement = self._measurer.measure(text)
            # The max chars = what fits on one line at this font/width
            # Use the original length as floor (don't go shorter)
            # and measure-derived capacity as ceiling (don't wrap)
            max_single_line_chars = self._estimate_max_chars_for_one_line()
            length_budget = min(max_single_line_chars, max(len(text), max_single_line_chars))

            change = rewriter.rewrite_bullet(
                bullet_id=bullet_id,
                bullet_text=text,
                facts=facts,
                jd=jd,
                target_keywords=target_kws[:10],
                max_chars=length_budget,
                planning_reason=planning_reason,
                rewrite_guidance=rewrite_guidance,
                nearby_bullets=nearby,
                entry_context=entry_context,
                strategy_context=strategy_context,
                covered_keywords=covered_keywords,
            )

            # Validate
            validation = validator.validate(change, facts)
            if not validation.valid:
                change.tailored_text = change.original_text
                change.action = "keep"
                change.reason = f"Rewrite rejected: {'; '.join(validation.issues)}"
            elif change.tailored_text.strip() == change.original_text.strip():
                change.action = "keep"
                change.reason = "No improvement found"
            elif len(change.tailored_text) > len(change.original_text) * 1.10:
                # Rewrite is >10% longer — padding
                change.tailored_text = change.original_text
                change.action = "keep"
                change.reason = f"Rewrite too long ({len(change.tailored_text)} vs {len(change.original_text)} chars) — reverted"
            elif len(change.tailored_text) < len(change.original_text) * 0.80:
                # Rewrite is >20% shorter — destructive, lost content
                change.tailored_text = change.original_text
                change.action = "keep"
                change.reason = f"Rewrite too short ({len(change.tailored_text)} vs {len(change.original_text)} chars) — lost content, reverted"
            else:
                # Check if the rewrite actually added any new words
                orig_words = set(change.original_text.lower().split())
                new_words = set(change.tailored_text.lower().split())
                added_words = new_words - orig_words
                removed_words = orig_words - new_words

                if len(removed_words) > len(added_words) + 3:
                    # Removed significantly more words than added — net loss
                    change.tailored_text = change.original_text
                    change.action = "keep"
                    change.reason = f"Rewrite removed {len(removed_words)} words but only added {len(added_words)} — net loss, reverted"

                # Check if the rewrite actually adds ATS value
                # Compare JD keywords in original vs rewrite
                if change.action != "keep":
                    jd_kws_lower = {kw.lower() for kw in target_kws}
                    orig_lower = change.original_text.lower()
                    new_lower = change.tailored_text.lower()

                    orig_jd_hits = {kw for kw in jd_kws_lower if kw in orig_lower}
                    new_jd_hits = {kw for kw in jd_kws_lower if kw in new_lower}
                    new_kw_additions = new_jd_hits - orig_jd_hits

                    if len(new_kw_additions) == 0:
                        # Rewrite doesn't add any JD keywords — pointless change
                        change.tailored_text = change.original_text
                        change.action = "keep"
                        change.reason = "Rewrite adds no new JD keywords — original is better"

                # Validate source_fact_ids actually exist
                if change.action != "keep":
                    fact_ids = {f["id"] for f in facts}
                    valid_ids = [fid for fid in change.source_fact_ids if fid in fact_ids]
                    change.source_fact_ids = valid_ids

                    # Validate target keywords are in the text
                    text_lower = change.tailored_text.lower()
                    present_kws = [kw for kw in change.target_keywords if kw.lower() in text_lower]
                    change.target_keywords = present_kws

                    # Final layout check: does the rewrite fit on one line?
                    if self._one_line_bullets:
                        post_measurement = self._measurer.measure(change.tailored_text)
                        if not post_measurement.fits_one_line:
                            change.tailored_text = change.original_text
                            change.action = "keep"
                            change.reason = f"Rewrite wraps to {post_measurement.line_count} lines — reverted to original"

            return change

        except Exception as e:
            logger.warning(f"Rewrite failed for {bullet_id}: {e}")
            return BulletChange(
                bullet_id=bullet_id,
                original_text=text,
                tailored_text=text,
                action="keep",
                reason=f"Rewrite failed: {str(e)[:100]}",
            )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _estimate_max_chars_for_one_line(self) -> int:
        """Use the measurer to find how many characters fit on one line.

        Uses a representative English text sample (proportional font widths
        vary by letter — 'i' is narrower than 'M') and binary searches
        until the measurer reports wrapping.
        Cached after first call.
        """
        if hasattr(self, "_max_chars_cache"):
            return self._max_chars_cache

        measurer = self._measurer

        # Representative resume text with typical letter distribution
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

        self._max_chars_cache = low
        return low

    def _collect_entry_bullets(self, content: ResumeContent) -> dict[str, list[str]]:
        """Build a map of entry_id -> [bullet texts] for nearby-bullet context."""
        result: dict[str, list[str]] = {}
        for section in content.sections:
            for entry in section.experience_entries:
                result[entry.id] = [b.text for b in entry.bullets]
            for entry in section.project_entries:
                result[entry.id] = [b.text for b in entry.bullets]
        return result

    def _build_coverage_report(
        self, result: TailoringResult, ir: ResumeIR,
        jd: JobAnalysis, match_result,
    ):
        """Build keyword coverage report."""
        all_jd_keywords = jd.all_keywords()
        resume_text = " ".join(
            c.tailored_text.lower() for c in result.bullet_changes
            if c.action != "remove"
        )
        for section in ir.content.sections:
            for cat in section.skill_categories:
                resume_text += " " + " ".join(s.lower() for s in cat.skills)

        for kw in all_jd_keywords[:30]:
            skill_info = next(
                (s for s in jd.all_skills_flat() if s.name.lower() == kw.lower()), None
            )
            importance = skill_info.importance if skill_info else 0.3

            if kw.lower() in resume_text:
                orig_text = " ".join(c.original_text.lower() for c in result.bullet_changes)
                for section in ir.content.sections:
                    for cat in section.skill_categories:
                        orig_text += " " + " ".join(s.lower() for s in cat.skills)

                status = "matched" if kw.lower() in orig_text else "added"
                source = "present in resume" if status == "matched" else "added via rewrite"
            else:
                status = "missing"
                source = "not in resume bank"

            result.keyword_coverage.append(KeywordCoverage(
                keyword=kw, importance=importance, status=status, source=source,
            ))

        result.required_skill_coverage = match_result.required_coverage
        result.technical_keyword_coverage = match_result.technical_coverage
        result.responsibility_coverage = match_result.responsibility_coverage

    def _enforce_bullet_length(
        self, result: TailoringResult, bank: ResumeBank, jd: JobAnalysis,
    ):
        """Shorten bullets significantly over max_bullet_chars."""
        max_chars = self._max_bullet_chars
        grace = 8

        for change in result.bullet_changes:
            if change.action == "remove":
                continue
            text = change.tailored_text
            overage = len(text) - max_chars
            if overage <= grace:
                continue

            if self._use_llm:
                try:
                    rewriter = self._get_rewriter()
                    facts = self._get_facts_for_bullet(
                        change.bullet_id, "", bank,
                    )
                    if not facts:
                        facts = [{"id": "orig", "text": text}]
                    shortened = rewriter.shorten_bullet(
                        bullet_id=change.bullet_id,
                        bullet_text=text,
                        facts=facts,
                        target_lines=1,
                        chars_per_line=max_chars,
                    )
                    new_text = shortened.tailored_text
                    if len(new_text) <= max_chars and len(new_text) >= max_chars * 0.7:
                        change.tailored_text = new_text
                        change.reason = (change.reason or "") + f" | Shortened to {len(new_text)} chars"
                        if change.action == "keep":
                            change.action = "rewrite"
                        continue
                except Exception:
                    pass

            # Deterministic fallback only if significantly over
            if overage > 20:
                truncated = text[:max_chars]
                last_space = truncated.rfind(" ")
                if last_space > max_chars * 0.7:
                    truncated = truncated[:last_space].rstrip(",;: ")
                    change.tailored_text = truncated
                    change.reason = (change.reason or "") + f" | Trimmed to {len(truncated)} chars"
                    if change.action == "keep":
                        change.action = "rewrite"

    def apply_tailoring(self, ir: ResumeIR, result: TailoringResult, fit_skills: bool = True) -> ResumeIR:
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
                    # Add new skills (only if accepted or not yet resolved)
                    if cat.category in result.added_skills:
                        existing = {s.lower() for s in cat.skills}
                        for new_skill in result.added_skills[cat.category]:
                            key = f"{cat.category}:{new_skill}"
                            accepted = result.additions_accepted.get(key, True)
                            if accepted and new_skill.lower() not in existing:
                                cat.skills.append(new_skill)
                                existing.add(new_skill.lower())

                    # Reorder skills (only if accepted or not yet resolved)
                    if cat.category in result.reordered_skills:
                        accepted = result.reorder_accepted.get(cat.category, True)
                        if accepted:
                            reordered = list(result.reordered_skills[cat.category])
                            existing_reordered = {s.lower() for s in reordered}
                            for s in cat.skills:
                                if s.lower() not in existing_reordered:
                                    reordered.append(s)
                            cat.skills = reordered

        # Ensure skills rows fit on one line — also prune the result
        # so the UI doesn't show suggestions that got trimmed.
        if fit_skills:
            self._fit_skills_to_line(new_ir, result)

        self._sync_elements_with_content(new_ir, accepted_changes, result.reordered_skills)
        return new_ir

    def _fit_skills_to_line(
        self, ir: ResumeIR, result: Optional[TailoringResult] = None,
    ):
        """Trim skills rows so each fits on a single rendered line.

        Skills are already ordered by relevance (most relevant first
        after the reorder step), so we drop from the end.  Also marks
        trimmed additions as rejected in the TailoringResult so the
        UI stays in sync.
        """
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

                # Remove skills from the end until it fits
                while len(cat.skills) > 1:
                    removed = cat.skills.pop()
                    # Mark this addition as rejected so UI doesn't show it
                    if result:
                        key = f"{cat.category}:{removed}"
                        if key not in result.additions_accepted:
                            result.additions_accepted[key] = False
                    row_text = f"{cat.category}: {', '.join(cat.skills)}"
                    m = measurer.measure_line(row_text)
                    if m.fits_one_line:
                        break

    def _sync_elements_with_content(
        self, ir: ResumeIR, changes: dict[str, BulletChange],
        reordered_skills: dict[str, list[str]],
    ):
        """Update layout element run text to match content changes."""
        from backend.models.resume_layout import ElementType, RunFormat

        skills_texts: dict[str, str] = {}
        for section in ir.content.sections:
            for cat in section.skill_categories:
                skills_texts[cat.category] = f"{cat.category}: " + ", ".join(cat.skills)

        new_elements = []
        for el in ir.layout.elements:
            if el.element_type == ElementType.BULLET:
                orig_text = "".join(
                    r.text for r in el.paragraph_format.runs if not r.is_tab
                ).strip()

                matched_change = None
                for change in changes.values():
                    clean_orig = orig_text.lstrip("• ").strip()
                    if clean_orig and change.original_text and clean_orig[:30] == change.original_text[:30]:
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
                for cat_name in list(reordered_skills.keys()) + list(skills_texts.keys()):
                    if orig_text.startswith(cat_name + ":"):
                        matched_cat = cat_name
                        break

                if matched_cat and matched_cat in skills_texts:
                    new_text = skills_texts[matched_cat]
                    if ":" in new_text:
                        label, value = new_text.split(":", 1)
                        first_run = el.paragraph_format.runs[0] if el.paragraph_format.runs else RunFormat()
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

    def _get_facts_for_bullet(
        self, bullet_id: str, entry_id: str, bank: ResumeBank,
    ) -> list[dict]:
        """Get source facts for a bullet from the bank."""
        for source in [bank.find_experience(entry_id), bank.find_project(entry_id)]:
            if source:
                for ab in source.approved_bullets:
                    if ab.id == bullet_id:
                        matched = [
                            {"id": f.id, "text": f.text}
                            for f in source.facts
                            if f.id in ab.source_fact_ids
                        ]
                        return matched or [{"id": f.id, "text": f.text} for f in source.facts]
                return [{"id": f.id, "text": f.text} for f in source.facts]
        return []
