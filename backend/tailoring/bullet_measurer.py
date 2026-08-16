"""Bullet Measurer — layout-accurate line count measurement.

Uses ReportLab's Paragraph.wrap() to determine the actual rendered
height of a bullet. Attempts to register the actual font from the
uploaded DOCX (e.g., Garamond) so measurements match what Word renders.

If the exact font isn't available on the system, falls back to the
closest built-in font.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Optional

from reportlab.lib.enums import TA_LEFT
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import Paragraph

from backend.models.resume_layout import ResumeLayout

logger = logging.getLogger(__name__)

# --- Font resolution ---

# Registered TTF fonts (cached across instances)
_registered_fonts: set[str] = set()

# Common system font directories
_FONT_DIRS = [
    "/System/Library/Fonts/Supplemental",
    "/System/Library/Fonts",
    "/Library/Fonts",
    os.path.expanduser("~/Library/Fonts"),
    # Linux
    "/usr/share/fonts/truetype",
    "/usr/local/share/fonts",
    # Windows
    r"C:\Windows\Fonts",
]

# Map font family names to search patterns for TTF files
_FONT_FILE_PATTERNS: dict[str, list[str]] = {
    "garamond": ["Garamond.ttf", "Garamond-Regular.ttf", "EBGaramond-Regular.ttf", "garamond.ttf"],
    "calibri": ["Calibri.ttf", "calibri.ttf"],
    "cambria": ["Cambria.ttf", "cambria.ttf"],
    "georgia": ["Georgia.ttf", "georgia.ttf"],
    "palatino": ["Palatino.ttc", "PalatinoLinotype.ttf"],
    "arial": ["Arial.ttf", "arial.ttf"],
    "helvetica": ["Helvetica.ttc", "HelveticaNeue.ttc"],
    "times new roman": ["Times New Roman.ttf", "times.ttf"],
    "times": ["Times.ttc", "Times New Roman.ttf"],
}

# Fallback: map font families to ReportLab built-in fonts
_SERIF = {"garamond", "georgia", "times", "times new roman", "palatino", "cambria"}
_SANS = {"arial", "helvetica", "calibri", "verdana", "tahoma", "segoe ui"}


def _find_font_file(family: str) -> Optional[str]:
    """Search system font directories for a TTF/TTC file matching the family."""
    fl = family.lower().strip()

    # Try known patterns first
    patterns = _FONT_FILE_PATTERNS.get(fl, [f"{family}.ttf", f"{family.title()}.ttf"])

    for font_dir in _FONT_DIRS:
        if not os.path.isdir(font_dir):
            continue
        for pattern in patterns:
            path = os.path.join(font_dir, pattern)
            if os.path.isfile(path):
                return path

    # Broader search: any file containing the family name
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


def _register_and_resolve(family: str, bold: bool = False, italic: bool = False) -> str:
    """Try to register the actual TTF font with ReportLab.

    Returns the ReportLab font name to use (either the registered TTF
    or a built-in fallback).
    """
    fl = family.lower().strip()
    rl_name = f"Custom-{family.replace(' ', '')}"

    # Already registered?
    if rl_name in _registered_fonts:
        return rl_name

    # Try to find and register the TTF
    font_path = _find_font_file(family)
    if font_path:
        try:
            from reportlab.pdfbase import pdfmetrics
            from reportlab.pdfbase.ttfonts import TTFont

            if font_path.endswith(".ttc"):
                # TTC (TrueType Collection) — use subfont index 0
                pdfmetrics.registerFont(TTFont(rl_name, font_path, subfontIndex=0))
            else:
                pdfmetrics.registerFont(TTFont(rl_name, font_path))

            _registered_fonts.add(rl_name)
            logger.info(f"Registered font '{family}' from {font_path}")
            return rl_name
        except Exception as e:
            logger.warning(f"Failed to register font '{family}' from {font_path}: {e}")

    # Fallback to ReportLab built-in
    return _builtin_fallback(fl, bold, italic)


def _builtin_fallback(family_lower: str, bold: bool, italic: bool) -> str:
    """Map to ReportLab built-in font as last resort."""
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


def _escape(text: str) -> str:
    """Escape text for ReportLab Paragraph XML."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# --- Measurement result ---

@dataclass
class BulletMeasurement:
    """Result of measuring a bullet's rendered layout."""
    text: str
    line_count: int
    rendered_width_pt: float
    rendered_height_pt: float
    available_width_pt: float
    leading_pt: float
    fits_one_line: bool
    overflow_ratio: float  # >1.0 means overflows


# --- Measurer ---

class BulletMeasurer:
    """Measure bullet line count using the actual font from the resume.

    Tries to find and register the exact font (e.g., Garamond) from
    the system so measurements match Word's rendering. Falls back to
    ReportLab built-in fonts if the exact font isn't available.
    """

    def __init__(
        self,
        layout: ResumeLayout,
        safety_margin: float = 0.03,
    ):
        self._layout = layout
        self._safety_margin = safety_margin

        # Compute available width for bullets (in points)
        page = layout.page
        content_width_pt = (
            page.width_in - page.margin_left_in - page.margin_right_in
        ) * 72

        # Bullet indentation
        bullet_style = layout.styles.get("bullet")
        if bullet_style and bullet_style.indent.left_in:
            left_indent_pt = bullet_style.indent.left_in * 72
        else:
            left_indent_pt = 0

        self._raw_width_pt = content_width_pt - left_indent_pt
        self._safe_width_pt = self._raw_width_pt * (1.0 - safety_margin)

        # Resolve the actual font
        font_family = "Times-Roman"
        font_size = 10.0
        leading = 11.5

        if bullet_style:
            font_family = _register_and_resolve(
                bullet_style.font.family,
                bullet_style.font.bold,
                bullet_style.font.italic,
            )
            font_size = bullet_style.font.size_pt
            ls = bullet_style.spacing.line_spacing or 1.15
            leading = font_size * ls
        else:
            df = layout.default_font
            if df.family:
                font_family = _register_and_resolve(df.family, False, False)
            if df.size_pt:
                font_size = df.size_pt
                leading = font_size * 1.15

        self._font_name = font_family
        self._font_size = font_size
        self._leading = leading

        self._style = ParagraphStyle(
            name="bullet_measure",
            fontName=font_family,
            fontSize=font_size,
            leading=leading,
            alignment=TA_LEFT,
        )

    @property
    def safe_width_pt(self) -> float:
        return self._safe_width_pt

    @property
    def raw_width_pt(self) -> float:
        return self._raw_width_pt

    @property
    def font_name(self) -> str:
        return self._font_name

    @property
    def font_size(self) -> float:
        return self._font_size

    def measure(self, text: str) -> BulletMeasurement:
        """Measure a bullet's rendered line count.

        The text should be the bullet content WITHOUT the bullet
        character. We prepend '•' for measurement.
        """
        display_text = f"\u2022 {_escape(text)}"

        para = Paragraph(display_text, self._style)
        w, h = para.wrap(self._safe_width_pt, 10000)

        line_count = max(1, round(h / self._leading))

        w_raw, h_raw = para.wrap(self._raw_width_pt, 10000)

        overflow_ratio = 1.0
        if line_count > 1:
            overflow_ratio = h / self._leading

        return BulletMeasurement(
            text=text,
            line_count=line_count,
            rendered_width_pt=w,
            rendered_height_pt=h,
            available_width_pt=self._safe_width_pt,
            leading_pt=self._leading,
            fits_one_line=(line_count == 1),
            overflow_ratio=overflow_ratio,
        )

    def measure_line(self, text: str) -> BulletMeasurement:
        """Measure arbitrary text (no bullet prefix)."""
        display_text = _escape(text)
        para = Paragraph(display_text, self._style)
        w, h = para.wrap(self._safe_width_pt, 10000)
        line_count = max(1, round(h / self._leading))
        return BulletMeasurement(
            text=text,
            line_count=line_count,
            rendered_width_pt=w,
            rendered_height_pt=h,
            available_width_pt=self._safe_width_pt,
            leading_pt=self._leading,
            fits_one_line=(line_count == 1),
            overflow_ratio=h / self._leading if line_count > 1 else 1.0,
        )

    def compute_compression_target(self, measurement: BulletMeasurement) -> float:
        """Calculate compression ratio needed to fit on one line."""
        if measurement.fits_one_line:
            return 1.0
        compression = 1.0 / measurement.overflow_ratio
        compression *= 0.95
        return min(compression, 0.95)
