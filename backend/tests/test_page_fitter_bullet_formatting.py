"""Tests for PageFitter._update_bullet preserving mid-bullet run
formatting when a bullet is shortened.

Regression: when the deterministic page-fitting step shortened an
overflowing bullet, it replaced the entire runs list of the matching
BULLET layout element with a SINGLE new RunFormat, copying formatting
only from the first run. Any bold/italic/color applied to a LATER run
(e.g. a bolded metric mid-bullet) was silently discarded, even though
the surrounding text was otherwise unchanged. Since _shorten_text()
only ever truncates from the end at a word boundary, the shortened text
is always a clean prefix of the original -- so per-run formatting can
be preserved exactly by truncating the run sequence in place instead.
"""

from backend.models.resume_content import (
    Bullet, ExperienceEntry, ResumeContent, ResumeSection, SectionType,
)
from backend.models.resume_ir import ResumeIR
from backend.models.resume_layout import (
    ElementType, LayoutElement, ParagraphFormat, ResumeLayout, RunFormat,
)
from backend.tailoring.page_fitter import BulletInfo, PageFitter


def _fitter_with_bullet(original_text: str, runs: list[RunFormat]) -> tuple[PageFitter, BulletInfo]:
    content = ResumeContent(sections=[
        ResumeSection(
            id="s1", type=SectionType.EXPERIENCE, title="Experience",
            experience_entries=[
                ExperienceEntry(
                    id="e1", company="Acme", role="Sales",
                    bullets=[Bullet(id="b0", text=original_text)],
                ),
            ],
        ),
    ])
    layout = ResumeLayout(elements=[
        LayoutElement(element_type=ElementType.BULLET, paragraph_format=ParagraphFormat(runs=runs)),
    ])
    ir = ResumeIR(content=content, layout=layout)

    fitter = PageFitter.__new__(PageFitter)
    fitter._ir = ir
    b_info = BulletInfo(
        section_idx=0, entry_idx=0, bullet_idx=0, bullet_id="b0",
        text=original_text, char_count=len(original_text), relevance=1.0,
        entry_type="experience",
    )
    return fitter, b_info


class TestMidBulletFormattingPreservedOnShorten:
    def test_bold_metric_survives_a_real_shortening(self):
        original = (
            "Increased revenue by 45% through targeted marketing "
            "campaigns across three regions"
        )
        runs = [
            RunFormat(text="• Increased revenue by ", bold=False),
            RunFormat(text="45%", bold=True),
            RunFormat(text=" through targeted marketing campaigns across three regions", bold=False),
        ]
        fitter, b_info = _fitter_with_bullet(original, runs)

        shortened = fitter._shorten_text(original, target_chars=40)
        assert shortened == original[:len(shortened)]  # genuinely a prefix

        fitter._update_bullet(b_info, shortened)

        result_runs = fitter._ir.layout.elements[0].paragraph_format.runs
        bold_runs = [r for r in result_runs if r.bold]
        assert bold_runs, "the bolded '45%' run should have survived"
        assert bold_runs[0].text == "45%"
        full_text = "".join(r.text for r in result_runs)
        assert full_text == "• " + shortened

    def test_shortened_past_the_bold_run_drops_it_cleanly(self):
        # If the cut point falls BEFORE the bold run even starts, it's
        # correctly absent -- not silently corrupted, just not included.
        original = "Led the initiative and delivered 45% growth this year"
        runs = [
            RunFormat(text="• Led the initiative and delivered ", bold=False),
            RunFormat(text="45%", bold=True),
            RunFormat(text=" growth this year", bold=False),
        ]
        fitter, b_info = _fitter_with_bullet(original, runs)
        shortened = "Led the initiative"  # a clean prefix, ends before "45%"
        fitter._update_bullet(b_info, shortened)

        result_runs = fitter._ir.layout.elements[0].paragraph_format.runs
        assert not any(r.bold for r in result_runs)
        assert "".join(r.text for r in result_runs) == "• " + shortened

    def test_no_formatting_change_bullet_still_shortens_correctly(self):
        # Regression guard: a plain (single-run, no mid-bullet
        # formatting) bullet must keep working exactly as before.
        original = "Managed a team of engineers across three offices nationwide"
        runs = [RunFormat(text=f"• {original}", bold=False)]
        fitter, b_info = _fitter_with_bullet(original, runs)
        shortened = fitter._shorten_text(original, target_chars=30)
        fitter._update_bullet(b_info, shortened)

        result_runs = fitter._ir.layout.elements[0].paragraph_format.runs
        assert "".join(r.text for r in result_runs) == "• " + shortened

    def test_non_prefix_text_falls_back_safely_without_garbling(self):
        # _update_bullet is a general method; if it's ever called with
        # text that ISN'T a clean prefix of the original (e.g. the
        # whitespace-restore path growing text back, or in principle any
        # other caller), the fix must fall back rather than produce
        # misaligned/garbled text.
        original = "Increased revenue by 45% through targeted campaigns"
        runs = [
            RunFormat(text="• Increased revenue by ", bold=False),
            RunFormat(text="45%", bold=True),
            RunFormat(text=" through targeted campaigns", bold=False),
        ]
        fitter, b_info = _fitter_with_bullet(original, runs)
        not_a_prefix = "Increased revenue by 45% via campaigns"  # rewritten, not truncated

        fitter._update_bullet(b_info, not_a_prefix)

        result_runs = fitter._ir.layout.elements[0].paragraph_format.runs
        full_text = "".join(r.text for r in result_runs)
        assert full_text == "• " + not_a_prefix  # text is correct, not garbled
