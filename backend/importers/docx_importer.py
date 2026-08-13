"""DOCX Importer.

Extracts resume content and formatting from .docx files using both
python-docx and direct OOXML (lxml) inspection for full fidelity.

Returns a ResumeIR with format_fidelity = exact_or_near_exact.
"""

from __future__ import annotations

import re
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Optional, Union

from docx import Document
from docx.oxml.ns import qn
from lxml import etree

from backend.models.resume_content import (
    Bullet,
    ContactInfo,
    EducationEntry,
    ExperienceEntry,
    GenericEntry,
    ProjectEntry,
    ResumeContent,
    ResumeSection,
    SectionType,
    SkillCategory,
)
from backend.models.resume_ir import (
    FormatFidelity,
    ParseDiagnostic,
    ResumeIR,
    SourceFormat,
)
from backend.models.resume_layout import (
    Alignment,
    BorderDef,
    BorderStyle,
    BulletSpec,
    FontSpec,
    HorizontalRule,
    IndentSpec,
    LayoutMode,
    PageSetup,
    ResumeLayout,
    SpacingSpec,
    StyleDef,
    TabAlignment,
    TabStop,
)

# OOXML namespaces
WML = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
WP = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
WPS = "http://schemas.microsoft.com/office/word/2010/wordprocessingShape"
VML = "urn:schemas-microsoft-com:vml"
MC = "http://schemas.openxmlformats.org/markup-compatibility/2006"

NS = {
    "w": WML,
    "wp": WP,
    "a": A,
    "wps": WPS,
    "v": VML,
    "mc": MC,
}


# --- Helpers ---


def _val(el: etree._Element, attr: str = "val") -> Optional[str]:
    """Get w:val attribute from an element."""
    return el.get(qn(f"w:{attr}")) if el is not None else None


def _int_val(el: etree._Element, attr: str = "val") -> Optional[int]:
    v = _val(el, attr)
    return int(v) if v is not None else None


def _half_pt_to_pt(half_pts: Optional[int]) -> Optional[float]:
    """Convert Word half-points to points."""
    return half_pts / 2.0 if half_pts is not None else None


def _twips_to_in(twips: Optional[int]) -> Optional[float]:
    """Convert twips (1/1440 inch) to inches."""
    return twips / 1440.0 if twips is not None else None


def _emu_to_in(emu: Optional[int]) -> Optional[float]:
    """Convert EMU (1/914400 inch) to inches."""
    return emu / 914400.0 if emu is not None else None


def _twips_to_pt(twips: Optional[int]) -> Optional[float]:
    """Convert twips (1/20 pt) to points."""
    return twips / 20.0 if twips is not None else None


# --- Paragraph-level formatting extraction ---


class ParagraphFormatting:
    """Extracted formatting for a single paragraph."""

    def __init__(self):
        self.font_family: Optional[str] = None
        self.font_size_pt: Optional[float] = None
        self.bold: bool = False
        self.italic: bool = False
        self.underline: bool = False
        self.small_caps: bool = False
        self.color: Optional[str] = None
        self.alignment: str = "left"
        self.space_before_pt: float = 0
        self.space_after_pt: float = 0
        self.line_spacing: Optional[float] = None
        self.line_spacing_pt: Optional[float] = None
        self.left_indent_in: float = 0
        self.right_indent_in: float = 0
        self.hanging_indent_in: float = 0
        self.first_line_indent_in: float = 0
        self.has_numbering: bool = False
        self.num_id: Optional[int] = None
        self.ilvl: int = 0
        self.tab_stops: list[TabStop] = []
        self.keep_with_next: bool = False
        self.keep_lines_together: bool = False
        self.has_bottom_border: bool = False
        self.bottom_border_width_pt: float = 0
        self.bottom_border_color: Optional[str] = None
        self.has_drawing: bool = False
        self.has_line_shape: bool = False
        self.text: str = ""
        self.visible_text: str = ""  # text excluding white/invisible runs
        self.runs: list[dict] = []  # [{text, bold, italic, font, size_pt, color}]
        self.word_style_name: Optional[str] = None
        self.is_empty: bool = True


def _extract_run_formatting(r_elem: etree._Element) -> dict:
    """Extract formatting from a single w:r element."""
    rPr = r_elem.find(qn("w:rPr"))
    run_info = {
        "text": "",
        "bold": False,
        "italic": False,
        "underline": False,
        "font_family": None,
        "font_size_pt": None,
        "color": None,
        "small_caps": False,
    }

    # Collect text from w:t elements
    for t in r_elem.findall(qn("w:t")):
        run_info["text"] += t.text or ""

    # Count tabs
    run_info["tab_count"] = len(r_elem.findall(qn("w:tab")))

    if rPr is None:
        return run_info

    # Font
    rFonts = rPr.find(qn("w:rFonts"))
    if rFonts is not None:
        run_info["font_family"] = (
            rFonts.get(qn("w:ascii"))
            or rFonts.get(qn("w:hAnsi"))
            or rFonts.get(qn("w:cs"))
        )

    # Size
    sz = rPr.find(qn("w:sz"))
    if sz is not None:
        v = _int_val(sz)
        run_info["font_size_pt"] = _half_pt_to_pt(v)

    # Bold
    b = rPr.find(qn("w:b"))
    if b is not None:
        bval = _val(b)
        run_info["bold"] = bval != "0" and bval != "false"

    # Italic
    i = rPr.find(qn("w:i"))
    if i is not None:
        ival = _val(i)
        run_info["italic"] = ival != "0" and ival != "false"

    # Underline
    u = rPr.find(qn("w:u"))
    if u is not None:
        uval = _val(u)
        run_info["underline"] = uval is not None and uval != "none"

    # Color
    color = rPr.find(qn("w:color"))
    if color is not None:
        run_info["color"] = _val(color)

    # Small caps
    smallCaps = rPr.find(qn("w:smallCaps"))
    if smallCaps is not None:
        run_info["small_caps"] = True

    return run_info


def _extract_paragraph_formatting(
    p_elem: etree._Element,
    numbering_map: dict,
) -> ParagraphFormatting:
    """Extract all formatting from a paragraph element via direct OOXML."""
    fmt = ParagraphFormatting()

    pPr = p_elem.find(qn("w:pPr"))

    # --- Paragraph-level properties ---
    if pPr is not None:
        # Style name
        pStyle = pPr.find(qn("w:pStyle"))
        if pStyle is not None:
            fmt.word_style_name = _val(pStyle)

        # Alignment
        jc = pPr.find(qn("w:jc"))
        if jc is not None:
            fmt.alignment = _val(jc) or "left"
            if fmt.alignment == "both":
                fmt.alignment = "justify"

        # Spacing
        spacing = pPr.find(qn("w:spacing"))
        if spacing is not None:
            before = spacing.get(qn("w:before"))
            if before:
                fmt.space_before_pt = _twips_to_pt(int(before)) or 0

            after = spacing.get(qn("w:after"))
            if after:
                fmt.space_after_pt = _twips_to_pt(int(after)) or 0

            line = spacing.get(qn("w:line"))
            line_rule = spacing.get(qn("w:lineRule"))
            if line:
                line_val = int(line)
                if line_rule == "exact" or line_rule == "atLeast":
                    fmt.line_spacing_pt = _twips_to_pt(line_val)
                else:
                    # Default: line spacing as proportion (240 = single)
                    fmt.line_spacing = line_val / 240.0

        # Indentation
        ind = pPr.find(qn("w:ind"))
        if ind is not None:
            left = ind.get(qn("w:left"))
            if left:
                fmt.left_indent_in = _twips_to_in(int(left)) or 0

            right = ind.get(qn("w:right"))
            if right:
                fmt.right_indent_in = _twips_to_in(int(right)) or 0

            hanging = ind.get(qn("w:hanging"))
            if hanging:
                fmt.hanging_indent_in = _twips_to_in(int(hanging)) or 0

            firstLine = ind.get(qn("w:firstLine"))
            if firstLine:
                fmt.first_line_indent_in = _twips_to_in(int(firstLine)) or 0

        # Numbering (bullets/lists)
        numPr = pPr.find(qn("w:numPr"))
        if numPr is not None:
            numId = numPr.find(qn("w:numId"))
            ilvl = numPr.find(qn("w:ilvl"))
            if numId is not None:
                fmt.has_numbering = True
                fmt.num_id = _int_val(numId)
                fmt.ilvl = _int_val(ilvl) or 0

        # Tab stops
        tabs = pPr.find(qn("w:tabs"))
        if tabs is not None:
            for tab in tabs.findall(qn("w:tab")):
                pos = tab.get(qn("w:pos"))
                val = tab.get(qn("w:val"))
                leader = tab.get(qn("w:leader"))
                if pos:
                    tab_align = TabAlignment.LEFT
                    if val == "right":
                        tab_align = TabAlignment.RIGHT
                    elif val == "center":
                        tab_align = TabAlignment.CENTER
                    elif val == "decimal":
                        tab_align = TabAlignment.DECIMAL

                    fmt.tab_stops.append(
                        TabStop(
                            position_in=_twips_to_in(int(pos)) or 0,
                            alignment=tab_align,
                        )
                    )

        # Paragraph borders
        pBdr = pPr.find(qn("w:pBdr"))
        if pBdr is not None:
            bottom = pBdr.find(qn("w:bottom"))
            if bottom is not None:
                bval = bottom.get(qn("w:val"))
                if bval and bval != "none":
                    fmt.has_bottom_border = True
                    sz = bottom.get(qn("w:sz"))
                    fmt.bottom_border_width_pt = (
                        int(sz) / 8.0 if sz else 0.75
                    )
                    fmt.bottom_border_color = bottom.get(qn("w:color"))

        # Keep with next
        kwn = pPr.find(qn("w:keepNext"))
        if kwn is not None:
            fmt.keep_with_next = True

        # Keep lines
        klt = pPr.find(qn("w:keepLines"))
        if klt is not None:
            fmt.keep_lines_together = True

    # --- Runs ---
    runs = []
    full_text_parts = []
    for r in p_elem.findall(qn("w:r")):
        run_info = _extract_run_formatting(r)
        runs.append(run_info)
        full_text_parts.append(run_info["text"])

    fmt.runs = runs
    fmt.text = "".join(full_text_parts).strip()
    fmt.is_empty = len(fmt.text) == 0

    # Build "visible text" — excluding runs with white/invisible color
    visible_parts = []
    for run in runs:
        color = run.get("color", "")
        # Skip white-colored text (commonly used as invisible anchors)
        if color and color.upper() in ("FFFFFF", "WHITE"):
            continue
        visible_parts.append(run.get("text", ""))
    fmt.visible_text = "".join(visible_parts).strip()

    # Determine dominant run formatting for the paragraph
    if runs:
        # Use first non-empty VISIBLE run's formatting as paragraph default
        for run in runs:
            color = run.get("color", "")
            if color and color.upper() in ("FFFFFF", "WHITE"):
                continue
            if run["text"].strip():
                fmt.font_family = run.get("font_family")
                fmt.font_size_pt = run.get("font_size_pt")
                fmt.bold = run.get("bold", False)
                fmt.italic = run.get("italic", False)
                fmt.underline = run.get("underline", False)
                fmt.color = run.get("color")
                fmt.small_caps = run.get("small_caps", False)
                break

    # --- Detect shapes / drawings (horizontal lines) ---
    for drawing in p_elem.findall(f".//{qn('w:drawing')}"):
        fmt.has_drawing = True
        # Check if it's a line shape
        for wsp in drawing.findall(f".//{{{WPS}}}wsp"):
            spPr = wsp.find(f"{{{WPS}}}spPr")
            if spPr is None:
                spPr = wsp.find(f"{{{A}}}spPr")
            if spPr is None:
                # Try nested
                spPr = wsp.find(f".//{{{A}}}prstGeom")
                if spPr is not None and spPr.get("prst") == "line":
                    fmt.has_line_shape = True
            else:
                prstGeom = spPr.find(f"{{{A}}}prstGeom")
                if prstGeom is not None and prstGeom.get("prst") == "line":
                    fmt.has_line_shape = True

    # Also check AlternateContent for VML fallback lines
    for ac in p_elem.findall(f".//{{{MC}}}AlternateContent"):
        for fb in ac.findall(f".//{{{MC}}}Fallback"):
            for line_el in fb.findall(f".//{{{VML}}}line"):
                fmt.has_line_shape = True

    return fmt


# --- Numbering / bullet extraction ---


def _parse_numbering(docx_bytes: bytes) -> dict:
    """Parse numbering.xml to extract bullet/list definitions.

    Returns a map of numId -> {ilvl -> {symbol, indent, hanging, font}}.
    """
    numbering_map: dict = {}
    try:
        with zipfile.ZipFile(BytesIO(docx_bytes), "r") as z:
            if "word/numbering.xml" not in z.namelist():
                return numbering_map
            xml = z.read("word/numbering.xml")
    except Exception:
        return numbering_map

    root = etree.fromstring(xml)

    # Build abstractNum map
    abstract_nums: dict = {}
    for an in root.findall(qn("w:abstractNum")):
        abstract_id = an.get(qn("w:abstractNumId"))
        levels: dict = {}
        for lvl in an.findall(qn("w:lvl")):
            ilvl = int(lvl.get(qn("w:ilvl"), "0"))
            numFmt = lvl.find(qn("w:numFmt"))
            lvlText = lvl.find(qn("w:lvlText"))
            pPr = lvl.find(qn("w:pPr"))
            rPr = lvl.find(qn("w:rPr"))

            level_info: dict = {
                "format": _val(numFmt) if numFmt is not None else "bullet",
                "text": _val(lvlText) if lvlText is not None else "",
                "indent_in": 0,
                "hanging_in": 0,
                "font": None,
            }

            if pPr is not None:
                ind = pPr.find(qn("w:ind"))
                if ind is not None:
                    left = ind.get(qn("w:left"))
                    if left:
                        level_info["indent_in"] = _twips_to_in(int(left)) or 0
                    hanging = ind.get(qn("w:hanging"))
                    if hanging:
                        level_info["hanging_in"] = _twips_to_in(int(hanging)) or 0

            if rPr is not None:
                rFonts = rPr.find(qn("w:rFonts"))
                if rFonts is not None:
                    level_info["font"] = (
                        rFonts.get(qn("w:ascii"))
                        or rFonts.get(qn("w:hAnsi"))
                    )

            levels[ilvl] = level_info
        abstract_nums[abstract_id] = levels

    # Build numId -> abstractNumId mapping
    for num in root.findall(qn("w:num")):
        num_id = int(num.get(qn("w:numId"), "0"))
        abstractNumId_el = num.find(qn("w:abstractNumId"))
        if abstractNumId_el is not None:
            abstract_id = _val(abstractNumId_el)
            if abstract_id in abstract_nums:
                numbering_map[num_id] = abstract_nums[abstract_id]

    return numbering_map


# --- Page setup extraction ---


def _extract_page_setup(docx_bytes: bytes) -> PageSetup:
    """Extract page dimensions and margins from document.xml sectPr."""
    page = PageSetup()
    try:
        with zipfile.ZipFile(BytesIO(docx_bytes), "r") as z:
            xml = z.read("word/document.xml")
    except Exception:
        return page

    root = etree.fromstring(xml)
    body = root.find(qn("w:body"))
    if body is None:
        return page

    sectPr = body.find(qn("w:sectPr"))
    if sectPr is None:
        return page

    pgSz = sectPr.find(qn("w:pgSz"))
    if pgSz is not None:
        w = pgSz.get(qn("w:w"))
        h = pgSz.get(qn("w:h"))
        if w:
            page.width_in = _twips_to_in(int(w)) or 8.5
        if h:
            page.height_in = _twips_to_in(int(h)) or 11.0

    pgMar = sectPr.find(qn("w:pgMar"))
    if pgMar is not None:
        for attr, field in [
            ("top", "margin_top_in"),
            ("bottom", "margin_bottom_in"),
            ("left", "margin_left_in"),
            ("right", "margin_right_in"),
            ("header", "margin_header_in"),
            ("footer", "margin_footer_in"),
        ]:
            v = pgMar.get(qn(f"w:{attr}"))
            if v:
                setattr(page, field, _twips_to_in(int(v)) or getattr(page, field))

    return page


# --- Style inheritance from styles.xml ---


def _parse_styles_xml(docx_bytes: bytes) -> dict[str, dict]:
    """Parse styles.xml to get default/named style definitions.

    Returns {style_id: {font, size_pt, bold, italic, ...}}.
    """
    styles_map: dict = {}
    try:
        with zipfile.ZipFile(BytesIO(docx_bytes), "r") as z:
            if "word/styles.xml" not in z.namelist():
                return styles_map
            xml = z.read("word/styles.xml")
    except Exception:
        return styles_map

    root = etree.fromstring(xml)

    # Document defaults
    docDefaults = root.find(qn("w:docDefaults"))
    default_font = None
    default_size = None
    if docDefaults is not None:
        rPrDefault = docDefaults.find(qn("w:rPrDefault"))
        if rPrDefault is not None:
            rPr = rPrDefault.find(qn("w:rPr"))
            if rPr is not None:
                rFonts = rPr.find(qn("w:rFonts"))
                if rFonts is not None:
                    default_font = (
                        rFonts.get(qn("w:ascii"))
                        or rFonts.get(qn("w:asciiTheme"))
                    )
                sz = rPr.find(qn("w:sz"))
                if sz is not None:
                    default_size = _half_pt_to_pt(_int_val(sz))

    styles_map["__defaults__"] = {
        "font": default_font,
        "size_pt": default_size,
    }

    for style in root.findall(qn("w:style")):
        style_id = style.get(qn("w:styleId"))
        if not style_id:
            continue

        info: dict = {"basedOn": None, "font": None, "size_pt": None,
                       "bold": None, "italic": None}

        basedOn = style.find(qn("w:basedOn"))
        if basedOn is not None:
            info["basedOn"] = _val(basedOn)

        # Name
        name_el = style.find(qn("w:name"))
        if name_el is not None:
            info["name"] = _val(name_el)

        rPr = style.find(qn("w:rPr"))
        if rPr is not None:
            rFonts = rPr.find(qn("w:rFonts"))
            if rFonts is not None:
                info["font"] = (
                    rFonts.get(qn("w:ascii"))
                    or rFonts.get(qn("w:hAnsi"))
                )
            sz = rPr.find(qn("w:sz"))
            if sz is not None:
                info["size_pt"] = _half_pt_to_pt(_int_val(sz))
            b = rPr.find(qn("w:b"))
            if b is not None:
                info["bold"] = _val(b) != "0"
            i_el = rPr.find(qn("w:i"))
            if i_el is not None:
                info["italic"] = _val(i_el) != "0"

        # Paragraph-level style properties
        pPr = style.find(qn("w:pPr"))
        if pPr is not None:
            spacing = pPr.find(qn("w:spacing"))
            if spacing is not None:
                before = spacing.get(qn("w:before"))
                after = spacing.get(qn("w:after"))
                if before:
                    info["space_before_pt"] = _twips_to_pt(int(before))
                if after:
                    info["space_after_pt"] = _twips_to_pt(int(after))

        styles_map[style_id] = info

    return styles_map


# --- Main importer ---


class DocxImporter:
    """Import a .docx file into a ResumeIR."""

    def __init__(self, file_path: Optional[str] = None, file_bytes: Optional[bytes] = None):
        if file_bytes is not None:
            self._bytes = file_bytes
        elif file_path is not None:
            self._bytes = Path(file_path).read_bytes()
        else:
            raise ValueError("Provide either file_path or file_bytes")

        self._filename = Path(file_path).name if file_path else "upload.docx"
        self._diagnostics: list[ParseDiagnostic] = []

    def _diag(self, level: str, message: str, element_id: Optional[str] = None):
        self._diagnostics.append(
            ParseDiagnostic(
                level=level,
                component="docx_importer",
                message=message,
                element_id=element_id,
            )
        )

    def import_resume(self) -> ResumeIR:
        """Run the full import pipeline."""
        # Parse supporting structures
        numbering_map = _parse_numbering(self._bytes)
        styles_map = _parse_styles_xml(self._bytes)
        page_setup = _extract_page_setup(self._bytes)

        # Parse paragraphs from document.xml
        with zipfile.ZipFile(BytesIO(self._bytes), "r") as z:
            doc_xml = z.read("word/document.xml")

        root = etree.fromstring(doc_xml)
        body = root.find(qn("w:body"))
        if body is None:
            self._diag("error", "No w:body found in document.xml")
            return ResumeIR(source_filename=self._filename)

        # Extract all paragraphs with formatting
        paragraphs: list[ParagraphFormatting] = []
        for p in body.findall(qn("w:p")):
            fmt = _extract_paragraph_formatting(p, numbering_map)
            paragraphs.append(fmt)

        # Build layout from detected formatting
        layout = self._build_layout(paragraphs, page_setup, numbering_map, styles_map)

        # Build content from paragraph text + structure
        content = self._build_content(paragraphs)

        return ResumeIR(
            content=content,
            layout=layout,
            source_format=SourceFormat.DOCX,
            format_fidelity=FormatFidelity.EXACT_OR_NEAR_EXACT,
            source_filename=self._filename,
            diagnostics=self._diagnostics,
        )

    def _build_layout(
        self,
        paragraphs: list[ParagraphFormatting],
        page_setup: PageSetup,
        numbering_map: dict,
        styles_map: dict,
    ) -> ResumeLayout:
        """Build the Layout Schema from extracted paragraph formatting."""
        layout = ResumeLayout(page=page_setup)

        # Set default font from styles.xml
        defaults = styles_map.get("__defaults__", {})
        if defaults.get("font"):
            layout.default_font.family = defaults["font"]
        if defaults.get("size_pt"):
            layout.default_font.size_pt = defaults["size_pt"]

        # Detect formatting patterns and create named styles
        style_groups = self._cluster_paragraph_styles(paragraphs)

        for style_name, style_fmt in style_groups.items():
            style_def = self._fmt_to_style_def(style_fmt, numbering_map)
            layout.styles[style_name] = style_def

        # Detect horizontal rules (shape-based lines)
        for i, p in enumerate(paragraphs):
            if p.has_line_shape:
                # Find which section this line is associated with
                rule = HorizontalRule(
                    source_type="shape",
                    width_pt=0.75,
                )
                layout.horizontal_rules[f"para_{i}"] = rule
            elif p.has_bottom_border:
                rule = HorizontalRule(
                    source_type="paragraph_border",
                    width_pt=p.bottom_border_width_pt,
                    color=p.bottom_border_color,
                )
                layout.horizontal_rules[f"para_{i}"] = rule

        return layout

    def _cluster_paragraph_styles(
        self, paragraphs: list[ParagraphFormatting]
    ) -> dict[str, ParagraphFormatting]:
        """Group paragraphs by formatting similarity to detect style roles.

        Returns a dict of style_name -> representative ParagraphFormatting.
        """
        styles: dict[str, ParagraphFormatting] = {}

        for p in paragraphs:
            if p.is_empty:
                # Empty paragraphs used as spacers
                if "spacer" not in styles:
                    styles["spacer"] = p
                continue

            role = self._classify_paragraph_role(p)
            if role not in styles:
                styles[role] = p

        return styles

    def _classify_paragraph_role(self, p: ParagraphFormatting) -> str:
        """Classify a paragraph into a semantic formatting role."""
        text = p.text.strip()
        # Use visible_text (excludes white-colored invisible anchors)
        visible = p.visible_text.strip() if p.visible_text else text

        # Name: large, bold, centered
        if (
            p.font_size_pt
            and p.font_size_pt >= 20
            and p.bold
            and p.alignment == "center"
        ):
            return "name"

        # Contact: centered, normal size
        if p.alignment == "center" and not p.bold and p.font_size_pt is None:
            return "contact"
        if p.alignment == "center" and not p.bold and p.font_size_pt and p.font_size_pt < 14:
            return "contact"

        # Section header: bold, all-caps visible text, potentially with line shape
        # Use visible text for uppercase check to handle invisible anchor chars
        if p.bold and visible.isupper() and len(visible) > 2:
            if p.has_line_shape or p.has_bottom_border:
                return "section_heading_with_rule"
            return "section_heading"

        # Bullet: starts with bullet character or has numbering
        if p.has_numbering or text.startswith("•") or text.startswith("-") or text.startswith("–"):
            return "bullet"

        # Skills/label row: "Label: value, value, value" pattern (even if bold)
        # Detect this BEFORE entry_header to avoid misclassifying skills rows
        if ":" in text:
            colon_pos = text.index(":")
            label = text[:colon_pos].strip()
            # Short label (1-3 words) followed by comma-separated values
            if len(label.split()) <= 4 and not any(c.isdigit() for c in label):
                remainder = text[colon_pos + 1:].strip()
                if "," in remainder or len(remainder.split()) >= 2:
                    return "skills_row"

        # Bold text with tabs (company/date row)
        has_tabs = any(r.get("tab_count", 0) > 0 for r in p.runs)
        if p.bold and has_tabs:
            return "entry_header"

        # Bold text without tabs (could be project name or company)
        if p.bold and not p.italic:
            return "entry_header"

        # Non-bold text with tabs (role/location row)
        if not p.bold and has_tabs:
            return "entry_subheader"

        # Italic text
        if p.italic:
            return "entry_subheader"

        # Skills-style: contains colon-separated label:value
        if ":" in text and not text.startswith("•"):
            return "skills_row"

        return "body_text"

    def _fmt_to_style_def(
        self, fmt: ParagraphFormatting, numbering_map: dict
    ) -> StyleDef:
        """Convert a ParagraphFormatting to a StyleDef."""
        font = FontSpec(
            family=fmt.font_family or "Arial",
            size_pt=fmt.font_size_pt or 10.0,
            bold=fmt.bold,
            italic=fmt.italic,
            underline=fmt.underline,
            small_caps=fmt.small_caps,
            color=fmt.color,
        )

        spacing = SpacingSpec(
            line_spacing=fmt.line_spacing,
            line_spacing_pt=fmt.line_spacing_pt,
            space_before_pt=fmt.space_before_pt,
            space_after_pt=fmt.space_after_pt,
        )

        indent = IndentSpec(
            left_in=fmt.left_indent_in,
            right_in=fmt.right_indent_in,
            hanging_in=fmt.hanging_indent_in,
            first_line_in=fmt.first_line_indent_in,
        )

        alignment = Alignment.LEFT
        if fmt.alignment == "center":
            alignment = Alignment.CENTER
        elif fmt.alignment == "right":
            alignment = Alignment.RIGHT
        elif fmt.alignment == "justify":
            alignment = Alignment.JUSTIFY

        # Detect left-right layout
        has_tabs = any(r.get("tab_count", 0) > 0 for r in fmt.runs)
        layout_mode = LayoutMode.LEFT_RIGHT if has_tabs else LayoutMode.NORMAL

        style = StyleDef(
            font=font,
            spacing=spacing,
            indent=indent,
            alignment=alignment,
            layout_mode=layout_mode,
            tab_stops=fmt.tab_stops,
            keep_with_next=fmt.keep_with_next,
            keep_lines_together=fmt.keep_lines_together,
            source_word_style=fmt.word_style_name,
        )

        # Bottom border
        if fmt.has_bottom_border:
            style.bottom_border = BorderDef(
                enabled=True,
                width_pt=fmt.bottom_border_width_pt,
                color=fmt.bottom_border_color,
            )

        # Bullet spec
        if fmt.has_numbering and fmt.num_id is not None:
            num_levels = numbering_map.get(fmt.num_id, {})
            level_info = num_levels.get(fmt.ilvl, {})
            symbol = level_info.get("text", "•")
            # Map Word symbol codes
            if symbol in ("\uf0b7", ""):
                symbol = "•"
            style.bullet = BulletSpec(
                symbol=symbol,
                font_family=level_info.get("font"),
                indent_in=level_info.get("indent_in", 0.25),
                hanging_in=level_info.get("hanging_in", 0.25),
            )

        return style

    def _extract_mixed_entry(self, p: ParagraphFormatting) -> dict:
        """Extract company, role, date, location from a paragraph with mixed bold/non-bold runs.

        Some resumes pack everything into one line:
          [bold] Company Name    Sep 2024 – Present
          [non-bold]         Research Analyst              Nashville, TN
        """
        bold_text_parts = []
        non_bold_text_parts = []

        for run in p.runs:
            text = run.get("text", "")
            if run.get("bold", False):
                bold_text_parts.append(text)
            else:
                non_bold_text_parts.append(text)

        bold_text = "".join(bold_text_parts).strip()
        non_bold_text = "".join(non_bold_text_parts).strip()

        result: dict = {"company": "", "role": "", "start_date": None, "end_date": None, "location": None}

        # Split bold text into company and date
        bold_segments = re.split(r"\s{3,}", bold_text)
        bold_segments = [s.strip() for s in bold_segments if s.strip()]

        if bold_segments:
            result["company"] = bold_segments[0]
            # Check remaining segments for dates
            for seg in bold_segments[1:]:
                if re.search(r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|present|\d{4})", seg, re.I):
                    result["start_date"] = self._extract_start_date(seg)
                    result["end_date"] = self._extract_end_date(seg)

        # Split non-bold text into role and location
        if non_bold_text:
            non_bold_segments = re.split(r"\s{3,}", non_bold_text)
            non_bold_segments = [s.strip() for s in non_bold_segments if s.strip()]
            if non_bold_segments:
                result["role"] = non_bold_segments[0]
                if len(non_bold_segments) >= 2:
                    result["location"] = non_bold_segments[-1]

        return result

    def _has_mixed_bold(self, p: ParagraphFormatting) -> bool:
        """Check if a paragraph has both bold and non-bold content runs."""
        has_bold = False
        has_non_bold = False
        for run in p.runs:
            if not run.get("text", "").strip():
                continue
            # Skip white-colored text
            if run.get("color", ""):
                c = run["color"].upper()
                if c in ("FFFFFF", "WHITE"):
                    continue
            if run.get("bold", False):
                has_bold = True
            else:
                has_non_bold = True
        return has_bold and has_non_bold

    def _is_section_heading(self, p: ParagraphFormatting) -> bool:
        """Check if a paragraph is a section heading."""
        role = self._classify_paragraph_role(p)
        return role in ("section_heading", "section_heading_with_rule")

    def _collect_bullets(
        self, paragraphs: list[ParagraphFormatting], start: int, bullet_counter: int
    ) -> tuple[list[Bullet], int, int]:
        """Collect bullet paragraphs starting at index. Returns (bullets, new_index, bullet_counter)."""
        bullets = []
        i = start
        while i < len(paragraphs):
            bp = paragraphs[i]
            if bp.is_empty:
                i += 1
                continue
            if self._is_section_heading(bp):
                break
            br = self._classify_paragraph_role(bp)
            if br in ("bullet", "body_text", "skills_row"):
                text = bp.text.strip()
                # Only treat as bullet content if it looks like a bullet
                if text.startswith("•") or text.startswith("-") or text.startswith("–") or br == "bullet":
                    bullet_counter += 1
                    text = re.sub(r"^[•\-–]\s*", "", text)
                    bullets.append(Bullet(id=f"bullet_{bullet_counter}", text=text))
                    i += 1
                else:
                    break
            else:
                break
        return bullets, i, bullet_counter

    def _build_content(
        self, paragraphs: list[ParagraphFormatting]
    ) -> ResumeContent:
        """Build the Content Schema from paragraph text."""
        content = ResumeContent()

        sections: list[ResumeSection] = []
        section_counter = 0
        entry_counter = 0
        bullet_counter = 0

        # First pass: find contact info (centered text at top)
        contact_lines: list[str] = []
        body_start = 0
        for i, p in enumerate(paragraphs):
            if p.is_empty:
                continue
            role = self._classify_paragraph_role(p)
            if role == "name":
                content.contact.name = p.text.strip()
                body_start = i + 1
            elif role == "contact":
                contact_lines.append(p.text.strip())
                body_start = i + 1
            else:
                break

        if contact_lines:
            contact_text = " ".join(contact_lines)
            self._parse_contact_line(content.contact, contact_text)

        # Second pass: build sections
        # Use visible_text for section titles to strip invisible anchor chars
        current_section: Optional[ResumeSection] = None
        current_section_type: Optional[SectionType] = None
        i = body_start

        while i < len(paragraphs):
            p = paragraphs[i]

            if p.is_empty:
                i += 1
                continue

            role = self._classify_paragraph_role(p)

            # Section heading — always starts a new section
            if role in ("section_heading", "section_heading_with_rule"):
                if current_section is not None:
                    sections.append(current_section)

                section_counter += 1
                visible = p.visible_text.strip() if p.visible_text else p.text.strip()
                section_type = self._detect_section_type(visible)
                current_section_type = section_type
                current_section = ResumeSection(
                    id=f"section_{section_counter}",
                    type=section_type,
                    title=visible,
                )
                i += 1
                continue

            # Ensure we have a section
            if current_section is None:
                section_counter += 1
                current_section = ResumeSection(
                    id=f"section_{section_counter}",
                    type=SectionType.CUSTOM,
                    title="",
                )
                current_section_type = SectionType.CUSTOM

            # Skills row (in skills section or any section)
            if role == "skills_row":
                text = p.text.strip()
                if ":" in text and current_section_type == SectionType.SKILLS:
                    cat, skills_text = text.split(":", 1)
                    entry_counter += 1
                    current_section.skill_categories.append(
                        SkillCategory(
                            id=f"skill_{entry_counter}",
                            category=cat.strip(),
                            skills=[s.strip() for s in skills_text.split(",") if s.strip()],
                        )
                    )
                elif ":" in text and current_section_type == SectionType.EDUCATION:
                    # Could be coursework under education
                    label, values = text.split(":", 1)
                    if label.strip().lower() in ("coursework", "relevant coursework", "courses"):
                        if current_section.education_entries:
                            current_section.education_entries[-1].coursework = [
                                c.strip() for c in values.split(",") if c.strip()
                            ]
                    else:
                        current_section.raw_lines.append(text)
                else:
                    current_section.raw_lines.append(text)
                i += 1
                continue

            # Entry header (company/project name)
            if role == "entry_header":
                entry_counter += 1
                header_text, right_text = self._split_left_right(p)

                if current_section_type == SectionType.EXPERIENCE or current_section_type == SectionType.VOLUNTEER:
                    # Check for mixed bold/non-bold paragraph (all info in one line)
                    if self._has_mixed_bold(p):
                        mixed = self._extract_mixed_entry(p)
                        entry = ExperienceEntry(
                            id=f"exp_{entry_counter}",
                            company=mixed["company"],
                            role=mixed["role"] or mixed["company"],
                            location=mixed["location"],
                            start_date=mixed["start_date"],
                            end_date=mixed["end_date"],
                        )
                    else:
                        # Look ahead for subheader (role line)
                        role_text = ""
                        location_text = ""
                        next_i = i + 1
                        while next_i < len(paragraphs) and paragraphs[next_i].is_empty:
                            next_i += 1
                        if next_i < len(paragraphs):
                            next_p = paragraphs[next_i]
                            if not self._is_section_heading(next_p):
                                next_role = self._classify_paragraph_role(next_p)
                                if next_role in ("entry_subheader", "body_text"):
                                    role_text, location_text = self._split_left_right(next_p)
                                    i = next_i  # consume the subheader

                        entry = ExperienceEntry(
                            id=f"exp_{entry_counter}",
                            company=header_text,
                            role=role_text or header_text,
                            location=location_text or None,
                            start_date=self._extract_start_date(right_text),
                            end_date=self._extract_end_date(right_text),
                        )

                    bullets, i, bullet_counter = self._collect_bullets(paragraphs, i + 1, bullet_counter)
                    entry.bullets = bullets
                    current_section.experience_entries.append(entry)
                    continue

                elif current_section_type == SectionType.EDUCATION:
                    # Look ahead for degree line
                    degree_text = ""
                    location_text = ""
                    next_i = i + 1
                    while next_i < len(paragraphs) and paragraphs[next_i].is_empty:
                        next_i += 1
                    if next_i < len(paragraphs):
                        next_p = paragraphs[next_i]
                        if not self._is_section_heading(next_p):
                            next_role = self._classify_paragraph_role(next_p)
                            if next_role not in ("entry_header",):
                                degree_text, location_text = self._split_left_right(next_p)
                                i = next_i

                    entry = EducationEntry(
                        id=f"edu_{entry_counter}",
                        institution=header_text,
                        location=location_text or None,
                        degree=degree_text or None,
                        end_date=right_text or None,
                    )

                    # Collect coursework and bullets
                    i += 1
                    while i < len(paragraphs):
                        bp = paragraphs[i]
                        if bp.is_empty:
                            i += 1
                            continue
                        if self._is_section_heading(bp):
                            break
                        br = self._classify_paragraph_role(bp)
                        if br in ("entry_header",):
                            break
                        text = bp.text.strip()
                        text = re.sub(r"^[•\-–]\s*", "", text)
                        if text.lower().startswith("coursework"):
                            cw = re.sub(r"^[Cc]oursework[:\s]*", "", text)
                            entry.coursework = [c.strip() for c in cw.split(",") if c.strip()]
                        elif br == "skills_row" and ":" in text:
                            # Handle "Label: values" lines in education (e.g., Coursework)
                            label, values = text.split(":", 1)
                            if label.strip().lower() in ("coursework", "relevant coursework", "courses"):
                                entry.coursework = [c.strip() for c in values.split(",") if c.strip()]
                            else:
                                bullet_counter += 1
                                entry.bullets.append(
                                    Bullet(id=f"bullet_{bullet_counter}", text=text)
                                )
                        else:
                            bullet_counter += 1
                            entry.bullets.append(
                                Bullet(id=f"bullet_{bullet_counter}", text=text)
                            )
                        i += 1

                    current_section.education_entries.append(entry)
                    continue

                elif current_section_type == SectionType.PROJECTS:
                    entry = ProjectEntry(
                        id=f"proj_{entry_counter}",
                        name=header_text,
                    )

                    bullets, i, bullet_counter = self._collect_bullets(paragraphs, i + 1, bullet_counter)
                    entry.bullets = bullets
                    current_section.project_entries.append(entry)
                    continue

                else:
                    entry = GenericEntry(
                        id=f"generic_{entry_counter}",
                        title=header_text,
                        subtitle=right_text or None,
                    )

                    bullets, i, bullet_counter = self._collect_bullets(paragraphs, i + 1, bullet_counter)
                    entry.bullets = bullets
                    current_section.generic_entries.append(entry)
                    continue

            # Bullet without an entry header
            if role == "bullet":
                bullet_counter += 1
                text = p.text.strip()
                text = re.sub(r"^[•\-–]\s*", "", text)

                added = False
                if current_section_type in (SectionType.EXPERIENCE, SectionType.VOLUNTEER) and current_section.experience_entries:
                    current_section.experience_entries[-1].bullets.append(
                        Bullet(id=f"bullet_{bullet_counter}", text=text)
                    )
                    added = True
                elif current_section_type == SectionType.PROJECTS and current_section.project_entries:
                    current_section.project_entries[-1].bullets.append(
                        Bullet(id=f"bullet_{bullet_counter}", text=text)
                    )
                    added = True
                elif current_section.generic_entries:
                    current_section.generic_entries[-1].bullets.append(
                        Bullet(id=f"bullet_{bullet_counter}", text=text)
                    )
                    added = True

                if not added:
                    current_section.raw_lines.append(text)

                i += 1
                continue

            # Entry subheader without a preceding entry_header
            if role == "entry_subheader":
                left_text, right_text = self._split_left_right(p)

                if current_section_type in (SectionType.EXPERIENCE, SectionType.VOLUNTEER):
                    entry_counter += 1
                    entry = ExperienceEntry(
                        id=f"exp_{entry_counter}",
                        company=left_text,
                        role=left_text,
                        location=right_text or None,
                    )

                    bullets, new_i, bullet_counter = self._collect_bullets(paragraphs, i + 1, bullet_counter)
                    entry.bullets = bullets
                    current_section.experience_entries.append(entry)
                    i = new_i
                    continue
                else:
                    current_section.raw_lines.append(p.text.strip())
                i += 1
                continue

            # Default: add as raw line
            current_section.raw_lines.append(p.text.strip())
            i += 1

        if current_section is not None:
            sections.append(current_section)

        content.sections = sections
        return content

    def _split_left_right(self, p: ParagraphFormatting) -> tuple[str, str]:
        """Split a paragraph into left and right parts.

        Detects separation via:
        1. Tab characters between runs
        2. Large runs of spaces (3+ spaces) within text
        """
        # Build text segments separated by tabs
        segments: list[str] = []
        current: list[str] = []

        for run in p.runs:
            tab_count = run.get("tab_count", 0)
            text = run.get("text", "")

            if tab_count > 0:
                if current:
                    segments.append("".join(current).strip())
                    current = []
                for _ in range(tab_count - 1):
                    segments.append("")
                if text.strip():
                    current.append(text)
            else:
                current.append(text)

        if current:
            segments.append("".join(current).strip())

        # Filter out empty segments
        segments = [s for s in segments if s.strip()]

        if len(segments) >= 2:
            return segments[0], segments[-1]

        # Fallback: split on large whitespace gaps within single segment
        if segments:
            full = segments[0]
            # Look for 3+ consecutive spaces as a separator
            parts = re.split(r"\s{3,}", full)
            parts = [p.strip() for p in parts if p.strip()]
            if len(parts) >= 2:
                return parts[0], parts[-1]
            return full, ""

        return "", ""

    def _parse_contact_line(self, contact: ContactInfo, text: str):
        """Parse a contact info line like '(973) 307-8946 | email | linkedin'."""
        parts = [p.strip() for p in text.split("|")]
        for part in parts:
            if not part:
                continue
            if "@" in part and "." in part:
                contact.email = part
            elif re.match(r"[\(\d\)\s\-\+]+$", part.replace(" ", "")):
                contact.phone = part
            elif "linkedin" in part.lower():
                contact.linkedin = part
            elif "github" in part.lower():
                contact.github = part
            elif "." in part and " " not in part:
                contact.website = part
            else:
                contact.extra_lines.append(part)

    def _detect_section_type(self, title: str) -> SectionType:
        """Detect section type from title text."""
        title_lower = title.lower().strip()

        if any(kw in title_lower for kw in ["experience", "work", "employment"]):
            return SectionType.EXPERIENCE
        if any(kw in title_lower for kw in ["education", "academic"]):
            return SectionType.EDUCATION
        if any(kw in title_lower for kw in ["project"]):
            return SectionType.PROJECTS
        if any(kw in title_lower for kw in ["skill", "technical", "technologies", "proficien"]):
            return SectionType.SKILLS
        if any(kw in title_lower for kw in ["certif"]):
            return SectionType.CERTIFICATIONS
        if any(kw in title_lower for kw in ["award", "honor"]):
            return SectionType.AWARDS
        if any(kw in title_lower for kw in ["volunteer", "extracurricular", "activit", "leadership"]):
            return SectionType.VOLUNTEER
        if any(kw in title_lower for kw in ["summary", "profile"]):
            return SectionType.SUMMARY
        if any(kw in title_lower for kw in ["objective"]):
            return SectionType.OBJECTIVE
        if any(kw in title_lower for kw in ["publication"]):
            return SectionType.PUBLICATIONS

        return SectionType.CUSTOM

    def _extract_start_date(self, date_str: str) -> Optional[str]:
        """Extract start date from a date range string."""
        if not date_str:
            return None
        # Pattern: "May 2026 – Present" or "Aug 2025 – Nov 2025"
        match = re.match(r"(.+?)\s*[–\-—]\s*(.+)", date_str)
        if match:
            return match.group(1).strip()
        return date_str.strip()

    def _extract_end_date(self, date_str: str) -> Optional[str]:
        """Extract end date from a date range string."""
        if not date_str:
            return None
        match = re.match(r"(.+?)\s*[–\-—]\s*(.+)", date_str)
        if match:
            return match.group(2).strip()
        return None
