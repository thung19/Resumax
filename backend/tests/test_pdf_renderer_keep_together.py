"""Tests for PdfRenderer's entry-header page-break protection.

Regression: KeepTogether was imported but never actually used anywhere
in the file -- flowables for SECTION_HEADING/ENTRY_HEADER/
ENTRY_SUBHEADER/BULLET were appended to the story sequentially with no
grouping construct, so nothing prevented ReportLab from placing a job's
company/date header at the very bottom of a page while its role/
location line and bullets flowed to the next page. Groups the header,
an immediately-following subheader, and the first bullet into one
KeepTogether block -- just those first few flowables, not the whole
entry, so a long entry with many bullets still flows across a page
break normally once its header is anchored.
"""

from reportlab.platypus import KeepTogether, Paragraph

from backend.models.resume_content import ContactInfo, ResumeContent
from backend.models.resume_ir import ResumeIR
from backend.models.resume_layout import (
    ElementType, LayoutElement, ParagraphFormat, ResumeLayout, RunFormat,
)
from backend.renderers.pdf_renderer import PdfRenderer


def _ir_with_elements(elements: list[LayoutElement]) -> ResumeIR:
    content = ResumeContent(contact=ContactInfo(name="Jane Doe"))
    return ResumeIR(content=content, layout=ResumeLayout(elements=elements))


def _header(text: str = "Acme Corp") -> LayoutElement:
    return LayoutElement(
        element_type=ElementType.ENTRY_HEADER,
        paragraph_format=ParagraphFormat(runs=[RunFormat(text=text, bold=True)]),
    )


def _subheader(text: str = "Engineer") -> LayoutElement:
    return LayoutElement(
        element_type=ElementType.ENTRY_SUBHEADER,
        paragraph_format=ParagraphFormat(runs=[RunFormat(text=text, italic=True)]),
    )


def _bullet(text: str = "Did a thing") -> LayoutElement:
    return LayoutElement(
        element_type=ElementType.BULLET,
        paragraph_format=ParagraphFormat(runs=[RunFormat(text=f"• {text}")]),
    )


class TestEntryHeaderKeptTogether:
    def test_header_subheader_and_first_bullet_grouped(self):
        ir = _ir_with_elements([_header(), _subheader(), _bullet("first"), _bullet("second")])
        story = PdfRenderer(ir)._build_story_from_elements()

        assert isinstance(story[0], KeepTogether)
        assert len(story[0]._content) == 3
        # The second bullet stays outside the group, free to flow.
        assert isinstance(story[1], Paragraph)

    def test_header_with_subheader_but_no_bullets(self):
        ir = _ir_with_elements([_header(), _subheader()])
        story = PdfRenderer(ir)._build_story_from_elements()
        assert isinstance(story[0], KeepTogether)
        assert len(story[0]._content) == 2

    def test_header_with_no_subheader_or_bullets_not_wrapped(self):
        ir = _ir_with_elements([_header(), LayoutElement(element_type=ElementType.SPACER)])
        story = PdfRenderer(ir)._build_story_from_elements()
        # A lone flowable isn't worth a KeepTogether wrapper.
        assert isinstance(story[0], Paragraph)

    def test_multiple_entries_each_get_their_own_group(self):
        ir = _ir_with_elements([
            _header("Acme Corp"), _subheader("Engineer"), _bullet("a"),
            _header("Globex Inc"), _subheader("Analyst"), _bullet("b"),
        ])
        story = PdfRenderer(ir)._build_story_from_elements()
        groups = [s for s in story if isinstance(s, KeepTogether)]
        assert len(groups) == 2

    def test_full_render_does_not_crash(self):
        ir = _ir_with_elements([_header(), _subheader(), _bullet(), _bullet("second")])
        pdf_bytes = PdfRenderer(ir).render()
        assert pdf_bytes.startswith(b"%PDF")
