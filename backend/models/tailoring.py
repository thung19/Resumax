"""Tailoring models.

Resume bank for storing facts and alternative bullets per experience,
and tailoring result models for tracking changes.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


# --- Resume Bank ---


class Fact(BaseModel):
    """A single factual claim that can support bullet generation."""
    id: str
    text: str
    tags: list[str] = Field(default_factory=list)  # e.g. ["python", "optimization"]


class ApprovedBullet(BaseModel):
    """A pre-written, approved bullet point."""
    id: str
    text: str
    source_fact_ids: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


class ExperienceBank(BaseModel):
    """Resume bank entry for a single experience."""
    experience_id: str  # matches ExperienceEntry.id or a stable key
    company: str = ""
    role: str = ""

    facts: list[Fact] = Field(default_factory=list)
    approved_bullets: list[ApprovedBullet] = Field(default_factory=list)
    alternative_bullets: list[ApprovedBullet] = Field(default_factory=list)

    technologies: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    metrics: list[str] = Field(default_factory=list)


class ProjectBank(BaseModel):
    """Resume bank entry for a project."""
    project_id: str
    name: str = ""

    facts: list[Fact] = Field(default_factory=list)
    approved_bullets: list[ApprovedBullet] = Field(default_factory=list)
    alternative_bullets: list[ApprovedBullet] = Field(default_factory=list)

    technologies: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)


class ResumeBank(BaseModel):
    """The full resume bank — larger than any single resume."""
    experiences: list[ExperienceBank] = Field(default_factory=list)
    projects: list[ProjectBank] = Field(default_factory=list)

    additional_skills: list[str] = Field(default_factory=list)
    certifications: list[str] = Field(default_factory=list)

    def find_experience(self, experience_id: str) -> Optional[ExperienceBank]:
        for exp in self.experiences:
            if exp.experience_id == experience_id:
                return exp
        return None

    def find_project(self, project_id: str) -> Optional[ProjectBank]:
        for proj in self.projects:
            if proj.project_id == project_id:
                return proj
        return None


# --- Tailoring Results ---


class BulletChange(BaseModel):
    """A single bullet change in the tailored resume."""
    bullet_id: str
    original_text: str
    tailored_text: str
    action: str = "rewrite"  # rewrite | reorder | add | remove | keep
    source_fact_ids: list[str] = Field(default_factory=list)
    target_keywords: list[str] = Field(default_factory=list)
    reason: str = ""
    accepted: bool = True  # user can reject
    resolved: bool = False  # has the user explicitly accepted/rejected this?


class KeywordCoverage(BaseModel):
    """Keyword coverage report."""
    keyword: str
    importance: float = 0.5
    status: str = "missing"  # matched | added | missing
    source: str = ""  # "present in resume" | "added via rewrite" | "not in resume bank"


class TailoringResult(BaseModel):
    """The result of tailoring a resume to a job description."""
    resume_id: str
    job_title: str = ""

    bullet_changes: list[BulletChange] = Field(default_factory=list)
    keyword_coverage: list[KeywordCoverage] = Field(default_factory=list)

    # Coverage metrics
    required_skill_coverage: float = 0.0
    technical_keyword_coverage: float = 0.0
    responsibility_coverage: float = 0.0
    preferred_skill_coverage: float = 0.0

    # Skills reordering
    reordered_skills: dict[str, list[str]] = Field(default_factory=dict)

    # Skills additions: category -> [new skills to add]
    added_skills: dict[str, list[str]] = Field(default_factory=dict)

    # Planning pass metadata
    planning_used: bool = False
    planning_error: Optional[str] = None
    planning_duration_ms: int = 0
