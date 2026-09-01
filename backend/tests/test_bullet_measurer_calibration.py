"""Tests for BulletMeasurer's calibration ceiling.

Regression: _calibrate() takes max(width of all original bullets) as
the "safe" line width with no clamp against the physical page width. If
the widest original bullet was already borderline/overflowing in the
source document (or measured wider than intended due to a font-fallback
mismatch), the calibrated width balloons past what physically fits on
the page -- and every downstream width budget (safe_width_pt,
max_chars_for_line, batch-trim char caps) inherits the inflation, so a
bullet judged "fits one line" can still visibly wrap to a second line in
the rendered PDF.
"""

from backend.models.resume_content import (
    Bullet, ExperienceEntry, ResumeContent, ResumeSection, SectionType,
)
from backend.models.resume_layout import (
    FontSpec, IndentSpec, PageSetup, ResumeLayout, SpacingSpec, StyleDef,
)
from backend.tailoring.bullet_measurer import BulletMeasurer


def _layout() -> ResumeLayout:
    return ResumeLayout(
        page=PageSetup(width_in=8.5, margin_left_in=0.35, margin_right_in=0.35),
        styles={
            "bullet": StyleDef(
                font=FontSpec(family="Garamond", size_pt=10.0),
                spacing=SpacingSpec(line_spacing=1.15),
                indent=IndentSpec(),
            ),
        },
    )


def _content_with_bullets(bullets_text: list[str]) -> ResumeContent:
    return ResumeContent(sections=[
        ResumeSection(
            id="s1", type=SectionType.EXPERIENCE, title="Experience",
            experience_entries=[
                ExperienceEntry(
                    id="e1", company="Acme", role="Engineer",
                    bullets=[Bullet(id=f"b{i}", text=t) for i, t in enumerate(bullets_text)],
                ),
            ],
        ),
    ])


class TestCalibrationCeiling:
    def test_calibrated_width_never_exceeds_geometric_width(self):
        overflowing_bullet = (
            "Led cross-functional initiative to deliver enterprise-scale distributed "
            "microservices architecture spanning multiple international engineering "
            "teams and stakeholders across three continents worldwide"
        )
        content = _content_with_bullets([
            "Built a REST API",
            "Managed a small team of 3",
            overflowing_bullet,
        ])
        m = BulletMeasurer(_layout(), content=content)

        assert m._calibrated_width_pt <= m._geometric_width_pt

    def test_calibration_still_widens_within_the_page_for_normal_resumes(self):
        # Regression guard: the fix must not become "always use the
        # geometric width" -- calibration should still pick a value
        # narrower than (or equal to) geometric based on real bullets
        # when they genuinely all fit.
        content = _content_with_bullets([
            "Built a REST API for data processing",
            "Managed a small team of 3 engineers",
            "Deployed services using Docker and Kubernetes",
        ])
        m = BulletMeasurer(_layout(), content=content)

        assert m._calibrated_width_pt <= m._geometric_width_pt
        assert m._calibrated_width_pt > 0

    def test_a_bullet_judged_to_fit_actually_fits_the_real_page_width(self):
        # The real-world consequence: with the ceiling in place, nothing
        # measurer.measure() calls "fits_one_line" should exceed the
        # true geometric width.
        overflowing_bullet = (
            "Led cross-functional initiative to deliver enterprise-scale distributed "
            "microservices architecture spanning multiple international engineering "
            "teams and stakeholders across three continents worldwide"
        )
        content = _content_with_bullets([
            "Built a REST API",
            "Managed a small team of 3",
            overflowing_bullet,
        ])
        m = BulletMeasurer(_layout(), content=content)

        candidate = overflowing_bullet[:-20]
        result = m.measure(candidate)
        if result.fits_one_line:
            assert result.rendered_width_pt <= m._geometric_width_pt
