"""Resume IR - Combined container.

Joins the Content Schema and Layout Schema with metadata
about source format, fidelity level, and parse diagnostics.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field

from .resume_content import ResumeContent
from .resume_layout import ResumeLayout


class SourceFormat(str, Enum):
    DOCX = "docx"
    PDF = "pdf"
    TEXT = "text"
    MANUAL = "manual"


class FormatFidelity(str, Enum):
    EXACT_OR_NEAR_EXACT = "exact_or_near_exact"  # DOCX source
    RECONSTRUCTED = "reconstructed"  # PDF source
    MINIMAL = "minimal"  # plain text source


class ParseDiagnostic(BaseModel):
    """A warning or note from the import process."""
    level: str = "info"  # info | warning | error
    component: str = ""  # e.g. "docx_importer", "formatting_detector"
    message: str = ""
    element_id: Optional[str] = None


class ResumeIR(BaseModel):
    """Top-level Resume Intermediate Representation.

    Content and Layout are independent. The LLM modifies Content.
    Rendering code reads Layout. They connect through element IDs
    and style bindings.
    """
    content: ResumeContent = Field(default_factory=ResumeContent)
    layout: ResumeLayout = Field(default_factory=ResumeLayout)

    # Metadata
    source_format: SourceFormat = SourceFormat.DOCX
    format_fidelity: FormatFidelity = FormatFidelity.EXACT_OR_NEAR_EXACT
    source_filename: Optional[str] = None

    # Parse diagnostics
    diagnostics: list[ParseDiagnostic] = Field(default_factory=list)
