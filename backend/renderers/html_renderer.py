"""HTML Renderer.

Converts a ResumeIR into an HTML/CSS page for live preview.
Replicates the Layout Schema's exact formatting:
- Uses detected fonts and sizes
- Zero line spacing padding — spacers are explicit elements
- Left/right rows via flexbox
- Inline bullet characters, no list markup
"""

from __future__ import annotations

import re
from html import escape
from typing import Optional

from backend.models.resume_content import (
    ResumeContent,
    ResumeSection,
    SectionType,
)
from backend.models.resume_ir import ResumeIR
from backend.models.resume_layout import ResumeLayout, StyleDef
from backend.models.tailoring import BulletChange, TailoringResult


def _px(pt: float) -> str:
    return f"{pt * 1.333:.1f}px"


def _in_css(inches: float) -> str:
    return f"{inches}in"


def _word_diff_html(original: str, tailored: str) -> str:
    """Produce inline HTML showing word-level changes on the resume itself.

    Removed words: red background + strikethrough
    Added words: green background
    Unchanged words: normal
    """
    orig_words = original.split()
    tail_words = tailored.split()

    # LCS to find common subsequence
    m, n = len(orig_words), len(tail_words)
    dp = [[0] * (n + 1) for _ in range(m + 1)]
    for i in range(1, m + 1):
        for j in range(1, n + 1):
            if orig_words[i - 1].lower() == tail_words[j - 1].lower():
                dp[i][j] = dp[i - 1][j - 1] + 1
            else:
                dp[i][j] = max(dp[i - 1][j], dp[i][j - 1])

    # Backtrack to find diff
    parts: list[str] = []
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
        # Build a bullet_id -> BulletChange map for diff highlighting
        # Only UNRESOLVED rewrites get diff highlighting.
        # Resolved ones (accepted or rejected) show clean text.
        self._diffs: dict[str, BulletChange] = {}
        if diff_changes:
            for c in diff_changes.bullet_changes:
                if c.action == "rewrite" and not c.resolved:
                    self._diffs[c.bullet_id] = c

    def _font(self) -> str:
        """Get primary font family."""
        for role in ["name", "entry_header", "bullet"]:
            s = self._styles.get(role)
            if s and s.font.family and s.font.family.lower() not in ("arial", "minorhansi"):
                return s.font.family
        return "Garamond"

    def _name_size(self) -> float:
        s = self._styles.get("name")
        return s.font.size_pt if s else 26.0

    def _heading_size(self) -> float:
        s = self._styles.get("section_heading_with_rule") or self._styles.get("section_heading")
        return s.font.size_pt if s else 12.0

    def _body_size(self) -> float:
        s = self._styles.get("bullet") or self._styles.get("entry_header")
        return s.font.size_pt if s else 10.0

    def render(self) -> str:
        page = self._layout.page
        font = self._font()
        body_sz = _px(self._body_size())
        name_sz = _px(self._name_size())
        heading_sz = _px(self._heading_size())

        css = f"""
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
  line-height: 1.15;
  color: #000;
}}
.name {{
  font-size: {name_sz};
  font-weight: bold;
  text-align: center;
  line-height: 1.15;
}}
.contact {{
  text-align: center;
  line-height: 1.15;
}}
.spacer {{ height: {_px(5)}; }}
.spacer-sm {{ height: {_px(3)}; }}
.heading {{
  font-size: {heading_sz};
  font-weight: bold;
  border-bottom: 0.75pt solid #000;
  padding-bottom: 1px;
  line-height: 1.15;
}}
.lr {{
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  line-height: 1.15;
}}
.lr .l {{ flex-shrink: 1; }}
.lr .r {{ flex-shrink: 0; text-align: right; white-space: nowrap; padding-left: 8px; }}
.bold {{ font-weight: bold; }}
.italic {{ font-style: italic; }}
.bullet {{
  line-height: 1.15;
}}
.label {{ font-weight: bold; }}
a.hl {{ color: #1a0dab; text-decoration: underline; }}
.diff-add {{
  background: #d1fae5;
  border-radius: 2px;
  padding: 0 2px;
}}
.diff-del {{
  background: #fee2e2;
  text-decoration: line-through;
  border-radius: 2px;
  padding: 0 2px;
  color: #991b1b;
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
        date = ""
        if e.start_date and e.end_date:
            date = f"{e.start_date} \u2013 {e.end_date}"
        elif e.start_date:
            date = e.start_date
        elif e.end_date:
            date = e.end_date

        parts.append(self._lr(e.company, date, "bold", "bold"))
        parts.append(self._lr(e.role, e.location or "", "italic", ""))

        for b in e.bullets:
            parts.append(self._render_bullet(b))
        return "\n".join(parts)

    def _render_education(self, e) -> str:
        parts: list[str] = []
        date = e.end_date or ""
        if e.start_date and e.end_date:
            date = f"{e.start_date} \u2013 {e.end_date}"

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
        date = ""
        if e.start_date and e.end_date:
            date = f"{e.start_date} \u2013 {e.end_date}"

        # Build name with optional hyperlink
        name_html = escape(e.name)
        if hasattr(e, 'url') and e.url:
            # The name might contain "| GitHub" — make "GitHub" the link
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
        return f'<div><span class="label">{escape(cat.category)}:</span> {escape(", ".join(cat.skills))}</div>'

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
        """Render a bullet, with inline diff highlighting if a change exists."""
        change = self._diffs.get(b.id)
        if change and change.original_text != change.tailored_text:
            diff_html = _word_diff_html(change.original_text, change.tailored_text)
            return f'<div class="bullet">\u2022 {diff_html}</div>'
        return f'<div class="bullet">\u2022 {escape(b.text)}</div>'
