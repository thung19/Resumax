"""Bullet Measurer — calibrated line-width measurement.

Uses ReportLab to measure text width, calibrated against the
original resume's bullets. Since we know every original bullet
fits on one line, the widest original bullet defines the real
line capacity — regardless of whether we have the exact font.

This handles the font mismatch problem: even if we measure with
Times-Roman instead of Garamond, the relative widths are consistent
enough that calibrating against the originals gives accurate results.

For exact measurements, install the resume's font on the system
and the measurer will auto-register it with ReportLab.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Optional

from reportlab.lib.enums import TA_LEFT
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import Paragraph

from backend.models.resume_content import ResumeContent, SectionType
from backend.models.resume_layout import ResumeLayout

logger = logging.getLogger(__name__)

# === Use shared font resolution module ===
from backend.renderers.fonts import register_and_resolve


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# --- Measurement result ---

@dataclass
class BulletMeasurement:
    text: str
    line_count: int
    rendered_width_pt: float
    rendered_height_pt: float
    available_width_pt: float
    leading_pt: float
    fits_one_line: bool
    overflow_ratio: float


# --- Measurer ---

class BulletMeasurer:
    """Calibrated bullet width measurement.

    Measures text width using ReportLab, calibrated against the
    original resume's bullets to determine the real line capacity.

    The calibration step measures all existing bullets and finds
    the widest one. Since those bullets fit on one line in the
    actual document, that width IS the real line capacity — even
    if we're using a different font for measurement.
    """

    def __init__(
        self,
        layout: ResumeLayout,
        content: Optional[ResumeContent] = None,
        safety_margin: float = 0.0,
    ):
        self._layout = layout
        self._safety_margin = safety_margin

        # Compute geometric content width (in points)
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

        self._geometric_width_pt = content_width_pt - left_indent_pt

        # Resolve font
        font_family = "Times-Roman"
        font_size = 10.0
        leading = 11.5

        if bullet_style:
            font_family = register_and_resolve(
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
                font_family = register_and_resolve(df.family, False, False)
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

        # Calibrate: find the real line capacity by measuring
        # the widest original bullet
        self._max_original_chars = 120  # fallback
        self._calibrated_width_pt = self._calibrate(content)

        # Apply safety margin
        self._safe_width_pt = self._calibrated_width_pt * (1.0 - safety_margin)

    def _calibrate(self, content: Optional[ResumeContent]) -> float:
        """Find the real line capacity by measuring all original bullets.

        Every bullet in the uploaded resume fits on one line.
        The widest one defines the max width we should allow.
        """
        if not content:
            return self._geometric_width_pt

        max_width = 0.0
        max_chars = 0
        bullet_count = 0

        for section in content.sections:
            for entry in section.experience_entries:
                for b in entry.bullets:
                    w = self._measure_raw_width(b.text)
                    if w > max_width:
                        max_width = w
                    if len(b.text) > max_chars:
                        max_chars = len(b.text)
                    bullet_count += 1
            for entry in section.project_entries:
                for b in entry.bullets:
                    w = self._measure_raw_width(b.text)
                    if w > max_width:
                        max_width = w
                    if len(b.text) > max_chars:
                        max_chars = len(b.text)
                    bullet_count += 1

        if max_chars > 0:
            self._max_original_chars = max_chars

        if max_width > 0 and bullet_count >= 3:
            # Clamp to the physical page width: the invariant this method
            # relies on ("every bullet in the uploaded resume fits on one
            # line") isn't actually verified anywhere — if the source
            # document's widest bullet was already borderline/overflowing
            # (common with copy-pasted resumes or a measurement font
            # that renders wider than the true original font), the
            # "calibrated safe width" balloons past what physically fits
            # on the page. Every downstream width budget (safe_width_pt,
            # max_chars_for_line, batch-trim char caps) inherits that
            # inflation, so a bullet judged "fits one line" can still
            # visibly wrap to a second line in the rendered PDF.
            calibrated = min(max_width, self._geometric_width_pt)
            if calibrated < max_width:
                logger.warning(
                    f"Calibrated line width {max_width:.1f}pt exceeds the "
                    f"physical page width {self._geometric_width_pt:.1f}pt "
                    f"(from {bullet_count} bullets) — clamping. The source "
                    f"resume's widest original bullet may already overflow "
                    f"its line."
                )
            else:
                logger.info(
                    f"Calibrated line width: {calibrated:.1f}pt "
                    f"(from {bullet_count} bullets, "
                    f"geometric: {self._geometric_width_pt:.1f}pt)"
                )
            return calibrated

        return self._geometric_width_pt

    def _measure_raw_width(self, text: str) -> float:
        """Measure the rendered width of bullet text (with bullet char).

        Uses pdfmetrics.stringWidth for accurate single-line width,
        since Paragraph.wrap() returns the available width, not the
        actual text width.
        """
        from reportlab.pdfbase.pdfmetrics import stringWidth
        display_text = f"\u2022 {text}"
        return stringWidth(display_text, self._font_name, self._font_size)

    @property
    def safe_width_pt(self) -> float:
        return self._safe_width_pt

    @property
    def raw_width_pt(self) -> float:
        return self._calibrated_width_pt

    @property
    def font_name(self) -> str:
        return self._font_name

    @property
    def font_size(self) -> float:
        return self._font_size

    def measure(self, text: str) -> BulletMeasurement:
        """Measure whether a bullet fits on one line.

        Compares the rendered width against the calibrated line
        capacity (derived from the widest original bullet).
        """
        true_w = self._measure_raw_width(text)

        # 1% tolerance — sub-pixel overflows won't cause visible wrapping
        tolerance = self._safe_width_pt * 0.01
        fits = true_w <= self._safe_width_pt + tolerance
        overflow_ratio = true_w / self._safe_width_pt if not fits else 1.0
        line_count = 1 if fits else max(2, round(overflow_ratio + 0.5))

        return BulletMeasurement(
            text=text,
            line_count=line_count,
            rendered_width_pt=true_w,
            rendered_height_pt=self._leading * line_count,
            available_width_pt=self._safe_width_pt,
            leading_pt=self._leading,
            fits_one_line=fits,
            overflow_ratio=overflow_ratio,
        )

    def measure_line(self, text: str) -> BulletMeasurement:
        """Measure arbitrary text (no bullet prefix)."""
        from reportlab.pdfbase.pdfmetrics import stringWidth
        true_w = stringWidth(text, self._font_name, self._font_size)

        fits = true_w <= self._safe_width_pt
        overflow_ratio = true_w / self._safe_width_pt if not fits else 1.0
        line_count = 1 if fits else max(2, round(overflow_ratio + 0.5))

        return BulletMeasurement(
            text=text,
            line_count=line_count,
            rendered_width_pt=true_w,
            rendered_height_pt=self._leading * line_count,
            available_width_pt=self._safe_width_pt,
            leading_pt=self._leading,
            fits_one_line=fits,
            overflow_ratio=overflow_ratio,
        )

    def max_chars_for_line(self) -> int:
        """Return the character count of the longest original bullet.

        Since that bullet fits on one line, this is a proven safe
        character limit. Using char count of actual content is more
        reliable than computing from average char widths, because
        the original bullets have representative letter distribution.
        """
        return self._max_original_chars

    def find_line_break(self, text: str) -> int:
        """Find the character index where bullet text overflows the line.

        Returns the index of the last character that fits on one line.
        Uses binary search with actual font width measurement.
        The text should NOT include the bullet prefix (•).
        """
        from reportlab.pdfbase.pdfmetrics import stringWidth

        full_w = self._measure_raw_width(text)
        if full_w <= self._safe_width_pt:
            return len(text)  # everything fits

        # Binary search for the break point
        # Account for the bullet prefix width
        prefix = "\u2022 "
        prefix_w = stringWidth(prefix, self._font_name, self._font_size)
        avail = self._safe_width_pt - prefix_w

        low, high = 0, len(text)
        while low < high:
            mid = (low + high + 1) // 2
            w = stringWidth(text[:mid], self._font_name, self._font_size)
            if w <= avail:
                low = mid
            else:
                high = mid - 1

        return low
