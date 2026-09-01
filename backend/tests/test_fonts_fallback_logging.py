"""Tests for fonts.py's font-not-found logging.

Regression: register_and_resolve() logged on successful registration and
on a registration *error*, but when find_font_file() simply returned
nothing (no exception, just not found), it fell through to
builtin_fallback() with zero logging. Calibri/Cambria/Aptos have been
Word's default body/heading fonts for roughly two decades and aren't
installed on most non-Windows servers, so this silently substituted
Helvetica/Times metrics for the real document font on a large share of
real uploaded resumes, with no record anywhere it happened.
"""

import backend.renderers.fonts as fonts_module
from backend.renderers.fonts import register_and_resolve


class TestMissingFontLogsAWarning:
    def setup_method(self):
        # Each font is only warned about once (see _warned_missing_fonts) —
        # reset between tests so assertions aren't order-dependent.
        fonts_module._warned_missing_fonts.clear()
        fonts_module._registered_fonts.clear()

    def test_missing_font_logs_a_warning(self, caplog):
        with caplog.at_level("WARNING", logger="backend.renderers.fonts"):
            resolved = register_and_resolve("ThisFontDoesNotExist12345", False, False)

        assert resolved  # still returns a usable builtin fallback name
        assert any(
            "ThisFontDoesNotExist12345" in r.message and "not found" in r.message
            for r in caplog.records
        )

    def test_same_missing_font_only_warned_once(self, caplog):
        with caplog.at_level("WARNING", logger="backend.renderers.fonts"):
            register_and_resolve("ThisFontDoesNotExist12345", False, False)
            register_and_resolve("ThisFontDoesNotExist12345", False, False)

        warnings = [r for r in caplog.records if "not found" in r.message]
        assert len(warnings) == 1

    def test_common_word_fonts_trigger_the_warning(self, caplog):
        # Calibri/Cambria/Aptos are Word defaults very unlikely to be
        # installed on the machine running the test suite.
        with caplog.at_level("WARNING", logger="backend.renderers.fonts"):
            for family in ["Calibri", "Cambria", "Aptos"]:
                register_and_resolve(family, False, False)

        warned_families = {
            r.message.split("'")[1] for r in caplog.records if "not found" in r.message
        }
        assert {"Calibri", "Cambria", "Aptos"}.issubset(warned_families)
