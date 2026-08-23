"""Tailoring models.

Resume bank for storing facts and alternative bullets per experience,
and tailoring result models for tracking changes.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field, field_validator


# --- Resume Bank ---


class Fact(BaseModel):
    """A single factual claim that can support bullet generation."""
    id: str
    text: str
    tags: list[str] = Field(default_factory=list)  # e.g. ["python", "optimization"]

    @field_validator("id")
    @classmethod
    def id_not_empty(cls, v: str) -> str:
        """Ensure id is not empty."""
        if not v or not v.strip():
            raise ValueError("Fact.id cannot be empty")
        return v

    @field_validator("text")
    @classmethod
    def text_not_empty(cls, v: str) -> str:
        """Ensure text is not empty."""
        if not v or not v.strip():
            raise ValueError("Fact.text cannot be empty")
        return v


class ApprovedBullet(BaseModel):
    """A pre-written, approved bullet point."""
    id: str
    text: str
    source_fact_ids: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)

    @field_validator("id")
    @classmethod
    def id_not_empty(cls, v: str) -> str:
        """Ensure id is not empty."""
        if not v or not v.strip():
            raise ValueError("ApprovedBullet.id cannot be empty")
        return v

    @field_validator("text")
    @classmethod
    def text_not_empty(cls, v: str) -> str:
        """Ensure text is not empty."""
        if not v or not v.strip():
            raise ValueError("ApprovedBullet.text cannot be empty")
        return v


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

    @field_validator("bullet_id")
    @classmethod
    def bullet_id_not_empty(cls, v: str) -> str:
        """Ensure bullet_id is not empty."""
        if not v or not v.strip():
            raise ValueError("BulletChange.bullet_id cannot be empty")
        return v

    @field_validator("action")
    @classmethod
    def action_valid(cls, v: str) -> str:
        """Ensure action is one of the allowed values."""
        allowed = {"rewrite", "remove", "keep", "reorder", "add"}
        if v not in allowed:
            raise ValueError(f"BulletChange.action must be one of {allowed}, got {v}")
        return v


class BulletSnapshot(BaseModel):
    """Pre-calculated snapshot of what skills/keywords are in a bullet.

    Calculated at tailoring time for the tailored_text.
    Used at accept/reject time to quickly recalculate coverage without re-scanning.

    IMPORTANT: Snapshots track DIRECT matches only (explicit keywords in text).
    IMPLIES matches (semantic inference) are NOT included in snapshots to prevent
    inflated coverage percentages. Coverage metrics reflect only proven keywords.
    """
    bullet_id: str
    # DIRECT matches only — exact keyword or alias found in text
    matched_required_skills: list[str] = Field(default_factory=list)
    matched_technical_keywords: list[str] = Field(default_factory=list)
    matched_responsibilities: list[str] = Field(default_factory=list)  # text snippets, not full

    # NEW: Matches from new categorized system
    matched_technical_requirements: list[str] = Field(default_factory=list)  # JDRequirement.keyword_phrase
    matched_deliverables: list[str] = Field(default_factory=list)  # ConcreteDeliverable.phrase


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

    # Quick Win: Pre-calculated snapshots for fast coverage recalculation
    # One snapshot per bullet, containing matched skills/keywords at tailoring time
    # On accept/reject, just sum snapshots of accepted bullets (no text scanning)
    bullet_snapshots: list[BulletSnapshot] = Field(default_factory=list)

    # Coverage metrics (original, kept for backward compatibility)
    required_skill_coverage: float = 0.0
    technical_keyword_coverage: float = 0.0
    responsibility_coverage: float = 0.0
    preferred_skill_coverage: float = 0.0

    # --- NEW (Simplified Metrics) ---
    # Skills and Activities matched from new categorized requirements
    skills_matched_coverage: float = 0.0  # % of technical_requirements matched
    activities_matched_coverage: float = 0.0  # % of deliverables matched

    # Detailed coverage breakdown (for Review tab UI)
    required_skills_matched: list[str] = Field(default_factory=list)
    required_skills_missing: list[str] = Field(default_factory=list)
    technical_keywords_matched: list[str] = Field(default_factory=list)
    technical_keywords_missing: list[str] = Field(default_factory=list)
    responsibilities_matched: list[str] = Field(default_factory=list)
    responsibilities_missing: list[str] = Field(default_factory=list)

    # Skills and Activities breakdown (from new categorized system)
    skills_matched: list[str] = Field(default_factory=list)
    skills_missing: list[str] = Field(default_factory=list)
    activities_matched: list[str] = Field(default_factory=list)
    activities_missing: list[str] = Field(default_factory=list)

    # --- NEW (Phase 4): Two-tier coverage metrics ---
    # Separates what ATS will find from what humans would understand
    ats_coverage: float = 0.0  # What ATS literally finds (0.0-1.0)
    human_coverage: float = 0.0  # What humans would recognize (0.0-1.0)
    coverage_gap: float = 0.0  # Difference: human - ats (what ATS misses but humans see)

    # Skills reordering: category -> [reordered skills]
    reordered_skills: dict[str, list[str]] = Field(default_factory=dict)
    reorder_accepted: dict[str, bool] = Field(default_factory=dict)  # category -> accepted

    # Skills additions: category -> [new skills to add]
    added_skills: dict[str, list[str]] = Field(default_factory=dict)
    additions_accepted: dict[str, bool] = Field(default_factory=dict)  # "cat:skill" -> accepted

    # Planning pass metadata
    planning_used: bool = False
    planning_error: Optional[str] = None
    planning_duration_ms: int = 0

    # Bullet fitting report
    fitting_report: Optional[dict] = None
    fitting_violations: list[str] = Field(default_factory=list)

    # Quick Win #4: Pre-computed skill occurrence matrix for fast recalculation
    # {skill_name: {bullet_id: True/False}} — enables O(1) coverage lookup instead of O(text_length)
    skill_occurrence_matrix: Optional[dict[str, dict[str, bool]]] = None

    # Debug log — tracks what the LLM returned and what the safety net did
    debug_log: list[str] = Field(default_factory=list)
