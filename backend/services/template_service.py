"""Template Service.

Extract formatting templates from uploaded resumes,
save/load them, and list available templates.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from backend.models.format_template import FormatTemplate, StyleEntry
from backend.models.resume_ir import ResumeIR
from backend.models.resume_layout import ResumeLayout

TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "templates"


def extract_template(ir: ResumeIR, name: str = "", description: str = "") -> FormatTemplate:
    """Extract a formatting template from a parsed resume."""
    layout = ir.layout

    template = FormatTemplate(
        name=name,
        description=description,
        page=layout.page.model_copy(),
        normal_style=layout.normal_style.model_copy(),
        body_font_family=layout.body_font_family,
        body_font_size_pt=layout.body_font_size_pt,
        body_line_spacing_val=layout.body_line_spacing_val,
        spacer_size_half_pt=layout.spacer_size_half_pt,
    )

    # Extract per-role styles
    for role, style_def in layout.styles.items():
        template.styles[role] = StyleEntry(
            font_family=style_def.font.family,
            font_size_pt=style_def.font.size_pt,
            bold=style_def.font.bold,
            italic=style_def.font.italic,
            line_spacing=style_def.spacing.line_spacing,
            indent_left_in=style_def.indent.left_in,
        )

    # Check for heading borders
    for role in ("section_heading", "section_heading_with_rule"):
        style = layout.styles.get(role)
        if style:
            # Check elements for bottom borders
            from backend.models.resume_layout import ElementType
            for el in layout.elements:
                if el.element_type == ElementType.SECTION_HEADING:
                    if el.paragraph_format.bottom_border and el.paragraph_format.bottom_border.enabled:
                        template.heading_has_border = True
                        template.heading_border_width_pt = el.paragraph_format.bottom_border.width_pt
                        break
                    if el.paragraph_format.has_drawing:
                        template.heading_has_border = True
                        break

    return template


def save_template(template: FormatTemplate, template_id: str):
    """Save a template to disk."""
    TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
    path = TEMPLATES_DIR / f"{template_id}.json"
    path.write_text(template.model_dump_json(indent=2))


def load_template(template_id: str) -> Optional[FormatTemplate]:
    """Load a template from disk."""
    path = TEMPLATES_DIR / f"{template_id}.json"
    if not path.exists():
        return None
    return FormatTemplate.model_validate_json(path.read_text())


def list_templates() -> list[dict]:
    """List all available templates."""
    TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
    templates = []
    for path in sorted(TEMPLATES_DIR.glob("*.json")):
        try:
            t = FormatTemplate.model_validate_json(path.read_text())
            templates.append({
                "id": path.stem,
                "name": t.name,
                "description": t.description,
                "font": t.body_font_family,
                "size": t.body_font_size_pt,
                "margins": {
                    "top": t.page.margin_top_in,
                    "bottom": t.page.margin_bottom_in,
                    "left": t.page.margin_left_in,
                    "right": t.page.margin_right_in,
                },
            })
        except Exception:
            continue
    return templates


def delete_template(template_id: str) -> bool:
    """Delete a template."""
    path = TEMPLATES_DIR / f"{template_id}.json"
    if path.exists():
        path.unlink()
        return True
    return False
