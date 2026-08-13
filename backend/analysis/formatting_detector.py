"""Formatting Pattern Detector.

Analyzes extracted paragraph formatting to infer a resume's
formatting grammar — reusable style roles and their relationships.

This goes beyond per-paragraph properties to detect patterns like:
- All section headers share the same bold+uppercase+rule style
- Bullets always have zero inter-bullet spacing
- Experiences are separated by additional spacing
- Dates align to the right margin via tabs
- Company/location rows use a specific indent pattern
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Optional

from backend.models.resume_layout import (
    Alignment,
    BorderDef,
    BulletSpec,
    FontSpec,
    HorizontalRule,
    IndentSpec,
    LayoutMode,
    ResumeLayout,
    SpacingSpec,
    StyleDef,
    TabStop,
)


@dataclass
class FormattingSignature:
    """A hashable formatting fingerprint for clustering."""
    font_family: Optional[str] = None
    font_size_pt: Optional[float] = None
    bold: bool = False
    italic: bool = False
    underline: bool = False
    alignment: str = "left"
    has_bullet: bool = False
    has_tabs: bool = False
    has_bottom_border: bool = False
    has_line_shape: bool = False
    is_all_caps: bool = False
    space_before_pt: float = 0
    space_after_pt: float = 0

    def signature_key(self) -> tuple:
        return (
            self.font_family,
            self.font_size_pt,
            self.bold,
            self.italic,
            self.underline,
            self.alignment,
            self.has_bullet,
            self.has_tabs,
            self.has_bottom_border or self.has_line_shape,
            self.is_all_caps,
        )


@dataclass
class DetectedPattern:
    """A detected formatting pattern with its semantic role."""
    role: str  # e.g. "section_heading", "bullet", "entry_header"
    signature: FormattingSignature
    count: int = 0
    example_texts: list[str] = field(default_factory=list)
    paragraph_indices: list[int] = field(default_factory=list)


class FormattingDetector:
    """Detects formatting patterns from a list of ParagraphFormatting objects."""

    def __init__(self, paragraphs: list):
        """Accept a list of ParagraphFormatting objects from the DOCX importer."""
        self._paragraphs = paragraphs
        self._patterns: dict[str, DetectedPattern] = {}
        self._relationships: list[dict] = []

    def detect(self) -> dict:
        """Run full detection pipeline.

        Returns a dict with:
        - patterns: dict of role -> DetectedPattern
        - relationships: list of detected formatting relationships
        - style_summary: human-readable summary
        """
        self._cluster_by_signature()
        self._detect_relationships()
        return {
            "patterns": self._patterns,
            "relationships": self._relationships,
            "style_summary": self._summarize(),
        }

    def _cluster_by_signature(self):
        """Group paragraphs by formatting similarity."""
        clusters: dict[tuple, list[int]] = defaultdict(list)

        for i, p in enumerate(self._paragraphs):
            if p.is_empty:
                continue

            sig = FormattingSignature(
                font_family=p.font_family,
                font_size_pt=p.font_size_pt,
                bold=p.bold,
                italic=p.italic,
                underline=p.underline,
                alignment=p.alignment,
                has_bullet=p.has_numbering or p.text.strip().startswith("•"),
                has_tabs=any(r.get("tab_count", 0) > 0 for r in p.runs),
                has_bottom_border=p.has_bottom_border,
                has_line_shape=p.has_line_shape,
                is_all_caps=p.text.strip().isupper() and len(p.text.strip()) > 2,
                space_before_pt=p.space_before_pt,
                space_after_pt=p.space_after_pt,
            )

            clusters[sig.signature_key()].append(i)

        # Assign roles to clusters
        for sig_key, indices in clusters.items():
            representative = self._paragraphs[indices[0]]
            role = self._infer_role_from_cluster(representative, indices)

            sig = FormattingSignature(
                font_family=representative.font_family,
                font_size_pt=representative.font_size_pt,
                bold=representative.bold,
                italic=representative.italic,
                alignment=representative.alignment,
                has_bullet=representative.has_numbering or representative.text.strip().startswith("•"),
                has_tabs=any(r.get("tab_count", 0) > 0 for r in representative.runs),
                has_bottom_border=representative.has_bottom_border,
                has_line_shape=representative.has_line_shape,
                is_all_caps=representative.text.strip().isupper() and len(representative.text.strip()) > 2,
            )

            pattern = DetectedPattern(
                role=role,
                signature=sig,
                count=len(indices),
                example_texts=[self._paragraphs[i].text.strip()[:80] for i in indices[:3]],
                paragraph_indices=indices,
            )

            # Merge if same role already exists
            if role in self._patterns:
                existing = self._patterns[role]
                existing.count += pattern.count
                existing.paragraph_indices.extend(pattern.paragraph_indices)
            else:
                self._patterns[role] = pattern

    def _infer_role_from_cluster(self, rep, indices: list[int]) -> str:
        """Infer the semantic role of a formatting cluster."""
        text = rep.text.strip()

        # Large bold centered = name
        if rep.font_size_pt and rep.font_size_pt >= 20 and rep.bold and rep.alignment == "center":
            return "name"

        # Centered non-bold = contact
        if rep.alignment == "center" and not rep.bold:
            return "contact"

        # Bold + all caps + (rule or large font) = section heading
        if rep.bold and text.isupper() and len(text) > 2:
            return "section_heading"

        # Bullets
        if rep.has_numbering or text.startswith("•") or text.startswith("-"):
            return "bullet"

        has_tabs = any(r.get("tab_count", 0) > 0 for r in rep.runs)

        # Bold with tabs = entry header (company + date)
        if rep.bold and has_tabs:
            return "entry_header"

        # Bold without tabs = entry header (project name, etc.)
        if rep.bold:
            return "entry_header"

        # Non-bold with tabs = entry subheader (role + location)
        if has_tabs:
            return "entry_subheader"

        # Contains colon = skills/label row
        if ":" in text:
            return "skills_row"

        return "body_text"

    def _detect_relationships(self):
        """Detect formatting relationships between paragraph groups."""
        # Check if section headings consistently have horizontal rules
        section_indices = self._patterns.get("section_heading", DetectedPattern(role="", signature=FormattingSignature())).paragraph_indices
        if section_indices:
            rules_count = sum(
                1 for i in section_indices
                if self._paragraphs[i].has_line_shape or self._paragraphs[i].has_bottom_border
            )
            if rules_count == len(section_indices):
                self._relationships.append({
                    "type": "section_headings_have_rules",
                    "description": "All section headings have horizontal rules",
                })
            elif rules_count > 0:
                self._relationships.append({
                    "type": "some_section_headings_have_rules",
                    "description": f"{rules_count}/{len(section_indices)} section headings have rules",
                })

        # Check bullet spacing consistency
        bullet_indices = self._patterns.get("bullet", DetectedPattern(role="", signature=FormattingSignature())).paragraph_indices
        if len(bullet_indices) >= 2:
            spacings = set()
            for i in bullet_indices:
                p = self._paragraphs[i]
                spacings.add((p.space_before_pt, p.space_after_pt))
            if len(spacings) == 1:
                sb, sa = spacings.pop()
                self._relationships.append({
                    "type": "uniform_bullet_spacing",
                    "description": f"All bullets: space_before={sb}pt, space_after={sa}pt",
                })

        # Check if entry headers have consistent tab-based date alignment
        entry_indices = self._patterns.get("entry_header", DetectedPattern(role="", signature=FormattingSignature())).paragraph_indices
        if entry_indices:
            tab_counts = []
            for i in entry_indices:
                tc = sum(r.get("tab_count", 0) for r in self._paragraphs[i].runs)
                tab_counts.append(tc)
            if all(tc > 0 for tc in tab_counts):
                self._relationships.append({
                    "type": "entry_headers_use_tabs",
                    "description": "Entry headers use tab-based date alignment",
                })

        # Detect spacing between experience groups
        if len(entry_indices) >= 2:
            inter_entry_spacings = []
            for idx in range(1, len(entry_indices)):
                curr_i = entry_indices[idx]
                # Check if paragraph before entry header is a spacer
                if curr_i > 0:
                    prev = self._paragraphs[curr_i - 1]
                    if prev.is_empty and prev.font_size_pt:
                        inter_entry_spacings.append(prev.font_size_pt)

            if inter_entry_spacings:
                self._relationships.append({
                    "type": "inter_entry_spacing",
                    "description": f"Entries separated by spacer paragraphs (sizes: {set(inter_entry_spacings)})",
                })

    def _summarize(self) -> list[str]:
        """Generate a human-readable summary of detected patterns."""
        lines = []
        for role, pattern in sorted(self._patterns.items()):
            sig = pattern.signature
            desc_parts = []
            if sig.font_family:
                desc_parts.append(sig.font_family)
            if sig.font_size_pt:
                desc_parts.append(f"{sig.font_size_pt}pt")
            if sig.bold:
                desc_parts.append("bold")
            if sig.italic:
                desc_parts.append("italic")
            if sig.is_all_caps:
                desc_parts.append("ALL_CAPS")
            if sig.alignment != "left":
                desc_parts.append(sig.alignment)
            if sig.has_tabs:
                desc_parts.append("tabs")
            if sig.has_bottom_border or sig.has_line_shape:
                desc_parts.append("rule")

            desc = ", ".join(desc_parts) if desc_parts else "default"
            lines.append(f"{role} ({pattern.count}x): {desc}")
            for ex in pattern.example_texts[:2]:
                lines.append(f"  e.g. \"{ex}\"")

        for rel in self._relationships:
            lines.append(f"[relationship] {rel['description']}")

        return lines

    def refine_layout(self, layout: ResumeLayout) -> ResumeLayout:
        """Refine a ResumeLayout using detected patterns.

        Updates style definitions to reflect true formatting grammar.
        """
        # For each detected pattern, ensure there's a style in the layout
        for role, pattern in self._patterns.items():
            sig = pattern.signature
            if role not in layout.styles:
                # Create a style from the pattern
                style = StyleDef(
                    font=FontSpec(
                        family=sig.font_family or layout.default_font.family,
                        size_pt=sig.font_size_pt or layout.default_font.size_pt,
                        bold=sig.bold,
                        italic=sig.italic,
                        underline=sig.underline,
                    ),
                    alignment=_str_to_alignment(sig.alignment),
                    spacing=SpacingSpec(
                        space_before_pt=sig.space_before_pt,
                        space_after_pt=sig.space_after_pt,
                    ),
                )
                if sig.has_tabs:
                    style.layout_mode = LayoutMode.LEFT_RIGHT
                if sig.has_bullet:
                    style.bullet = BulletSpec()
                layout.styles[role] = style

        return layout


def _str_to_alignment(s: str) -> Alignment:
    mapping = {
        "center": Alignment.CENTER,
        "right": Alignment.RIGHT,
        "justify": Alignment.JUSTIFY,
    }
    return mapping.get(s, Alignment.LEFT)
