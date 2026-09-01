"""Tests for DocxImporter's run-splitting (_capture_run_formats).

Regression (live bug report): a downloaded .docx was missing the dates
for one experience entry. Root cause: OOXML allows a single <w:r> run to
contain more than one content child in sequence -- e.g.
<w:tab/><w:t>Jun 2025 - Aug 2025</w:t> together in ONE run, exactly what
Word writes when you press Tab and keep typing without changing
formatting. The importer used to capture that as a single RunFormat with
both is_tab=True and text="Jun 2025 - Aug 2025" set -- but every renderer
treats is_tab=True as "just emit a tab, there's nothing else here" (see
docx_renderer.py), so the date text was silently dropped at render time
even though it was captured correctly at import time. This is exactly
what surfaced in the real resume: the other three experience entries had
their tab and date in *separate* <w:r> elements (unaffected), only this
one combined them in a single run.
"""

from docx.oxml.ns import qn
from lxml import etree

from backend.importers.docx_importer import DocxImporter

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _run(xml_inner: str) -> etree._Element:
    xml = f'<w:r xmlns:w="{W_NS}">{xml_inner}</w:r>'
    return etree.fromstring(xml)


def _importer() -> DocxImporter:
    # _capture_run_formats is a pure function of the XML element -- it
    # doesn't touch any instance state -- so skip __init__ (which
    # requires real file bytes) rather than constructing a whole .docx.
    return DocxImporter.__new__(DocxImporter)


class TestTabAndTextInSameRun:
    def test_tab_then_text_splits_into_two_runs(self):
        # The exact shape found in the source .docx that reproduced this bug.
        r_elem = _run(
            '<w:rPr><w:b/></w:rPr><w:tab/><w:t>Jun 2025 – Aug 2025</w:t>'
        )
        runs = _importer()._capture_run_formats(r_elem)

        assert len(runs) == 2
        assert runs[0].is_tab is True
        assert runs[0].text == ""
        assert runs[1].is_tab is False
        assert runs[1].text == "Jun 2025 – Aug 2025"

    def test_formatting_preserved_on_both_split_pieces(self):
        r_elem = _run('<w:rPr><w:b/></w:rPr><w:tab/><w:t>Aug 2025</w:t>')
        runs = _importer()._capture_run_formats(r_elem)
        assert runs[0].bold is True
        assert runs[1].bold is True

    def test_plain_tab_run_unaffected(self):
        # The "normal" case (tab alone) must still behave exactly as before.
        r_elem = _run('<w:rPr><w:b/></w:rPr><w:tab/>')
        runs = _importer()._capture_run_formats(r_elem)
        assert len(runs) == 1
        assert runs[0].is_tab is True
        assert runs[0].text == ""

    def test_plain_text_run_unaffected(self):
        r_elem = _run('<w:rPr><w:b/></w:rPr><w:t>MetLife Investment Management</w:t>')
        runs = _importer()._capture_run_formats(r_elem)
        assert len(runs) == 1
        assert runs[0].is_tab is False
        assert runs[0].text == "MetLife Investment Management"

    def test_bare_formatting_only_run_yields_one_empty_run(self):
        # A run with rPr but no <w:t>/<w:tab> content at all (e.g. a
        # proofing-error marker) must still yield exactly one RunFormat,
        # not zero -- matches prior behavior for any caller counting runs.
        r_elem = _run("<w:rPr><w:b/></w:rPr>")
        runs = _importer()._capture_run_formats(r_elem)
        assert len(runs) == 1
        assert runs[0].text == ""
        assert runs[0].is_tab is False

    def test_multiple_tabs_around_text_all_captured(self):
        r_elem = _run("<w:tab/><w:tab/><w:t>Remote</w:t>")
        runs = _importer()._capture_run_formats(r_elem)
        assert [ (r.is_tab, r.text) for r in runs ] == [
            (True, ""), (True, ""), (False, "Remote"),
        ]

    def test_text_then_tab_splits_in_document_order(self):
        r_elem = _run('<w:t>Atlantic Health System</w:t><w:tab/>')
        runs = _importer()._capture_run_formats(r_elem)
        assert [ (r.is_tab, r.text) for r in runs ] == [
            (False, "Atlantic Health System"), (True, ""),
        ]


class TestExtractRunFormattings:
    """Tests for _extract_run_formattings, the sibling of
    _capture_run_formats that feeds *structured content extraction*
    (company/role/date splitting via _split_left_right) rather than
    rendering. Same underlying OOXML fact (a <w:r> can hold tab(s) and
    text in any order), but this path had the *opposite*-shaped bug:
    text-then-tab in one run (e.g. "Acme Corp" + tab, exactly what Word
    writes when you type a name, hit Tab, and keep typing) used to glue
    onto whatever text followed instead of ending the left column there,
    since the old code assumed a run's text always comes AFTER its tabs.
    Live report: this is the "company name and date squished together,
    date disappears" version of the already-fixed "date disappears
    entirely" bug.
    """

    def test_text_then_tab_splits_into_two_pieces(self):
        from backend.importers.docx_importer import _extract_run_formattings
        r_elem = _run('<w:rPr><w:b/></w:rPr><w:t>Acme Corp</w:t><w:tab/>')
        pieces = _extract_run_formattings(r_elem)
        assert [(p["text"], p["tab_count"]) for p in pieces] == [
            ("Acme Corp", 0), ("", 1),
        ]
        assert all(p["bold"] for p in pieces)

    def test_tab_then_text_still_works(self):
        from backend.importers.docx_importer import _extract_run_formattings
        r_elem = _run('<w:tab/><w:t>Jun 2020 - Aug 2021</w:t>')
        pieces = _extract_run_formattings(r_elem)
        assert [(p["text"], p["tab_count"]) for p in pieces] == [
            ("", 1), ("Jun 2020 - Aug 2021", 0),
        ]

    def test_manual_line_break_becomes_a_space_not_glued_text(self):
        from backend.importers.docx_importer import _extract_run_formattings
        r_elem = _run("<w:t>First line</w:t><w:br/><w:t>Second line</w:t>")
        pieces = _extract_run_formattings(r_elem)
        assert len(pieces) == 1
        assert pieces[0]["text"] == "First line Second line"

    def test_bare_formatting_only_run_yields_one_empty_piece(self):
        from backend.importers.docx_importer import _extract_run_formattings
        r_elem = _run("<w:rPr><w:b/></w:rPr>")
        pieces = _extract_run_formattings(r_elem)
        assert len(pieces) == 1
        assert pieces[0]["text"] == ""
        assert pieces[0]["tab_count"] == 0

    def test_split_left_right_end_to_end_text_then_tab_in_one_run(self):
        # The exact shape that reproduced the live bug: company name and
        # a tab together in ONE run, then the date in a SEPARATE run.
        from backend.importers.docx_importer import _extract_run_formattings

        class _FakeParagraph:
            pass

        r1 = _run('<w:rPr><w:b/></w:rPr><w:t>Acme Corp</w:t><w:tab/>')
        r2 = _run('<w:rPr><w:b/></w:rPr><w:t>Jun 2020 - Aug 2021</w:t>')
        p = _FakeParagraph()
        p.runs = _extract_run_formattings(r1) + _extract_run_formattings(r2)

        left, right = _importer()._split_left_right(p)
        assert left == "Acme Corp"
        assert right == "Jun 2020 - Aug 2021"
