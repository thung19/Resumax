"""Tests for element_styles.py's _split_at_tab (via extract_formatting).

Regression: `first_tab` was set to the index of ANY tab run, including
one at position 0 (a leading tab before any real text — e.g. a paragraph
starting "<tab>Acme Corp<tab>Jan 2023"). That made `left_runs` empty and
dumped both the header text and the date into `right_runs`, concatenated
with no separator ("Acme CorpJan 2023") jammed into the right-aligned
cell -- while docx_renderer.py's independent implementation handled the
identical input correctly, so PDF/HTML rendering of such a resume
diverged from its DOCX export.
"""

from backend.models.resume_layout import (
    ElementType, LayoutElement, ParagraphFormat, RunFormat,
)
from backend.renderers.element_styles import extract_formatting


def _el(runs: list[RunFormat]) -> LayoutElement:
    return LayoutElement(
        id="e1", element_type=ElementType.ENTRY_HEADER,
        paragraph_format=ParagraphFormat(runs=runs),
    )


class TestLeadingTabBeforeText:
    def test_leading_tab_does_not_swallow_the_header_into_right_runs(self):
        el = _el([
            RunFormat(text="", is_tab=True),
            RunFormat(text="Acme Corp", bold=True),
            RunFormat(text="", is_tab=True),
            RunFormat(text="Jan 2023 - Present", bold=True),
        ])
        fmt = extract_formatting(el, "Garamond", 10.0)

        assert fmt.has_left_right is True
        assert "".join(r.text for r in fmt.left_runs) == "Acme Corp"
        assert "".join(r.text for r in fmt.right_runs) == "Jan 2023 - Present"

    def test_no_leading_tab_still_works(self):
        # Regression guard: the ordinary, already-correct shape must be
        # unaffected.
        el = _el([
            RunFormat(text="Acme Corp", bold=True),
            RunFormat(text="", is_tab=True),
            RunFormat(text="Jan 2023 - Present", bold=True),
        ])
        fmt = extract_formatting(el, "Garamond", 10.0)

        assert fmt.has_left_right is True
        assert "".join(r.text for r in fmt.left_runs) == "Acme Corp"
        assert "".join(r.text for r in fmt.right_runs) == "Jan 2023 - Present"

    def test_multiple_leading_tabs_before_text(self):
        el = _el([
            RunFormat(text="", is_tab=True),
            RunFormat(text="", is_tab=True),
            RunFormat(text="Acme Corp", bold=True),
            RunFormat(text="", is_tab=True),
            RunFormat(text="Jan 2023", bold=True),
        ])
        fmt = extract_formatting(el, "Garamond", 10.0)

        assert "".join(r.text for r in fmt.left_runs) == "Acme Corp"
        assert "".join(r.text for r in fmt.right_runs) == "Jan 2023"

    def test_only_tabs_no_text_returns_no_split(self):
        el = _el([RunFormat(text="", is_tab=True), RunFormat(text="", is_tab=True)])
        fmt = extract_formatting(el, "Garamond", 10.0)
        assert fmt.has_left_right is False
