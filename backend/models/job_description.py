"""Job Description models.

Structured representation of an analyzed job description
with extracted skills, responsibilities, and relevance weights.
"""

from __future__ import annotations

from typing import Optional, Any
from pydantic import BaseModel, Field


class WeightedItem(BaseModel):
    """An item with an importance weight (0.0 - 1.0)."""
    name: str
    importance: float = 0.5
    category: str = ""  # e.g. "language", "framework", "tool"


class Responsibility(BaseModel):
    text: str
    importance: float = 0.5
    keywords: list[str] = Field(default_factory=list)


class JobAnalysis(BaseModel):
    """Structured analysis of a job description."""
    raw_text: str = ""

    # Role info
    job_title: str = ""
    company: str = ""
    location: str = ""
    job_type: str = ""  # full-time, internship, contract

    # Skills by category
    required_skills: list[WeightedItem] = Field(default_factory=list)
    preferred_skills: list[WeightedItem] = Field(default_factory=list)

    # Technology breakdown
    programming_languages: list[WeightedItem] = Field(default_factory=list)
    frameworks: list[WeightedItem] = Field(default_factory=list)
    databases: list[WeightedItem] = Field(default_factory=list)
    infrastructure: list[WeightedItem] = Field(default_factory=list)
    tools: list[WeightedItem] = Field(default_factory=list)

    # Other
    methodologies: list[WeightedItem] = Field(default_factory=list)
    domain_knowledge: list[WeightedItem] = Field(default_factory=list)
    soft_skills: list[WeightedItem] = Field(default_factory=list)

    # Responsibilities
    responsibilities: list[Responsibility] = Field(default_factory=list)

    # Keyword analysis
    repeated_terms: list[WeightedItem] = Field(default_factory=list)
    ats_phrases: list[str] = Field(default_factory=list)

    # --- New: Categorized Requirements (incremental migration) ---
    # These complement the existing fields above.
    # Gradually replacing required_skills / preferred_skills with structured requirements.

    # Technical keywords with ATS + theme tracking
    technical_requirements: list[JDRequirement] = Field(default_factory=list)
    # Examples: {"keyword_phrase": "Python", "ats_searchable": ["python"]}
    #           {"keyword_phrase": "Agile methodologies", "ats_searchable": ["agile", "scrum"],
    #            "theme_indicators": ["scrum", "sprints", "iterative"]}

    # Concrete deliverables (activities that can be keyword-tracked)
    deliverables: list[ConcreteDeliverable] = Field(default_factory=list)
    # Examples: {"phrase": "Dashboard creation", "ats_searchable": ["dashboard", "dashboards"]}
    #           {"phrase": "API design", "ats_searchable": ["api", "design"]}

    # Behavioral/soft requirements (demonstrated through context)
    behavioral_requirements: list[BehavioralRequirement] = Field(default_factory=list)
    # Examples: {"phrase": "Cross-team collaboration", "evidence_indicators": ["cross-functional", "team"]}
    #           {"phrase": "Mentoring", "evidence_indicators": ["Tech Lead", "mentored", "team lead"]}

    # Gates (education, experience, eligibility)
    education_requirements: EducationGate | None = None
    experience_requirements: ExperienceGate | None = None
    eligibility_requirements: EligibilityGate | None = None

    def all_skills_flat(self) -> list[WeightedItem]:
        """Return all skills as a flat list, deduplicated by name."""
        seen: set[str] = set()
        result: list[WeightedItem] = []
        for source in [
            self.required_skills,
            self.preferred_skills,
            self.programming_languages,
            self.frameworks,
            self.databases,
            self.infrastructure,
            self.tools,
            self.methodologies,
        ]:
            for item in source:
                key = item.name.lower().strip()
                if key not in seen:
                    seen.add(key)
                    result.append(item)
        return result

    def all_keywords(self) -> list[str]:
        """Return ATS-relevant keywords for coverage matching.

        Only includes actual technology/skill names and ATS phrases.
        Excludes themes, repeated common words, and soft skills.
        Deduplicates by checking if one keyword contains another
        (e.g., "full-stack" and "full-stack development" → keep shorter).
        """
        kws: list[str] = []

        # Primary: named skills
        for item in self.all_skills_flat():
            kws.append(item.name)

        # Secondary: ATS phrases (already filtered at extraction)
        kws.extend(self.ats_phrases)

        # Note: responsibility keywords are NOT included here.
        # They are often generic phrases ("user-facing features",
        # "code quality") that inflate the keyword list without
        # being real ATS scan terms. The skills + ATS phrases
        # from the deterministic pass already cover the real keywords.

        # Deduplicate: exact match and containment
        seen_lower: set[str] = set()
        result: list[str] = []
        for kw in kws:
            key = kw.lower().strip()
            if not key or key in seen_lower:
                continue
            # Skip if a shorter version is already present
            # e.g., skip "full-stack development" if "full-stack" exists
            if any(
                existing in key and existing != key
                for existing in seen_lower
            ):
                continue
            seen_lower.add(key)
            result.append(kw)
        return result


# --- New: Categorized Requirements for Better ATS Matching ---


class JDRequirement(BaseModel):
    """A requirement from JD with ATS-scannable and conceptual tracking.

    Handles both exact keyword matches (ATS) and thematic understanding (humans/LLM).
    Examples:
    - "Python" → ats_searchable=["python"], theme_indicators=["python"]
    - "Agile methodologies" → ats_searchable=["agile", "scrum"],
                              theme_indicators=["scrum", "sprints", "iterative", "agile"]
    """
    keyword_phrase: str  # What the JD literally says
    theme: str  # Conceptual category for humans/LLM
    requirement_level: str = "required"  # "required" | "preferred" | "optional"
    requirement_type: str  # "keyword" | "activity" | "concept" | "behavioral"

    ats_searchable: list[str] = Field(default_factory=list)  # What ATS can find
    theme_indicators: list[str] = Field(default_factory=list)  # Evidence of this concept
    importance: float = 0.5  # Weight for scoring


class EducationGate(BaseModel):
    """Education requirement (degree, field, graduation date)."""
    degree_level: str = ""  # "High School", "Bachelor's", "Master's", "PhD"
    field_of_study: str = ""  # "Computer Science", "related field", etc.
    graduation_window: tuple[str, str] | None = None  # ("2025", "2027")
    minimum_gpa: float | None = None
    required: bool = True


class ExperienceGate(BaseModel):
    """Experience requirement (years, level, or track)."""
    minimum_years: float = 0.0
    experience_level: str = ""  # "intern", "junior", "mid", "senior", "staff"
    required: bool = True


class EligibilityGate(BaseModel):
    """Eligibility requirements (security clearance, location, visa, etc.)."""
    work_authorization: str = ""  # "US Citizen", "Green Card", "requires sponsorship"
    security_clearance: str = ""  # "None", "Secret", "Top Secret", "preferred"
    location_flexible: bool = True
    visa_sponsorship_available: bool | None = None  # None = unknown
    student_only: bool = False
    other_requirements: list[str] = Field(default_factory=list)  # Custom gates


class ConcreteDeliverable(BaseModel):
    """Concrete activity/deliverable that can be keyword-tracked.

    Examples: Dashboard creation, API design, Documentation
    These appear in resumes as concrete terms.
    """
    phrase: str  # "Dashboard creation"
    requirement_level: str = "required"  # "required" | "preferred"

    ats_searchable: list[str] = Field(default_factory=list)  # ["dashboard", "dashboards", "created dashboards"]
    importance: float = 0.5


class BehavioralRequirement(BaseModel):
    """Behavioral/soft requirement that's demonstrated through context, not keywords.

    Examples: Cross-team collaboration, Mentoring, Communication
    These are NOT scored as keywords but noted as demonstrated.
    """
    phrase: str  # "Cross-team collaboration"
    requirement_level: str = "required"

    evidence_indicators: list[str] = Field(default_factory=list)  # Job titles, descriptions that show this
    # Examples for "mentoring": ["Tech Lead", "mentored", "team lead", "manager"]

    importance: float = 0.5
    notes: str = ""  # "Demonstrated through team lead role"


# --- Resume Extraction Models ---


class RequirementMatch(BaseModel):
    """Match result for a single JD requirement against resume.

    Tracks both ATS-scannable and conceptual matches.
    """
    requirement_phrase: str  # What JD says
    requirement_level: str  # "required" | "preferred"
    requirement_type: str  # "keyword" | "activity" | "concept" | "behavioral"

    # ATS matching
    ats_found: bool  # Was the exact keyword/phrase found?
    ats_matches: list[str] = Field(default_factory=list)  # Which ATS terms matched
    ats_frequency: int = 0  # How many times found

    # Conceptual/thematic matching (for concepts/behaviors)
    theme_confidence: float = 0.0  # 0.0-1.0, how confident is this conceptually matched
    theme_evidence: list[str] = Field(default_factory=list)  # What evidence found
    theme_notes: str = ""  # Human-readable notes

    # Overall verdict
    human_understandable: bool = False  # Would a human see this requirement met?


