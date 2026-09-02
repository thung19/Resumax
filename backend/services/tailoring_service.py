"""Tailoring Service.

Pipeline:
1. JD Analysis (hybrid LLM + deterministic)
2. Compute target character count per bullet (from layout measurer)
3. Tailoring Engine (single LLM pass)
   - LLM gets coverage feedback WITH IMPLIES (semantic understanding)
4. Safety Net (fabrication check only — revert overflows to original)
5. Build snapshots + calculate coverage from snapshots (DIRECT matches only)
6. Skills fitting + page fitting

MATCHING STRATEGY:
- LLM FEEDBACK (Stage 3): Uses IMPLIES logic (broader context for LLM)
- FINAL METRICS (Stage 5): Uses DIRECT matches only (honest assessment)

This gives the LLM semantic understanding while users see conservative coverage.
"""

from __future__ import annotations

import copy
import logging
import re
from typing import Optional

from backend.analysis.job_analyzer import JobAnalyzer
from backend.analysis.skill_dedup import dedupe_skill_names, is_duplicate_skill
from backend.config import get_config
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
from backend.tailoring.claim_validator import TECH_TERMS, ClaimValidator
from backend.tailoring.tailoring_engine import TailoringEngine

logger = logging.getLogger(__name__)


def _dedupe_marked_skills(skills: list[str]) -> list[str]:
    """Like `dedupe_skill_names`, but aware of the "[LLM]" marker prefix
    used to track newly-added skills.

    Without this, "[LLM]GraphQL" (added via `skill_additions`) and plain
    "GraphQL" (present via a separate `skill_reorders` list) don't compare
    equal to any of the exact/normalized/variant checks — the marker
    survives until `_fit_skills_to_line` strips it later, at which point
    both entries become identical text and the duplicate becomes visible.
    This dedupes by the marker-stripped form while preserving whichever
    original (marked or not) string appeared first.
    """
    plain_to_original: dict[str, str] = {}
    ordered_plain: list[str] = []
    for s in skills:
        plain = s.replace("[LLM]", "").strip()
        if not plain or plain in plain_to_original:
            continue
        plain_to_original[plain] = s
        ordered_plain.append(plain)

    return [plain_to_original[p] for p in dedupe_skill_names(ordered_plain)]


def _dedupe_additions_across_categories(
    added_skills: dict[str, list[str]],
) -> dict[str, list[str]]:
    """Keep each proposed new skill under only the first category that
    claims it.

    Two independent LLM calls (`TailoringEngine.tailor()` and
    `.optimize_skills()`) each propose skill additions, and neither sees
    the other's output — they can independently propose adding the *same*
    skill under two *different* category names (e.g. "Jenkins" under both
    "Databases & Tools" and "Technologies"). The per-category "cat not in
    added_skills" guard at the call site only stops a whole category's
    list from being overwritten; it never catches this cross-category
    collision. Left unmerged, the skill shows up as two separate "Skills
    Added" review cards — reject one and the other silently survives,
    since accept/reject is tracked per `"category:skill"` key.
    """
    deduped: dict[str, list[str]] = {}
    claimed: list[str] = []
    for cat, skills in added_skills.items():
        kept = [s for s in skills if not is_duplicate_skill(s, claimed)]
        claimed.extend(kept)
        if kept:
            deduped[cat] = kept
    return deduped


def _tech_terms_present(text: str) -> list[str]:
    """Find known technology/skill terms (from `claim_validator.TECH_TERMS`)
    already present in `text`, returned with their original casing.

    Used to protect pre-existing skill keywords (e.g. "JavaScript", "D3.js")
    from being silently dropped during batch trimming. `target_keywords`
    only tracks JD terms the main tailoring pass newly added to a bullet —
    it says nothing about terms that were already there, so without this,
    an "aggressive rephrasing" trim pass is free to treat them as ordinary
    padding and cut them.
    """
    lowered = text.lower()
    found = []
    for term in TECH_TERMS:
        match = re.search(r"(?<!\w)" + re.escape(term) + r"(?!\w)", lowered)
        if match:
            found.append(text[match.start():match.end()])
    return found


# Synthetic bullet_id for the Skills-section pseudo-snapshot built by
# _build_bullet_snapshots — see its docstring for why this exists.
_SKILLS_SNAPSHOT_ID = "__skills_section__"

# Same-length-ish, past-tense synonyms for common resume action verbs,
# shortest-first so a swap is more likely to still fit the line-width
# budget after batch trimming has already run. Used by
# _enforce_verb_variety to break up an overused leading verb — the trim
# pass under character-budget pressure tends to fall back to the same
# generic, short verb ("Built") for every bullet it has to shorten,
# since it's the safest word to reach for.
ACTION_VERB_SYNONYMS: dict[str, list[str]] = {
    "built": ["Made", "Wrote", "Formed", "Coded", "Created", "Assembled", "Engineered", "Constructed"],
    "created": ["Made", "Built", "Formed", "Devised", "Authored", "Established", "Engineered"],
    "developed": ["Built", "Coded", "Crafted", "Engineered", "Designed", "Constructed"],
    "designed": ["Built", "Shaped", "Crafted", "Architected", "Engineered", "Structured"],
    "implemented": ["Built", "Coded", "Deployed", "Executed", "Rolled out", "Established"],
    "led": ["Ran", "Drove", "Headed", "Guided", "Directed", "Spearheaded", "Chaired"],
    "managed": ["Ran", "Oversaw", "Directed", "Handled", "Supervised", "Coordinated"],
    "automated": ["Streamlined", "Scripted", "Mechanized", "Systematized"],
    "integrated": ["Connected", "Linked", "Combined", "Unified", "Merged", "Embedded"],
    "improved": ["Boosted", "Refined", "Upgraded", "Enhanced", "Strengthened", "Elevated"],
    "optimized": ["Tuned", "Refined", "Streamlined", "Accelerated", "Sharpened"],
    "reduced": ["Cut", "Lowered", "Trimmed", "Shrank", "Slashed", "Curbed"],
    "increased": ["Grew", "Raised", "Lifted", "Boosted", "Expanded", "Scaled"],
    "deployed": ["Shipped", "Launched", "Released", "Rolled out", "Published"],
    "maintained": ["Ran", "Sustained", "Upheld", "Supported", "Preserved"],
    "tested": ["Verified", "Validated", "Vetted", "Audited", "Checked"],
    "wrote": ["Authored", "Drafted", "Composed", "Produced", "Penned"],
    "analyzed": ["Studied", "Examined", "Assessed", "Evaluated", "Investigated"],
}


def _enforce_verb_variety(result: TailoringResult, measurer) -> None:
    """Cap any single leading action verb at two uses across the final
    resume, swapping in a synonym for the third-and-later rewritten
    bullets that use it.

    Only ever rewrites bullets this pass is already allowed to touch
    (action == "rewrite") — an original, untouched ("keep") bullet is
    never edited just to vary its wording, since that's the candidate's
    own authentic phrasing, not something tailoring produced. Counting
    still includes "keep" bullets, though: if two original bullets
    already say "Built" and a rewrite lands on "Built" too, the reader
    sees it three times regardless of which ones we technically changed,
    so the count has to reflect the whole visible resume, not just our
    own edits.

    Only swaps to a verb not already used elsewhere as a leading word,
    and only if the swapped bullet still fits its line (via `measurer`,
    same calibrated check the trim pass itself uses) — a failed swap is
    left as-is rather than risk overflow, and logged either way.
    """
    def leading_word(text: str) -> tuple[str, str]:
        stripped = text.strip()
        parts = stripped.split(maxsplit=1)
        if not parts:
            return "", ""
        first = parts[0].strip(",.;:()–—")
        rest = parts[1] if len(parts) > 1 else ""
        return first, rest

    used_lower: set[str] = set()
    counts: dict[str, int] = {}
    for change in result.bullet_changes:
        if change.action not in ("rewrite", "keep"):
            continue
        first, _ = leading_word(change.tailored_text)
        if first:
            used_lower.add(first.lower())

    for change in result.bullet_changes:
        if change.action not in ("rewrite", "keep"):
            continue
        first, rest = leading_word(change.tailored_text)
        if not first:
            continue
        key = first.lower()
        counts[key] = counts.get(key, 0) + 1
        if counts[key] <= 2:
            continue
        if change.action != "rewrite":
            # Never rewrite an untouched original bullet just to vary its
            # wording — that's the candidate's own phrasing. Still worth
            # surfacing that the resume-as-a-whole repeats it, though.
            result.debug_log.append(
                f"VERB REPEATED '{first}' ({counts[key]}x) in "
                f"{change.bullet_id} — unedited original bullet, left as-is"
            )
            continue

        synonyms = ACTION_VERB_SYNONYMS.get(key)
        if not synonyms:
            result.debug_log.append(
                f"VERB REPEATED '{first}' ({counts[key]}x) in "
                f"{change.bullet_id} — no known synonym, left as-is"
            )
            continue

        for candidate in synonyms:
            if candidate.lower() in used_lower:
                continue
            new_text = f"{candidate} {rest}" if rest else candidate
            if measurer is not None:
                measurement = measurer.measure(new_text)
                if not measurement.fits_one_line:
                    continue
            change.tailored_text = new_text
            change.reason = (
                (change.reason or "")
                + f" | Varied repeated verb: '{first}' -> '{candidate}'"
            ).strip(" |")
            used_lower.add(candidate.lower())
            result.debug_log.append(
                f"VARIED {change.bullet_id}: '{first}' -> '{candidate}' "
                f"(was repeated {counts[key]}x)"
            )
            break
        else:
            result.debug_log.append(
                f"VERB REPEATED '{first}' ({counts[key]}x) in "
                f"{change.bullet_id} — no unused synonym fit the line, left as-is"
            )


def _flag_if_still_overflowing(change: BulletChange, measurer) -> None:
    """After giving up on trimming a bullet (batch trim exhausted its 3
    rounds), check whether whatever text got shipped actually fits one
    line.

    Every "give up" branch in _batch_trim_overflows used to
    unconditionally revert to original_text with no check that the
    original itself fits — a bullet whose original text was already
    borderline/overflowing in the source document (or became so under
    an inflated calibrated char-cap) could ship still wrapping to a
    second line, with the only evidence being a debug_log line easy to
    miss. This surfaces it directly on the bullet's own `reason`, which
    is what the accept/reject review UI actually shows the user.
    """
    if measurer is None:
        return
    if not measurer.measure(change.tailored_text).fits_one_line:
        change.reason = (
            (change.reason or "").rstrip(" —")
            + " — still may wrap to a second line; could not fit within "
            "the character budget after 3 trim attempts"
        ).strip(" —")


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
        # Defaults sourced from config.py's LayoutConfig, which the
        # LAYOUT_MAX_BULLETS/LAYOUT_SINGLE_LINE/LAYOUT_MAX_CHARS env vars
        # were already documented to control but, before this fix, never
        # actually reached -- these values were independently hardcoded
        # literals that happened to match the config defaults. The live
        # /tailor endpoint (main.py) always passes explicit values from
        # TailorRequest regardless, so this mainly matters for any other
        # caller (tests, scripts) that relies on tailor()'s own defaults.
        max_bullets_per_entry: int = get_config().layout.max_bullets_per_entry,
        one_line_bullets: bool = True,
        enforce_single_line: bool = get_config().layout.enforce_single_line_bullets,
        max_bullet_chars: int = get_config().layout.max_chars_per_line,
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

            # Calculate coverage feedback for the LLM
            coverage_feedback = self._calculate_coverage_feedback(jd, ir.content, bank)

            engine = TailoringEngine()
            engine_result = engine.tailor(
                content=ir.content,
                jd_raw_text=jd.raw_text,
                max_chars_per_line=max_chars,
                max_bullets_per_entry=max_bullets_per_entry,
                available_width_pt=measurer.raw_width_pt if measurer else 0,
                font_name=measurer.font_name if measurer else "",
                font_size=measurer.font_size if measurer else 0,
                jd=jd,
                coverage_feedback=coverage_feedback,
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
                    result.reordered_skills[cat] = dedupe_skill_names(skills)
                for cat, skills in engine_result.skill_additions.items():
                    result.added_skills[cat] = dedupe_skill_names(skills)

            # === STAGE 2B: Optimize Skills Section (separate pass) ===
            # Skills are metadata and should be optimized independently
            # from bullets to prioritize JD-matching skills and remove soft skills
            try:
                skills_result = engine.optimize_skills(ir.content, jd)
                if skills_result.get("llm_error"):
                    result.debug_log.append(
                        f"SKILLS OPTIMIZATION FAILED (keeping original): "
                        f"{skills_result['llm_error']}"
                    )
                else:
                    # Merge skills optimizations into result
                    for cat, skills in skills_result.get("skill_reorders", {}).items():
                        if cat not in result.reordered_skills:
                            result.reordered_skills[cat] = dedupe_skill_names(skills)
                    for cat, skills in skills_result.get("skill_additions", {}).items():
                        if cat not in result.added_skills:
                            result.added_skills[cat] = dedupe_skill_names(skills)
                    # Note: skill_removals are tracked in the debug log for transparency
                    removals = skills_result.get("skill_removals", {})
                    if removals:
                        result.debug_log.append(
                            f"Skills marked for removal: {removals}"
                        )

                    result.debug_log.append(
                        f"Skills optimized ({skills_result.get('duration_ms', 0)}ms): "
                        f"reordered {len(result.reordered_skills)} categories, "
                        f"added {len(result.added_skills)} skills"
                    )
            except Exception as e:
                result.debug_log.append(
                    f"SKILLS OPTIMIZATION ERROR (continuing): {str(e)[:100]}"
                )
                logger.warning(f"Skills optimization error (graceful fallback): {e}")
            else:
                result.planning_error = engine_result.llm_error
                result.debug_log.append(
                    f"LLM FAILED: {engine_result.llm_error}"
                )
                logger.warning(
                    f"Tailoring engine failed: {engine_result.llm_error}"
                )

            # See _dedupe_additions_across_categories: the two independent
            # skill-proposing LLM calls above can each add the same skill
            # under a different category name.
            result.added_skills = _dedupe_additions_across_categories(
                result.added_skills
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

            # Quick Win #4: Store skill occurrence matrix for fast recalculation on accept/reject
            if match_result.skill_matrix and match_result.skill_matrix.matrix:
                result.skill_occurrence_matrix = match_result.skill_matrix.matrix
                result.debug_log.append(
                    f"Skill matrix pre-computed: {len(match_result.skill_matrix.skill_set)} skills, "
                    f"{len(match_result.skill_matrix.bullet_ids)} bullets"
                )
        except Exception as e:
            # Graceful degradation: coverage report failure doesn't block result
            result.debug_log.append(
                f"COVERAGE REPORT FAILED: {str(e)[:100]}"
            )
            logger.warning(f"Coverage report error (continuing anyway): {e}")
            # Still return the result; coverage metrics just won't be populated

        # === STAGE 5: Build bullet snapshots LAST ===
        # IMPORTANT: This must be AFTER safety net and trimming so snapshots
        # reflect the FINAL state of each bullet (after any reverts)
        try:
            self._build_bullet_snapshots(result, jd, content=ir.content)
            result.debug_log.append(
                f"Bullet snapshots built: {len(result.bullet_snapshots)} snapshots "
                f"for fast accept/reject recalculation"
            )
        except Exception as e:
            result.debug_log.append(
                f"SNAPSHOT BUILDING FAILED (continuing): {str(e)[:100]}"
            )
            logger.warning(f"Snapshot building error: {e}")

        # === STAGE 6: Calculate coverage from snapshots (single source of truth) ===
        # NOW that snapshots are built (reflecting final state), use them
        # to calculate all coverage metrics and detail breakdowns.
        # This ensures top metrics and detail lists always match perfectly.
        try:
            self._calculate_coverage_from_snapshots(result, jd)
            result.debug_log.append(
                "Coverage calculated from snapshots: single source of truth"
            )
        except Exception as e:
            result.debug_log.append(
                f"COVERAGE CALCULATION FROM SNAPSHOTS FAILED: {str(e)[:100]}"
            )
            logger.warning(f"Coverage from snapshots error: {e}")

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

        # === Batch overflow trimming ===
        if measurer:
            self._batch_trim_overflows(result, measurer, validator, bank, ir)

        # Cap any repeated leading verb at 2 uses (runs last since the
        # trim pass above is the main source of collapsing bullets onto
        # the same generic verb under character-budget pressure).
        _enforce_verb_variety(result, measurer)
        self._log_repeated_descriptors(result)

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
        from backend.config import get_config
        from backend.tailoring.tailoring_engine import (
            TailoringEngine,
            _compute_char_cap,
        )

        # Load configuration for trimming settings
        config = get_config()

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

        # Leading verbs already used by bullets NOT in this overflow batch
        # (i.e. bullets that already fit and won't be touched here) — the
        # trim LLM only ever sees the overflowing bullets themselves, so
        # without this it has no way to know "Built" is already used
        # elsewhere and tends to converge on it anyway under length
        # pressure. See prompts.py's VERB VARIETY rule.
        overflow_ids = {c.bullet_id for c in overflow_changes}
        existing_leading_verbs = [
            c.tailored_text.strip().split()[0].strip(",.;:()")
            for c in result.bullet_changes
            if c.bullet_id not in overflow_ids and c.tailored_text.strip()
        ]

        trimmer = TailoringEngine()
        full_resume_text = self._build_full_resume_text(ir.content)
        failure_history: dict[str, dict] = {}  # Track failures for retry feedback

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
                chars_over = len(change.tailored_text) - char_cap

                result.debug_log.append(
                    f"TRIMMING {change.bullet_id} (round {round_num + 1}): "
                    f"{len(change.tailored_text)} chars, "
                    f"{measurement.rendered_width_pt:.0f}pt "
                    f"> {measurement.available_width_pt:.0f}pt "
                    f"(char cap: {char_cap}, "
                    f"overflow: \"{overflow_text[:30]}\")"
                )

                # Protect both JD keywords the main pass added AND any
                # pre-existing tech/skill terms already in the bullet text —
                # otherwise the trim pass can drop things like "JavaScript"
                # or "D3.js" as if they were ordinary padding.
                must_keep = list(dict.fromkeys(
                    list(change.target_keywords or [])
                    + _tech_terms_present(change.tailored_text)
                ))

                batch_items.append({
                    "bullet_id": change.bullet_id,
                    "text": change.tailored_text,
                    "break_index": break_idx,
                    "max_chars": char_cap,
                    "keywords": must_keep,
                })

            # Single LLM call for all overflowing bullets (with failure history on retry)
            trim_results = trimmer.batch_trim_bullets(
                batch_items,
                is_retry=(round_num > 0),
                failure_history=failure_history if round_num > 0 else None,
                avoid_leading_verbs=existing_leading_verbs,
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
                        _flag_if_still_overflowing(change, measurer)
                    else:
                        still_overflowing.append(change)
                        # Track failure for retry: LLM returned nothing
                        failure_history[change.bullet_id] = {
                            "previous_attempt": change.tailored_text,
                            "overflow_chars": len(change.tailored_text) - next(
                                (b["max_chars"] for b in batch_items if b["bullet_id"] == change.bullet_id),
                                120
                            ),
                            "prev_char_count": len(change.tailored_text),
                        }
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
                    _flag_if_still_overflowing(change, measurer)
                    if not is_final:
                        # Track failure for retry: validation failed
                        failure_history[change.bullet_id] = {
                            "previous_attempt": trimmed,
                            "overflow_chars": len(trimmed) - next(
                                (b["max_chars"] for b in batch_items if b["bullet_id"] == change.bullet_id),
                                120
                            ),
                            "prev_char_count": len(trimmed),
                        }
                    continue

                trim_m = measurer.measure(trimmed)
                batch_item = next(
                    (b for b in batch_items if b["bullet_id"] == change.bullet_id),
                    None,
                )
                # Fitting the line isn't enough — confirm the trim didn't drop
                # a required keyword (JD term or pre-existing skill/tech term)
                # while "aggressively rephrasing" to save space.
                missing_keywords = [
                    kw for kw in (batch_item["keywords"] if batch_item else [])
                    if kw.lower() not in trimmed.lower()
                ]

                if trim_m.fits_one_line and not missing_keywords:
                    change.reason = (
                        (change.reason or "") + " (trimmed to fit)"
                    )
                    result.debug_log.append(
                        f"TRIMMED {change.bullet_id}: "
                        f"{len(trimmed)} chars, "
                        f"{trim_m.rendered_width_pt:.0f}pt "
                        f"(round {round_num + 1})"
                    )
                elif trim_m.fits_one_line and missing_keywords:
                    result.debug_log.append(
                        f"TRIM DROPPED KEYWORDS {change.bullet_id}: "
                        f"{missing_keywords} missing from \"{trimmed}\""
                    )
                    if is_final:
                        result.debug_log.append(
                            f"REVERTED {change.bullet_id}: "
                            f"trim dropped required keywords"
                        )
                        change.tailored_text = change.original_text
                        change.action = "keep"
                        change.reason = "Rewrite too long, trim dropped required keywords"
                        _flag_if_still_overflowing(change, measurer)
                    else:
                        still_overflowing.append(change)
                        failure_history[change.bullet_id] = {
                            "previous_attempt": trimmed,
                            "overflow_chars": 0,
                            "prev_char_count": len(trimmed),
                            "missing_keywords": missing_keywords,
                        }
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
                        _flag_if_still_overflowing(change, measurer)
                    else:
                        still_overflowing.append(change)
                        # Track failure for retry: made no progress
                        failure_history[change.bullet_id] = {
                            "previous_attempt": trimmed,
                            "overflow_chars": len(trimmed) - next(
                                (b["max_chars"] for b in batch_items if b["bullet_id"] == change.bullet_id),
                                120
                            ),
                            "prev_char_count": len(trimmed),
                        }
                else:
                    # Made progress but still overflows — use shorter text for retry
                    result.debug_log.append(
                        f"RETRIM {change.bullet_id}: "
                        f"{len(trimmed)} chars, "
                        f"{trim_m.rendered_width_pt:.0f}pt"
                    )
                    if is_final:
                        # Unlike the other give-up branches, `trimmed`
                        # here already passed fabrication validation
                        # (line ~711 above) AND is strictly shorter than
                        # original_text (guaranteed by the `elif
                        # len(trimmed) >= len(original_text)` branch
                        # above not having matched) — reverting to the
                        # longer, MORE-overflowing original would be
                        # strictly worse for layout while gaining
                        # nothing. Keep the shorter, validated attempt.
                        result.debug_log.append(
                            f"KEPT PARTIAL TRIM {change.bullet_id}: "
                            f"{len(trimmed)} chars, still overflows after "
                            f"3 rounds but is shorter than the {len(original_text)}-char original"
                        )
                        change.tailored_text = trimmed
                        change.reason = (change.reason or "") + " (partially trimmed, could not fully fit)"
                        _flag_if_still_overflowing(change, measurer)
                    else:
                        still_overflowing.append(change)
                        # Track failure for retry: still overflowing despite progress
                        failure_history[change.bullet_id] = {
                            "previous_attempt": trimmed,
                            "overflow_chars": max(0, len(trimmed) - next(
                                (b["max_chars"] for b in batch_items if b["bullet_id"] == change.bullet_id),
                                120
                            )),
                            "prev_char_count": len(trimmed),
                        }
                        # Update text to the trimmed version for next attempt
                        change.tailored_text = trimmed

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
    def _log_repeated_descriptors(result: TailoringResult):
        """Log (not fix) other descriptive words repeated 3+ times.

        Leading-verb repetition is actually fixed by
        _enforce_verb_variety; this is a lighter-weight diagnostic pass
        over everything else a rewrite adds mid-bullet (e.g. "scalable",
        "robust") — safely auto-swapping an arbitrary mid-sentence
        adjective risks awkward grammar in a way swapping a leading verb
        doesn't, so this stays informational, surfaced in the debug
        panel rather than silently rewriting more of the bullet.
        """
        from collections import Counter

        # Words that are fine to repeat (tech terms, common verbs —
        # verbs are excluded here since _enforce_verb_variety already
        # handles leading-verb repetition; no need to double-report).
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
    # Coverage feedback for LLM
    # ------------------------------------------------------------------

    def _calculate_coverage_feedback(
        self,
        jd: JobAnalysis,
        content: ResumeContent,
        bank: Optional[ResumeBank] = None,
    ) -> dict:
        """Calculate coverage gaps and underweight skills for LLM feedback.

        IMPORTANT: This uses _text_contains_keyword() WITH IMPLIES logic.
        LLM needs semantic understanding for good rewrite decisions.

        But final stored metrics use snapshots (DIRECT matches only).
        This gives LLM broader context while users see conservative metrics.

        Returns a dict with:
        - required_coverage, technical_coverage, responsibility_coverage (0.0-100)
        - matched_required, total_required, etc. (counts)
        - missing_skills (list of skill names not in resume)
        - underweight_skills (list of skill names in resume but only 1x)
        """
        from backend.tailoring.matcher import Matcher, _text_contains_keyword

        # Build resume text
        resume_parts = []
        for section in content.sections:
            for entry in section.experience_entries:
                resume_parts.append(entry.company)
                resume_parts.append(entry.role)
                for b in entry.bullets:
                    resume_parts.append(b.text)
            for entry in section.project_entries:
                resume_parts.append(entry.name)
                for b in entry.bullets:
                    resume_parts.append(b.text)
            for cat in section.skill_categories:
                resume_parts.extend(cat.skills)

        resume_text = " ".join(resume_parts)
        resume_text_lower = resume_text.lower()

        # Calculate coverage metrics (same as matcher)
        all_skills = jd.all_skills_flat()
        matched_technical = sum(
            s.importance for s in all_skills
            if _text_contains_keyword(resume_text_lower, s.name)
        )
        total_technical = sum(s.importance for s in all_skills)
        technical_coverage = (matched_technical / total_technical * 100) if total_technical > 0 else 0.0

        required = jd.required_skills
        matched_required = sum(
            s.importance for s in required
            if _text_contains_keyword(resume_text_lower, s.name)
        )
        total_required = sum(s.importance for s in required)
        required_coverage = (matched_required / total_required * 100) if total_required > 0 else 0.0

        responsibilities = jd.responsibilities
        matched_resp = sum(
            r.importance for r in responsibilities
            if any(_text_contains_keyword(resume_text_lower, kw) for kw in r.keywords)
            or any(_text_contains_keyword(resume_text_lower, word)
                   for word in r.text.split() if len(word) > 4)
        )
        total_resp = sum(r.importance for r in responsibilities)
        responsibility_coverage = (matched_resp / total_resp * 100) if total_resp > 0 else 0.0

        # Find missing skills (required skills not in resume)
        missing_skills = []
        for skill in required:
            if not _text_contains_keyword(resume_text_lower, skill.name):
                missing_skills.append(skill.name)

        # Find underweight skills (in resume but appear only once or sparingly)
        underweight_skills = []
        for skill in all_skills:
            if _text_contains_keyword(resume_text_lower, skill.name):
                # Count occurrences
                skill_lower = skill.name.lower()
                count = resume_text_lower.count(skill_lower)
                # If appears only once and high importance, mark for emphasis
                if count <= 1 and skill.importance >= 0.7:
                    underweight_skills.append(skill.name)

        # Format missing/underweight for display
        missing_str = ", ".join(missing_skills) if missing_skills else "None"
        underweight_str = ", ".join(underweight_skills) if underweight_skills else "None"

        return {
            "required_coverage": int(required_coverage),
            "matched_required": sum(1 for s in required if _text_contains_keyword(resume_text_lower, s.name)),
            "total_required": len(required),
            "technical_coverage": int(technical_coverage),
            "matched_technical": sum(1 for s in all_skills if _text_contains_keyword(resume_text_lower, s.name)),
            "total_technical": len(all_skills),
            "responsibility_coverage": int(responsibility_coverage),
            "matched_resp": sum(1 for r in responsibilities if any(_text_contains_keyword(resume_text_lower, kw) for kw in r.keywords)),
            "total_resp": len(responsibilities),
            "missing_skills": missing_str,
            "underweight_skills": underweight_str,
        }

    # ------------------------------------------------------------------

    def _build_coverage_report(
        self,
        result: TailoringResult,
        ir: ResumeIR,
        jd: JobAnalysis,
        match_result,
    ):
        """Build keyword coverage report (legacy).

        NOTE: Coverage metrics and detail breakdown are now calculated from
        snapshots after they're built. This function only builds keyword_coverage
        for historical tracking.
        """
        from backend.tailoring.matcher import _text_contains_keyword

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

    # ------------------------------------------------------------------
    # Build bullet snapshots
    # ------------------------------------------------------------------

    @staticmethod
    def _snapshot_for_text(bullet_id: str, text_lower: str, jd: JobAnalysis):
        """Build one BulletSnapshot's matches against `text_lower` (already
        _normalize()-d). Factored out of _build_bullet_snapshots so the
        same matching logic can run against a real bullet's text or
        against a synthetic "text" built from the Skills section.
        """
        from backend.models.tailoring import BulletSnapshot
        from backend.tailoring.matcher import (
            _text_contains_keyword_direct,
            match_jd_requirement,
            match_deliverable,
        )

        snapshot = BulletSnapshot(bullet_id=bullet_id)

        # OLD APPROACH (kept for backward compatibility):
        # Match required skills (DIRECT only — no IMPLIES)
        for skill in jd.required_skills:
            if _text_contains_keyword_direct(text_lower, skill.name):
                snapshot.matched_required_skills.append(skill.name)

        # Match technical keywords (non-required, DIRECT only — no IMPLIES)
        for skill in jd.all_skills_flat():
            if skill not in jd.required_skills:
                if _text_contains_keyword_direct(text_lower, skill.name):
                    snapshot.matched_technical_keywords.append(skill.name)

        # Match responsibilities (DIRECT keyword matching only)
        for resp in jd.responsibilities:
            matched = any(
                _text_contains_keyword_direct(text_lower, kw)
                for kw in resp.keywords
            ) or any(
                _text_contains_keyword_direct(text_lower, word)
                for word in resp.text.split() if len(word) > 4
            )
            if matched:
                snapshot.matched_responsibilities.append(resp.text[:100])

        # NEW APPROACH (Phase 4):
        # Also check categorized requirements for richer matching
        # This enables tracking of both explicit and inferred matches
        if jd.technical_requirements:
            for req in jd.technical_requirements:
                match = match_jd_requirement(text_lower, req)
                if match.ats_found or match.human_understandable:
                    # Track that this requirement is satisfied
                    if req not in snapshot.matched_required_skills:
                        if req.requirement_level == "required":
                            snapshot.matched_required_skills.append(req.keyword_phrase)
                        else:
                            snapshot.matched_technical_keywords.append(req.keyword_phrase)
                    # ALSO track in new field for cleaner metrics
                    if req.keyword_phrase not in snapshot.matched_technical_requirements:
                        snapshot.matched_technical_requirements.append(req.keyword_phrase)

        if jd.deliverables:
            for deliverable in jd.deliverables:
                match = match_deliverable(text_lower, deliverable)
                if match.ats_found:
                    snapshot.matched_responsibilities.append(deliverable.phrase)
                    # ALSO track in new field for cleaner metrics
                    if deliverable.phrase not in snapshot.matched_deliverables:
                        snapshot.matched_deliverables.append(deliverable.phrase)

        return snapshot

    def _build_bullet_snapshots(
        self,
        result: TailoringResult,
        jd: JobAnalysis,
        content: Optional[ResumeContent] = None,
    ):
        """Build snapshots from FINAL bullet state (after all reverting).

        IMPORTANT: This is called LAST, after safety net and trimming,
        so snapshots reflect the true final state of each bullet.

        Snapshots enable fast O(bullets) coverage recalculation on accept/reject
        instead of O(skills × text) text scanning.

        CRITICAL: Uses DIRECT matches only (no IMPLIES inference) to prevent
        inflated coverage percentages. Only explicit keywords count.

        Phase 4: Now also uses categorized requirements for more sophisticated matching.

        `content`: when provided, also builds one synthetic snapshot
        (bullet_id=_SKILLS_SNAPSHOT_ID) from the resume's Skills section
        text. Without this, a JD skill listed ONLY in the Skills section
        (never repeated in a bullet — an extremely common resume
        pattern) was reported "missing" from the headline coverage %
        and detail breakdown, even though a separate keyword list built
        elsewhere (main.py's _recalculate_coverage) DID scan the Skills
        section and could show the same skill as "matched" — the two
        could directly contradict each other in the UI.
        """
        from backend.tailoring.matcher import _normalize

        result.bullet_snapshots = []  # Clear any previous snapshots

        for change in result.bullet_changes:
            if change.action == "remove":
                # Removed bullets don't need snapshots
                continue

            # Use the FINAL text (after any reverts)
            # If action is "rewrite", use tailored_text; if "keep", use original
            final_text = change.tailored_text if change.action == "rewrite" else change.original_text
            text_lower = _normalize(final_text)

            result.bullet_snapshots.append(
                self._snapshot_for_text(change.bullet_id, text_lower, jd)
            )

        if content is not None:
            skills_text_parts = []
            for section in content.sections:
                if section.type != SectionType.SKILLS:
                    continue
                for cat in section.skill_categories:
                    skills_text_parts.append(f"{cat.category}: {', '.join(cat.skills)}")
            if skills_text_parts:
                skills_text_lower = _normalize(" ".join(skills_text_parts))
                result.bullet_snapshots.append(
                    self._snapshot_for_text(_SKILLS_SNAPSHOT_ID, skills_text_lower, jd)
                )

    # ------------------------------------------------------------------
    # Calculate coverage from snapshots (single source of truth)
    # (Existing implementation kept for backward compatibility)
    # ------------------------------------------------------------------

    def _calculate_coverage_from_snapshots(self, result: TailoringResult, jd: JobAnalysis):
        """Calculate all coverage metrics and detail breakdowns from snapshots.

        CRITICAL: Snapshots are the single source of truth for coverage.
        This ensures top metrics perfectly match detail breakdown.

        Called AFTER snapshots are built (final state), ensures all coverage
        reflects the true resume that will be rendered.
        """
        # Include all bullets except removed ones
        included_bullet_ids = set()
        for change in result.bullet_changes:
            if change.action != "remove":
                included_bullet_ids.add(change.bullet_id)

        # Build snapshot lookup
        snapshots_by_id = {s.bullet_id: s for s in result.bullet_snapshots}

        # The Skills-section pseudo-snapshot (see _build_bullet_snapshots)
        # isn't tied to any BulletChange, so it's never picked up by the
        # loop above — include it explicitly whenever it exists, so a
        # skill listed only in the Skills section counts toward coverage
        # the same way a skill mentioned in a bullet does.
        if _SKILLS_SNAPSHOT_ID in snapshots_by_id:
            included_bullet_ids.add(_SKILLS_SNAPSHOT_ID)

        # === COVERAGE METRICS (top percentages) ===
        required = jd.required_skills
        if required:
            matched_importance = 0
            for skill in required:
                for bid in included_bullet_ids:
                    snapshot = snapshots_by_id.get(bid)
                    if snapshot and skill.name in snapshot.matched_required_skills:
                        matched_importance += skill.importance
                        break
            total_importance = sum(s.importance for s in required)
            result.required_skill_coverage = matched_importance / total_importance if total_importance > 0 else 0.0
        else:
            result.required_skill_coverage = 0.0

        # Technical keywords coverage
        all_skills = jd.all_skills_flat()
        if all_skills:
            matched_importance = 0
            for skill in all_skills:
                for bid in included_bullet_ids:
                    snapshot = snapshots_by_id.get(bid)
                    if snapshot and skill.name in snapshot.matched_technical_keywords:
                        matched_importance += skill.importance
                        break
            total_importance = sum(s.importance for s in all_skills)
            result.technical_keyword_coverage = matched_importance / total_importance if total_importance > 0 else 0.0
        else:
            result.technical_keyword_coverage = 0.0

        # Responsibilities coverage
        responsibilities = jd.responsibilities
        if responsibilities:
            matched_importance = 0
            for resp in responsibilities:
                for bid in included_bullet_ids:
                    snapshot = snapshots_by_id.get(bid)
                    # Membership check against the full stored snippet —
                    # not "does any single WORD of resp.text equal a full
                    # multi-word snippet string", which is essentially
                    # never true and made this metric report ~0% coverage
                    # regardless of how well-matched the resume actually
                    # was (snapshot.matched_responsibilities stores full
                    # resp.text[:100] snippets, see _build_bullet_snapshots).
                    if snapshot and resp.text[:100] in snapshot.matched_responsibilities:
                        matched_importance += resp.importance
                        break
            total_importance = sum(r.importance for r in responsibilities)
            result.responsibility_coverage = matched_importance / total_importance if total_importance > 0 else 0.0
        else:
            result.responsibility_coverage = 0.0

        # === NEW (Simplified Metrics): Skills and Activities Coverage ===
        # From new categorized requirements system
        if jd.technical_requirements:
            matched_count = 0
            for req in jd.technical_requirements:
                for bid in included_bullet_ids:
                    snapshot = snapshots_by_id.get(bid)
                    if snapshot and req.keyword_phrase in snapshot.matched_technical_requirements:
                        matched_count += 1
                        break
            total_count = len(jd.technical_requirements)
            result.skills_matched_coverage = matched_count / total_count if total_count > 0 else 0.0
        else:
            result.skills_matched_coverage = 0.0

        if jd.deliverables:
            matched_count = 0
            for deliverable in jd.deliverables:
                for bid in included_bullet_ids:
                    snapshot = snapshots_by_id.get(bid)
                    if snapshot and deliverable.phrase in snapshot.matched_deliverables:
                        matched_count += 1
                        break
            total_count = len(jd.deliverables)
            result.activities_matched_coverage = matched_count / total_count if total_count > 0 else 0.0
        else:
            result.activities_matched_coverage = 0.0

        # === DETAIL BREAKDOWN (what makes each percentage) ===
        result.required_skills_matched = []
        result.required_skills_missing = []
        result.technical_keywords_matched = []
        result.technical_keywords_missing = []
        result.responsibilities_matched = []
        result.responsibilities_missing = []

        # Required skills
        for skill in required:
            found = False
            for bid in included_bullet_ids:
                if skill.name in snapshots_by_id.get(bid, {}).matched_required_skills:
                    found = True
                    break
            if found:
                result.required_skills_matched.append(skill.name)
            else:
                result.required_skills_missing.append(skill.name)

        # Technical keywords
        for skill in all_skills:
            if skill in required:
                continue
            found = False
            for bid in included_bullet_ids:
                if skill.name in snapshots_by_id.get(bid, {}).matched_technical_keywords:
                    found = True
                    break
            if found:
                result.technical_keywords_matched.append(skill.name)
            else:
                result.technical_keywords_missing.append(skill.name)

        # Responsibilities
        for resp in responsibilities:
            found = False
            for bid in included_bullet_ids:
                snapshot = snapshots_by_id.get(bid)
                if snapshot and resp.text[:100] in snapshot.matched_responsibilities:
                    found = True
                    break
            if found:
                result.responsibilities_matched.append(resp.text[:100])
            else:
                result.responsibilities_missing.append(resp.text[:100])

        # === NEW (Simplified Metrics): Skills and Activities Breakdown ===
        # From new categorized requirements system
        result.skills_matched = []
        result.skills_missing = []
        for req in jd.technical_requirements:
            found = False
            for bid in included_bullet_ids:
                snapshot = snapshots_by_id.get(bid)
                if snapshot and req.keyword_phrase in snapshot.matched_technical_requirements:
                    found = True
                    break
            if found:
                result.skills_matched.append(req.keyword_phrase)
            else:
                result.skills_missing.append(req.keyword_phrase)

        result.activities_matched = []
        result.activities_missing = []
        for deliverable in jd.deliverables:
            found = False
            for bid in included_bullet_ids:
                snapshot = snapshots_by_id.get(bid)
                if snapshot and deliverable.phrase in snapshot.matched_deliverables:
                    found = True
                    break
            if found:
                result.activities_matched.append(deliverable.phrase)
            else:
                result.activities_missing.append(deliverable.phrase)

        # === NEW (Phase 4): Calculate two-tier coverage if categorized requirements exist ===
        if jd.technical_requirements or jd.deliverables:
            # Build resume text from final bullets
            resume_text = " ".join(
                change.tailored_text if change.action == "rewrite" else change.original_text
                for change in result.bullet_changes
                if change.action != "remove"
            )
            # Use new two-tier calculation
            ats_cov = self.calculate_ats_coverage(resume_text, jd)
            result.ats_coverage = ats_cov.get("ats_coverage", 0.0)
            result.human_coverage = ats_cov.get("human_coverage", 0.0)
            result.coverage_gap = result.human_coverage - result.ats_coverage

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
                    # Capture what was already on the resume BEFORE this
                    # category is touched — used below so a rejected
                    # addition can't sneak back in via the separate
                    # skill_reorders list (see rejected_additions).
                    original_skills = [
                        s.replace("[LLM]", "") for s in cat.skills
                    ]

                    if cat.category in result.added_skills:
                        for new_skill in result.added_skills[cat.category]:
                            key = f"{cat.category}:{new_skill}"
                            if key in result.additions_accepted:
                                accepted = result.additions_accepted[key]
                            else:
                                # No explicit addition decision yet. The LLM's
                                # skill_reorders list for this category often
                                # independently repeats the same new skill (it
                                # describes the whole desired final order, new
                                # items included) — from the user's point of
                                # view, seeing that skill via the "Skills
                                # Reordered" card and rejecting it there IS
                                # rejecting the skill, even though addition/
                                # reorder are tracked as separate decisions.
                                # Without this, an explicit reorder-reject was
                                # silently ignored: this loop still defaulted
                                # to True and appended the skill anyway, and
                                # it landed at the very end of the line since
                                # the (rejected) reorder step that would have
                                # positioned it never ran.
                                in_reorder = (
                                    cat.category in result.reordered_skills
                                    and is_duplicate_skill(
                                        new_skill,
                                        result.reordered_skills[cat.category],
                                    )
                                )
                                reorder_decision = result.reorder_accepted.get(
                                    cat.category
                                )
                                accepted = not (in_reorder and reorder_decision is False)
                            # Compare against unmarked text — variant-aware
                            # (case, punctuation, and known pairs like
                            # "HTML"/"HTML5" or "Git"/"GitHub"), re-checked
                            # against skills added earlier in this same loop.
                            plain_existing = [
                                s.replace("[LLM]", "") for s in cat.skills
                            ]
                            if accepted and not is_duplicate_skill(new_skill, plain_existing):
                                # === CHANGE: Mark LLM-added skills with internal marker ===
                                # When skills fitting trims the row, it can see which skills
                                # came from the LLM (JD-matched) vs original resume.
                                # Trim the original skills first; protect the LLM additions.
                                marked_skill = f"[LLM]{new_skill}"
                                cat.skills.append(marked_skill)

                    if cat.category in result.reordered_skills:
                        accepted = result.reorder_accepted.get(
                            cat.category, True,
                        )
                        if accepted:
                            # The LLM's own reordered list can itself contain
                            # duplicates/near-duplicates (e.g. "Python" twice,
                            # or "Git" and "GitHub") — dedupe before trusting it.
                            reordered = dedupe_skill_names(
                                list(result.reordered_skills[cat.category])
                            )
                            # A skill the user explicitly rejected as an
                            # ADDITION must not sneak back in just because
                            # the separate reorder list also happens to
                            # include it — additions/reorders are tracked
                            # independently, so accepting a reorder says
                            # nothing about a specific addition decision.
                            # Only filters genuinely-new items; a skill that
                            # was already on the resume before tailoring
                            # stays regardless of any addition rejection.
                            # Variant-aware on both sides in case the
                            # spelling in the reorder list doesn't exactly
                            # match how the addition was rejected.
                            rejected_additions = [
                                key.split(":", 1)[1]
                                for key, accepted in result.additions_accepted.items()
                                if not accepted
                                and key.startswith(f"{cat.category}:")
                            ]
                            reordered = [
                                s for s in reordered
                                if is_duplicate_skill(s, original_skills)
                                or not is_duplicate_skill(s, rejected_additions)
                            ]
                            for s in cat.skills:
                                # Compare unmarked — "s" may already carry an
                                # "[LLM]" marker from the additions step just
                                # above, which would otherwise never match a
                                # plain entry already in "reordered" (they'd
                                # only collide later, once the marker is
                                # stripped for display, as a visible dupe).
                                if not is_duplicate_skill(s.replace("[LLM]", ""), reordered):
                                    reordered.append(s)
                            cat.skills = reordered

                    # Always clean up the final list, even for categories the
                    # LLM never touched — a category can already contain
                    # duplicates from the source resume itself (e.g. "GraphQL"
                    # listed twice, or "TypeScript" alongside "JavaScript/
                    # TypeScript"), and those would otherwise pass through
                    # tailoring untouched. Marker-aware so a "[LLM]"-tagged
                    # addition and a plain entry for the same skill collapse
                    # into one instead of surviving as look-alike duplicates.
                    cat.skills = _dedupe_marked_skills(cat.skills)

                # Cross-category cleanup: every check above only compared a
                # category against itself, so the same skill could still be
                # claimed by two different buckets (e.g. "GraphQL" added to
                # both "Technologies" and "Concepts" by the two independent
                # LLM calls, each blind to what the other — or any other
                # category — decided). Walk the whole section once, keeping
                # a running set of what's already been claimed; first
                # category in the resume's own order wins.
                claimed: list[str] = []
                for cat in section.skill_categories:
                    kept = []
                    for s in cat.skills:
                        plain = s.replace("[LLM]", "")
                        if is_duplicate_skill(plain, claimed):
                            continue
                        kept.append(s)
                        claimed.append(plain)
                    cat.skills = kept

                # A category name in added_skills/reordered_skills that
                # never matched any of the resume's actual skill
                # categories (above, both are only ever looked up by
                # "cat.category in result.X") means the LLM's proposal
                # for that whole category silently never applied — no
                # error, nothing in the rendered resume, and no
                # indication in the UI that a decision was even possible.
                # This can't be safely auto-recovered (there's no
                # reliable way to guess which existing category the LLM
                # actually meant, and creating a brand new one risks a
                # confusing near-duplicate category), but it should at
                # least be visible for debugging rather than silent.
                actual_categories = {c.category for c in section.skill_categories}
                unmatched = (
                    set(result.added_skills) | set(result.reordered_skills)
                ) - actual_categories
                for name in sorted(unmatched):
                    result.debug_log.append(
                        f"SKILLS CATEGORY MISMATCH: '{name}' from the LLM's "
                        f"proposal doesn't match any of this resume's actual "
                        f"skill categories ({sorted(actual_categories)}) — "
                        f"its additions/reorder were silently never applied."
                    )

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
                        # Copy ALL properties from original run, only replace text
                        run_props = first_run.model_dump(exclude={"text"})
                        el.paragraph_format.runs = [RunFormat(
                            text=new_text,
                            **run_props,
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
                        # Copy ALL properties from original run, override specific ones
                        base_props = first_run.model_dump(exclude={"text", "bold"})
                        el.paragraph_format.runs = [
                            RunFormat(
                                text=label + ": ",
                                bold=True,
                                **base_props,
                            ),
                            RunFormat(
                                text=value.strip(),
                                bold=False,
                                **base_props,
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

    # ------------------------------------------------------------------
    # NEW (Phase 3): Coverage using categorized requirements
    # ------------------------------------------------------------------

    def calculate_ats_coverage(self, resume_text: str, jd: JobAnalysis) -> dict:
        """Calculate coverage using new categorized JD requirements.

        This demonstrates the new two-tier approach:
        - ATS-found: What ATS will literally find
        - Human-understandable: What humans would recognize as meeting the requirement

        Phase 3 implementation: for demonstration and migration planning.
        Eventually this will replace the snapshot-based approach.

        Args:
            resume_text: Full resume text (combined)
            jd: Analyzed job description with categorized requirements

        Returns:
            Dict with ats_coverage, human_coverage, and requirement_matches
        """
        from backend.tailoring.matcher import (
            match_jd_requirement,
            match_deliverable,
            match_behavioral_requirement,
            _normalize,
        )

        resume_text_lower = _normalize(resume_text)
        all_matches = []

        # Match technical requirements
        ats_found_count = 0
        human_understood_count = 0

        for requirement in jd.technical_requirements:
            match = match_jd_requirement(resume_text_lower, requirement)
            all_matches.append(match)
            if match.ats_found:
                ats_found_count += 1
            if match.human_understandable:
                human_understood_count += 1

        # Match deliverables
        for deliverable in jd.deliverables:
            match = match_deliverable(resume_text_lower, deliverable)
            all_matches.append(match)
            if match.ats_found:
                ats_found_count += 1
            if match.human_understandable:
                human_understood_count += 1

        # Match behavioral requirements (for context, not scoring)
        for behavior in jd.behavioral_requirements:
            match = match_behavioral_requirement(resume_text_lower, behavior)
            all_matches.append(match)

        total_scored = (
            len(jd.technical_requirements) + len(jd.deliverables)
        )

        return {
            "ats_coverage": (ats_found_count / total_scored) if total_scored > 0 else 0.0,
            "human_coverage": (human_understood_count / total_scored) if total_scored > 0 else 0.0,
            "ats_found": ats_found_count,
            "human_understood": human_understood_count,
            "total": total_scored,
            "requirement_matches": all_matches,
            "matched_by_ats": [m for m in all_matches if m.ats_found],
            "human_understandable_but_not_ats": [
                m for m in all_matches
                if m.human_understandable and not m.ats_found
            ],
            "not_found": [m for m in all_matches if not m.ats_found],
        }

    def save_edited_bullets(
        self,
        result: TailoringResult,
        edited_bullets: dict[str, str],  # bullet_id -> new_text
        jd: JobAnalysis,
        content: Optional[ResumeContent] = None,
    ) -> TailoringResult:
        """Save user-edited bullet text and recalculate coverage metrics.

        User edits bullets on the review tab → clicks Save → this updates the
        result with new text and recalculates all coverage metrics.

        Args:
            result: Current TailoringResult with user's edits
            edited_bullets: {bullet_id: new_text} for bullets that were edited
            jd: Job description (for recalculating coverage)
            content: current resume content, so coverage also accounts for
                the Skills section (see _build_bullet_snapshots) — optional
                for backward compatibility, but coverage undercounts
                Skills-section-only matches without it.

        Returns:
            Updated TailoringResult with new text and recalculated metrics
        """
        # Update bullet changes with edited text
        for change in result.bullet_changes:
            if change.bullet_id in edited_bullets:
                new_text = edited_bullets[change.bullet_id]
                # Mark as edited by user (even if it was originally a rewrite)
                change.tailored_text = new_text
                change.action = "rewrite"  # Ensure it's marked as a change
                change.accepted = True  # User explicitly edited it

        # Rebuild snapshots with edited text
        self._build_bullet_snapshots(result, jd, content=content)

        # Recalculate coverage from updated snapshots
        self._calculate_coverage_from_snapshots(result, jd)

        return result
