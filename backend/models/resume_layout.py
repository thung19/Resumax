"""Resume IR - Layout Schema.

Represents HOW the resume looks, independent of content.
Stores exact per-element formatting captured from the source document
so the renderer can reproduce it faithfully.
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
    NORMAL = "normal"
    LEFT_RIGHT = "left_right"
    COLUMNS = "columns"


# --- Low-level formatting models ---

class BorderDef(BaseModel):
    enabled: bool = False
    style: BorderStyle = BorderStyle.SINGLE
    width_pt: float = 0.75
    color: Optional[str] = None
    space_pt: float = 0


class TabStop(BaseModel):
    position_twips: int = 0
    alignment: TabAlignment = TabAlignment.LEFT
    leader: TabLeader = TabLeader.NONE


class FontSpec(BaseModel):
    family: str = "Arial"
    size_pt: float = 10.0
    bold: bool = False
    italic: bool = False
    underline: bool = False
    strikethrough: bool = False
    small_caps: bool = False
    color: Optional[str] = None


class SpacingSpec(BaseModel):
    line_spacing: Optional[float] = None
    line_spacing_pt: Optional[float] = None
    space_before_pt: float = 0
    space_after_pt: float = 0


class IndentSpec(BaseModel):
    left_in: float = 0
    right_in: float = 0
    first_line_in: float = 0
    hanging_in: float = 0


class BulletSpec(BaseModel):
    symbol: str = "\u2022"
    font_family: Optional[str] = None
    font_size_pt: Optional[float] = None
    indent_in: float = 0.18
    hanging_in: float = 0.12
    color: Optional[str] = None


class HorizontalRule(BaseModel):
    width_pt: float = 0.75
    color: Optional[str] = None
    style: BorderStyle = BorderStyle.SINGLE


class NormalStyle(BaseModel):
    """Word Normal style properties extracted from the source document.

    Controls what empty paragraphs and unstyled text inherit.
    """
    font_family: str = "Arial"
    font_size_pt: float = 11.0
    line_spacing_val: int = 276  # w:line value (240 = 1.0x, 276 = 1.15x)
    line_rule: str = "auto"
    space_before_twips: int = 0
    space_after_twips: int = 0
    source_type: str = "paragraph_border"


# --- Per-run formatting (exact capture) ---

class RunFormat(BaseModel):
    """Exact formatting for a single run in a paragraph."""
    text: str = ""
    is_tab: bool = False
    font_family: Optional[str] = None
    font_size_half_pt: Optional[int] = None  # Word's native half-point unit
    bold: Optional[bool] = None
    bold_cs: Optional[bool] = None  # complex script bold
    italic: Optional[bool] = None
    italic_cs: Optional[bool] = None
    underline: Optional[str] = None
    color: Optional[str] = None
    small_caps: bool = False
    # Hyperlink
    hyperlink_url: Optional[str] = None


# --- Per-paragraph formatting (exact capture) ---

class ParagraphFormat(BaseModel):
    """Exact formatting for a single paragraph as captured from the source."""
    # Spacing
    line_value: Optional[int] = None  # twips (240 = single)
    line_rule: Optional[str] = None  # "auto", "exact", "atLeast"
    space_before_twips: Optional[int] = None
    space_after_twips: Optional[int] = None

    # Indentation (twips)
    indent_left_twips: Optional[int] = None
    indent_right_twips: Optional[int] = None
    indent_hanging_twips: Optional[int] = None
    indent_first_line_twips: Optional[int] = None

    # Alignment
    alignment: Optional[str] = None  # "left", "center", "right", "both"

    # Tab stops
    tab_stops: list[TabStop] = Field(default_factory=list)

    # Borders
    bottom_border: Optional[BorderDef] = None

    # Drawing/shape
    has_drawing: bool = False
    drawing_type: Optional[str] = None  # "line", "image", etc.
    drawing_width_emu: Optional[int] = None
    drawing_height_emu: Optional[int] = None

    # Style reference
    word_style: Optional[str] = None

    # Keep with next / keep lines
    keep_with_next: bool = False
    keep_lines: bool = False

    # Paragraph-level default run properties (pPr/rPr)
    # These control what unstyled/empty parts of the paragraph inherit.
    # Different from individual run formatting.
    ppr_font_size_half_pt: Optional[int] = None
    ppr_font_family: Optional[str] = None

    # Runs (exact content + formatting)
    runs: list[RunFormat] = Field(default_factory=list)


# --- Layout element types ---

class ElementType(str, Enum):
    NAME = "name"
    CONTACT = "contact"
    SPACER = "spacer"
    SECTION_HEADING = "section_heading"
    ENTRY_HEADER = "entry_header"       # company+date or project name
    ENTRY_SUBHEADER = "entry_subheader"  # role+location
    BULLET = "bullet"
    SKILLS_ROW = "skills_row"
    RAW = "raw"


class LayoutElement(BaseModel):
    """A single element in the document layout sequence.

    Links a content element to its exact paragraph formatting.
    The renderer walks this sequence to reproduce the document.
    """
    element_type: ElementType
    content_id: Optional[str] = None  # links to content schema element
    paragraph_format: ParagraphFormat = Field(default_factory=ParagraphFormat)
    # For spacers: the font size that controls the gap height
    spacer_size_half_pt: Optional[int] = None


# --- Page setup ---

class PageSetup(BaseModel):
    size: PageSize = PageSize.LETTER
    width_in: float = 8.5
    height_in: float = 11.0
    margin_top_in: float = 1.0
    margin_bottom_in: float = 1.0
    margin_left_in: float = 1.0
    margin_right_in: float = 1.0
    margin_header_in: float = 0.5
    margin_footer_in: float = 0.5


# --- Style definition (kept for pattern detection) ---

class StyleDef(BaseModel):
    font: FontSpec = Field(default_factory=FontSpec)
    spacing: SpacingSpec = Field(default_factory=SpacingSpec)
    indent: IndentSpec = Field(default_factory=IndentSpec)
    alignment: Alignment = Alignment.LEFT
    layout_mode: LayoutMode = LayoutMode.NORMAL
    bottom_border: Optional[BorderDef] = None
    bullet: Optional[BulletSpec] = None
    tab_stops: list[TabStop] = Field(default_factory=list)
    keep_with_next: bool = False
    keep_lines_together: bool = False
    source_word_style: Optional[str] = None


# --- Top-level layout ---

class ResumeLayout(BaseModel):
    """The complete layout schema for a resume."""
    page: PageSetup = Field(default_factory=PageSetup)

    # Named styles (from pattern detection)
    styles: dict[str, StyleDef] = Field(default_factory=dict)

    # Global defaults
    default_font: FontSpec = Field(default_factory=FontSpec)

    # Word Normal style (extracted from source document)
    normal_style: NormalStyle = Field(default_factory=NormalStyle)

    # Body font: the actual font used for content (bullets, entries)
    # as opposed to default_font which is docDefaults (often a theme)
    body_font_family: str = ""
    body_font_size_pt: float = 10.0
    body_line_spacing_val: int = 240  # w:line value for content paragraphs

    # Normalized spacer size (most common across all spacer elements)
    spacer_size_half_pt: int = 10  # 5pt default

    # Horizontal rules
    horizontal_rules: dict[str, HorizontalRule] = Field(default_factory=dict)

    # === NEW: Exact document element sequence ===
    # This is the ordered list of every paragraph in the document
    # with its exact formatting. The renderer walks this to reproduce
    # the document faithfully.
    elements: list[LayoutElement] = Field(default_factory=list)

    # Hyperlink targets: relationship_id -> URL
    hyperlinks: dict[str, str] = Field(default_factory=dict)
