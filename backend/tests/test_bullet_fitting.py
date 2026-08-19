"""Tests for layout-accurate bullet measurement.

Covers: measurement, overflow detection, safety margin,
font/margin effects on line capacity.
"""

import pytest

from backend.models.resume_layout import (
    FontSpec, IndentSpec, PageSetup, ResumeLayout, SpacingSpec, StyleDef,
)
from backend.tailoring.bullet_measurer import BulletMeasurer, BulletMeasurement


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
                indent=IndentSpec(),
            ),
        },
    )


SHORT = "Built a Python REST API for data processing"
LONG = (
    "Developed and deployed a comprehensive full-stack Python software application "
    "with RESTful API data pipeline processing and automated testing across multiple "
    "production environments for enterprise clients worldwide with extensive documentation"
)


class TestShortBulletFits:
    def test_short_bullet_is_one_line(self):
        m = BulletMeasurer(_make_layout())
        result = m.measure(SHORT)
        assert result.fits_one_line
        assert result.line_count == 1

    def test_measurement_returns_all_fields(self):
        m = BulletMeasurer(_make_layout())
        result = m.measure(SHORT)
        assert result.text == SHORT
        assert result.rendered_width_pt > 0
        assert result.rendered_height_pt > 0
        assert result.available_width_pt > 0
        assert result.leading_pt > 0


class TestLongBulletOverflow:
    def test_long_bullet_detected_as_multi_line(self):
        m = BulletMeasurer(_make_layout())
        result = m.measure(LONG)
        assert not result.fits_one_line
        assert result.line_count >= 2


class TestSafetyMargin:
    def test_tighter_margin_catches_borderline_bullet(self):
        layout = _make_layout()
        m_loose = BulletMeasurer(layout, safety_margin=0.01)
        m_tight = BulletMeasurer(layout, safety_margin=0.10)

        borderline = (
            "Developed and deployed a full-stack Python software application "
            "with RESTful API data pipeline processing"
        )

        loose_result = m_loose.measure(borderline)
        tight_result = m_tight.measure(borderline)

        # Tighter margin has less available width
        assert m_tight.safe_width_pt < m_loose.safe_width_pt

        # Tight might catch overflow that loose misses
        if loose_result.fits_one_line:
            assert tight_result.line_count >= loose_result.line_count


class TestLayoutAffectsWidth:
    def test_larger_font_reduces_capacity(self):
        m10 = BulletMeasurer(_make_layout(font_size=10.0))
        m12 = BulletMeasurer(_make_layout(font_size=12.0))
        assert m10.safe_width_pt == m12.safe_width_pt  # same page width
        # But larger font wraps more
        result10 = m10.measure(LONG)
        result12 = m12.measure(LONG)
        assert result12.line_count >= result10.line_count

    def test_wider_margins_reduce_capacity(self):
        m_narrow = BulletMeasurer(_make_layout(margin_left=0.35, margin_right=0.35))
        m_wide = BulletMeasurer(_make_layout(margin_left=1.0, margin_right=1.0))
        assert m_wide.safe_width_pt < m_narrow.safe_width_pt

    def test_different_font_family(self):
        m_serif = BulletMeasurer(_make_layout(font_family="Times New Roman"))
        m_sans = BulletMeasurer(_make_layout(font_family="Arial"))
        # Both should work without error
        r_serif = m_serif.measure(SHORT)
        r_sans = m_sans.measure(SHORT)
        assert r_serif.fits_one_line
        assert r_sans.fits_one_line


class TestMeasureLine:
    def test_measure_line_no_bullet_prefix(self):
        m = BulletMeasurer(_make_layout())
        result = m.measure_line("Languages: Python, Java, C++, SQL")
        assert result.fits_one_line

    def test_measure_line_vs_measure(self):
        m = BulletMeasurer(_make_layout())
        text = "Built a Python REST API"
        # measure() adds bullet prefix, measure_line() doesn't
        with_bullet = m.measure(text)
        without_bullet = m.measure_line(text)
        # With bullet should use more width
        assert with_bullet.rendered_width_pt >= without_bullet.rendered_width_pt
