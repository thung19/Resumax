"""Tests for _enforce_verb_variety (backend/services/tailoring_service.py).

Regression: the LLM's batch-trim safety net, under character-budget
pressure, tends to fall back to the same short generic verb ("Built")
across multiple bullets it has to shorten. The prior "dedup" check
(_dedup_adjectives) never caught this: it explicitly exempted "built"
(and every other common resume verb) from its own count, and even when
it *did* flag something, it only ever wrote a debug-log line -- nothing
was actually changed. _enforce_verb_variety replaces it with an actual
fix: cap any leading verb at two uses, swapping a synonym into the
third-and-later rewritten bullets that use it.
"""

from backend.models.tailoring import BulletChange, TailoringResult
from backend.services.tailoring_service import _enforce_verb_variety


class _FakeMeasurement:
    def __init__(self, fits: bool):
        self.fits_one_line = fits


class _AlwaysFitsMeasurer:
    def measure(self, text: str) -> _FakeMeasurement:
        return _FakeMeasurement(True)


class _NeverFitsMeasurer:
    def measure(self, text: str) -> _FakeMeasurement:
        return _FakeMeasurement(False)


def _rewrite(bullet_id: str, text: str) -> BulletChange:
    return BulletChange(
        bullet_id=bullet_id,
        original_text="original text here",
        tailored_text=text,
        action="rewrite",
    )


def _keep(bullet_id: str, text: str) -> BulletChange:
    return BulletChange(
        bullet_id=bullet_id, original_text=text, tailored_text=text, action="keep",
    )


class TestEnforceVerbVariety:
    def test_third_occurrence_of_repeated_verb_is_swapped(self):
        result = TailoringResult(resume_id="r1")
        result.bullet_changes = [
            _rewrite("b1", "Built a REST API for internal tools"),
            _rewrite("b2", "Built a CI/CD pipeline for deployments"),
            _rewrite("b3", "Built a dashboard for analytics"),
        ]
        _enforce_verb_variety(result, _AlwaysFitsMeasurer())

        firsts = [c.tailored_text.split()[0] for c in result.bullet_changes]
        assert firsts[0] == "Built"
        assert firsts[1] == "Built"
        assert firsts[2] != "Built"  # only the 3rd+ occurrence changes
        assert len(set(firsts)) == 2

    def test_first_two_occurrences_untouched(self):
        result = TailoringResult(resume_id="r1")
        result.bullet_changes = [
            _rewrite("b1", "Built a REST API"),
            _rewrite("b2", "Built a dashboard"),
        ]
        original = [c.tailored_text for c in result.bullet_changes]
        _enforce_verb_variety(result, _AlwaysFitsMeasurer())
        assert [c.tailored_text for c in result.bullet_changes] == original

    def test_fourth_occurrence_gets_a_different_synonym_than_the_third(self):
        result = TailoringResult(resume_id="r1")
        result.bullet_changes = [
            _rewrite("b1", "Built a REST API"),
            _rewrite("b2", "Built a dashboard"),
            _rewrite("b3", "Built a pipeline"),
            _rewrite("b4", "Built a scraper"),
        ]
        _enforce_verb_variety(result, _AlwaysFitsMeasurer())
        firsts = [c.tailored_text.split()[0] for c in result.bullet_changes]
        # No verb should now appear more than twice, and the two swapped
        # bullets shouldn't have collided on the same replacement.
        assert firsts.count("Built") == 2
        swapped = [f for f in firsts if f != "Built"]
        assert len(swapped) == 2
        assert swapped[0] != swapped[1]

    def test_kept_original_bullets_are_never_rewritten(self):
        result = TailoringResult(resume_id="r1")
        result.bullet_changes = [
            _keep("b1", "Built a REST API"),
            _keep("b2", "Built a dashboard"),
            _keep("b3", "Built a pipeline"),
        ]
        original = [c.tailored_text for c in result.bullet_changes]
        _enforce_verb_variety(result, _AlwaysFitsMeasurer())
        assert [c.tailored_text for c in result.bullet_changes] == original
        assert any("unedited original" in line for line in result.debug_log)

    def test_keep_bullets_still_count_toward_the_total(self):
        # Two original (untouched) bullets already say "Built"; a third,
        # rewritten bullet also lands on "Built" -- that's 3 visible
        # occurrences even though only one of them is "ours" to fix.
        result = TailoringResult(resume_id="r1")
        result.bullet_changes = [
            _keep("b1", "Built a REST API"),
            _keep("b2", "Built a dashboard"),
            _rewrite("b3", "Built a pipeline"),
        ]
        _enforce_verb_variety(result, _AlwaysFitsMeasurer())
        assert result.bullet_changes[0].tailored_text == "Built a REST API"
        assert result.bullet_changes[1].tailored_text == "Built a dashboard"
        assert not result.bullet_changes[2].tailored_text.startswith("Built")

    def test_synonym_not_used_if_it_would_overflow_the_line(self):
        result = TailoringResult(resume_id="r1")
        result.bullet_changes = [
            _rewrite("b1", "Built a REST API"),
            _rewrite("b2", "Built a dashboard"),
            _rewrite("b3", "Built a pipeline"),
        ]
        original_third = result.bullet_changes[2].tailored_text
        _enforce_verb_variety(result, _NeverFitsMeasurer())
        # No candidate fits -> left as-is, logged.
        assert result.bullet_changes[2].tailored_text == original_third
        assert any("no unused synonym fit the line" in line for line in result.debug_log)

    def test_unknown_verb_with_no_synonym_table_entry_left_as_is(self):
        result = TailoringResult(resume_id="r1")
        result.bullet_changes = [
            _rewrite("b1", "Spearheaded a REST API"),
            _rewrite("b2", "Spearheaded a dashboard"),
            _rewrite("b3", "Spearheaded a pipeline"),
        ]
        original_third = result.bullet_changes[2].tailored_text
        _enforce_verb_variety(result, _AlwaysFitsMeasurer())
        assert result.bullet_changes[2].tailored_text == original_third
        assert any("no known synonym" in line for line in result.debug_log)

    def test_no_measurer_still_swaps(self):
        # apply_tailoring's caller sometimes has no measurer available at
        # all (e.g. layout detection failed) -- the swap should still
        # happen rather than silently doing nothing.
        result = TailoringResult(resume_id="r1")
        result.bullet_changes = [
            _rewrite("b1", "Built a REST API"),
            _rewrite("b2", "Built a dashboard"),
            _rewrite("b3", "Built a pipeline"),
        ]
        _enforce_verb_variety(result, None)
        firsts = [c.tailored_text.split()[0] for c in result.bullet_changes]
        assert firsts[2] != "Built"

    def test_two_different_overused_verbs_both_fixed(self):
        result = TailoringResult(resume_id="r1")
        result.bullet_changes = [
            _rewrite("b1", "Built a REST API"),
            _rewrite("b2", "Built a dashboard"),
            _rewrite("b3", "Built a pipeline"),
            _rewrite("b4", "Led a migration project"),
            _rewrite("b5", "Led a hiring initiative"),
            _rewrite("b6", "Led a design review"),
        ]
        _enforce_verb_variety(result, _AlwaysFitsMeasurer())
        firsts = [c.tailored_text.split()[0] for c in result.bullet_changes]
        assert firsts.count("Built") == 2
        assert firsts.count("Led") == 2
