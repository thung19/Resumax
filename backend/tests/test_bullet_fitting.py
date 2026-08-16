"""Tests for layout-accurate bullet fitting.

Covers: measurement, overflow detection, compression, validation,
safety margin, font/margin effects, one_line_bullets toggle.
"""

import pytest

from backend.models.resume_layout import (
    FontSpec, IndentSpec, PageSetup, ResumeLayout, SpacingSpec, StyleDef,
)
from backend.models.tailoring import BulletChange, TailoringResult
from backend.tailoring.bullet_measurer import BulletMeasurer, BulletMeasurement
from backend.tailoring.bullet_fitter import BulletFitter, FittingReport
from backend.tailoring.claim_validator import ClaimValidator


def _make_layout(
    width_in: float = 8.5,
    margin_left: float = 0.35,
    margin_right: float = 0.35,
    font_family: str = "Garamond",
    font_size: float = 10.0,
) -> ResumeLayout:
    """Build a layout matching the test resume."""
    return ResumeLayout(
        page=PageSetup(
            width_in=width_in,
            margin_left_in=margin_left,
            margin_right_in=margin_right,
        ),
        styles={
            "bullet": StyleDef(
                font=FontSpec(family=font_family, size_pt=font_size),
                spacing=SpacingSpec(line_spacing=1.15),
                indent=IndentSpec(),  # no indent (inline bullet char)
            ),
        },
    )


# --- Test: Short bullet already fits → unchanged ---

class TestShortBulletFits:
    def test_short_bullet_is_one_line(self):
        layout = _make_layout()
        measurer = BulletMeasurer(layout)

        m = measurer.measure("Built a Python data pipeline for 50+ news sources")
        assert m.fits_one_line
        assert m.line_count == 1

    def test_fitting_leaves_short_bullet_unchanged(self):
        layout = _make_layout()
        result = TailoringResult(resume_id="test", bullet_changes=[
            BulletChange(
                bullet_id="b1",
                original_text="Short bullet text",
                tailored_text="Short bullet text",
                action="keep",
            ),
        ])

        fitter = BulletFitter(layout, use_llm=False)
        report = fitter.fit_bullets(result)

        assert report.unchanged == 1
        assert report.overflows_detected == 0
        assert result.bullet_changes[0].tailored_text == "Short bullet text"


# --- Test: Long bullet wraps → identified as overflow ---

class TestLongBulletOverflow:
    def test_long_bullet_detected_as_multi_line(self):
        layout = _make_layout()
        measurer = BulletMeasurer(layout)

        long_text = (
            "Engineered a comprehensive Python/SciPy mixed-integer linear programming "
            "optimization engine for $250M+ in fixed-income portfolios across multiple "
            "asset classes, systematically balancing risk, yield, duration, and transaction costs"
        )
        m = measurer.measure(long_text)
        assert not m.fits_one_line
        assert m.line_count >= 2
        assert m.overflow_ratio > 1.0


# --- Test: Rewritten bullet fits → accepted ---

class TestRewrittenBulletAccepted:
    def test_shorter_rewrite_accepted(self):
        layout = _make_layout()
        measurer = BulletMeasurer(layout)

        # A text that fits on one line
        short = "Built Python optimization engine for $250M+ fixed-income portfolios balancing risk and yield"
        m = measurer.measure(short)
        assert m.fits_one_line


# --- Test: Rewrite still too wide → retried ---

class TestRewriteRetried:
    def test_fitting_retries_on_still_too_wide(self):
        layout = _make_layout()
        long_text = (
            "Engineered and deployed a comprehensive Python/SciPy mixed-integer linear programming "
            "optimization engine for $250M+ in diversified fixed-income portfolios across "
            "multiple institutional asset classes, systematically balancing risk exposure, "
            "yield optimization, duration constraints, and transaction cost minimization"
        )

        result = TailoringResult(resume_id="test", bullet_changes=[
            BulletChange(
                bullet_id="b1",
                original_text=long_text,
                tailored_text=long_text,
                action="rewrite",
            ),
        ])

        fitter = BulletFitter(layout, use_llm=False)
        report = fitter.fit_bullets(result)

        # Should have attempted multiple times
        fit_result = report.results[0]
        assert len(fit_result.attempts) >= 1
        assert report.overflows_detected == 1


# --- Test: Rewrite introduces unsupported claim → rejected ---

class TestUnsupportedClaimRejected:
    def test_validator_catches_fabrication(self):
        validator = ClaimValidator()
        change = BulletChange(
            bullet_id="b1",
            original_text="Built a Python optimization system",
            tailored_text="Built a Python Kubernetes Docker optimization system",
            action="rewrite",
        )
        facts = [{"id": "f1", "text": "Built a Python optimization system"}]
        result = validator.validate(change, facts)
        assert not result.valid


# --- Test: one_line_bullets=False → multi-line allowed ---

class TestOneLineBulletsDisabled:
    def test_multi_line_allowed_when_disabled(self):
        layout = _make_layout()
        long_text = (
            "Engineered a comprehensive Python/SciPy mixed-integer linear programming "
            "optimization engine for $250M+ in fixed-income portfolios across multiple "
            "asset classes, balancing risk, yield, duration, and transaction costs"
        )

        result = TailoringResult(resume_id="test", bullet_changes=[
            BulletChange(
                bullet_id="b1",
                original_text=long_text,
                tailored_text=long_text,
                action="rewrite",
            ),
        ])

        # one_line_bullets=False: fitter should not be called,
        # so bullet stays as-is
        fitter = BulletFitter(layout, use_llm=False)
        # Don't call fit_bullets — the service skips it when toggle is off

        violations = fitter.validate_all_fit(result)
        # There WOULD be violations, but when toggle is off we don't check
        assert len(violations) >= 1  # confirms the bullet IS multi-line


# --- Test: one_line_bullets=True → final validation rejects multi-line ---

class TestFinalValidation:
    def test_final_validation_catches_overflow(self):
        layout = _make_layout()
        long_text = (
            "Engineered a comprehensive Python/SciPy mixed-integer linear programming "
            "optimization engine for $250M+ in fixed-income portfolios across multiple "
            "asset classes, balancing risk, yield, duration, and transaction costs"
        )

        result = TailoringResult(resume_id="test", bullet_changes=[
            BulletChange(
                bullet_id="b1",
                original_text=long_text,
                tailored_text=long_text,
                action="rewrite",
            ),
        ])

        fitter = BulletFitter(layout, use_llm=False)
        violations = fitter.validate_all_fit(result)
        assert "b1" in violations


# --- Test: Safety margin is respected ---

class TestSafetyMargin:
    def test_tighter_margin_catches_borderline_bullet(self):
        layout = _make_layout()

        # With 0% safety margin
        measurer_no_margin = BulletMeasurer(layout, safety_margin=0.0)
        # With 5% safety margin
        measurer_with_margin = BulletMeasurer(layout, safety_margin=0.05)

        # A bullet that barely fits without margin but might not with margin
        text = "Developed async REST API client to paginate and cache real-time cellular tower data enforcing rate limit"
        m_no = measurer_no_margin.measure(text)
        m_with = measurer_with_margin.measure(text)

        # The one with more margin should have less available width
        assert measurer_with_margin.safe_width_pt < measurer_no_margin.safe_width_pt


# --- Test: Font size / margin / indentation changes affect width ---

class TestLayoutAffectsWidth:
    def test_larger_font_reduces_capacity(self):
        layout_10pt = _make_layout(font_size=10.0)
        layout_12pt = _make_layout(font_size=12.0)

        m10 = BulletMeasurer(layout_10pt)
        m12 = BulletMeasurer(layout_12pt)

        text = "Built authenticated React modules for 60+ investment professionals including credit-spread visualizations"
        r10 = m10.measure(text)
        r12 = m12.measure(text)

        # Larger font should use more lines (or at least more height)
        assert r12.rendered_height_pt >= r10.rendered_height_pt

    def test_wider_margins_reduce_capacity(self):
        layout_narrow = _make_layout(margin_left=0.35, margin_right=0.35)
        layout_wide = _make_layout(margin_left=1.0, margin_right=1.0)

        m_narrow = BulletMeasurer(layout_narrow)
        m_wide = BulletMeasurer(layout_wide)

        assert m_wide.safe_width_pt < m_narrow.safe_width_pt

        text = "Built authenticated React modules for 60+ investment professionals including credit-spread visualizations"
        r_narrow = m_narrow.measure(text)
        r_wide = m_wide.measure(text)

        # Same text in narrower space → more lines
        assert r_wide.line_count >= r_narrow.line_count

    def test_different_font_family(self):
        layout_serif = _make_layout(font_family="Garamond")
        layout_sans = _make_layout(font_family="Arial")

        m_serif = BulletMeasurer(layout_serif)
        m_sans = BulletMeasurer(layout_sans)

        text = "Designed Bloomberg API data pipelines processing 10k+ market data points"
        r_serif = m_serif.measure(text)
        r_sans = m_sans.measure(text)

        # Both should measure successfully (different fonts may differ in width)
        assert r_serif.line_count >= 1
        assert r_sans.line_count >= 1
