"""DOCX Renderer.

Reproduces the source document by walking the captured element sequence
and replaying each paragraph's exact formatting properties.

When content changes (tailoring), the renderer replaces text while
preserving the original formatting structure.
"""

from __future__ import annotations

from io import BytesIO
from typing import Optional

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt
from docx.text.paragraph import Paragraph
from docx.text.run import Run

from backend.models.resume_content import (
    ResumeContent,
    ResumeSection,
    SectionType,
)
from backend.models.resume_ir import ResumeIR
from backend.models.resume_layout import (
    ElementType,
    LayoutElement,
    ParagraphFormat,
    ResumeLayout,
    RunFormat,
    TabAlignment,
    TabStop,
)


class DocxRenderer:
    """Render a ResumeIR to a .docx file."""

    def __init__(self, ir: ResumeIR):
        self._ir = ir
        self._content = ir.content
        self._layout = ir.layout
        self._doc = Document()
        self._font = self._detect_font()
        page = self._layout.page
        self._content_width_twips = int(
            (page.width_in - page.margin_left_in - page.margin_right_in) * 1440
        )
        self._spacer_half_pt = self._detect_spacer_size()

    def _detect_spacer_size(self) -> int:
        """Pick the most common spacer size across all elements.

        The source document may have inconsistent spacer sizes.
        We normalise to a single value so spacing is uniform.
        """
        from collections import Counter
        sizes: Counter = Counter()
        for el in self._layout.elements:
            if el.element_type == ElementType.SPACER and el.spacer_size_half_pt is not None:
                sizes[el.spacer_size_half_pt] += 1
        if sizes:
            return sizes.most_common(1)[0][0]
        return 10  # 5pt default

    def _detect_font(self) -> str:
        for role in ["name", "entry_header", "bullet", "contact"]:
            style = self._layout.styles.get(role)
            if style and style.font.family and style.font.family.lower() not in ("arial", "minorhansi"):
                return style.font.family
        return "Garamond"

    def render(self) -> bytes:
        self._setup_page()
        if self._layout.elements:
            self._render_from_elements()
        else:
            self._render_from_content()
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

        # Normal style: match the original document's defaults
        # Original: font=Arial, sz=22 (11pt), line=276 (1.15x), after=not set
        normal = self._doc.styles["Normal"]

        # Use the document's defaults — Normal style typically overrides docDefaults
        # docDefaults says 12pt but Normal style in the original is 11pt (sz=22)
        default_font = self._layout.default_font
        normal_font = default_font.family if default_font.family and default_font.family.lower() != "minorhansi" else "Arial"
        normal.font.name = normal_font
        normal.font.size = Pt(11)  # Standard Normal style size (sz=22)

        pf = normal.paragraph_format
        # Do NOT set space_after=0 — let it inherit the original default
        pPr = normal.element.find(qn("w:pPr"))
        if pPr is None:
            pPr = OxmlElement("w:pPr")
            normal.element.append(pPr)
        spacing = pPr.find(qn("w:spacing"))
        if spacing is None:
            spacing = OxmlElement("w:spacing")
            pPr.append(spacing)
        # Match original Normal style: line=276 (1.15x spacing)
        # Individual paragraphs override to line=240 where needed
        spacing.set(qn("w:line"), "276")
        spacing.set(qn("w:lineRule"), "auto")
        # Explicitly zero before/after to prevent Word defaults leaking
        spacing.set(qn("w:before"), "0")
        spacing.set(qn("w:after"), "0")

    # --- OOXML helpers ---

    def _apply_paragraph_format(self, para: Paragraph, pf: ParagraphFormat):
        """Apply captured paragraph formatting to a python-docx paragraph."""
        pPr = para._element.get_or_add_pPr()

        # Spacing – always write explicit before/after to prevent
        # Word Normal-style defaults from leaking extra space.
        if pf.line_value is not None or pf.space_before_twips is not None or pf.space_after_twips is not None:
            spacing = pPr.find(qn("w:spacing"))
            if spacing is None:
                spacing = OxmlElement("w:spacing")
                pPr.append(spacing)
            if pf.line_value is not None:
                spacing.set(qn("w:line"), str(pf.line_value))
            if pf.line_rule is not None:
                spacing.set(qn("w:lineRule"), pf.line_rule)
            spacing.set(qn("w:before"), str(pf.space_before_twips or 0))
            spacing.set(qn("w:after"), str(pf.space_after_twips or 0))

        # Indentation
        if any([pf.indent_left_twips, pf.indent_right_twips,
                pf.indent_hanging_twips, pf.indent_first_line_twips]):
            ind = pPr.find(qn("w:ind"))
            if ind is None:
                ind = OxmlElement("w:ind")
                pPr.append(ind)
            if pf.indent_left_twips is not None:
                ind.set(qn("w:left"), str(pf.indent_left_twips))
            if pf.indent_right_twips is not None:
                ind.set(qn("w:right"), str(pf.indent_right_twips))
            if pf.indent_hanging_twips is not None:
                ind.set(qn("w:hanging"), str(pf.indent_hanging_twips))
            if pf.indent_first_line_twips is not None:
                ind.set(qn("w:firstLine"), str(pf.indent_first_line_twips))

        # Alignment
        if pf.alignment:
            jc = pPr.find(qn("w:jc"))
            if jc is None:
                jc = OxmlElement("w:jc")
                pPr.append(jc)
            jc.set(qn("w:val"), pf.alignment)

        # Tab stops
        if pf.tab_stops:
            tabs = pPr.find(qn("w:tabs"))
            if tabs is None:
                tabs = OxmlElement("w:tabs")
                pPr.append(tabs)
            for ts in pf.tab_stops:
                tab = OxmlElement("w:tab")
                tab.set(qn("w:val"), ts.alignment.value)
                tab.set(qn("w:pos"), str(ts.position_twips))
                tabs.append(tab)

        # Bottom border
        if pf.bottom_border and pf.bottom_border.enabled:
            pBdr = OxmlElement("w:pBdr")
            bottom = OxmlElement("w:bottom")
            bottom.set(qn("w:val"), "single")
            bottom.set(qn("w:sz"), str(int(pf.bottom_border.width_pt * 8)))
            bottom.set(qn("w:space"), "1")
            if pf.bottom_border.color:
                bottom.set(qn("w:color"), pf.bottom_border.color)
            else:
                bottom.set(qn("w:color"), "000000")
            pBdr.append(bottom)
            pPr.append(pBdr)

        # Keep with next
        if pf.keep_with_next:
            kwn = OxmlElement("w:keepNext")
            pPr.append(kwn)

        # Word style
        if pf.word_style:
            pStyle = pPr.find(qn("w:pStyle"))
            if pStyle is None:
                pStyle = OxmlElement("w:pStyle")
                pPr.insert(0, pStyle)
            pStyle.set(qn("w:val"), pf.word_style)

    def _apply_run_format(self, run: Run, rf: RunFormat):
        """Apply captured run formatting."""
        rPr = run._element.get_or_add_rPr()

        # Font family
        fam = rf.font_family or self._font
        rFonts = rPr.find(qn("w:rFonts"))
        if rFonts is None:
            rFonts = OxmlElement("w:rFonts")
            rPr.insert(0, rFonts)
        rFonts.set(qn("w:ascii"), fam)
        rFonts.set(qn("w:hAnsi"), fam)
        rFonts.set(qn("w:eastAsia"), fam)
        rFonts.set(qn("w:cs"), fam)

        # Size
        if rf.font_size_half_pt is not None:
            for tag in ["w:sz", "w:szCs"]:
                el = rPr.find(qn(tag))
                if el is None:
                    el = OxmlElement(tag)
                    rPr.append(el)
                el.set(qn("w:val"), str(rf.font_size_half_pt))

        # Bold
        if rf.bold is True:
            if rPr.find(qn("w:b")) is None:
                rPr.append(OxmlElement("w:b"))
        if rf.bold_cs is True:
            if rPr.find(qn("w:bCs")) is None:
                rPr.append(OxmlElement("w:bCs"))

        # Italic
        if rf.italic is True:
            if rPr.find(qn("w:i")) is None:
                rPr.append(OxmlElement("w:i"))
        if rf.italic_cs is True:
            if rPr.find(qn("w:iCs")) is None:
                rPr.append(OxmlElement("w:iCs"))

        # Underline
        if rf.underline:
            u = rPr.find(qn("w:u"))
            if u is None:
                u = OxmlElement("w:u")
                rPr.append(u)
            u.set(qn("w:val"), rf.underline)

        # Color
        if rf.color:
            c = rPr.find(qn("w:color"))
            if c is None:
                c = OxmlElement("w:color")
                rPr.append(c)
            c.set(qn("w:val"), rf.color)

        # Small caps
        if rf.small_caps:
            if rPr.find(qn("w:smallCaps")) is None:
                rPr.append(OxmlElement("w:smallCaps"))

    def _add_runs_to_paragraph(self, para: Paragraph, runs: list[RunFormat]):
        """Add runs with their exact formatting to a paragraph."""
        for rf in runs:
            if rf.hyperlink_url:
                # Create a w:hyperlink element with the run inside
                self._add_hyperlink_run(para, rf)
                continue

            run = para.add_run()
            if rf.is_tab:
                run._element.append(OxmlElement("w:tab"))
                if rf.text:
                    t = OxmlElement("w:t")
                    t.set(qn("xml:space"), "preserve")
                    t.text = rf.text
                    run._element.append(t)
            elif rf.text:
                t = OxmlElement("w:t")
                t.set(qn("xml:space"), "preserve")
                t.text = rf.text
                run._element.append(t)
            self._apply_run_format(run, rf)

    def _add_hyperlink_run(self, para: Paragraph, rf: RunFormat):
        """Add a hyperlink run to a paragraph using OOXML."""
        # Add relationship
        from docx.opc.constants import RELATIONSHIP_TYPE as RT
        part = self._doc.part
        r_id = part.relate_to(rf.hyperlink_url, "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink", is_external=True)

        # Build hyperlink element
        hyperlink = OxmlElement("w:hyperlink")
        hyperlink.set(qn("r:id"), r_id)

        # Build run inside hyperlink
        run_el = OxmlElement("w:r")
        rPr = OxmlElement("w:rPr")
        # Hyperlink style
        rStyle = OxmlElement("w:rStyle")
        rStyle.set(qn("w:val"), "Hyperlink")
        rPr.append(rStyle)
        # Font
        if rf.font_family:
            rFonts = OxmlElement("w:rFonts")
            for a in ["ascii", "hAnsi", "eastAsia", "cs"]:
                rFonts.set(qn(f"w:{a}"), rf.font_family)
            rPr.append(rFonts)
        if rf.font_size_half_pt:
            sz = OxmlElement("w:sz")
            sz.set(qn("w:val"), str(rf.font_size_half_pt))
            rPr.append(sz)
            szCs = OxmlElement("w:szCs")
            szCs.set(qn("w:val"), str(rf.font_size_half_pt))
            rPr.append(szCs)
        if rf.bold:
            rPr.append(OxmlElement("w:b"))
        run_el.append(rPr)

        # Text
        t = OxmlElement("w:t")
        t.set(qn("xml:space"), "preserve")
        t.text = rf.text
        run_el.append(t)

        hyperlink.append(run_el)
        para._element.append(hyperlink)

    def _set_paragraph_rPr(self, para: Paragraph, half_pt: int, font: str):
        """Set pPr/rPr — paragraph-level default run properties.

        Controls the visual height and font of empty (spacer) paragraphs.
        Both font size AND font family must be set, otherwise Word falls
        back to the Normal style font which may differ.
        """
        pPr = para._element.get_or_add_pPr()
        rPr = pPr.find(qn("w:rPr"))
        if rPr is None:
            rPr = OxmlElement("w:rPr")
            pPr.append(rPr)
        # Font family
        rFonts = rPr.find(qn("w:rFonts"))
        if rFonts is None:
            rFonts = OxmlElement("w:rFonts")
            rPr.insert(0, rFonts)
        for attr in ("ascii", "hAnsi", "eastAsia", "cs"):
            rFonts.set(qn(f"w:{attr}"), font)
        # Font size
        for tag in ["w:sz", "w:szCs"]:
            el = rPr.find(qn(tag))
            if el is None:
                el = OxmlElement(tag)
                rPr.append(el)
            el.set(qn("w:val"), str(half_pt))

    def _clean_lr_paragraph(self, pf: ParagraphFormat) -> ParagraphFormat:
        """Collapse a tab-separated paragraph into a clean left/right layout.

        Source documents often use 5-15 consecutive tab runs to visually
        push dates to the right.  We collapse these into:
          [left text runs] [single tab] [right text runs]
        with a single right-aligned tab stop at the content width.

        If the right side is empty or has no date/location content,
        we just strip the tabs and return the left side only (keeping
        any non-tab runs that follow, like hyperlink text).
        """
        import re

        has_tab_run = any(r.is_tab for r in pf.runs)
        if not has_tab_run:
            return pf

        pf = pf.model_copy(deep=True)

        # Split runs at the first tab into left and right groups
        left_runs: list[RunFormat] = []
        right_runs: list[RunFormat] = []
        seen_tab = False
        for r in pf.runs:
            if r.is_tab and not seen_tab:
                seen_tab = True
                continue
            if r.is_tab and seen_tab:
                continue  # skip subsequent tabs
            if not seen_tab:
                left_runs.append(r)
            else:
                if not right_runs and not r.text.strip():
                    continue  # skip whitespace padding
                right_runs.append(r)

        right_text = "".join(r.text for r in right_runs).strip()

        # Check if right side looks like a date or location
        has_date_or_location = bool(re.search(
            r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|present|\d{4}|,\s*[A-Z]{2}\b)",
            right_text, re.I
        ))

        if not right_text or not has_date_or_location:
            # No meaningful right content — strip tabs, keep all non-tab runs
            all_non_tab = [r for r in pf.runs if not r.is_tab]
            pf.runs = all_non_tab
            pf.tab_stops = []
            return pf

        # Strip trailing whitespace from left, leading from right
        if left_runs and left_runs[-1].text:
            left_runs[-1] = left_runs[-1].model_copy()
            left_runs[-1].text = left_runs[-1].text.rstrip()
        if right_runs and right_runs[0].text:
            right_runs[0] = right_runs[0].model_copy()
            right_runs[0].text = right_runs[0].text.lstrip()

        # Build: left_runs + [single tab run] + right_runs
        tab_fmt = right_runs[0] if right_runs else (left_runs[0] if left_runs else RunFormat())
        tab_run = RunFormat(
            text="",
            is_tab=True,
            font_family=tab_fmt.font_family,
            font_size_half_pt=tab_fmt.font_size_half_pt,
            bold=tab_fmt.bold,
        )
        pf.runs = left_runs + [tab_run] + right_runs
        pf.tab_stops = [TabStop(
            position_twips=self._content_width_twips,
            alignment=TabAlignment.RIGHT,
        )]
        return pf

    def _replay_paragraph(self, pf: ParagraphFormat) -> Paragraph:
        """Create a paragraph that exactly replays captured formatting."""
        para = self._doc.add_paragraph()
        self._apply_paragraph_format(para, pf)
        self._add_runs_to_paragraph(para, pf.runs)
        return para

    # --- Element-based rendering ---

    def _render_from_elements(self):
        """Walk the element sequence and reproduce each paragraph."""
        # Remove default empty paragraph
        if self._doc.paragraphs:
            p = self._doc.paragraphs[0]._element
            p.getparent().remove(p)

        # Build a content lookup for substitutions
        content_map = self._build_content_map()

        # Track which content we've rendered so we can substitute text
        section_idx = 0
        entry_idx = 0
        bullet_idx = 0
        current_section = None

        for el in self._layout.elements:
            pf = el.paragraph_format

            if el.element_type == ElementType.SPACER:
                # Replay spacer as a clean empty paragraph.
                # Strip all runs (some spacers have whitespace-only
                # tab/text runs from the source that bloat the gap).
                pf_copy = pf.model_copy(deep=True)
                pf_copy.runs = []
                pf_copy.tab_stops = []
                if pf_copy.line_value is None:
                    pf_copy.line_value = 240
                    pf_copy.line_rule = "auto"
                para = self._replay_paragraph(pf_copy)
                self._set_paragraph_rPr(para, self._spacer_half_pt, self._font)

            elif el.element_type in (ElementType.NAME, ElementType.CONTACT):
                # Replay with potentially updated contact info
                self._replay_paragraph(pf)

            elif el.element_type == ElementType.SECTION_HEADING:
                # Replay heading — convert drawing-based line to border
                pf_copy = pf.model_copy(deep=True)
                if pf.has_drawing and not pf.bottom_border:
                    from backend.models.resume_layout import BorderDef
                    pf_copy.bottom_border = BorderDef(
                        enabled=True, width_pt=0.75, color="000000"
                    )
                # Remove the empty drawing run and any invisible-color runs
                clean_runs = []
                for r in pf_copy.runs:
                    if r.color and r.color.upper() in ("FFFFFF", "WHITE"):
                        continue
                    if not r.text.strip() and not r.is_tab and not r.font_size_half_pt:
                        continue
                    clean_runs.append(r)
                pf_copy.runs = clean_runs
                self._replay_paragraph(pf_copy)

            elif el.element_type == ElementType.ENTRY_HEADER:
                self._replay_paragraph(self._clean_lr_paragraph(pf))

            elif el.element_type == ElementType.ENTRY_SUBHEADER:
                self._replay_paragraph(self._clean_lr_paragraph(pf))

            elif el.element_type == ElementType.BULLET:
                self._replay_paragraph(pf)

            elif el.element_type == ElementType.SKILLS_ROW:
                self._replay_paragraph(pf)

            else:
                self._replay_paragraph(pf)

    def _build_content_map(self) -> dict:
        """Build a map for content substitution during tailoring."""
        return {}

    # --- Fallback content-based rendering (when no elements captured) ---

    def _render_from_content(self):
        """Fallback renderer when no element sequence is available."""
        if self._doc.paragraphs:
            p = self._doc.paragraphs[0]._element
            p.getparent().remove(p)

        contact = self._content.contact
        font = self._font

        if contact.name:
            para = self._doc.add_paragraph()
            self._set_spacing(para, 240, "auto")
            self._set_align(para, "center")
            run = para.add_run(contact.name)
            self._set_run_font(run, font, half_pt=52, bold=True)

        parts = []
        if contact.phone: parts.append(contact.phone)
        if contact.email: parts.append(contact.email)
        if contact.linkedin: parts.append(contact.linkedin)
        if contact.github: parts.append(contact.github)
        if parts:
            para = self._doc.add_paragraph()
            self._set_align(para, "center")
            run = para.add_run(" | ".join(parts))
            self._set_run_font(run, font)

        for section in self._content.sections:
            self._render_section_fallback(section)

    def _render_section_fallback(self, section: ResumeSection):
        """Fallback section renderer."""
        font = self._font

        # Spacer
        self._add_empty_para(font)

        if section.title:
            para = self._doc.add_paragraph()
            self._set_spacing(para, 240, "auto")
            self._add_border(para)
            run = para.add_run(section.title)
            self._set_run_font(run, font, bold=True)

        self._add_empty_para(font, half_pt=10)

        if section.type == SectionType.EDUCATION:
            for e in section.education_entries:
                date = e.end_date or ""
                self._add_lr_para(font, e.institution, date, bold=True)
                degree = e.degree or ""
                self._add_lr_para(font, degree, e.location or "")
                if e.coursework:
                    self._add_empty_para(font, half_pt=10)
                    para = self._doc.add_paragraph()
                    self._set_spacing(para, 240, "auto")
                    r1 = para.add_run("Coursework: ")
                    self._set_run_font(r1, font, bold=True)
                    r2 = para.add_run(", ".join(e.coursework))
                    self._set_run_font(r2, font)

        elif section.type in (SectionType.EXPERIENCE, SectionType.VOLUNTEER):
            for i, e in enumerate(section.experience_entries):
                if i > 0:
                    self._add_empty_para(font)
                date = ""
                if e.start_date and e.end_date:
                    date = f"{e.start_date} \u2013 {e.end_date}"
                self._add_lr_para(font, e.company, date, bold=True)
                self._add_lr_para(font, e.role, e.location or "", italic=True)
                for b in e.bullets:
                    para = self._doc.add_paragraph()
                    self._set_spacing(para, 240, "auto")
                    run = para.add_run(f"\u2022 {b.text}")
                    self._set_run_font(run, font)

        elif section.type == SectionType.PROJECTS:
            for i, e in enumerate(section.project_entries):
                if i > 0:
                    self._add_empty_para(font)
                para = self._doc.add_paragraph()
                self._set_spacing(para, 240, "auto")
                run = para.add_run(e.name)
                self._set_run_font(run, font, bold=True)
                for b in e.bullets:
                    para = self._doc.add_paragraph()
                    self._set_spacing(para, 240, "auto")
                    run = para.add_run(f"\u2022 {b.text}")
                    self._set_run_font(run, font)

        elif section.type == SectionType.SKILLS:
            for cat in section.skill_categories:
                para = self._doc.add_paragraph()
                self._set_spacing(para, 240, "auto")
                r1 = para.add_run(f"{cat.category}: ")
                self._set_run_font(r1, font, bold=True)
                r2 = para.add_run(", ".join(cat.skills))
                self._set_run_font(r2, font)

    # --- Simple helpers for fallback ---

    def _set_spacing(self, para, line, rule):
        pPr = para._element.get_or_add_pPr()
        sp = OxmlElement("w:spacing")
        sp.set(qn("w:line"), str(line))
        sp.set(qn("w:lineRule"), rule)
        sp.set(qn("w:before"), "0")
        sp.set(qn("w:after"), "0")
        pPr.append(sp)

    def _set_align(self, para, val):
        pPr = para._element.get_or_add_pPr()
        jc = OxmlElement("w:jc")
        jc.set(qn("w:val"), val)
        pPr.append(jc)

    def _set_run_font(self, run, family, half_pt=None, bold=False, italic=False):
        rPr = run._element.get_or_add_rPr()
        rf = OxmlElement("w:rFonts")
        for a in ["ascii", "hAnsi", "eastAsia", "cs"]:
            rf.set(qn(f"w:{a}"), family)
        rPr.insert(0, rf)
        if half_pt:
            for t in ["w:sz", "w:szCs"]:
                el = OxmlElement(t)
                el.set(qn("w:val"), str(half_pt))
                rPr.append(el)
        if bold:
            rPr.append(OxmlElement("w:b"))
        if italic:
            rPr.append(OxmlElement("w:i"))

    def _add_empty_para(self, font, half_pt=None):
        para = self._doc.add_paragraph()
        self._set_spacing(para, 240, "auto")
        run = para.add_run()
        self._set_run_font(run, font, half_pt=half_pt)

    def _add_border(self, para):
        pPr = para._element.get_or_add_pPr()
        pBdr = OxmlElement("w:pBdr")
        bottom = OxmlElement("w:bottom")
        bottom.set(qn("w:val"), "single")
        bottom.set(qn("w:sz"), "6")
        bottom.set(qn("w:space"), "1")
        bottom.set(qn("w:color"), "000000")
        pBdr.append(bottom)
        pPr.append(pBdr)

    def _add_lr_para(self, font, left, right, bold=False, italic=False):
        para = self._doc.add_paragraph()
        self._set_spacing(para, 240, "auto")
        # Right tab stop
        pPr = para._element.get_or_add_pPr()
        tabs = OxmlElement("w:tabs")
        tab = OxmlElement("w:tab")
        tab.set(qn("w:val"), "right")
        tab.set(qn("w:pos"), str(self._content_width_twips))
        tabs.append(tab)
        pPr.append(tabs)

        r1 = para.add_run(left)
        self._set_run_font(r1, font, bold=bold, italic=italic)
        rt = para.add_run()
        self._set_run_font(rt, font, bold=bold, italic=italic)
        rt._element.append(OxmlElement("w:tab"))
        r2 = para.add_run(right)
        self._set_run_font(r2, font, bold=bold, italic=italic)
