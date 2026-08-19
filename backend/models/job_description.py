"""Job Description models.

Structured representation of an analyzed job description
with extracted skills, responsibilities, and relevance weights.
"""

from __future__ import annotations

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
