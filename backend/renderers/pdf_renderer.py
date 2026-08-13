"""PDF Renderer.

Generates a high-fidelity PDF from a ResumeIR using ReportLab.
This is the visual ground truth for layout verification and the
preferred submission format.

Also provides page count verification and overflow detection.
"""

from __future__ import annotations

from io import BytesIO
from typing import Optional

from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch, mm
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
    ResumeLayout,
    StyleDef,
)


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
        self.overflow_elements: list[dict] = []


def _pt(val: float) -> float:
    """Points passthrough (reportlab uses points natively)."""
    return val


def _align(a: Alignment) -> int:
    return {
        Alignment.LEFT: TA_LEFT,
        Alignment.CENTER: TA_CENTER,
        Alignment.RIGHT: TA_RIGHT,
        Alignment.JUSTIFY: TA_JUSTIFY,
    }.get(a, TA_LEFT)


class PdfRenderer:
    """Render a ResumeIR to PDF bytes."""

    def __init__(self, ir: ResumeIR):
        self._ir = ir
        self._content = ir.content
        self._layout = ir.layout
        self._styles_map = ir.layout.styles
        self._content_width: float = 0  # set in render()
        self._overflow = OverflowInfo()

    def render(self) -> bytes:
        """Render to PDF and return bytes."""
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

        page_template = PageTemplate(id="resume", frames=[frame])
        doc.addPageTemplates([page_template])

        # Build flowables
        story = self._build_story()

        # Track page count
        doc.build(story, canvasmaker=self._make_canvas_factory())

        self._overflow.page_count = doc.page
        self._overflow.overflow = doc.page > 1

        return buf.getvalue()

    def get_overflow_info(self) -> OverflowInfo:
        """Get overflow info after rendering."""
        return self._overflow

    def _make_canvas_factory(self):
        """Return the default canvas — no special overrides needed."""
        from reportlab.pdfgen.canvas import Canvas
        return Canvas

    # --- Style builders ---

    def _get_style_def(self, role: str) -> StyleDef:
        return self._styles_map.get(role, StyleDef())

    def _para_style(self, role: str, name: Optional[str] = None) -> ParagraphStyle:
        """Build a ParagraphStyle from a layout style role."""
        sd = self._get_style_def(role)
        f = sd.font
        s = sd.spacing

        line_height = s.line_spacing if s.line_spacing else 1.15
        if s.line_spacing_pt:
            line_height = s.line_spacing_pt / max(f.size_pt, 1)

        style = ParagraphStyle(
            name=name or role,
            fontName=self._resolve_font(f.family, f.bold, f.italic),
            fontSize=f.size_pt,
            leading=f.size_pt * line_height,
            alignment=_align(sd.alignment),
            spaceBefore=s.space_before_pt,
            spaceAfter=s.space_after_pt,
            leftIndent=sd.indent.left_in * inch,
            rightIndent=sd.indent.right_in * inch,
            textColor=self._parse_color(f.color),
        )

        if sd.indent.hanging_in:
            style.firstLineIndent = -sd.indent.hanging_in * inch
            style.leftIndent = (sd.indent.left_in + sd.indent.hanging_in) * inch

        return style

    def _resolve_font(self, family: str, bold: bool, italic: bool) -> str:
        """Map font family + style to a ReportLab font name.

        ReportLab has limited built-in fonts. Map common resume fonts
        to the closest built-in equivalent.
        """
        # Garamond-family → Times (closest serif)
        serif_families = {"garamond", "georgia", "times", "times new roman", "palatino", "cambria"}
        sans_families = {"arial", "helvetica", "calibri", "verdana", "tahoma", "segoe ui"}

        family_lower = family.lower().strip()

        if family_lower in serif_families or "garamond" in family_lower:
            base = "Times"
        elif family_lower in sans_families:
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

    def _parse_color(self, hex_color: Optional[str]) -> Color:
        if not hex_color or hex_color.upper() in ("000000", "AUTO", "FFFFFF"):
            return black
        try:
            r = int(hex_color[0:2], 16) / 255.0
            g = int(hex_color[2:4], 16) / 255.0
            b = int(hex_color[4:6], 16) / 255.0
            return Color(r, g, b)
        except (ValueError, IndexError):
            return black

    # --- Left-right row via table ---

    def _left_right_row(
        self,
        left_text: str,
        right_text: str,
        left_style: ParagraphStyle,
        right_style: Optional[ParagraphStyle] = None,
    ) -> Table:
        """Create a row with left-aligned and right-aligned text using a table."""
        if right_style is None:
            right_style = ParagraphStyle(
                "right_tmp",
                parent=left_style,
                alignment=TA_RIGHT,
            )

        left_para = Paragraph(self._escape(left_text), left_style)
        right_para = Paragraph(self._escape(right_text), right_style)

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

    def _escape(self, text: str) -> str:
        """Escape text for ReportLab Paragraph XML."""
        return (
            text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )

    # --- Story builder ---

    def _build_story(self) -> list:
        """Build the full list of flowables."""
        story: list = []

        contact = self._content.contact
        name_style = self._para_style("name", "name_style")
        contact_style = self._para_style("contact", "contact_style")

        # Name
        if contact.name:
            story.append(Paragraph(self._escape(contact.name), name_style))

        # Contact line
        parts = []
        if contact.phone:
            parts.append(contact.phone)
        if contact.email:
            parts.append(contact.email)
        if contact.linkedin:
            parts.append(contact.linkedin)
        if contact.github:
            parts.append(contact.github)
        if contact.website:
            parts.append(contact.website)
        if parts:
            story.append(Paragraph(self._escape(" | ".join(parts)), contact_style))

        # Sections
        for section in self._content.sections:
            story.extend(self._render_section(section))

        return story

    def _render_section(self, section: ResumeSection) -> list:
        """Render a section to flowables."""
        items: list = []

        heading_style = self._para_style(
            "section_heading_with_rule"
            if "section_heading_with_rule" in self._styles_map
            else "section_heading",
            f"heading_{section.id}",
        )

        # Spacer before section
        items.append(Spacer(1, 5))

        # Section heading + line
        if section.title:
            items.append(Paragraph(self._escape(section.title), heading_style))
            items.append(HorizontalLine(self._content_width, thickness=0.75))
            items.append(Spacer(1, 2))

        # Content by type
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
            bullet_style = self._para_style("bullet", f"raw_{section.id}")
            items.append(Paragraph(self._escape(line), bullet_style))

        return items

    def _render_experience(self, entry: ExperienceEntry) -> list:
        items: list = []

        header_style = self._para_style("entry_header", f"eh_{entry.id}")
        sub_style = self._para_style("entry_subheader", f"es_{entry.id}")
        bullet_style = self._para_style("bullet", f"b_{entry.id}")
        right_style = ParagraphStyle("r_tmp", parent=header_style, alignment=TA_RIGHT, fontName=self._resolve_font(header_style.fontName.replace("-Bold", "").replace("-Roman", ""), True, False))

        # Company + date
        date_str = ""
        if entry.start_date and entry.end_date:
            date_str = f"{entry.start_date} \u2013 {entry.end_date}"
        elif entry.start_date:
            date_str = entry.start_date
        elif entry.end_date:
            date_str = entry.end_date

        items.append(self._left_right_row(entry.company, date_str, header_style))

        # Role + location
        sub_right = ParagraphStyle("sr_tmp", parent=sub_style, alignment=TA_RIGHT)
        items.append(self._left_right_row(entry.role, entry.location or "", sub_style, sub_right))

        # Bullets
        for bullet in entry.bullets:
            text = f"\u2022 {self._escape(bullet.text)}"
            bs = ParagraphStyle(
                f"bullet_{bullet.id}",
                parent=bullet_style,
                leftIndent=0.25 * inch,
                firstLineIndent=-0.13 * inch,
            )
            items.append(Paragraph(text, bs))

        return items

    def _render_education(self, entry: EducationEntry) -> list:
        items: list = []
        header_style = self._para_style("entry_header", f"eh_{entry.id}")
        sub_style = self._para_style("entry_subheader", f"es_{entry.id}")
        skills_style = self._para_style("skills_row", f"sk_{entry.id}")

        date_str = entry.end_date or ""
        if entry.start_date and entry.end_date:
            date_str = f"{entry.start_date} \u2013 {entry.end_date}"

        items.append(self._left_right_row(entry.institution, date_str, header_style))

        degree_str = entry.degree or ""
        if entry.gpa:
            degree_str += f" | GPA: {entry.gpa}"
        sub_right = ParagraphStyle("sr_tmp", parent=sub_style, alignment=TA_RIGHT)
        items.append(self._left_right_row(degree_str, entry.location or "", sub_style, sub_right))

        if entry.coursework:
            text = f"<b>Coursework:</b> {self._escape(', '.join(entry.coursework))}"
            items.append(Paragraph(text, skills_style))

        bullet_style = self._para_style("bullet", f"b_{entry.id}")
        for bullet in entry.bullets:
            text = f"\u2022 {self._escape(bullet.text)}"
            bs = ParagraphStyle(f"bullet_{bullet.id}", parent=bullet_style, leftIndent=0.25 * inch, firstLineIndent=-0.13 * inch)
            items.append(Paragraph(text, bs))

        return items

    def _render_project(self, entry: ProjectEntry) -> list:
        items: list = []
        header_style = self._para_style("entry_header", f"eh_{entry.id}")
        bullet_style = self._para_style("bullet", f"b_{entry.id}")

        date_str = ""
        if entry.start_date and entry.end_date:
            date_str = f"{entry.start_date} \u2013 {entry.end_date}"

        if date_str:
            items.append(self._left_right_row(entry.name, date_str, header_style))
        else:
            items.append(Paragraph(f"<b>{self._escape(entry.name)}</b>", header_style))

        for bullet in entry.bullets:
            text = f"\u2022 {self._escape(bullet.text)}"
            bs = ParagraphStyle(f"bullet_{bullet.id}", parent=bullet_style, leftIndent=0.25 * inch, firstLineIndent=-0.13 * inch)
            items.append(Paragraph(text, bs))

        return items

    def _render_skill_category(self, cat: SkillCategory) -> list:
        skills_style = self._para_style("skills_row", f"sk_{cat.id}")
        text = f"<b>{self._escape(cat.category)}:</b> {self._escape(', '.join(cat.skills))}"
        return [Paragraph(text, skills_style)]

    def _render_generic(self, entry: GenericEntry) -> list:
        items: list = []
        header_style = self._para_style("entry_header", f"eh_{entry.id}")
        bullet_style = self._para_style("bullet", f"b_{entry.id}")

        if entry.title:
            items.append(Paragraph(f"<b>{self._escape(entry.title)}</b>", header_style))

        for bullet in entry.bullets:
            text = f"\u2022 {self._escape(bullet.text)}"
            bs = ParagraphStyle(f"bullet_{bullet.id}", parent=bullet_style, leftIndent=0.25 * inch, firstLineIndent=-0.13 * inch)
            items.append(Paragraph(text, bs))

        return items
