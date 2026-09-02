"""Tests for PdfRenderer's font-coverage warning.

Regression: _resolve_font() only ever emits ReportLab's built-in base-14
fonts (Helvetica/Times) -- no TTF is registered anywhere in this
renderer, and those fonts only cover WinAnsiEncoding (~Latin-1). Any
character outside that (Chinese, Cyrillic, Arabic, emoji, etc.) silently
renders as a solid glyph box with no error anywhere -- confirmed via
visual PDF rendering during the audit that found this. A real fix needs
bundling and registering a Unicode-coverage font, a bigger separate
change; this at least surfaces the failure instead of letting it stay
silent.
"""

import logging

from backend.models.resume_content import ContactInfo, ResumeContent
from backend.models.resume_ir import ResumeIR
from backend.renderers.pdf_renderer import PdfRenderer


class TestFontCoverageWarning:
    def test_non_latin_name_logs_a_warning(self, caplog):
        content = ResumeContent(contact=ContactInfo(name="王小明"))
        with caplog.at_level("WARNING", logger="backend.renderers.pdf_renderer"):
            PdfRenderer(ResumeIR(content=content))
        assert any("aren't supported" in r.message for r in caplog.records)

    def test_cyrillic_text_logs_a_warning(self, caplog):
        content = ResumeContent(contact=ContactInfo(name="Иванов"))
        with caplog.at_level("WARNING", logger="backend.renderers.pdf_renderer"):
            PdfRenderer(ResumeIR(content=content))
        assert any("aren't supported" in r.message for r in caplog.records)

    def test_plain_english_content_logs_nothing(self, caplog):
        content = ResumeContent(contact=ContactInfo(name="Jane Doe"))
        with caplog.at_level("WARNING", logger="backend.renderers.pdf_renderer"):
            PdfRenderer(ResumeIR(content=content))
        assert not any("aren't supported" in r.message for r in caplog.records)

    def test_accented_latin_characters_do_not_trigger_the_warning(self, caplog):
        # café/naïve-style accented Western-European characters ARE
        # covered by WinAnsiEncoding -- must not false-positive.
        content = ResumeContent(contact=ContactInfo(name="José García"))
        with caplog.at_level("WARNING", logger="backend.renderers.pdf_renderer"):
            PdfRenderer(ResumeIR(content=content))
        assert not any("aren't supported" in r.message for r in caplog.records)
