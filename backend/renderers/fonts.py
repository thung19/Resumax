"""Shared font resolution logic for ReportLab and OOXML rendering.

This module handles:
1. Finding font files on the system (TTF/OTF)
2. Registering them with ReportLab
3. Mapping font families to ReportLab names
4. Providing fallback fonts

Used by both bullet_measurer.py and element_styles.py to ensure
consistent font handling across the codebase.
"""

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

# Bundled fonts directory (ships with the app)
_BUNDLED_FONTS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "fonts"
)

_FONT_DIRS = [
    _BUNDLED_FONTS_DIR,  # check bundled fonts first
    "/System/Library/Fonts/Supplemental",
    "/System/Library/Fonts",
    "/Library/Fonts",
    os.path.expanduser("~/Library/Fonts"),
    "/usr/share/fonts/truetype",
    "/usr/local/share/fonts",
    r"C:\Windows\Fonts",
]

_FONT_FILE_PATTERNS: dict[str, list[str]] = {
    "garamond": [
        "EBGaramond-Regular.ttf",  # bundled open-source Garamond
        "Garamond.ttf",
        "Garamond-Regular.ttf",
    ],
    "calibri": ["Calibri.ttf", "calibri.ttf"],
    "cambria": ["Cambria.ttf", "cambria.ttf"],
    "georgia": ["Georgia.ttf", "georgia.ttf"],
    "palatino": ["Palatino.ttc", "PalatinoLinotype.ttf"],
    "arial": ["Arial.ttf", "arial.ttf"],
    "helvetica": ["Helvetica.ttc", "HelveticaNeue.ttc"],
    "times new roman": ["Times New Roman.ttf", "times.ttf"],
    "times": ["Times.ttc", "Times New Roman.ttf"],
}

_SERIF = {"garamond", "georgia", "times", "times new roman", "palatino", "cambria"}
_SANS = {"arial", "helvetica", "calibri", "verdana", "tahoma", "segoe ui"}

_registered_fonts: set[str] = set()
_warned_missing_fonts: set[str] = set()


def find_font_file(family: str) -> Optional[str]:
    """Find a font file on the system by family name.

    Searches multiple directories and checks common file naming patterns.
    Returns the full path to the TTF/OTF file, or None if not found.
    """
    fl = family.lower().strip()
    patterns = _FONT_FILE_PATTERNS.get(fl, [f"{family}.ttf", f"{family.title()}.ttf"])
    for font_dir in _FONT_DIRS:
        if not os.path.isdir(font_dir):
            continue
        for pattern in patterns:
            path = os.path.join(font_dir, pattern)
            if os.path.isfile(path):
                return path

    # Fallback: scan directory for any font containing the family name
    for font_dir in _FONT_DIRS:
        if not os.path.isdir(font_dir):
            continue
        try:
            for f in os.listdir(font_dir):
                if fl in f.lower() and f.lower().endswith((".ttf", ".otf")):
                    return os.path.join(font_dir, f)
        except OSError:
            continue
    return None


def register_and_resolve(family: str, bold: bool = False, italic: bool = False) -> str:
    """Register a font with ReportLab and return its name.

    If the font file is found, registers it with ReportLab using TTFont.
    Returns a ReportLab-compatible font name string.

    If the font isn't found, returns a builtin fallback (e.g., "Times-Roman").
    """
    fl = family.lower().strip()
    rl_name = f"Custom-{family.replace(' ', '')}"

    if rl_name in _registered_fonts:
        return rl_name

    font_path = find_font_file(family)
    if font_path:
        try:
            from reportlab.pdfbase import pdfmetrics
            from reportlab.pdfbase.ttfonts import TTFont

            if font_path.endswith(".ttc"):
                pdfmetrics.registerFont(TTFont(rl_name, font_path, subfontIndex=0))
            else:
                pdfmetrics.registerFont(TTFont(rl_name, font_path))
            _registered_fonts.add(rl_name)
            logger.info(f"Registered font '{family}' from {font_path}")
            return rl_name
        except Exception as e:
            logger.warning(f"Failed to register font '{family}': {e}")

    # find_font_file() returned nothing (no exception, just not found) —
    # this used to fall through to builtin_fallback with zero logging.
    # Calibri/Cambria/Aptos have been Word's default body/heading fonts
    # for roughly two decades and aren't installed on most non-Windows
    # servers, so this is expected to trigger for a large share of real
    # uploaded resumes, silently substituting Helvetica/Times metrics
    # for whatever font the document actually specifies with no record
    # anywhere that it happened — makes a bad width measurement much
    # harder to debug later. Logged once per family, not on every call,
    # since this can be checked many times per resume.
    fallback = builtin_fallback(fl, bold, italic)
    if fl not in _warned_missing_fonts:
        _warned_missing_fonts.add(fl)
        logger.warning(
            f"Font '{family}' not found on this system (bundled or "
            f"installed) — falling back to builtin '{fallback}' metrics, "
            f"which may not match the real font's character widths."
        )

    return fallback


def builtin_fallback(family_lower: str, bold: bool, italic: bool) -> str:
    """Return a ReportLab builtin font name as a fallback.

    Maps family to serif (Times) or sans (Helvetica), then adds bold/italic.
    """
    if family_lower in _SERIF or "garamond" in family_lower:
        base = "Times"
    elif family_lower in _SANS:
        base = "Helvetica"
    else:
        base = "Helvetica"

    if bold and italic:
        return f"{base}-BoldItalic" if base == "Times" else f"{base}-BoldOblique"
    elif bold:
        return f"{base}-Bold"
    elif italic:
        return f"{base}-Italic" if base == "Times" else f"{base}-Oblique"
    return f"{base}-Roman" if base == "Times" else base
