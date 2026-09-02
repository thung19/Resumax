"""Plain Text Renderer.

Generates ATS/application-portal-friendly plain text from a ResumeIR.
No markdown syntax, no invisible formatting characters, no unicode
beyond basic bullet points.
"""

from __future__ import annotations

from backend.models.resume_content import (
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


class TextRenderer:
    """Render a ResumeIR to plain text."""

    def __init__(self, ir: ResumeIR, line_width: int = 80):
        self._content = ir.content
        self._line_width = line_width

    def render(self) -> str:
        """Return the full resume as plain text."""
        lines: list[str] = []

        # Contact
        contact = self._content.contact
        if contact.name:
            lines.append(contact.name)

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
        if contact_parts:
            lines.append(" | ".join(contact_parts))

        # Sections
        for section in self._content.sections:
            lines.append("")
            lines.extend(self._render_section(section))

        return "\n".join(lines) + "\n"

    def _render_section(self, section: ResumeSection) -> list[str]:
        lines: list[str] = []

        if section.title:
            lines.append(section.title.upper())

        if section.type == SectionType.EDUCATION:
            for entry in section.education_entries:
                lines.extend(self._render_education_entry(entry))

        elif section.type in (SectionType.EXPERIENCE, SectionType.VOLUNTEER):
            for i, entry in enumerate(section.experience_entries):
                if i > 0:
                    lines.append("")
                lines.extend(self._render_experience_entry(entry))

        elif section.type == SectionType.PROJECTS:
            for i, entry in enumerate(section.project_entries):
                if i > 0:
                    lines.append("")
                lines.extend(self._render_project_entry(entry))

        elif section.type == SectionType.SKILLS:
            for cat in section.skill_categories:
                lines.append(f"{cat.category}: {', '.join(cat.skills)}")

        else:
            for entry in section.generic_entries:
                lines.extend(self._render_generic_entry(entry))

        for line in section.raw_lines:
            lines.append(line)

        return lines

    def _render_experience_entry(self, entry: ExperienceEntry) -> list[str]:
        lines: list[str] = []

        # Company + date
        date_str = ""
        if entry.start_date and entry.end_date:
            date_str = f"{entry.start_date} - {entry.end_date}"
        elif entry.start_date:
            date_str = entry.start_date
        elif entry.end_date:
            date_str = entry.end_date

        header = entry.company
        if date_str:
            header = f"{entry.company} | {date_str}"
        lines.append(header)

        # Role + location
        role_line = entry.role
        if entry.location:
            role_line = f"{entry.role} | {entry.location}"
        lines.append(role_line)

        # Bullets
        for bullet in entry.bullets:
            lines.append(f"- {bullet.text}")

        return lines

    def _render_education_entry(self, entry: EducationEntry) -> list[str]:
        lines: list[str] = []

        header = entry.institution
        if entry.end_date:
            header = f"{entry.institution} | {entry.end_date}"
        lines.append(header)

        degree_line = ""
        if entry.degree:
            degree_line = entry.degree
        if entry.gpa:
            degree_line += f" | GPA: {entry.gpa}"
        if entry.location:
            degree_line += f" | {entry.location}"
        if degree_line:
            lines.append(degree_line)

        if entry.coursework:
            lines.append(f"Coursework: {', '.join(entry.coursework)}")

        for bullet in entry.bullets:
            lines.append(f"- {bullet.text}")

        return lines

    def _render_project_entry(self, entry: ProjectEntry) -> list[str]:
        lines: list[str] = []

        header = entry.name
        if entry.start_date and entry.end_date:
            header = f"{entry.name} | {entry.start_date} - {entry.end_date}"
        lines.append(header)

        for bullet in entry.bullets:
            lines.append(f"- {bullet.text}")

        return lines

    def _render_generic_entry(self, entry: GenericEntry) -> list[str]:
        lines: list[str] = []
        if entry.title:
            lines.append(entry.title)
        if entry.subtitle:
            lines.append(entry.subtitle)
        for bullet in entry.bullets:
            lines.append(f"- {bullet.text}")
        return lines
