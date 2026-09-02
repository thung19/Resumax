"""Tests for DocxRenderer._render_from_content, the fallback renderer
used when no captured layout element sequence is available (currently
reachable via docx_importer.py's "no w:body" malformed-upload path).

Found by the same renderer audit that fixed the equivalent
subtitle-dropping bug in text_renderer.py/pdf_renderer.py. Three
separate content-loss bugs in this one fallback path:

1. Currently-employed entries (start_date set, no end_date) rendered
   with NO date at all -- the date was only built `if start and end`,
   with no fallback to a single date, unlike pdf_renderer.py and
   text_renderer.py which both already handled this correctly.
2. GenericEntry/raw_lines content (Certifications/Awards/Publications/
   Custom sections) was entirely dropped -- the if/elif chain had no
   `else` and never read section.raw_lines at all, so such a section
   rendered as just its bare heading.
3. Project dates were never rendered at all -- the PROJECTS branch
   never read start_date/end_date, unlike the other two renderers.
"""

from docx import Document

from backend.models.resume_content import (
    ContactInfo, ExperienceEntry, GenericEntry, ProjectEntry, ResumeContent,
    ResumeSection, SectionType,
)
from backend.models.resume_ir import ResumeIR
from backend.renderers.docx_renderer import DocxRenderer


def _paragraph_texts(docx_bytes: bytes) -> list[str]:
    import io
    doc = Document(io.BytesIO(docx_bytes))
    return [p.text for p in doc.paragraphs if p.text.strip()]


def _render(content: ResumeContent) -> list[str]:
    return _paragraph_texts(DocxRenderer(ResumeIR(content=content)).render())


class TestCurrentlyEmployedDateFallback:
    def test_start_date_only_still_renders_a_date(self):
        content = ResumeContent(
            contact=ContactInfo(name="Jane Doe"),
            sections=[ResumeSection(
                id="s1", type=SectionType.EXPERIENCE, title="Experience",
                experience_entries=[ExperienceEntry(
                    id="e1", company="Acme Corp", role="Senior Engineer",
                    start_date="Jan 2023", end_date=None,
                )],
            )],
        )
        texts = " ".join(_render(content))
        assert "Jan 2023" in texts

    def test_end_date_only_still_renders_a_date(self):
        content = ResumeContent(
            contact=ContactInfo(name="Jane Doe"),
            sections=[ResumeSection(
                id="s1", type=SectionType.EXPERIENCE, title="Experience",
                experience_entries=[ExperienceEntry(
                    id="e1", company="Acme Corp", role="Senior Engineer",
                    start_date=None, end_date="Present",
                )],
            )],
        )
        texts = " ".join(_render(content))
        assert "Present" in texts

    def test_both_dates_still_render_a_range(self):
        content = ResumeContent(
            contact=ContactInfo(name="Jane Doe"),
            sections=[ResumeSection(
                id="s1", type=SectionType.EXPERIENCE, title="Experience",
                experience_entries=[ExperienceEntry(
                    id="e1", company="Acme Corp", role="Senior Engineer",
                    start_date="Jan 2023", end_date="Aug 2023",
                )],
            )],
        )
        texts = " ".join(_render(content))
        assert "Jan 2023" in texts and "Aug 2023" in texts


class TestGenericEntryAndRawLinesNotDropped:
    def test_generic_entry_title_subtitle_and_bullets_rendered(self):
        from backend.models.resume_content import Bullet
        content = ResumeContent(
            contact=ContactInfo(name="Jane Doe"),
            sections=[ResumeSection(
                id="s1", type=SectionType.CERTIFICATIONS, title="Certifications",
                generic_entries=[GenericEntry(
                    id="g1", title="AWS Certified Solutions Architect",
                    subtitle="Issued March 2024",
                    bullets=[Bullet(id="b1", text="Renewed annually")],
                )],
            )],
        )
        texts = _render(content)
        assert "AWS Certified Solutions Architect" in texts
        assert "Issued March 2024" in texts
        assert any("Renewed annually" in t for t in texts)

    def test_raw_lines_rendered(self):
        content = ResumeContent(
            contact=ContactInfo(name="Jane Doe"),
            sections=[ResumeSection(
                id="s1", type=SectionType.CERTIFICATIONS, title="Certifications",
                raw_lines=["Some unstructured certification line"],
            )],
        )
        texts = _render(content)
        assert "Some unstructured certification line" in texts

    def test_section_no_longer_reduces_to_bare_heading(self):
        content = ResumeContent(
            contact=ContactInfo(name="Jane Doe"),
            sections=[ResumeSection(
                id="s1", type=SectionType.CERTIFICATIONS, title="Certifications",
                generic_entries=[GenericEntry(id="g1", title="AWS Certified")],
                raw_lines=["A raw line"],
            )],
        )
        texts = _render(content)
        # Previously this whole section collapsed to just the heading.
        assert len(texts) > 2


class TestProjectDatesRendered:
    def test_project_date_range_included(self):
        content = ResumeContent(
            contact=ContactInfo(name="Jane Doe"),
            sections=[ResumeSection(
                id="s1", type=SectionType.PROJECTS, title="Projects",
                project_entries=[ProjectEntry(
                    id="p1", name="SmartNote", start_date="Jun 2023", end_date="Aug 2023",
                )],
            )],
        )
        texts = " ".join(_render(content))
        assert "Jun 2023" in texts and "Aug 2023" in texts

    def test_project_without_dates_still_renders_name(self):
        content = ResumeContent(
            contact=ContactInfo(name="Jane Doe"),
            sections=[ResumeSection(
                id="s1", type=SectionType.PROJECTS, title="Projects",
                project_entries=[ProjectEntry(id="p1", name="SmartNote")],
            )],
        )
        texts = _render(content)
        assert any("SmartNote" in t for t in texts)
