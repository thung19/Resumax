"""Centralized formatting extraction from layout elements.

This module is the single source of truth for interpreting element-level
formatting captured from source documents.  Both the DOCX and PDF
renderers import from here so that spacing, font sizes, bold flags, and
spacer heights are computed in exactly one place.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from backend.models.resume_layout import (
    ElementType,
    LayoutElement,
    ParagraphFormat,
    ResumeLayout,
    RunFormat,
)


# ------------------------------------------------------------------
# Data classes – renderer-neutral formatting
# ------------------------------------------------------------------

@dataclass
class RunSegment:
    """A text segment with formatting normalised to points."""

    text: str = ""
    font_family: Optional[str] = None
    font_size_pt: Optional[float] = None
    bold: bool = False
    italic: bool = False
    color: Optional[str] = None
    is_tab: bool = False
    hyperlink_url: Optional[str] = None
    small_caps: bool = False
    underline: Optional[str] = None


@dataclass
class ElementFormatting:
    """Renderer-neutral formatting extracted from a single LayoutElement."""

    # Dominant run formatting
    font_family: str = "Arial"
    font_size_pt: float = 10.0
    bold: bool = False
    italic: bool = False
    color: Optional[str] = None

    # Paragraph spacing (points)
    line_height_pt: float = 10.0
    space_before_pt: float = 0.0
    space_after_pt: float = 0.0

    # Alignment
    alignment: str = "left"

    # Indentation (points)
    indent_left_pt: float = 0.0
    indent_right_pt: float = 0.0
    indent_hanging_pt: float = 0.0
    indent_first_line_pt: float = 0.0

    # All runs
    runs: list[RunSegment] = field(default_factory=list)

    # Tab-based left / right split
    has_left_right: bool = False
    left_runs: list[RunSegment] = field(default_factory=list)
    right_runs: list[RunSegment] = field(default_factory=list)

    # Border
    has_bottom_border: bool = False
    border_width_pt: float = 0.75
    border_color: Optional[str] = None

    # Drawing converted to border
    has_drawing_line: bool = False


# ------------------------------------------------------------------
# Unit conversion helpers
# ------------------------------------------------------------------

def _half_pt(v: Optional[int]) -> Optional[float]:
    """Half-points → points."""
    return v / 2.0 if v is not None else None


def _twips_pt(v: Optional[int]) -> Optional[float]:
    """Twips (1/20 pt) → points."""
    return v / 20.0 if v is not None else None


# ------------------------------------------------------------------
# Core extraction
# ------------------------------------------------------------------

def extract_formatting(
    el: LayoutElement,
    default_font: str = "Arial",
    default_size_pt: float = 10.0,
) -> ElementFormatting:
    """Extract normalised formatting from a *LayoutElement*.

    This is the single function both renderers should call to get
    consistent spacing, fonts, and bold/italic flags.
    """
    pf = el.paragraph_format
    fmt = ElementFormatting()

    # --- runs ---
    segments: list[RunSegment] = []
    for rf in pf.runs:
        segments.append(RunSegment(
            text=rf.text,
            font_family=rf.font_family,
            font_size_pt=_half_pt(rf.font_size_half_pt),
            bold=rf.bold or False,
            italic=rf.italic or False,
            color=rf.color,
            is_tab=rf.is_tab,
            hyperlink_url=rf.hyperlink_url,
            small_caps=rf.small_caps,
            underline=rf.underline,
        ))
    fmt.runs = segments

    # --- dominant visible run ---
    for seg in segments:
        if not seg.text.strip():
            continue
        if seg.color and seg.color.upper() in ("FFFFFF", "WHITE"):
            continue
        fmt.font_family = seg.font_family or default_font
        fmt.font_size_pt = seg.font_size_pt or default_size_pt
        fmt.bold = seg.bold
        fmt.italic = seg.italic
        fmt.color = seg.color
        break
    else:
        fmt.font_family = default_font
        fmt.font_size_pt = default_size_pt

    # --- spacing ---
    # Word's w:line values with lineRule="auto" are proportional:
    #   240 = single, 276 = 1.15x, 480 = double.
    # "Single" in Word is NOT 1.0x — it is approximately 1.2x (the
    # font's built-in line metric).  ReportLab's `leading` is the
    # absolute baseline-to-baseline distance, so we must apply the
    # same multiplier Word does.
    WORD_SINGLE_MULTIPLIER = 1.2  # Word "single" ≈ 120% of font size

    lv = pf.line_value
    lr = pf.line_rule

    if lv is not None:
        if lr in ("exact", "atLeast"):
            fmt.line_height_pt = lv / 20.0
        else:
            # Proportional: 240 = single (1.2x), 276 = 1.15 * 1.2x …
            ratio = lv / 240.0
            fmt.line_height_pt = fmt.font_size_pt * ratio * WORD_SINGLE_MULTIPLIER
    else:
        fmt.line_height_pt = fmt.font_size_pt * WORD_SINGLE_MULTIPLIER

    fmt.space_before_pt = _twips_pt(pf.space_before_twips) or 0.0
    fmt.space_after_pt = _twips_pt(pf.space_after_twips) or 0.0

    # --- alignment ---
    fmt.alignment = pf.alignment or "left"

    # --- indentation ---
    fmt.indent_left_pt = _twips_pt(pf.indent_left_twips) or 0.0
    fmt.indent_right_pt = _twips_pt(pf.indent_right_twips) or 0.0
    fmt.indent_hanging_pt = _twips_pt(pf.indent_hanging_twips) or 0.0
    fmt.indent_first_line_pt = _twips_pt(pf.indent_first_line_twips) or 0.0

    # --- borders ---
    if pf.bottom_border and pf.bottom_border.enabled:
        fmt.has_bottom_border = True
        fmt.border_width_pt = pf.bottom_border.width_pt
        fmt.border_color = pf.bottom_border.color

    if pf.has_drawing and pf.drawing_type == "line":
        fmt.has_drawing_line = True

    # --- left / right split ---
    _split_at_tab(fmt)

    return fmt


def _split_at_tab(fmt: ElementFormatting) -> None:
    """Partition runs into left and right groups, collapsing multiple tabs.

    Source documents often have 5-15 consecutive tab runs to push
    dates across.  We collapse everything between the first tab and
    the next real text into a single split.
    """
    # Find the first REAL (non-tab, non-blank) run before looking for the
    # split tab. A leading tab before any text (e.g. a paragraph starting
    # "<tab>Acme Corp<tab>Jan 2023") is indentation, not a left/right
    # column separator — without this, `first_tab` below would land on
    # that leading tab, making `left_runs` empty and dumping BOTH the
    # header text and the date into `right_runs` concatenated together
    # with no separator (e.g. "Acme CorpJan 2023") jammed into the
    # right-aligned cell.
    first_text_idx: Optional[int] = None
    for i, seg in enumerate(fmt.runs):
        if not seg.is_tab and seg.text.strip():
            first_text_idx = i
            break
    if first_text_idx is None:
        return

    first_tab: Optional[int] = None
    for i in range(first_text_idx, len(fmt.runs)):
        if fmt.runs[i].is_tab:
            first_tab = i
            break
    if first_tab is None:
        return

    fmt.has_left_right = True
    fmt.left_runs = [s for s in fmt.runs[first_text_idx:first_tab] if not s.is_tab]

    # Skip all tab runs and whitespace-only padding to find right content
    fmt.right_runs = []
    for s in fmt.runs[first_tab:]:
        if s.is_tab:
            continue
        if not fmt.right_runs and not s.text.strip():
            continue  # skip whitespace padding between tabs and text
        fmt.right_runs.append(s)

    # Strip leading whitespace from first right run
    if fmt.right_runs and fmt.right_runs[0].text:
        fmt.right_runs[0] = RunSegment(
            text=fmt.right_runs[0].text.lstrip(),
            font_family=fmt.right_runs[0].font_family,
            font_size_pt=fmt.right_runs[0].font_size_pt,
            bold=fmt.right_runs[0].bold,
            italic=fmt.right_runs[0].italic,
            color=fmt.right_runs[0].color,
            hyperlink_url=fmt.right_runs[0].hyperlink_url,
            small_caps=fmt.right_runs[0].small_caps,
            underline=fmt.right_runs[0].underline,
        )

    if not fmt.right_runs or not "".join(r.text for r in fmt.right_runs).strip():
        fmt.has_left_right = False
        fmt.right_runs = []


# ------------------------------------------------------------------
# Spacer height
# ------------------------------------------------------------------

def spacer_height_pt(
    el: LayoutElement,
    default_size_pt: float = 10.0,
) -> float:
    """Visual height of a spacer (empty paragraph) in points.

    Determined by its font size and line-spacing rule – the same
    calculation Word uses for an empty paragraph.
    """
    WORD_SINGLE_MULTIPLIER = 1.2

    pf = el.paragraph_format

    font_size = default_size_pt
    if el.spacer_size_half_pt is not None:
        font_size = el.spacer_size_half_pt / 2.0
    else:
        for rf in pf.runs:
            if rf.font_size_half_pt is not None:
                font_size = rf.font_size_half_pt / 2.0
                break

    lv = pf.line_value
    if lv is not None:
        lr = pf.line_rule
        if lr in ("exact", "atLeast"):
            return lv / 20.0
        return font_size * (lv / 240.0) * WORD_SINGLE_MULTIPLIER

    return font_size * WORD_SINGLE_MULTIPLIER


# ------------------------------------------------------------------
# Layout-level defaults
# ------------------------------------------------------------------

def default_font_from_layout(layout: ResumeLayout) -> str:
    """Normalised default font family for the document."""
    df = layout.default_font
    if df.family and df.family.lower() not in ("minorhansi", "minorbidi"):
        return df.family
    for role in ("bullet", "entry_header", "entry_subheader", "contact"):
        style = layout.styles.get(role)
        if style and style.font.family and style.font.family.lower() not in ("minorhansi", "minorbidi"):
            return style.font.family
    return "Arial"


def default_size_from_layout(layout: ResumeLayout) -> float:
    """Default body font size in points."""
    for role in ("bullet", "entry_subheader", "entry_header"):
        style = layout.styles.get(role)
        if style and style.font.size_pt:
            return style.font.size_pt
    return 10.0


def joined_text(runs: list[RunSegment]) -> str:
    """Concatenate run texts, skipping invisible/white runs."""
    parts: list[str] = []
    for r in runs:
        if r.color and r.color.upper() in ("FFFFFF", "WHITE"):
            continue
        parts.append(r.text)
    return "".join(parts).strip()
