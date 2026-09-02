"""Tests for element_styles.format_date_range and its adoption across all
four renderers plus tailoring_engine.py's LLM-facing resume text.

Found by a renderer-consistency audit: the "build a date string from
start/end" logic was independently hand-written six times (docx/pdf/text/
html renderers + tailoring_engine.py) and had drifted -- project entries
only got a date at all in docx_renderer.py, education entries silently
dropped start_date in text_renderer.py and docx_renderer.py's fallback
path while pdf/html rendered a full range, and a currently-employed
experience entry (start_date set, no end_date) got no date at all in
tailoring_engine.py's LLM-facing text. Consolidated into one helper so this
can't drift again; text_renderer.py intentionally keeps a plain ASCII
hyphen separator (module docstring: "no unicode beyond basic bullet
points", for ATS/portal compatibility) while the others use an en dash --
that's a deliberate difference, not something to unify away.
"""

from backend.renderers.element_styles import format_date_range


class TestFormatDateRange:
    def test_both_dates_gives_a_range(self):
        assert format_date_range("Jan 2023", "Aug 2023") == "Jan 2023 – Aug 2023"

    def test_start_only_falls_back_to_start(self):
        assert format_date_range("Jan 2023", None) == "Jan 2023"

    def test_end_only_falls_back_to_end(self):
        assert format_date_range(None, "Present") == "Present"

    def test_neither_returns_empty_string(self):
        assert format_date_range(None, None) == ""

    def test_custom_separator(self):
        assert format_date_range("Jan 2023", "Aug 2023", sep="-") == "Jan 2023 - Aug 2023"


class TestCrossRendererConsistency:
    """Every renderer should agree on whether a date is shown at all, for
    every entry type, given the same start/end combination -- only the
    separator character should differ (en dash vs. text_renderer's ASCII
    hyphen)."""

    def _all_renders(self, content):
        from backend.models.resume_ir import ResumeIR
        from backend.renderers.docx_renderer import DocxRenderer
        from backend.renderers.html_renderer import HtmlRenderer
        from backend.renderers.pdf_renderer import PdfRenderer
        from backend.renderers.text_renderer import TextRenderer
        import io
        from docx import Document

        ir = ResumeIR(content=content)
        docx_bytes = DocxRenderer(ir).render()
        docx_text = "\n".join(p.text for p in Document(io.BytesIO(docx_bytes)).paragraphs)
        pdf_bytes = PdfRenderer(ir).render()
        txt_text = TextRenderer(ir).render()
        html_text = HtmlRenderer(ir).render()
        return {"docx": docx_text, "pdf_bytes": pdf_bytes, "txt": txt_text, "html": html_text}

    def test_project_start_only_shows_a_date_everywhere(self):
        from backend.models.resume_content import (
            ContactInfo, ProjectEntry, ResumeContent, ResumeSection, SectionType,
        )
        content = ResumeContent(
            contact=ContactInfo(name="Jane Doe"),
            sections=[ResumeSection(
                id="s1", type=SectionType.PROJECTS, title="Projects",
                project_entries=[ProjectEntry(id="p1", name="SmartNote", start_date="Jun 2023")],
            )],
        )
        out = self._all_renders(content)
        assert "Jun 2023" in out["docx"]
        assert "Jun 2023" in out["txt"]
        assert "Jun 2023" in out["html"]
        # PDF is binary; just confirm it renders without crashing.
        assert out["pdf_bytes"].startswith(b"%PDF")

    def test_education_start_only_shows_a_date_everywhere(self):
        from backend.models.resume_content import (
            ContactInfo, EducationEntry, ResumeContent, ResumeSection, SectionType,
        )
        content = ResumeContent(
            contact=ContactInfo(name="Jane Doe"),
            sections=[ResumeSection(
                id="s1", type=SectionType.EDUCATION, title="Education",
                education_entries=[EducationEntry(
                    id="e1", institution="State University", start_date="Aug 2020",
                )],
            )],
        )
        out = self._all_renders(content)
        assert "Aug 2020" in out["docx"]
        assert "Aug 2020" in out["txt"]
        assert "Aug 2020" in out["html"]


class TestTailoringEngineDateFallback:
    def test_currently_employed_entry_gets_a_date_in_llm_text(self):
        from backend.models.resume_content import (
            ContactInfo, ExperienceEntry, ResumeContent, ResumeSection, SectionType,
        )
        from backend.tailoring.tailoring_engine import _build_resume_text

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
        text = _build_resume_text(content, max_chars=200)
        assert "Jan 2023" in text
