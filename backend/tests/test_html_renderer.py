"""Tests for HtmlRenderer's editable-preview affordances.

Covers the data-* attributes the editable preview (Preview.tsx) relies on
to map a direct edit back to a real bullet, and to lock the skills section
out of free-text editing. Regression for: direct edits in the preview were
silently dropped on Save because bullets had no stable id in the DOM and
skills rows were editable with nowhere for those edits to go.
"""

from backend.models.resume_content import (
    Bullet, ExperienceEntry, ResumeContent, ResumeSection, SectionType,
    SkillCategory,
)
from backend.models.resume_ir import ResumeIR
from backend.models.resume_layout import FontSpec, ResumeLayout
from backend.renderers.html_renderer import HtmlRenderer, _sanitize_font_family


def _ir(font_family: str = "Arial") -> ResumeIR:
    content = ResumeContent(
        sections=[
            ResumeSection(
                id="sec-exp",
                type=SectionType.EXPERIENCE,
                title="Experience",
                experience_entries=[
                    ExperienceEntry(
                        id="entry-1",
                        company="Acme",
                        role="Engineer",
                        bullets=[Bullet(id="bullet-abc123", text="Built things")],
                    ),
                ],
            ),
            ResumeSection(
                id="sec-skills",
                type=SectionType.SKILLS,
                title="Skills",
                skill_categories=[
                    SkillCategory(id="cat-1", category="Languages", skills=["Python", "SQL"]),
                ],
            ),
        ],
    )
    layout = ResumeLayout(default_font=FontSpec(family=font_family))
    return ResumeIR(content=content, layout=layout)


class TestEditablePreviewAttributes:
    def test_bullet_carries_its_real_id(self):
        html = HtmlRenderer(_ir()).render()
        assert 'data-bullet-id="bullet-abc123"' in html

    def test_skill_row_is_marked_for_lockout(self):
        html = HtmlRenderer(_ir()).render()
        assert 'data-skill-category="Languages"' in html


class TestFontFamilyInjection:
    """Regression: the document font name comes straight from an uploaded
    .docx's raw `w:rFonts` XML attribute (docx_importer.py) with no
    upstream sanitization, and used to be interpolated unescaped into two
    CSS sites (the @font-face rule and .page{font-family:...}) inside a
    <style> block returned as raw HTMLResponse — a crafted .docx could
    break out of <style> into arbitrary HTML/script (stored XSS), executed
    either by <iframe src="/preview/..."> (view mode) or by innerHTML
    injection into the editable preview's Shadow DOM (edit mode).
    """

    def test_style_breakout_payload_cannot_close_the_style_tag(self):
        payload = "Arial'; } </style><img src=x onerror=alert(1)><style>a{font-family:'Arial"
        html = HtmlRenderer(_ir(payload)).render()
        assert "</style><img" not in html
        assert "onerror=" not in html

    def test_quote_breakout_payload_is_stripped(self):
        payload = "Arial'; } body { background: url('javascript:alert(1)') } .x {'"
        html = HtmlRenderer(_ir(payload)).render()
        assert "javascript:" not in html
        assert "'" not in _extract_font_family_value(html)

    def test_benign_font_names_pass_through_unchanged(self):
        for name in ["Garamond", "Times New Roman", "Book Antiqua", "Segoe UI"]:
            assert _sanitize_font_family(name) == name

    def test_malicious_characters_stripped_but_letters_survive(self):
        assert _sanitize_font_family("Aria<l>") == "Arial"
        assert _sanitize_font_family("<script>alert(1)</script>") == "scriptalert1script"

    def test_empty_or_fully_stripped_name_falls_back_to_arial(self):
        assert _sanitize_font_family("") == "Arial"
        assert _sanitize_font_family("<<<>>>") == "Arial"


def _extract_font_family_value(html: str) -> str:
    """Pull the raw value between the quotes of the .page font-family
    declaration, for assertions that need the value in isolation."""
    marker = "font-family: '"
    start = html.index(marker) + len(marker)
    end = html.index("'", start)
    return html[start:end]
