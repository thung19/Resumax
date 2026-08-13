"""DOCX Renderer.

Converts a ResumeIR into an editable .docx with formatting that exactly
replicates the source document's approach:

- Single line spacing (240 twips, auto) on all paragraphs
- NO space-before / space-after — all vertical spacing via empty spacer paragraphs
- Spacer paragraphs with specific font sizes (e.g., sz=10 = 5pt)
- Tab characters for left/right alignment (company↹date, role↹location)
- Inline bullet character '•' with NO Word list numbering or hanging indent
- Font set on every run via rFonts (ascii, hAnsi, eastAsia, cs)
- Right tab stop at content width for left-right rows
"""

from __future__ import annotations

from io import BytesIO
from typing import Optional

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, Twips
from docx.text.paragraph import Paragraph
from docx.text.run import Run

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
from backend.models.resume_layout import ResumeLayout, StyleDef


class DocxRenderer:
    """Render a ResumeIR to a .docx file."""

    def __init__(self, ir: ResumeIR):
        self._ir = ir
        self._content = ir.content
        self._layout = ir.layout
        self._styles = ir.layout.styles
        self._doc = Document()
        # Primary font from layout detection
        self._font = self._detect_font()
        # Content width for right-tab positioning
        page = self._layout.page
        self._content_width_twips = int(
            (page.width_in - page.margin_left_in - page.margin_right_in) * 1440
        )

    def _detect_font(self) -> str:
        """Get the primary font family from the layout styles."""
        for role in ["name", "entry_header", "bullet", "contact"]:
            style = self._styles.get(role)
            if style and style.font.family:
                family = style.font.family
                if family.lower() not in ("arial", "minorhansi"):
                    return family
        return "Garamond"

    def render(self) -> bytes:
        """Render and return .docx bytes."""
        self._setup_page()
        self._build_body()
        buf = BytesIO()
        self._doc.save(buf)
        return buf.getvalue()

    # --- Page setup ---

    def _setup_page(self):
        page = self._layout.page
        section = self._doc.sections[0]
        section.page_width = Inches(page.width_in)
        section.page_height = Inches(page.height_in)
        section.top_margin = Inches(page.margin_top_in)
        section.bottom_margin = Inches(page.margin_bottom_in)
        section.left_margin = Inches(page.margin_left_in)
        section.right_margin = Inches(page.margin_right_in)

        # Configure Normal style to match original document defaults
        normal_style = self._doc.styles["Normal"]
        normal_style.font.name = self._font
        normal_style.font.size = Pt(11)  # sz=22
        pf = normal_style.paragraph_format
        pf.space_after = Pt(0)
        pf.space_before = Pt(0)
        # Set line spacing to single (240 twips / auto) via OOXML
        pPr = normal_style.element.find(qn("w:pPr"))
        if pPr is None:
            pPr = OxmlElement("w:pPr")
            normal_style.element.append(pPr)
        spacing = pPr.find(qn("w:spacing"))
        if spacing is None:
            spacing = OxmlElement("w:spacing")
            pPr.append(spacing)
        spacing.set(qn("w:line"), "240")
        spacing.set(qn("w:lineRule"), "auto")
        spacing.set(qn("w:after"), "0")
        spacing.set(qn("w:before"), "0")

    # --- Low-level helpers ---

    def _set_single_spacing(self, para: Paragraph):
        """Set line=240 lineRule=auto (single spacing), no space before/after."""
        pPr = para._element.get_or_add_pPr()
        spacing = pPr.find(qn("w:spacing"))
        if spacing is None:
            spacing = OxmlElement("w:spacing")
            pPr.append(spacing)
        spacing.set(qn("w:line"), "240")
        spacing.set(qn("w:lineRule"), "auto")
        # Ensure no space before/after
        spacing.set(qn("w:before"), "0")
        spacing.set(qn("w:after"), "0")

    def _set_font_on_run(
        self,
        run: Run,
        family: Optional[str] = None,
        size_half_pt: Optional[int] = None,
        bold: Optional[bool] = None,
        italic: Optional[bool] = None,
    ):
        """Set font on a run using direct OOXML for full control."""
        fam = family or self._font
        rPr = run._element.get_or_add_rPr()

        # rFonts
        rFonts = rPr.find(qn("w:rFonts"))
        if rFonts is None:
            rFonts = OxmlElement("w:rFonts")
            rPr.insert(0, rFonts)
        rFonts.set(qn("w:ascii"), fam)
        rFonts.set(qn("w:hAnsi"), fam)
        rFonts.set(qn("w:eastAsia"), fam)
        rFonts.set(qn("w:cs"), fam)

        # Size (half-points)
        if size_half_pt is not None:
            sz = rPr.find(qn("w:sz"))
            if sz is None:
                sz = OxmlElement("w:sz")
                rPr.append(sz)
            sz.set(qn("w:val"), str(size_half_pt))
            szCs = rPr.find(qn("w:szCs"))
            if szCs is None:
                szCs = OxmlElement("w:szCs")
                rPr.append(szCs)
            szCs.set(qn("w:val"), str(size_half_pt))

        # Bold
        if bold is True:
            b_el = rPr.find(qn("w:b"))
            if b_el is None:
                b_el = OxmlElement("w:b")
                rPr.append(b_el)
        elif bold is False:
            b_el = rPr.find(qn("w:b"))
            if b_el is not None:
                rPr.remove(b_el)

        # Italic
        if italic is True:
            i_el = rPr.find(qn("w:i"))
            if i_el is None:
                i_el = OxmlElement("w:i")
                rPr.append(i_el)
        elif italic is False:
            i_el = rPr.find(qn("w:i"))
            if i_el is not None:
                rPr.remove(i_el)

    def _add_right_tab_stop(self, para: Paragraph):
        """Add a right-aligned tab stop at the content width."""
        pPr = para._element.get_or_add_pPr()
        tabs_el = pPr.find(qn("w:tabs"))
        if tabs_el is None:
            tabs_el = OxmlElement("w:tabs")
            pPr.append(tabs_el)
        tab = OxmlElement("w:tab")
        tab.set(qn("w:val"), "right")
        tab.set(qn("w:pos"), str(self._content_width_twips))
        tabs_el.append(tab)

    def _add_tab_run(self, para: Paragraph, bold: bool = False, italic: bool = False):
        """Add a run containing a tab character with font set."""
        run = para.add_run()
        self._set_font_on_run(run, bold=bold, italic=italic)
        run._element.append(OxmlElement("w:tab"))
        return run

    def _set_alignment(self, para: Paragraph, align: str):
        """Set paragraph alignment."""
        pPr = para._element.get_or_add_pPr()
        jc = pPr.find(qn("w:jc"))
        if jc is None:
            jc = OxmlElement("w:jc")
            pPr.append(jc)
        jc.set(qn("w:val"), align)

    def _add_bottom_border(self, para: Paragraph):
        """Add a thin bottom border (horizontal rule) to a paragraph."""
        pPr = para._element.get_or_add_pPr()
        pBdr = OxmlElement("w:pBdr")
        bottom = OxmlElement("w:bottom")
        bottom.set(qn("w:val"), "single")
        bottom.set(qn("w:sz"), "6")  # 0.75pt
        bottom.set(qn("w:space"), "1")
        bottom.set(qn("w:color"), "000000")
        pBdr.append(bottom)
        pPr.append(pBdr)

    # --- Paragraph builders ---

    def _add_spacer(self, size_half_pt: int | None = None) -> Paragraph:
        """Add an empty spacer paragraph.

        The original uses two spacer styles:
        - No explicit size (inherits ~11pt default) for section gaps
        - sz=10 (5pt) for tight gaps between entries and after headings

        Pass size_half_pt=10 for small spacers, None for default-size spacers.
        """
        para = self._doc.add_paragraph()
        self._set_single_spacing(para)
        run = para.add_run()
        self._set_font_on_run(run, size_half_pt=size_half_pt)
        return para

    def _add_name(self, name: str) -> Paragraph:
        """Name line: large bold centered."""
        name_style = self._styles.get("name")
        size_hp = int(name_style.font.size_pt * 2) if name_style else 52

        para = self._doc.add_paragraph()
        self._set_single_spacing(para)
        self._set_alignment(para, "center")
        run = para.add_run(name)
        self._set_font_on_run(run, size_half_pt=size_hp, bold=True)
        return para

    def _add_contact(self, text: str) -> Paragraph:
        """Contact line: centered, default size, single spacing."""
        para = self._doc.add_paragraph()
        self._set_alignment(para, "center")
        # Original has no explicit spacing on contact — just inherits
        # But we set font explicitly
        run = para.add_run(text)
        self._set_font_on_run(run)
        return para

    def _add_section_heading(self, title: str) -> Paragraph:
        """Section heading: bold, with bottom border line.

        The original document has section headings with a DRAWING shape
        for the line and no explicit font size on the heading text.
        We use a bottom border for compatibility and set the size
        only if detected as different from body text.
        """
        heading_style = self._styles.get("section_heading_with_rule") or self._styles.get("section_heading")
        body_style = self._styles.get("bullet") or self._styles.get("entry_header")

        # Only set explicit size if heading is different from body
        size_hp = None
        if heading_style and body_style:
            if abs(heading_style.font.size_pt - body_style.font.size_pt) > 0.5:
                size_hp = int(heading_style.font.size_pt * 2)

        para = self._doc.add_paragraph()
        self._set_single_spacing(para)
        self._add_bottom_border(para)
        run = para.add_run(title)
        self._set_font_on_run(run, size_half_pt=size_hp, bold=True)
        return para

    def _add_left_right(
        self,
        left_text: str,
        right_text: str,
        left_bold: bool = False,
        left_italic: bool = False,
        right_bold: bool = False,
        right_italic: bool = False,
    ) -> Paragraph:
        """Add a paragraph with left text + tab + right text."""
        para = self._doc.add_paragraph()
        self._set_single_spacing(para)
        self._add_right_tab_stop(para)

        # Left run
        run_left = para.add_run(left_text)
        self._set_font_on_run(run_left, bold=left_bold, italic=left_italic)

        # Tab (inherit left formatting so tab leader matches)
        self._add_tab_run(para, bold=left_bold, italic=left_italic)

        # Right run
        run_right = para.add_run(right_text)
        self._set_font_on_run(run_right, bold=right_bold, italic=right_italic)

        return para

    def _add_bullet(self, text: str) -> Paragraph:
        """Add a bullet paragraph: '• text' with no indentation.

        The original document uses inline '• ' characters,
        NOT Word's numbering/list system. No hanging indent.
        """
        para = self._doc.add_paragraph()
        self._set_single_spacing(para)
        run = para.add_run(f"\u2022 {text}")
        self._set_font_on_run(run)
        return para

    def _add_plain(self, text: str, bold: bool = False, italic: bool = False) -> Paragraph:
        """Add a plain text paragraph."""
        para = self._doc.add_paragraph()
        self._set_single_spacing(para)
        run = para.add_run(text)
        self._set_font_on_run(run, bold=bold, italic=italic)
        return para

    def _add_label_value(self, label: str, value: str) -> Paragraph:
        """Add a 'Label: value' paragraph with bold label."""
        para = self._doc.add_paragraph()
        self._set_single_spacing(para)

        run_label = para.add_run(f"{label}: ")
        self._set_font_on_run(run_label, bold=True)

        run_value = para.add_run(value)
        self._set_font_on_run(run_value, bold=False)
        return para

    # --- Body builder ---

    def _build_body(self):
        """Build the document body."""
        # Remove default empty paragraph
        if self._doc.paragraphs:
            p = self._doc.paragraphs[0]._element
            p.getparent().remove(p)

        contact = self._content.contact

        # Name
        if contact.name:
            self._add_name(contact.name)

        # Contact info
        contact_parts = []
        if contact.phone:
            contact_parts.append(contact.phone)
        if contact.email:
            contact_parts.append(contact.email)
        if contact.linkedin:
            contact_parts.append(contact.linkedin)
        if contact.github:
            contact_parts.append(contact.github)
        if contact.website:
            contact_parts.append(contact.website)
        for extra in contact.extra_lines:
            contact_parts.append(extra)
        if contact_parts:
            self._add_contact(" | ".join(contact_parts))

        # Sections
        for section in self._content.sections:
            self._render_section(section)

    def _render_section(self, section: ResumeSection):
        """Render a section.

        Spacing pattern from the original document:
        - Default-size spacer before section heading
        - Small spacer (sz=10) after section heading
        - Default-size spacer between entries within a section
        """
        # Spacer before heading (no explicit size — inherits default)
        self._add_spacer()

        # Section heading with line
        if section.title:
            self._add_section_heading(section.title)

        # Small spacer after heading (sz=10 = 5pt)
        self._add_spacer(10)

        # Content
        if section.type == SectionType.EDUCATION:
            for entry in section.education_entries:
                self._render_education(entry)

        elif section.type in (SectionType.EXPERIENCE, SectionType.VOLUNTEER):
            for i, entry in enumerate(section.experience_entries):
                if i > 0:
                    # Default-size spacer between entries
                    self._add_spacer()
                self._render_experience(entry)

        elif section.type == SectionType.PROJECTS:
            for i, entry in enumerate(section.project_entries):
                if i > 0:
                    self._add_spacer()
                self._render_project(entry)

        elif section.type == SectionType.SKILLS:
            for cat in section.skill_categories:
                self._render_skill_category(cat)

        else:
            for entry in section.generic_entries:
                self._render_generic(entry)

        for line in section.raw_lines:
            self._add_plain(line)

    def _render_experience(self, entry: ExperienceEntry):
        """Render an experience entry."""
        # Company + date (bold)
        date_str = ""
        if entry.start_date and entry.end_date:
            date_str = f"{entry.start_date} \u2013 {entry.end_date}"
        elif entry.start_date:
            date_str = entry.start_date
        elif entry.end_date:
            date_str = entry.end_date

        self._add_left_right(
            entry.company, date_str,
            left_bold=True, right_bold=True,
        )

        # Role + location (italic)
        self._add_left_right(
            entry.role, entry.location or "",
            left_italic=True, right_italic=False,
        )

        # Bullets
        for bullet in entry.bullets:
            self._add_bullet(bullet.text)

    def _render_education(self, entry: EducationEntry):
        """Render an education entry."""
        date_str = entry.end_date or ""
        if entry.start_date and entry.end_date:
            date_str = f"{entry.start_date} \u2013 {entry.end_date}"

        self._add_left_right(
            entry.institution, date_str,
            left_bold=True, right_bold=True,
        )

        # Degree + location
        degree_str = entry.degree or ""
        if entry.gpa:
            degree_str += f" | GPA: {entry.gpa}"

        self._add_left_right(
            degree_str, entry.location or "",
        )

        # Small spacer before coursework (matches original sz=10 italic spacer)
        if entry.coursework or entry.bullets:
            self._add_spacer(10)  # 5pt gap

        # Coursework
        if entry.coursework:
            self._add_label_value("Coursework", ", ".join(entry.coursework))

        # Bullets
        for bullet in entry.bullets:
            self._add_bullet(bullet.text)

    def _render_project(self, entry: ProjectEntry):
        """Render a project entry."""
        date_str = ""
        if entry.start_date and entry.end_date:
            date_str = f"{entry.start_date} \u2013 {entry.end_date}"

        if date_str:
            self._add_left_right(
                entry.name, date_str,
                left_bold=True, right_bold=True,
            )
        else:
            self._add_plain(entry.name, bold=True)

        for bullet in entry.bullets:
            self._add_bullet(bullet.text)

    def _render_skill_category(self, cat: SkillCategory):
        """Render a skill category."""
        self._add_label_value(cat.category, ", ".join(cat.skills))

    def _render_generic(self, entry: GenericEntry):
        """Render a generic entry."""
        subtitle = entry.subtitle or ""
        if subtitle:
            self._add_left_right(entry.title or "", subtitle, left_bold=True)
        elif entry.title:
            self._add_plain(entry.title, bold=True)

        if entry.description:
            self._add_plain(entry.description)

        for bullet in entry.bullets:
            self._add_bullet(bullet.text)
