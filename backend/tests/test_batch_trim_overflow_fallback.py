"""Tests for _batch_trim_overflows' final-round "give up" behavior
(backend/services/tailoring_service.py) and the _flag_if_still_overflowing
helper.

Regression: every "give up after 3 rounds" branch used to unconditionally
revert to the bullet's original_text with no check that the original
itself fits one line -- so a bullet whose original text was already
borderline/overflowing could ship still wrapping to a second line, with
nothing but an easily-missed debug_log line as evidence. Worse, the
specific branch for "the LLM made real progress shortening the bullet
across rounds but it still doesn't quite fit" reverted to the *original*
(longer, MORE overflowing) text even though it already had a shorter,
already-validated partial trim in hand -- objectively worse for layout
while gaining nothing.
"""

from unittest.mock import patch

from backend.models.resume_content import (
    Bullet, ExperienceEntry, ResumeContent, ResumeSection, SectionType,
)
from backend.models.resume_ir import ResumeIR
from backend.models.tailoring import BulletChange, ResumeBank, TailoringResult
from backend.services.tailoring_service import (
    TailoringService, _flag_if_still_overflowing,
)
from backend.tailoring.claim_validator import ClaimValidator


class _FakeMeasurement:
    def __init__(self, fits: bool):
        self.fits_one_line = fits
        self.rendered_width_pt = 999.0
        self.available_width_pt = 500.0


class _FakeMeasurer:
    """len(text) <= FIT_THRESHOLD => fits one line."""
    FIT_THRESHOLD = 100
    raw_width_pt = 500.0
    font_name = "Helvetica"  # a real ReportLab built-in name
    font_size = 10.0

    def measure(self, text: str) -> _FakeMeasurement:
        return _FakeMeasurement(fits=len(text) <= self.FIT_THRESHOLD)

    def find_line_break(self, text: str) -> int:
        return min(len(text), self.FIT_THRESHOLD)


ORIGINAL_TEXT = (
    "Led cross-functional initiative to deliver enterprise-scale distributed "
    "microservices architecture spanning multiple continents for global "
    "enterprise clients worldwide"
)  # 178 chars, always overflows against FIT_THRESHOLD=100


def _ir_and_bank(bullet_id: str) -> tuple[ResumeIR, ResumeBank]:
    content = ResumeContent(
        sections=[
            ResumeSection(
                id="sec-1",
                type=SectionType.EXPERIENCE,
                title="Experience",
                experience_entries=[
                    ExperienceEntry(
                        id="entry-1",
                        company="Acme",
                        role="Engineer",
                        bullets=[Bullet(id=bullet_id, text=ORIGINAL_TEXT)],
                    ),
                ],
            ),
        ],
    )
    return ResumeIR(content=content), ResumeBank()


def _result(bullet_id: str, tailored_text: str) -> TailoringResult:
    result = TailoringResult(resume_id="r1")
    result.bullet_changes = [
        BulletChange(
            bullet_id=bullet_id,
            original_text=ORIGINAL_TEXT,
            tailored_text=tailored_text,
            action="rewrite",
        ),
    ]
    return result


class TestFlagIfStillOverflowing:
    def test_appends_warning_when_text_does_not_fit(self):
        change = BulletChange(
            bullet_id="b1", original_text="x", tailored_text=ORIGINAL_TEXT,
            action="keep", reason="Rewrite too long, trim failed",
        )
        _flag_if_still_overflowing(change, _FakeMeasurer())
        assert "still may wrap to a second line" in change.reason

    def test_no_warning_when_text_fits(self):
        change = BulletChange(
            bullet_id="b1", original_text="x", tailored_text="short",
            action="keep", reason="Rewrite too long, trim failed",
        )
        _flag_if_still_overflowing(change, _FakeMeasurer())
        assert "still may wrap" not in change.reason

    def test_no_measurer_is_a_safe_noop(self):
        change = BulletChange(
            bullet_id="b1", original_text="x", tailored_text=ORIGINAL_TEXT,
            action="keep", reason="",
        )
        _flag_if_still_overflowing(change, None)  # must not raise
        assert change.reason == ""


class TestBatchTrimFinalRoundFallback:
    def test_partial_progress_keeps_shorter_validated_trim_not_longer_original(self):
        """The LLM makes real progress every round (each trim shorter than
        the last) but never quite gets under the fit threshold. On the
        final round, the shorter, already-validated partial trim must
        survive -- not a revert to the longer original."""
        bullet_id = "b1"
        ir, bank = _ir_and_bank(bullet_id)
        result = _result(bullet_id, ORIGINAL_TEXT)
        measurer = _FakeMeasurer()
        validator = ClaimValidator()

        # Each call returns progressively shorter text, still overflowing
        # (> FIT_THRESHOLD=100 chars) even on the final (3rd) round --
        # built by word-truncating ORIGINAL_TEXT so lengths are exact and
        # this can't silently drift into exercising the wrong branch.
        words = ORIGINAL_TEXT.split()
        progressive_trims = [
            " ".join(words[:16]),  # 157 chars, still overflowing
            " ".join(words[:14]),  # 138 chars, shorter, still overflowing
            " ".join(words[:12]),  # 127 chars, shortest, still over 100
        ]
        assert len(progressive_trims[0]) > len(progressive_trims[1]) > len(progressive_trims[2]) > 100
        call_count = {"n": 0}

        def fake_batch_trim(batch_items, **kwargs):
            i = call_count["n"]
            call_count["n"] += 1
            return {bullet_id: progressive_trims[min(i, len(progressive_trims) - 1)]}

        svc = TailoringService()
        with patch(
            "backend.tailoring.tailoring_engine.TailoringEngine.batch_trim_bullets",
            side_effect=fake_batch_trim,
        ):
            svc._batch_trim_overflows(result, measurer, validator, bank, ir)

        final_text = result.bullet_changes[0].tailored_text
        assert final_text == progressive_trims[-1]
        assert final_text != ORIGINAL_TEXT
        assert len(final_text) < len(ORIGINAL_TEXT)
        # Action stays "rewrite" -- this is a (partial) rewrite, not a
        # revert to the untouched original.
        assert result.bullet_changes[0].action == "rewrite"
        assert "still may wrap to a second line" in result.bullet_changes[0].reason

    def test_llm_returns_nothing_all_three_rounds_reverts_to_original(self):
        bullet_id = "b1"
        ir, bank = _ir_and_bank(bullet_id)
        result = _result(bullet_id, ORIGINAL_TEXT)
        measurer = _FakeMeasurer()
        validator = ClaimValidator()

        def fake_batch_trim(batch_items, **kwargs):
            return {bullet_id: None}

        svc = TailoringService()
        with patch(
            "backend.tailoring.tailoring_engine.TailoringEngine.batch_trim_bullets",
            side_effect=fake_batch_trim,
        ):
            svc._batch_trim_overflows(result, measurer, validator, bank, ir)

        change = result.bullet_changes[0]
        assert change.tailored_text == ORIGINAL_TEXT
        assert change.action == "keep"
        # Original itself doesn't fit either -- must be flagged, not silent.
        assert "still may wrap to a second line" in change.reason
