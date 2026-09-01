"""Tests for DocxImporter._classify_paragraph_role.

Regression: a bold entry header whose company/project name contains a
colon (e.g. "Acme Inc: A Case Study", "Client: Big Corp") used to match
the skills-row "Label: value, value" heuristic before the entry_header
check ever ran, since the colon check had no way to tell the two apart.
Confirmed real-world consequence: the entry's actual company name and
tab-separated date were lost into an unstructured line, and its bullets
got silently reattached under a fabricated header. A real skills row
never has a tab-separated date on the same line, so has_tabs is used as
the tie-breaker.
"""

from backend.importers.docx_importer import DocxImporter, ParagraphFormatting


def _importer() -> DocxImporter:
    return DocxImporter.__new__(DocxImporter)


def _para(text: str, bold: bool, tab_count: int = 0, font_size_pt: float = 11) -> ParagraphFormatting:
    p = ParagraphFormatting()
    p.text = text
    p.visible_text = text
    p.bold = bold
    p.italic = False
    p.alignment = "left"
    p.font_size_pt = font_size_pt
    p.runs = [{"tab_count": tab_count, "text": "x"}] if tab_count else []
    return p


class TestColonVsEntryHeader:
    def test_bold_header_with_colon_and_tab_is_entry_header_not_skills_row(self):
        p = _para("Acme Inc: A Case Study\tSep 2020 - Present", bold=True, tab_count=1)
        assert _importer()._classify_paragraph_role(p) == "entry_header"

    def test_client_colon_project_with_tab_is_entry_header(self):
        p = _para("Client: Big Corp\tJan 2019 - Mar 2019", bold=True, tab_count=1)
        assert _importer()._classify_paragraph_role(p) == "entry_header"

    def test_genuine_bold_skills_row_without_tabs_still_classified_correctly(self):
        p = _para("Languages: Python, Java, C++", bold=True, tab_count=0)
        assert _importer()._classify_paragraph_role(p) == "skills_row"

    def test_genuine_non_bold_skills_row_without_tabs_still_classified_correctly(self):
        p = _para("Tools: Git, Docker, Kubernetes", bold=False, tab_count=0)
        assert _importer()._classify_paragraph_role(p) == "skills_row"

    def test_plain_bold_header_without_colon_still_entry_header(self):
        p = _para("Acme Corp\tJun 2020 - Aug 2021", bold=True, tab_count=1)
        assert _importer()._classify_paragraph_role(p) == "entry_header"
