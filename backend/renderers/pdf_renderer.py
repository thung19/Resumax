"""PDF Renderer.

Generates a high-fidelity PDF from a ResumeIR using ReportLab.
When the IR contains a captured element sequence the renderer walks
that sequence so spacing, bold flags, and font sizes come from the
same single source of truth used by the DOCX renderer.

Falls back to content-based rendering when no elements are present.
"""

from __future__ import annotations

from io import BytesIO
from typing import Optional

from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    Flowable,
    KeepTogether,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)
from reportlab.lib.colors import black, Color

from backend.models.resume_content import (
    Bullet,
    EducationEntry,
    ExperienceEntry,
    GenericEntry,
    ProjectEntry,
    ResumeContent,
    ResumeSection,
    SectionType,
    SkillCategory,
)
from backend.models.resume_ir import ResumeIR
from backend.models.resume_layout import (
    Alignment,
    ElementType,
    LayoutElement,
    ResumeLayout,
    StyleDef,
)
from backend.renderers.element_styles import (
    ElementFormatting,
    RunSegment,
    default_font_from_layout,
    default_size_from_layout,
    extract_formatting,
    joined_text,
    spacer_height_pt,
)


# ------------------------------------------------------------------
# Flowable helpers
# ------------------------------------------------------------------

class HorizontalLine(Flowable):
    """A horizontal line flowable."""

    def __init__(self, width: float, thickness: float = 0.75, color=black):
        super().__init__()
        self.line_width = width
        self.thickness = thickness
        self.line_color = color
        self.height = thickness + 1

    def draw(self):
        self.canv.setStrokeColor(self.line_color)
        self.canv.setLineWidth(self.thickness)
        self.canv.line(0, 0, self.line_width, 0)


class OverflowInfo:
    """Information about page overflow."""

    def __init__(self):
        self.page_count: int = 1
        self.overflow: bool = False
        # Vertical space (in points) left below the last line of content on
        # the final page. Lets callers detect "fits, but way underfull" —
        # not just the binary page_count/overflow this class started with.
        self.whitespace_pt: float = 0.0


# ------------------------------------------------------------------
# Font resolution
# ------------------------------------------------------------------

_SERIF = {"garamond", "georgia", "times", "times new roman", "palatino", "cambria"}
_SANS = {"arial", "helvetica", "calibri", "verdana", "tahoma", "segoe ui"}


def _resolve_font(family: str, bold: bool = False, italic: bool = False) -> str:
    """Map a font family + style flags to a ReportLab built-in name."""
    lo = family.lower().strip()
    base = "Times" if (lo in _SERIF or "garamond" in lo) else "Helvetica"

    if bold and italic:
        return f"{base}-BoldItalic" if base == "Times" else f"{base}-BoldOblique"
    if bold:
        return f"{base}-Bold"
    if italic:
        return f"{base}-Italic" if base == "Times" else f"{base}-Oblique"
    return f"{base}-Roman" if base == "Times" else base


def _rl_align(alignment: str) -> int:
    return {
        "left": TA_LEFT,
        "center": TA_CENTER,
        "right": TA_RIGHT,
        "both": TA_JUSTIFY,
        "justify": TA_JUSTIFY,
    }.get(alignment, TA_LEFT)


def _parse_color(hex_color: Optional[str]) -> Color:
    if not hex_color or hex_color.upper() in ("000000", "AUTO", "FFFFFF"):
        return black
    try:
        r = int(hex_color[0:2], 16) / 255.0
        g = int(hex_color[2:4], 16) / 255.0
        b_ = int(hex_color[4:6], 16) / 255.0
        return Color(r, g, b_)
    except (ValueError, IndexError):
        return black


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ------------------------------------------------------------------
# Renderer
# ------------------------------------------------------------------

class PdfRenderer:
    """Render a ResumeIR to PDF bytes."""

    def __init__(self, ir: ResumeIR):
        self._ir = ir
        self._content = ir.content
        self._layout = ir.layout
        self._content_width: float = 0
        self._overflow = OverflowInfo()
        self._default_font = default_font_from_layout(ir.layout)
        self._default_size = default_size_from_layout(ir.layout)
        self._spacer_height = self._detect_spacer_height()

    # ----------------------------------------------------------
    # Public API
    # ----------------------------------------------------------

    def render(self) -> bytes:
        buf = BytesIO()
        page = self._layout.page

        page_w = page.width_in * inch
        page_h = page.height_in * inch
        ml = page.margin_left_in * inch
        mr = page.margin_right_in * inch
        mt = page.margin_top_in * inch
        mb = page.margin_bottom_in * inch

        self._content_width = page_w - ml - mr

        frame = Frame(
            ml, mb,
            self._content_width, page_h - mt - mb,
            leftPadding=0, rightPadding=0,
            topPadding=0, bottomPadding=0,
        )

        doc = BaseDocTemplate(
            buf,
            pagesize=(page_w, page_h),
            leftMargin=ml, rightMargin=mr,
            topMargin=mt, bottomMargin=mb,
        )
        doc.addPageTemplates([PageTemplate(id="resume", frames=[frame])])

        story = self._build_story()
        doc.build(story, canvasmaker=self._make_canvas_factory())

        self._overflow.page_count = doc.page
        self._overflow.overflow = doc.page > 1
        # `frame` is mutated in place by ReportLab as flowables are laid
        # out — after build(), frame._y is the y-coordinate where the next
        # flowable would have started on the last page used, and frame._y1
        # is the bottom margin's y-coordinate. Their gap is exactly the
        # unused vertical space below the last line of content.
        self._overflow.whitespace_pt = max(0.0, frame._y - frame._y1)
        return buf.getvalue()

    def get_overflow_info(self) -> OverflowInfo:
        return self._overflow

    def _detect_spacer_height(self) -> float:
        """Pick the most common spacer height for consistent spacing."""
        from collections import Counter
        heights: Counter = Counter()
        for el in self._layout.elements:
            if el.element_type == ElementType.SPACER:
                h = spacer_height_pt(el, self._default_size)
                # Round to nearest 0.5pt to group similar sizes
                h = round(h * 2) / 2
                heights[h] += 1
        if heights:
            return heights.most_common(1)[0][0]
        return spacer_height_pt(
            LayoutElement(element_type=ElementType.SPACER, spacer_size_half_pt=10),
            self._default_size,
        )

    def _make_canvas_factory(self):
        from reportlab.pdfgen.canvas import Canvas
        return Canvas

    # ----------------------------------------------------------
    # Story builder – dispatch to element-walk or fallback
    # ----------------------------------------------------------

    def _build_story(self) -> list:
        if self._layout.elements:
            return self._build_story_from_elements()
        return self._build_story_from_content()

    # ----------------------------------------------------------
    # Element-walk rendering (primary path)
    # ----------------------------------------------------------

    def _build_story_from_elements(self) -> list:
        """Walk the element sequence and build flowables.

        All spacing, fonts, and bold flags come from `extract_formatting`
        via the shared element_styles module – the same data the DOCX
        renderer uses.
        """
        story: list = []
        elements = self._layout.elements

        for el in elements:
            fmt = extract_formatting(el, self._default_font, self._default_size)

            if el.element_type == ElementType.SPACER:
                story.append(Spacer(1, self._spacer_height))

            elif el.element_type == ElementType.NAME:
                story.append(self._el_paragraph(fmt, align_override="center"))

            elif el.element_type == ElementType.CONTACT:
                story.append(self._el_paragraph(fmt, align_override="center"))

            elif el.element_type == ElementType.SECTION_HEADING:
                story.append(self._el_section_heading(fmt))
                if fmt.has_bottom_border or fmt.has_drawing_line:
                    color = _parse_color(fmt.border_color) if fmt.border_color else black
                    story.append(HorizontalLine(
                        self._content_width,
                        thickness=fmt.border_width_pt if fmt.has_bottom_border else 0.75,
                        color=color,
                    ))

            elif el.element_type in (ElementType.ENTRY_HEADER, ElementType.ENTRY_SUBHEADER):
                if fmt.has_left_right:
                    story.append(self._el_left_right_row(fmt))
                else:
                    story.append(self._el_paragraph(fmt))

            elif el.element_type == ElementType.BULLET:
                story.append(self._el_paragraph(fmt))

            elif el.element_type == ElementType.SKILLS_ROW:
                story.append(self._el_skills_row(fmt))

            else:
                story.append(self._el_paragraph(fmt))

        return story

    # --- element flowable builders ---

    def _el_style(
        self,
        fmt: ElementFormatting,
        name: str = "el",
        align_override: Optional[str] = None,
        bold_override: Optional[bool] = None,
        italic_override: Optional[bool] = None,
    ) -> ParagraphStyle:
        """Build a ReportLab ParagraphStyle from an ElementFormatting."""
        bold = bold_override if bold_override is not None else fmt.bold
        italic = italic_override if italic_override is not None else fmt.italic
        alignment = align_override or fmt.alignment

        left_indent = fmt.indent_left_pt
        first_line = 0.0
        if fmt.indent_hanging_pt:
            left_indent = fmt.indent_left_pt + fmt.indent_hanging_pt
            first_line = -fmt.indent_hanging_pt

        return ParagraphStyle(
            name=name,
            fontName=_resolve_font(fmt.font_family, bold, italic),
            fontSize=fmt.font_size_pt,
            leading=fmt.line_height_pt,
            alignment=_rl_align(alignment),
            spaceBefore=fmt.space_before_pt,
            spaceAfter=fmt.space_after_pt,
            leftIndent=left_indent,
            rightIndent=fmt.indent_right_pt,
            firstLineIndent=first_line,
            textColor=_parse_color(fmt.color),
        )

    def _runs_to_markup(self, runs: list[RunSegment], base_font: str, base_size: float, base_bold: bool) -> str:
        """Convert RunSegments to ReportLab XML markup with per-run formatting."""
        parts: list[str] = []
        for r in runs:
            if r.color and r.color.upper() in ("FFFFFF", "WHITE"):
                continue
            text = _escape(r.text)
            if not text:
                continue
            # Determine if this run differs from the base style
            run_bold = r.bold
            run_italic = r.italic
            if run_bold and not base_bold:
                text = f"<b>{text}</b>"
            elif not run_bold and base_bold:
                # Run is explicitly not bold but base is bold —
                # wrap in a font tag to un-bold
                unbold_font = _resolve_font(r.font_family or base_font, False, run_italic)
                text = f'<font name="{unbold_font}">{text}</font>'
            if run_italic:
                text = f"<i>{text}</i>"
            parts.append(text)
        return "".join(parts)

    def _el_paragraph(
        self,
        fmt: ElementFormatting,
        align_override: Optional[str] = None,
    ) -> Paragraph:
        """Generic paragraph built from element formatting + runs."""
        style = self._el_style(fmt, align_override=align_override)
        markup = self._runs_to_markup(fmt.runs, fmt.font_family, fmt.font_size_pt, fmt.bold)
        if not markup:
            markup = " "
        return Paragraph(markup, style)

    def _el_section_heading(self, fmt: ElementFormatting) -> Paragraph:
        """Section heading — filter out invisible/drawing runs."""
        style = self._el_style(fmt, name="heading")
        clean_runs = [
            r for r in fmt.runs
            if not (r.color and r.color.upper() in ("FFFFFF", "WHITE"))
            and (r.text.strip() or r.font_size_pt)
        ]
        markup = self._runs_to_markup(clean_runs, fmt.font_family, fmt.font_size_pt, fmt.bold)
        if not markup:
            markup = " "
        return Paragraph(markup, style)

    def _el_left_right_row(self, fmt: ElementFormatting) -> Table:
        """Left-right row (company/date, role/location) from tab-split runs."""
        # Left side
        left_bold = any(r.bold for r in fmt.left_runs if r.text.strip())
        left_italic = any(r.italic for r in fmt.left_runs if r.text.strip())
        left_style = self._el_style(
            fmt, name="lr_left",
            bold_override=left_bold,
            italic_override=left_italic,
        )
        left_markup = self._runs_to_markup(
            fmt.left_runs, fmt.font_family, fmt.font_size_pt, left_bold,
        )

        # Right side – derive bold/italic from right runs specifically
        right_bold = any(r.bold for r in fmt.right_runs if r.text.strip())
        right_italic = any(r.italic for r in fmt.right_runs if r.text.strip())
        right_style = self._el_style(
            fmt, name="lr_right",
            align_override="right",
            bold_override=right_bold,
            italic_override=right_italic,
        )
        right_markup = self._runs_to_markup(
            fmt.right_runs, fmt.font_family, fmt.font_size_pt, right_bold,
        )

        left_para = Paragraph(left_markup or " ", left_style)
        right_para = Paragraph(right_markup or " ", right_style)

        table = Table(
            [[left_para, right_para]],
            colWidths=[self._content_width * 0.7, self._content_width * 0.3],
        )
        table.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ]))
        return table

    def _el_skills_row(self, fmt: ElementFormatting) -> Paragraph:
        """Skills / label:value row — bold label, normal values."""
        style = self._el_style(fmt, bold_override=False)
        # Build markup with per-run bold
        markup = self._runs_to_markup(fmt.runs, fmt.font_family, fmt.font_size_pt, False)
        if not markup:
            markup = " "
        return Paragraph(markup, style)

    # ----------------------------------------------------------
    # Fallback: content-based rendering (no elements captured)
    # ----------------------------------------------------------

    def _build_story_from_content(self) -> list:
        story: list = []
        contact = self._content.contact

        name_style = self._content_style("name")
        contact_style = self._content_style("contact")

        if contact.name:
            story.append(Paragraph(_escape(contact.name), name_style))

        parts = []
        if contact.phone: parts.append(contact.phone)
        if contact.email: parts.append(contact.email)
        if contact.linkedin: parts.append(contact.linkedin)
        if contact.github: parts.append(contact.github)
        if contact.website: parts.append(contact.website)
        if parts:
            story.append(Paragraph(_escape(" | ".join(parts)), contact_style))

        for section in self._content.sections:
            story.extend(self._render_section(section))

        return story

    def _content_style(self, role: str, name: Optional[str] = None) -> ParagraphStyle:
        """Build a ParagraphStyle from the StyleDef for a role (fallback path)."""
        sd = self._layout.styles.get(role, StyleDef())
        f = sd.font
        s = sd.spacing

        line_height = s.line_spacing if s.line_spacing else 1.15
        if s.line_spacing_pt:
            line_height = s.line_spacing_pt / max(f.size_pt, 1)

        style = ParagraphStyle(
            name=name or role,
            fontName=_resolve_font(f.family, f.bold, f.italic),
            fontSize=f.size_pt,
            leading=f.size_pt * line_height,
            alignment=_rl_align(sd.alignment.value),
            spaceBefore=s.space_before_pt,
            spaceAfter=s.space_after_pt,
            leftIndent=sd.indent.left_in * inch,
            rightIndent=sd.indent.right_in * inch,
            textColor=_parse_color(f.color),
        )

        if sd.indent.hanging_in:
            style.firstLineIndent = -sd.indent.hanging_in * inch
            style.leftIndent = (sd.indent.left_in + sd.indent.hanging_in) * inch

        return style

    def _left_right_row(
        self,
        left_text: str,
        right_text: str,
        left_style: ParagraphStyle,
        right_style: Optional[ParagraphStyle] = None,
    ) -> Table:
        if right_style is None:
            right_style = ParagraphStyle(
                "right_tmp", parent=left_style, alignment=TA_RIGHT,
            )
        left_para = Paragraph(_escape(left_text), left_style)
        right_para = Paragraph(_escape(right_text), right_style)
        table = Table(
            [[left_para, right_para]],
            colWidths=[self._content_width * 0.7, self._content_width * 0.3],
        )
        table.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ]))
        return table

    def _render_section(self, section: ResumeSection) -> list:
        items: list = []
        heading_style = self._content_style(
            "section_heading_with_rule"
            if "section_heading_with_rule" in self._layout.styles
            else "section_heading",
            f"heading_{section.id}",
        )

        items.append(Spacer(1, 5))
        if section.title:
            items.append(Paragraph(_escape(section.title), heading_style))
            items.append(HorizontalLine(self._content_width, thickness=0.75))
            items.append(Spacer(1, 2))

        if section.type == SectionType.EDUCATION:
            for entry in section.education_entries:
                items.extend(self._render_education(entry))
        elif section.type in (SectionType.EXPERIENCE, SectionType.VOLUNTEER):
            for i, entry in enumerate(section.experience_entries):
                if i > 0:
                    items.append(Spacer(1, 2))
                items.extend(self._render_experience(entry))
        elif section.type == SectionType.PROJECTS:
            for i, entry in enumerate(section.project_entries):
                if i > 0:
                    items.append(Spacer(1, 1))
                items.extend(self._render_project(entry))
        elif section.type == SectionType.SKILLS:
            for cat in section.skill_categories:
                items.extend(self._render_skill_category(cat))
        else:
            for entry in section.generic_entries:
                items.extend(self._render_generic(entry))

        for line in section.raw_lines:
            bullet_style = self._content_style("bullet", f"raw_{section.id}")
            items.append(Paragraph(_escape(line), bullet_style))

        return items

    def _render_experience(self, entry: ExperienceEntry) -> list:
        items: list = []
        header_style = self._content_style("entry_header", f"eh_{entry.id}")
        sub_style = self._content_style("entry_subheader", f"es_{entry.id}")
        bullet_style = self._content_style("bullet", f"b_{entry.id}")

        date_str = ""
        if entry.start_date and entry.end_date:
            date_str = f"{entry.start_date} \u2013 {entry.end_date}"
        elif entry.start_date:
            date_str = entry.start_date
        elif entry.end_date:
            date_str = entry.end_date

        right_style = ParagraphStyle("r_tmp", parent=header_style, alignment=TA_RIGHT)
        items.append(self._left_right_row(entry.company, date_str, header_style, right_style))

        sub_right = ParagraphStyle("sr_tmp", parent=sub_style, alignment=TA_RIGHT)
        items.append(self._left_right_row(entry.role, entry.location or "", sub_style, sub_right))

        for bullet in entry.bullets:
            text = f"\u2022 {_escape(bullet.text)}"
            items.append(Paragraph(text, bullet_style))

        return items

    def _render_education(self, entry: EducationEntry) -> list:
        items: list = []
        header_style = self._content_style("entry_header", f"eh_{entry.id}")
        sub_style = self._content_style("entry_subheader", f"es_{entry.id}")
        skills_style = self._content_style("skills_row", f"sk_{entry.id}")

        date_str = entry.end_date or ""
        if entry.start_date and entry.end_date:
            date_str = f"{entry.start_date} \u2013 {entry.end_date}"

        right_style = ParagraphStyle("r_tmp", parent=header_style, alignment=TA_RIGHT)
        items.append(self._left_right_row(entry.institution, date_str, header_style, right_style))

        degree_str = entry.degree or ""
        if entry.gpa:
            degree_str += f" | GPA: {entry.gpa}"
        sub_right = ParagraphStyle("sr_tmp", parent=sub_style, alignment=TA_RIGHT)
        items.append(self._left_right_row(degree_str, entry.location or "", sub_style, sub_right))

        if entry.coursework:
            text = f"<b>Coursework:</b> {_escape(', '.join(entry.coursework))}"
            items.append(Paragraph(text, skills_style))

        bullet_style = self._content_style("bullet", f"b_{entry.id}")
        for bullet in entry.bullets:
            text = f"\u2022 {_escape(bullet.text)}"
            items.append(Paragraph(text, bullet_style))

        return items

    def _render_project(self, entry: ProjectEntry) -> list:
        items: list = []
        header_style = self._content_style("entry_header", f"eh_{entry.id}")
        bullet_style = self._content_style("bullet", f"b_{entry.id}")

        date_str = ""
        if entry.start_date and entry.end_date:
            date_str = f"{entry.start_date} \u2013 {entry.end_date}"

        if date_str:
            right_style = ParagraphStyle("r_tmp", parent=header_style, alignment=TA_RIGHT)
            items.append(self._left_right_row(entry.name, date_str, header_style, right_style))
        else:
            items.append(Paragraph(_escape(entry.name), header_style))

        for bullet in entry.bullets:
            text = f"\u2022 {_escape(bullet.text)}"
            items.append(Paragraph(text, bullet_style))

        return items

    def _render_skill_category(self, cat: SkillCategory) -> list:
        skills_style = self._content_style("skills_row", f"sk_{cat.id}")
        text = f"<b>{_escape(cat.category)}:</b> {_escape(', '.join(cat.skills))}"
        return [Paragraph(text, skills_style)]

    def _render_generic(self, entry: GenericEntry) -> list:
        items: list = []
        header_style = self._content_style("entry_header", f"eh_{entry.id}")
        sub_style = self._content_style("entry_subheader", f"es_{entry.id}")
        bullet_style = self._content_style("bullet", f"b_{entry.id}")

        if entry.title:
            items.append(Paragraph(_escape(entry.title), header_style))

        if entry.subtitle:
            items.append(Paragraph(_escape(entry.subtitle), sub_style))

        for bullet in entry.bullets:
            text = f"\u2022 {_escape(bullet.text)}"
            items.append(Paragraph(text, bullet_style))

        return items
