"""Resume IR - Content Schema.

Represents WHAT the resume says, independent of formatting.
Every content element has an ID for traceability and diff tracking.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class SectionType(str, Enum):
    HEADER = "header"
    EXPERIENCE = "experience"
    EDUCATION = "education"
    PROJECTS = "projects"
    SKILLS = "skills"
    CERTIFICATIONS = "certifications"
    PUBLICATIONS = "publications"
    AWARDS = "awards"
    VOLUNTEER = "volunteer"
    SUMMARY = "summary"
    OBJECTIVE = "objective"
    CUSTOM = "custom"


# --- Leaf content elements ---


class ContactInfo(BaseModel):
    """Top-level contact / header information."""
    name: str = ""
    email: Optional[str] = None
    phone: Optional[str] = None
    location: Optional[str] = None
    linkedin: Optional[str] = None
    github: Optional[str] = None
    website: Optional[str] = None
    extra_lines: list[str] = Field(default_factory=list)


class Bullet(BaseModel):
    """A single resume bullet point."""
    id: str
    text: str
    source_fact_ids: list[str] = Field(default_factory=list)
    target_keywords: list[str] = Field(default_factory=list)
    original_text: Optional[str] = None  # preserved for diff


class ExperienceEntry(BaseModel):
    """One job / internship / position."""
    id: str
    company: str
    location: Optional[str] = None
    role: str
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    bullets: list[Bullet] = Field(default_factory=list)


class EducationEntry(BaseModel):
    """One school / degree."""
    id: str
    institution: str
    location: Optional[str] = None
    degree: Optional[str] = None
    field_of_study: Optional[str] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    gpa: Optional[str] = None
    bullets: list[Bullet] = Field(default_factory=list)
    coursework: list[str] = Field(default_factory=list)


class ProjectEntry(BaseModel):
    """One project."""
    id: str
    name: str
    url: Optional[str] = None
    technologies: list[str] = Field(default_factory=list)
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    bullets: list[Bullet] = Field(default_factory=list)


class SkillCategory(BaseModel):
    """A category of skills (e.g. 'Languages', 'Frameworks')."""
    id: str
    category: str
    skills: list[str] = Field(default_factory=list)


class GenericEntry(BaseModel):
    """Fallback for sections that don't fit other types."""
    id: str
    title: Optional[str] = None
    subtitle: Optional[str] = None
    date: Optional[str] = None
    description: Optional[str] = None
    bullets: list[Bullet] = Field(default_factory=list)


# --- Section container ---


class ResumeSection(BaseModel):
    """A top-level resume section."""
    id: str
    type: SectionType
    title: str  # e.g. "EXPERIENCE", "EDUCATION"

    # Only ONE of these should be populated, depending on type
    experience_entries: list[ExperienceEntry] = Field(default_factory=list)
    education_entries: list[EducationEntry] = Field(default_factory=list)
    project_entries: list[ProjectEntry] = Field(default_factory=list)
    skill_categories: list[SkillCategory] = Field(default_factory=list)
    generic_entries: list[GenericEntry] = Field(default_factory=list)

    # Raw text lines for sections we can't fully parse
    raw_lines: list[str] = Field(default_factory=list)


class ResumeContent(BaseModel):
    """The complete content schema for a resume."""
    contact: ContactInfo = Field(default_factory=ContactInfo)
    sections: list[ResumeSection] = Field(default_factory=list)
