"""Tests for PdfRenderer's content-fallback rendering paths.

Regression: _render_generic() (the content-fallback renderer for
GenericEntry, used for e.g. Certifications/Awards/Publications sections)
only ever emitted entry.title and bullets -- entry.subtitle (typically a
date or issuer, populated by docx_importer.py from the right-aligned
text on such a line) was silently dropped, the same bug independently
present in text_renderer.py.
"""

from backend.models.resume_content import ContactInfo, GenericEntry, ResumeContent, ResumeSection, SectionType
from backend.models.resume_ir import ResumeIR
from backend.renderers.pdf_renderer import PdfRenderer


def _ir_with_generic_entry(title: str, subtitle: str | None) -> ResumeIR:
    content = ResumeContent(
        contact=ContactInfo(name="Jane Doe"),
        sections=[ResumeSection(
            id="s1", type=SectionType.CERTIFICATIONS, title="CERTIFICATIONS",
            generic_entries=[GenericEntry(id="g1", title=title, subtitle=subtitle)],
        )],
    )
    return ResumeIR(content=content)


class TestGenericEntrySubtitle:
    def test_subtitle_included_as_a_paragraph(self):
        ir = _ir_with_generic_entry("AWS Certified Solutions Architect", "Issued March 2024")
        renderer = PdfRenderer(ir)
        entry = ir.content.sections[0].generic_entries[0]

        items = renderer._render_generic(entry)
        texts = [getattr(i, "text", None) for i in items]

        assert "AWS Certified Solutions Architect" in texts
        assert "Issued March 2024" in texts

    def test_no_subtitle_produces_no_extra_paragraph(self):
        ir = _ir_with_generic_entry("AWS Certified", None)
        renderer = PdfRenderer(ir)
        entry = ir.content.sections[0].generic_entries[0]

        items = renderer._render_generic(entry)
        texts = [getattr(i, "text", None) for i in items]

        assert texts == ["AWS Certified"]

    def test_full_render_does_not_crash_with_subtitle_present(self):
        ir = _ir_with_generic_entry("AWS Certified Solutions Architect", "Issued March 2024")
        pdf_bytes = PdfRenderer(ir).render()
        assert pdf_bytes.startswith(b"%PDF")
