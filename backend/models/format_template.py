"""Format Template model.

Captures the formatting style of a resume (fonts, sizes, spacing,
margins, spacer structure) without any content. Users can save a
template from an uploaded resume and apply it to future documents.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

from backend.models.resume_layout import NormalStyle, PageSetup


class StyleEntry(BaseModel):
    """Formatting for one semantic role (bullet, heading, etc.)."""
    font_family: str = "Garamond"
    font_size_pt: float = 10.0
    bold: bool = False
    italic: bool = False
    line_spacing: Optional[float] = 1.0
    indent_left_in: float = 0.0


class FormatTemplate(BaseModel):
    """A reusable formatting template extracted from a resume."""
    name: str = ""
    description: str = ""

    # Page layout
    page: PageSetup = Field(default_factory=PageSetup)

    # Word Normal style
    normal_style: NormalStyle = Field(default_factory=NormalStyle)

    # Body content defaults
    body_font_family: str = "Garamond"
    body_font_size_pt: float = 10.0
    body_line_spacing_val: int = 240  # 240 = 1.0x

    # Spacer (blank line) size
    spacer_size_half_pt: int = 10  # 5pt

    # Per-role styles
    styles: dict[str, StyleEntry] = Field(default_factory=dict)

    # Section heading decoration
    heading_has_border: bool = False
    heading_border_width_pt: float = 0.75
