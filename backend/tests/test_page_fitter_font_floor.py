"""Tests for PageFitter's font-size-reduction floor (Step 5 of fit()).

Regression: the loop guard checked current_font BEFORE decrementing,
then applied the decremented value without re-checking it against the
floor. When the starting size isn't an exact 0.5pt increment above
min_font_pt (e.g. 10.3pt -- plausible for a resume with a custom/odd
point size), the last iteration applied a size below the documented
minimum-readable floor (confirmed: a 10.3pt start bottomed out at 8.3pt
against an 8.5pt floor).
"""

from unittest.mock import patch

from backend.models.resume_content import (
    Bullet, ContactInfo, ExperienceEntry, ResumeContent, ResumeSection,
    SectionType,
)
from backend.models.resume_ir import ResumeIR
from backend.models.resume_layout import FontSpec, ResumeLayout, StyleDef
from backend.tailoring.page_fitter import PageFitter


def _ir_with_font_size(size_pt: float) -> ResumeIR:
    content = ResumeContent(
        contact=ContactInfo(name="Test User", email="test@test.com"),
        sections=[
            ResumeSection(
                id="s1", type=SectionType.EXPERIENCE, title="Experience",
                experience_entries=[
                    ExperienceEntry(
                        id="e1", company="Acme", role="Engineer",
                        start_date="2020", end_date="2022",
                        bullets=[Bullet(id="b0", text="Did a thing.")],
                    ),
                ],
            ),
        ],
    )
    layout = ResumeLayout(styles={"bullet": StyleDef(font=FontSpec(size_pt=size_pt))})
    return ResumeIR(content=content, layout=layout)


class TestFontFloorNeverUndershot:
    def test_non_grid_aligned_start_never_goes_below_floor(self):
        # 10.3pt is not an exact 0.5pt increment above 8.5pt -- exactly
        # the case that used to bottom out at 8.3pt.
        ir = _ir_with_font_size(10.3)
        fitter = PageFitter(
            ir, target_pages=1, min_font_pt=8.5,
            allow_removal=False, allow_spacing_compression=False,
        )
        # Force every attempt to still overflow, so the loop runs all
        # the way down to the floor.
        with patch.object(PageFitter, "_check_pages", return_value=2):
            fitter.fit()

        applied_sizes = [
            float(a.split()[-1].rstrip("pt"))
            for a in fitter._report.actions_taken
            if a.startswith("Reduced body font to")
        ]
        assert applied_sizes, "expected at least one font reduction"
        assert min(applied_sizes) >= 8.5
        assert applied_sizes[-1] == 8.5

    def test_grid_aligned_start_unaffected(self):
        # Regression guard: the fix must not change behavior for the
        # already-correct case (a clean 0.5pt-grid starting size).
        ir = _ir_with_font_size(10.5)
        fitter = PageFitter(
            ir, target_pages=1, min_font_pt=8.5,
            allow_removal=False, allow_spacing_compression=False,
        )
        with patch.object(PageFitter, "_check_pages", return_value=2):
            fitter.fit()

        applied_sizes = [
            float(a.split()[-1].rstrip("pt"))
            for a in fitter._report.actions_taken
            if a.startswith("Reduced body font to")
        ]
        assert applied_sizes == [10.0, 9.5, 9.0, 8.5]
