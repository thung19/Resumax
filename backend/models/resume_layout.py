"""Resume IR - Layout Schema.

Represents HOW the resume looks, independent of content.
Describes page geometry, reusable styles, and per-element style bindings.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional, Union

from pydantic import BaseModel, Field


# --- Enums ---


class PageSize(str, Enum):
    LETTER = "letter"
    A4 = "a4"


class Alignment(str, Enum):
    LEFT = "left"
    CENTER = "center"
    RIGHT = "right"
    JUSTIFY = "justify"


class TabAlignment(str, Enum):
    LEFT = "left"
    CENTER = "center"
    RIGHT = "right"
    DECIMAL = "decimal"


class TabLeader(str, Enum):
    NONE = "none"
    DOT = "dot"
    DASH = "dash"
    UNDERSCORE = "underscore"


class BorderStyle(str, Enum):
    SINGLE = "single"
    DOUBLE = "double"
    THICK = "thick"
    DOTTED = "dotted"
    DASHED = "dashed"
    NONE = "none"


class LayoutMode(str, Enum):
    """How a row lays out its content."""
    NORMAL = "normal"
    LEFT_RIGHT = "left_right"  # e.g. company on left, location on right
    COLUMNS = "columns"


# --- Sub-models ---


class BorderDef(BaseModel):
    """A border on one or more sides of a paragraph."""
    enabled: bool = False
    style: BorderStyle = BorderStyle.SINGLE
    width_pt: float = 0.75
    color: Optional[str] = None  # hex color
    space_pt: float = 0  # space between text and border


class TabStop(BaseModel):
    """A tab stop definition."""
    position_in: float  # inches from left margin
    alignment: TabAlignment = TabAlignment.LEFT
    leader: TabLeader = TabLeader.NONE


class FontSpec(BaseModel):
    """Font specification with fallback."""
    family: str = "Arial"
    fallback: Optional[str] = None
    size_pt: float = 10.0
    bold: bool = False
    italic: bool = False
    underline: bool = False
    strikethrough: bool = False
    small_caps: bool = False
    color: Optional[str] = None  # hex
    letter_spacing_pt: Optional[float] = None


class SpacingSpec(BaseModel):
    """Paragraph spacing."""
    line_spacing: Optional[float] = None  # multiplier (1.0 = single)
    line_spacing_pt: Optional[float] = None  # exact pt value (overrides multiplier)
    space_before_pt: float = 0
    space_after_pt: float = 0


class IndentSpec(BaseModel):
    """Paragraph indentation."""
    left_in: float = 0
    right_in: float = 0
    first_line_in: float = 0  # positive = first-line indent
    hanging_in: float = 0  # positive = hanging indent


class BulletSpec(BaseModel):
    """Bullet / list formatting."""
    symbol: str = "\u2022"  # bullet character
    font_family: Optional[str] = None
    font_size_pt: Optional[float] = None
    indent_in: float = 0.18
    hanging_in: float = 0.12
    color: Optional[str] = None


class HorizontalRule(BaseModel):
    """A horizontal line (paragraph bottom border, shape, etc.)."""
    width_pt: float = 0.75
    color: Optional[str] = None
    style: BorderStyle = BorderStyle.SINGLE
    # How the rule is implemented in the source doc
    source_type: str = "paragraph_border"  # paragraph_border | shape | table_border


# --- Style definition ---


class StyleDef(BaseModel):
    """A reusable style definition in the layout schema."""
    font: FontSpec = Field(default_factory=FontSpec)
    spacing: SpacingSpec = Field(default_factory=SpacingSpec)
    indent: IndentSpec = Field(default_factory=IndentSpec)
    alignment: Alignment = Alignment.LEFT
    layout_mode: LayoutMode = LayoutMode.NORMAL

    # Borders
    top_border: Optional[BorderDef] = None
    bottom_border: Optional[BorderDef] = None
    left_border: Optional[BorderDef] = None
    right_border: Optional[BorderDef] = None

    # Bullet formatting (only for bullet styles)
    bullet: Optional[BulletSpec] = None

    # Tab stops
    tab_stops: list[TabStop] = Field(default_factory=list)

    # Paragraph control
    keep_with_next: bool = False
    keep_lines_together: bool = False
    page_break_before: bool = False

    # Word style name from source doc (for round-trip fidelity)
    source_word_style: Optional[str] = None


# --- Page setup ---


class PageSetup(BaseModel):
    """Page geometry."""
    size: PageSize = PageSize.LETTER
    width_in: float = 8.5
    height_in: float = 11.0
    margin_top_in: float = 1.0
    margin_bottom_in: float = 1.0
    margin_left_in: float = 1.0
    margin_right_in: float = 1.0
    margin_header_in: float = 0.5
    margin_footer_in: float = 0.5


class HeaderFooter(BaseModel):
    """Header / footer content reference."""
    enabled: bool = False
    content: Optional[str] = None
    style_ref: Optional[str] = None


# --- Element-to-style binding ---


class ElementStyleBinding(BaseModel):
    """Maps a content element (by ID or semantic role) to a style."""
    element_id: Optional[str] = None
    semantic_role: Optional[str] = None  # e.g. "section_heading", "bullet"
    style_ref: str  # key into ResumeLayout.styles


# --- Top-level layout ---


class ResumeLayout(BaseModel):
    """The complete layout schema for a resume."""
    page: PageSetup = Field(default_factory=PageSetup)

    # Named styles: key is the style name (e.g. "section_heading", "bullet")
    styles: dict[str, StyleDef] = Field(default_factory=dict)

    # Bindings from content elements to styles
    bindings: list[ElementStyleBinding] = Field(default_factory=list)

    # Header/footer
    header: HeaderFooter = Field(default_factory=HeaderFooter)
    footer: HeaderFooter = Field(default_factory=HeaderFooter)

    # Global defaults
    default_font: FontSpec = Field(default_factory=FontSpec)

    # Section ordering (list of section IDs in display order)
    section_order: list[str] = Field(default_factory=list)

    # Detected horizontal rules and their positions (section_id they follow)
    horizontal_rules: dict[str, HorizontalRule] = Field(default_factory=dict)
