"""HTML Renderer.

Converts a ResumeIR into an HTML/CSS page for live preview.
Derives spacing and font values from the shared element_styles module
so the preview matches the DOCX and PDF outputs.
"""

from __future__ import annotations

import base64
import functools
import re
from collections import Counter
from html import escape
from typing import Optional

from backend.models.resume_content import (
    ResumeContent,
    ResumeSection,
    SectionType,
)
from backend.models.resume_ir import ResumeIR
from backend.models.resume_layout import ElementType, ResumeLayout, StyleDef
from backend.models.tailoring import BulletChange, TailoringResult
from backend.renderers.element_styles import (
    default_font_from_layout,
    default_size_from_layout,
    extract_formatting,
    format_date_range,
    spacer_height_pt,
)
from backend.renderers.fonts import find_font_file

# Font names are interpolated raw into CSS below (both the @font-face rule
# and the .page{font-family:...} declaration). They originate from an
# uploaded .docx's raw `w:rFonts` XML attribute (docx_importer.py), which
# has no upstream sanitization — fully attacker-controlled. A strict
# allowlist (not html.escape()) is used because the value sits inside a
# single-quoted CSS string, not HTML text content: escaping `<`/`&` alone
# doesn't stop a `'` from closing the CSS string and breaking out of the
# surrounding <style> block into arbitrary HTML (stored XSS).
_SAFE_FONT_FAMILY_RE = re.compile(r"[^\w .\-]", re.UNICODE)


def _sanitize_font_family(name: str) -> str:
    """Strip anything outside letters/digits/space/hyphen/period, and cap
    the length, before a font-family name is used in generated CSS."""
    cleaned = _SAFE_FONT_FAMILY_RE.sub("", name).strip()[:100]
    return cleaned or "Arial"


@functools.lru_cache(maxsize=32)
def _font_face_css(family: str) -> str:
    """An `@font-face` rule embedding the real font file for `family`, if
    one can be found on disk — empty string otherwise.

    Without this, the CSS `font-family` declaration below is just a name;
    the browser has to independently guess a local font to match it, with
    no guarantee it picks the same file DOCX/Word would use for the same
    name (e.g. "Garamond" covers several unrelated typefaces with very
    different character widths — a browser silently substituting a wider
    one makes lines wrap earlier than the DOCX, even though both still
    read as "a serif font" at a glance). Embedding the actual bytes here
    removes that guess entirely: once a family name has an `@font-face`,
    the browser always uses it for that name, never a locally-installed
    lookalike. Reuses `find_font_file` (same bundled/system search
    `bullet_measurer.py`'s ReportLab path already relies on) rather than
    duplicating font-discovery logic.
    """
    path = find_font_file(family)
    if not path or path.lower().endswith(".ttc"):
        # .ttc (TrueType Collection) isn't reliably supported by browsers
        # via @font-face — leave those to the existing name-based fallback.
        return ""
    fmt = "opentype" if path.lower().endswith(".otf") else "truetype"
    try:
        data = base64.b64encode(open(path, "rb").read()).decode("ascii")
    except OSError:
        return ""
    mime = "font/otf" if fmt == "opentype" else "font/ttf"
    return (
        f"@font-face {{ font-family: '{family}'; "
        f"src: url(data:{mime};base64,{data}) format('{fmt}'); }}"
    )


def _px(pt: float) -> str:
    return f"{pt * 1.333:.1f}px"


def _in_css(inches: float) -> str:
    return f"{inches}in"


def _word_diff_html(original: str, tailored: str) -> str:
    """Produce inline HTML showing word-level changes on the resume itself.

    Removed words: strikethrough with muted styling
    Added words: highlighted background
    Unchanged words: normal
    """
    orig_words = original.split()
    tail_words = tailored.split()

    m, n = len(orig_words), len(tail_words)
    dp = [[0] * (n + 1) for _ in range(m + 1)]
    for i in range(1, m + 1):
        for j in range(1, n + 1):
            if orig_words[i - 1].lower() == tail_words[j - 1].lower():
                dp[i][j] = dp[i - 1][j - 1] + 1
            else:
                dp[i][j] = max(dp[i - 1][j], dp[i][j - 1])

    i, j = m, n
    ops: list[tuple[str, str]] = []
    while i > 0 or j > 0:
        if i > 0 and j > 0 and orig_words[i - 1].lower() == tail_words[j - 1].lower():
            ops.append(("keep", tail_words[j - 1]))
            i -= 1
            j -= 1
        elif j > 0 and (i == 0 or dp[i][j - 1] >= dp[i - 1][j]):
            ops.append(("add", tail_words[j - 1]))
            j -= 1
        else:
            ops.append(("del", orig_words[i - 1]))
            i -= 1

    ops.reverse()

    parts: list[str] = []
    for op, word in ops:
        ew = escape(word)
        if op == "keep":
            parts.append(ew)
        elif op == "add":
            parts.append(f'<span class="diff-add">{ew}</span>')
        elif op == "del":
            parts.append(f'<span class="diff-del">{ew}</span>')

    return " ".join(parts)


class HtmlRenderer:
    """Render a ResumeIR to HTML/CSS."""

    def __init__(self, ir: ResumeIR, diff_changes: Optional[TailoringResult] = None):
        self._ir = ir
        self._content = ir.content
        self._layout = ir.layout
        self._styles = ir.layout.styles

        # Derive values from element_styles (single source of truth).
        # Sanitized here, once, since this value flows unescaped into
        # generated CSS in both _font_face_css() and render() below.
        self._doc_font = _sanitize_font_family(default_font_from_layout(ir.layout))
        self._doc_size = default_size_from_layout(ir.layout)
        self._spacer_pt = self._detect_spacer_height()
        self._line_height = self._detect_line_height()

        # Build a bullet_id -> BulletChange map for diff highlighting
        self._diffs: dict[str, BulletChange] = {}
        self._added_skills: dict[str, list[str]] = {}
        self._reordered_categories: set[str] = set()
        if diff_changes:
            for c in diff_changes.bullet_changes:
                if c.action == "rewrite" and not c.resolved:
                    self._diffs[c.bullet_id] = c
            # Only highlight skills that are still pending (unresolved).
            # Once accepted, remove the green highlight — the skill is
            # now part of the resume and shouldn't look like a diff.
            for cat, skills in (diff_changes.added_skills or {}).items():
                pending_skills = []
                for skill in skills:
                    key = f"{cat}:{skill}"
                    if key not in diff_changes.additions_accepted:
                        pending_skills.append(skill)
                if pending_skills:
                    self._added_skills[cat] = pending_skills
            for cat in diff_changes.reordered_skills:
                if cat not in diff_changes.reorder_accepted:
                    self._reordered_categories.add(cat)

    # ----------------------------------------------------------
    # Element-derived formatting (shared source of truth)
    # ----------------------------------------------------------

    def _detect_spacer_height(self) -> float:
        """Normalised spacer height in pt, matching DOCX/PDF renderers."""
        heights: Counter = Counter()
        for el in self._layout.elements:
            if el.element_type == ElementType.SPACER:
                h = spacer_height_pt(el, self._doc_size)
                h = round(h * 2) / 2
                heights[h] += 1
        if heights:
            return heights.most_common(1)[0][0]
        # Fallback: 5pt font single-spaced
        return 5.0 * 1.2

    def _detect_line_height(self) -> float:
        """Line-height ratio derived from elements."""
        for el in self._layout.elements:
            if el.element_type in (ElementType.BULLET, ElementType.ENTRY_HEADER):
                fmt = extract_formatting(el, self._doc_font, self._doc_size)
                if fmt.font_size_pt > 0:
                    return round(fmt.line_height_pt / fmt.font_size_pt, 2)
        return 1.2

    def _font(self) -> str:
        return self._doc_font

    def _name_size(self) -> float:
        for el in self._layout.elements:
            if el.element_type == ElementType.NAME:
                fmt = extract_formatting(el, self._doc_font, self._doc_size)
                return fmt.font_size_pt
        s = self._styles.get("name")
        return s.font.size_pt if s else 26.0

    def _heading_size(self) -> float:
        for el in self._layout.elements:
            if el.element_type == ElementType.SECTION_HEADING:
                fmt = extract_formatting(el, self._doc_font, self._doc_size)
                return fmt.font_size_pt
        s = self._styles.get("section_heading_with_rule") or self._styles.get("section_heading")
        return s.font.size_pt if s else 12.0

    def _body_size(self) -> float:
        return self._doc_size

    # ----------------------------------------------------------
    # Render
    # ----------------------------------------------------------

    def render(self) -> str:
        page = self._layout.page
        font = self._font()
        body_sz = _px(self._body_size())
        name_sz = _px(self._name_size())
        heading_sz = _px(self._heading_size())
        spacer_h = _px(self._spacer_pt)
        lh = self._line_height
        font_face = _font_face_css(font)

        css = f"""
{font_face}
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{
  background: #e0e0e0;
  display: flex;
  justify-content: center;
  padding: 20px 0;
}}
.page {{
  background: white;
  box-shadow: 0 2px 8px rgba(0,0,0,0.15);
  width: {_in_css(page.width_in)};
  min-height: {_in_css(page.height_in)};
  padding: {_in_css(page.margin_top_in)} {_in_css(page.margin_right_in)} {_in_css(page.margin_bottom_in)} {_in_css(page.margin_left_in)};
  font-family: '{font}', 'Times New Roman', serif;
  font-size: {body_sz};
  line-height: {lh};
  color: #000;
}}
.name {{
  font-size: {name_sz};
  font-weight: bold;
  text-align: center;
  line-height: {lh};
}}
.contact {{
  text-align: center;
  line-height: {lh};
}}
.spacer {{ height: {spacer_h}; }}
.heading {{
  font-size: {heading_sz};
  font-weight: bold;
  border-bottom: 0.75pt solid #000;
  padding-bottom: 1px;
  line-height: {lh};
}}
.lr {{
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  line-height: {lh};
}}
.lr .l {{ flex-shrink: 1; }}
.lr .r {{ flex-shrink: 0; text-align: right; white-space: nowrap; padding-left: 8px; }}
.bold {{ font-weight: bold; }}
.italic {{ font-style: italic; }}
.bullet {{
  line-height: {lh};
}}
.label {{ font-weight: bold; }}
a.hl {{ color: #1a0dab; text-decoration: underline; }}
.diff-add {{
  background: #c1d4c6;
  border-radius: 2px;
  padding: 0 2px;
}}
.diff-del {{
  background: #d9c2c2;
  text-decoration: line-through;
  border-radius: 2px;
  padding: 0 2px;
  color: #7a5a5a;
  opacity: 0.7;
}}
"""

        body = self._build_body()

        return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<style>{css}</style>
</head>
<body>
<div class="page">
{body}
</div>
</body>
</html>"""

    # ----------------------------------------------------------
    # Body builder
    # ----------------------------------------------------------

    def _build_body(self) -> str:
        parts: list[str] = []
        c = self._content.contact

        if c.name:
            parts.append(f'<div class="name">{escape(c.name)}</div>')

        cp = []
        if c.phone: cp.append(escape(c.phone))
        if c.email: cp.append(escape(c.email))
        if c.linkedin: cp.append(escape(c.linkedin))
        if c.github: cp.append(escape(c.github))
        if c.website: cp.append(escape(c.website))
        if cp:
            parts.append(f'<div class="contact">{" | ".join(cp)}</div>')

        for section in self._content.sections:
            parts.append(self._render_section(section))

        return "\n".join(parts)

    def _render_section(self, s: ResumeSection) -> str:
        parts: list[str] = []

        parts.append('<div class="spacer"></div>')

        if s.title:
            parts.append(f'<div class="heading">{escape(s.title)}</div>')
            parts.append('<div class="spacer"></div>')

        if s.type == SectionType.EDUCATION:
            for e in s.education_entries:
                parts.append(self._render_education(e))
        elif s.type in (SectionType.EXPERIENCE, SectionType.VOLUNTEER):
            for i, e in enumerate(s.experience_entries):
                if i > 0:
                    parts.append('<div class="spacer"></div>')
                parts.append(self._render_experience(e))
        elif s.type == SectionType.PROJECTS:
            for i, e in enumerate(s.project_entries):
                if i > 0:
                    parts.append('<div class="spacer"></div>')
                parts.append(self._render_project(e))
        elif s.type == SectionType.SKILLS:
            for cat in s.skill_categories:
                parts.append(self._render_skill(cat))
        else:
            for e in s.generic_entries:
                parts.append(self._render_generic(e))
        for line in s.raw_lines:
            parts.append(f'<div>{escape(line)}</div>')

        return "\n".join(parts)

    def _lr(self, left: str, right: str, left_cls: str = "", right_cls: str = "") -> str:
        return (
            f'<div class="lr">'
            f'<span class="l {left_cls}">{escape(left)}</span>'
            f'<span class="r {right_cls}">{escape(right)}</span>'
            f'</div>'
        )

    def _render_experience(self, e) -> str:
        parts: list[str] = []
        date = format_date_range(e.start_date, e.end_date)

        parts.append(self._lr(e.company, date, "bold", "bold"))
        parts.append(self._lr(e.role, e.location or "", "italic", ""))

        for b in e.bullets:
            parts.append(self._render_bullet(b))
        return "\n".join(parts)

    def _render_education(self, e) -> str:
        parts: list[str] = []
        date = format_date_range(e.start_date, e.end_date)

        parts.append(self._lr(e.institution, date, "bold", "bold"))

        degree = e.degree or ""
        if e.gpa:
            degree += f" | GPA: {e.gpa}"
        parts.append(self._lr(degree, e.location or ""))

        if e.coursework:
            parts.append('<div class="spacer"></div>')
            parts.append(
                f'<div><span class="label">Coursework:</span> {escape(", ".join(e.coursework))}</div>'
            )

        for b in e.bullets:
            parts.append(self._render_bullet(b))
        return "\n".join(parts)

    def _render_project(self, e) -> str:
        parts: list[str] = []
        date = format_date_range(e.start_date, e.end_date)

        name_html = escape(e.name)
        if hasattr(e, 'url') and e.url:
            if "|" in e.name:
                name_part, link_text = e.name.rsplit("|", 1)
                link_text = link_text.strip()
                name_html = f'{escape(name_part.strip())} | <a class="hl" href="{escape(e.url)}" target="_blank">{escape(link_text)}</a>'
            else:
                name_html = f'<a class="hl" href="{escape(e.url)}" target="_blank">{escape(e.name)}</a>'

        if date:
            parts.append(
                f'<div class="lr">'
                f'<span class="l bold">{name_html}</span>'
                f'<span class="r bold">{escape(date)}</span>'
                f'</div>'
            )
        else:
            parts.append(f'<div class="bold">{name_html}</div>')
        for b in e.bullets:
            parts.append(self._render_bullet(b))
        return "\n".join(parts)

    def _render_skill(self, cat) -> str:
        # data-skill-category marks this row so the editable preview can
        # lock it from direct text edits — skills already have a dedicated
        # accept/reject review flow, and free-text edits here can't be
        # parsed back into individual skill records without risking
        # silently corrupting the category (see Preview.tsx).
        added_set = {s.lower() for s in self._added_skills.get(cat.category, [])}
        is_reordered = cat.category in self._reordered_categories

        if not added_set and not is_reordered:
            return f'<div data-skill-category="{escape(cat.category)}"><span class="label">{escape(cat.category)}:</span> {escape(", ".join(cat.skills))}</div>'

        skill_parts = []
        for i, skill in enumerate(cat.skills):
            prefix = ", " if i > 0 else ""
            if skill.lower() in added_set:
                skill_parts.append(f'{prefix}<span class="diff-add">{escape(skill)}</span>')
            else:
                skill_parts.append(f'{prefix}{escape(skill)}')

        return f'<div data-skill-category="{escape(cat.category)}"><span class="label">{escape(cat.category)}:</span> {"".join(skill_parts)}</div>'

    def _render_generic(self, e) -> str:
        parts: list[str] = []
        if e.title:
            if e.subtitle:
                parts.append(self._lr(e.title, e.subtitle, "bold", ""))
            else:
                parts.append(f'<div class="bold">{escape(e.title)}</div>')
        for b in e.bullets:
            parts.append(self._render_bullet(b))
        return "\n".join(parts)

    def _render_bullet(self, b) -> str:
        # data-bullet-id lets the editable preview map a direct edit back to
        # the real BulletChange/Bullet \u2014 without it, the frontend has no way
        # to know which bullet changed and previously fabricated a random id
        # per edit, so nothing typed here ever matched anything on save.
        change = self._diffs.get(b.id)
        if change and change.original_text != change.tailored_text:
            diff_html = _word_diff_html(change.original_text, change.tailored_text)
            return f'<div class="bullet" data-bullet-id="{escape(b.id)}">\u2022 {diff_html}</div>'
        return f'<div class="bullet" data-bullet-id="{escape(b.id)}">\u2022 {escape(b.text)}</div>'
