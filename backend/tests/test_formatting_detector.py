"""Tests for FormattingDetector.

Found by an audit hunting for the same "text/structure classification
that doesn't hold for all real inputs" bug class already found and
fixed twice in docx_importer.py this session. FormattingDetector is
live, not dead code -- it's used in the upload handler (refine_layout,
which mutates ir.layout.styles returned to the client) and the
/inspect/{resume_id} endpoint.

Regression 1: section-heading detection required ALL-CAPS text, same as
docx_importer.py's (separate, already-correct) classifier. A bold,
title-case heading like "Work Experience" fell through to
"entry_header", merging it with real entry headers (company names).

Regression 2: when two DIFFERENT formatting clusters both resolved to
the same role, only `count`/`paragraph_indices` were merged --
`signature`/`example_texts` silently kept whichever cluster was
inserted first, even if a later, more common cluster for that role had
different formatting (e.g. a different font size).

Regression 3: the bold entry_header checks ran BEFORE the colon/
skills-row check, so a bold colon-containing line never reached it at
all -- disagreeing with docx_importer.py's own classification (fixed
earlier this session to check colon first) for the identical paragraph.
"""

from backend.analysis.formatting_detector import FormattingDetector
from backend.importers.docx_importer import DocxImporter, ParagraphFormatting


def _make(text: str, bold: bool = False, font_size_pt: float | None = None,
          alignment: str = "left", runs: list | None = None) -> ParagraphFormatting:
    p = ParagraphFormatting()
    p.text = text
    p.visible_text = text
    p.bold = bold
    p.italic = False
    p.alignment = alignment
    p.font_size_pt = font_size_pt
    p.has_numbering = False
    p.is_empty = False
    p.runs = runs or []
    return p


class TestSectionHeadingWithoutAllCaps:
    def test_title_case_bold_larger_heading_recognized(self):
        paras = [
            _make("Work Experience", bold=True, font_size_pt=14),
            _make("Acme Corp", bold=True, font_size_pt=11),
            _make("Did a thing at Acme", bold=False, font_size_pt=11),
            _make("Did another thing", bold=False, font_size_pt=11),
        ]
        result = FormattingDetector(paras).detect()
        assert "section_heading" in result["patterns"]
        assert result["patterns"]["section_heading"].example_texts == ["Work Experience"]
        # The company name must NOT be swept into section_heading too.
        assert "entry_header" in result["patterns"]
        assert result["patterns"]["entry_header"].example_texts == ["Acme Corp"]

    def test_bold_paragraph_with_bottom_border_is_a_heading_regardless_of_case(self):
        p = _make("Work Experience", bold=True, font_size_pt=11)
        p.has_bottom_border = True
        role = FormattingDetector([p])._infer_role_from_cluster(p, [0])
        assert role == "section_heading"

    def test_ordinary_bold_company_name_at_body_size_stays_entry_header(self):
        # Regression guard: the fix must not turn every bold paragraph
        # into a heading -- only ones distinctly larger/ruled.
        paras = [
            _make("Acme Corp", bold=True, font_size_pt=11),
            _make("Did a thing", bold=False, font_size_pt=11),
            _make("Did another thing", bold=False, font_size_pt=11),
        ]
        result = FormattingDetector(paras).detect()
        assert "section_heading" not in result["patterns"]
        assert result["patterns"]["entry_header"].example_texts == ["Acme Corp"]

    def test_all_caps_heading_still_works(self):
        p = _make("WORK EXPERIENCE", bold=True, font_size_pt=11)
        role = FormattingDetector([p])._infer_role_from_cluster(p, [0])
        assert role == "section_heading"


class TestMergedPatternSignatureReconciled:
    def test_dominant_cluster_by_count_wins_the_merged_signature(self):
        # Three body-sized bold "entry_header" paragraphs (a real,
        # common cluster) plus one differently-formatted bold paragraph
        # that also resolves to entry_header -- the merged pattern's
        # signature/example_texts should reflect the DOMINANT (3-count)
        # cluster, not whichever happened to be inserted first.
        paras = [
            # 11.5pt is close enough to the 11pt baseline to stay under
            # the section_heading size-difference threshold (>1.5pt) --
            # this cluster should resolve to entry_header too, just a
            # differently-sized one, to isolate the merge-reconciliation
            # behavior from the separate size-based heading heuristic.
            _make("Odd One Out", bold=True, font_size_pt=11.5),  # its own cluster, count=1
            _make("Acme Corp", bold=True, font_size_pt=11),
            _make("Globex Inc", bold=True, font_size_pt=11),
            _make("Initech", bold=True, font_size_pt=11),
        ]
        result = FormattingDetector(paras).detect()
        pattern = result["patterns"]["entry_header"]
        assert pattern.count == 4
        # Signature should reflect the 3-paragraph dominant cluster
        # (font_size_pt=11), not the 1-paragraph "Odd One Out" cluster.
        assert pattern.signature.font_size_pt == 11


class TestColonCheckedBeforeBoldEntryHeader:
    def test_bold_skills_row_classified_consistently_with_docx_importer(self):
        p = _make("Languages: English, Spanish, French", bold=True)
        importer_role = DocxImporter.__new__(DocxImporter)._classify_paragraph_role(p)
        detector_role = FormattingDetector([p])._infer_role_from_cluster(p, [0])
        assert importer_role == detector_role == "skills_row"

    def test_entry_header_with_colon_and_tabs_still_entry_header(self):
        p = _make(
            "Client: Big Corp\tJan 2019", bold=True,
            runs=[{"tab_count": 1, "text": "x"}],
        )
        role = FormattingDetector([p])._infer_role_from_cluster(p, [0])
        assert role == "entry_header"
