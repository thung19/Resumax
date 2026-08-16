"""Bullet Measurer — layout-accurate line count measurement.

Uses ReportLab's Paragraph.wrap() to determine the actual rendered
height of a bullet, using the same font mapping and indentation as
the PDF renderer. This gives us the exact same wrapping decision
the final PDF will make.

The key insight: Paragraph.wrap(avail_width, avail_height) returns
(actual_width, actual_height). Since we know the leading (line height),
actual_height / leading = number of lines.
"""

from __future__ import annotations

from dataclasses import dataclass
from html import escape
from typing import Optional

from reportlab.lib.enums import TA_LEFT
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph

from backend.models.resume_layout import PageSetup, ResumeLayout, StyleDef


# --- Font mapping (mirrors pdf_renderer.py) ---

_SERIF = {"garamond", "georgia", "times", "times new roman", "palatino", "cambria"}
_SANS = {"arial", "helvetica", "calibri", "verdana", "tahoma", "segoe ui"}


def _resolve_font(family: str, bold: bool, italic: bool) -> str:
    """Map font family + style to ReportLab built-in font name.
    Must stay in sync with PdfRenderer._resolve_font."""
    fl = family.lower().strip()
    if fl in _SERIF or "garamond" in fl:
        base = "Times"
    elif fl in _SANS:
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
    overflow_ratio: float  # >1.0 means overflows (e.g. 1.15 = 15% too wide)


# --- Measurer ---

class BulletMeasurer:
    """Measure bullet line count using the actual ReportLab layout engine.

    Constructs a ParagraphStyle matching the PDF renderer's configuration
    for bullet points, then uses Paragraph.wrap() to determine how many
    lines the text would occupy.
    """

    def __init__(
        self,
        layout: ResumeLayout,
        safety_margin: float = 0.03,  # 3% safety margin
    ):
        self._layout = layout
        self._safety_margin = safety_margin

        # Compute available width for bullets (in points)
        page = layout.page
        content_width_pt = (page.width_in - page.margin_left_in - page.margin_right_in) * 72

        # Bullet indentation — use detected style or PDF renderer defaults
        bullet_style = layout.styles.get("bullet")
        if bullet_style and bullet_style.indent.left_in:
            left_indent_pt = bullet_style.indent.left_in * 72
        else:
            left_indent_pt = 0  # Original resume uses inline bullets, no indent

        self._raw_width_pt = content_width_pt - left_indent_pt
        self._safe_width_pt = self._raw_width_pt * (1.0 - safety_margin)

        # Build the paragraph style
        font_family = "Times-Roman"  # default
        font_size = 10.0
        leading = 11.5  # 1.15 line spacing at 10pt

        if bullet_style:
            font_family = _resolve_font(
                bullet_style.font.family, bullet_style.font.bold, bullet_style.font.italic
            )
            font_size = bullet_style.font.size_pt
            ls = bullet_style.spacing.line_spacing or 1.15
            leading = font_size * ls
        else:
            # Try to detect from layout default font
            df = layout.default_font
            if df.family:
                font_family = _resolve_font(df.family, False, False)
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
        """Available width for bullet text, with safety margin applied."""
        return self._safe_width_pt

    @property
    def raw_width_pt(self) -> float:
        """Available width without safety margin."""
        return self._raw_width_pt

    @property
    def font_name(self) -> str:
        return self._font_name

    @property
    def font_size(self) -> float:
        return self._font_size

    def measure(self, text: str) -> BulletMeasurement:
        """Measure a bullet's rendered line count.

        The text should be the bullet content WITHOUT the bullet character
        (the '•' prefix). We prepend it for measurement since the renderer
        does the same.
        """
        # Build the text as the renderer would output it
        display_text = f"\u2022 {_escape(text)}"

        para = Paragraph(display_text, self._style)
        # wrap() returns (actual_width_used, actual_height)
        w, h = para.wrap(self._safe_width_pt, 10000)

        line_count = max(1, round(h / self._leading))

        # Calculate overflow ratio: how wide is it relative to safe width?
        # We need to check against the raw (no-safety) width for the ratio
        w_raw, h_raw = para.wrap(self._raw_width_pt, 10000)
        raw_lines = max(1, round(h_raw / self._leading))

        # If it wraps with safety margin but not without, the overflow is small
        overflow_ratio = 1.0
        if line_count > 1:
            # Estimate: how much wider than one line?
            # Use the raw width to see if it's close
            overflow_ratio = h / self._leading  # e.g. 2.0 means exactly 2 lines

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
        """Measure arbitrary text (no bullet prefix) against the content width."""
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
        """Calculate the compression ratio needed to fit on one line.

        Returns a ratio like 0.85 meaning "shorten to 85% of current length".
        """
        if measurement.fits_one_line:
            return 1.0

        # Target: fit within safe_width in one line
        # Current: occupies overflow_ratio lines
        # Compression needed: 1.0 / overflow_ratio (with some margin)
        compression = 1.0 / measurement.overflow_ratio
        # Add a small extra margin for safety
        compression *= 0.95
        return min(compression, 0.95)  # never ask for less than 5% reduction
