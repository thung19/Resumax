"""Tests for TextRenderer.

Regression: _render_generic_entry() only emitted entry.title and
bullets -- entry.subtitle was never referenced anywhere in the file.
docx_importer.py populates subtitle from the right-aligned text
(typically a date/issuer) for any section that isn't Education/
Experience/Projects/Skills, e.g. Certifications/Awards/Publications --
so every plain-text/ATS export of such a section silently lost the
date/issuer for each entry.
"""

from backend.models.resume_content import (
    ContactInfo, GenericEntry, ResumeContent, ResumeSection, SectionType,
)
from backend.models.resume_ir import ResumeIR
from backend.renderers.text_renderer import TextRenderer


class TestGenericEntrySubtitle:
    def test_subtitle_included_in_output(self):
        content = ResumeContent(
            contact=ContactInfo(name="Jane Doe"),
            sections=[ResumeSection(
                id="s1", type=SectionType.CERTIFICATIONS, title="CERTIFICATIONS",
                generic_entries=[GenericEntry(
                    id="g1", title="AWS Certified Solutions Architect",
                    subtitle="Issued March 2024",
                )],
            )],
        )
        output = TextRenderer(ResumeIR(content=content)).render()
        assert "AWS Certified Solutions Architect" in output
        assert "Issued March 2024" in output

    def test_no_subtitle_does_not_add_blank_line(self):
        content = ResumeContent(
            contact=ContactInfo(name="Jane Doe"),
            sections=[ResumeSection(
                id="s1", type=SectionType.CERTIFICATIONS, title="CERTIFICATIONS",
                generic_entries=[GenericEntry(id="g1", title="AWS Certified")],
            )],
        )
        output = TextRenderer(ResumeIR(content=content)).render()
        lines = [l for l in output.split("\n") if l.strip()]
        assert "AWS Certified" in lines
