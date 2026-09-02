"""Semantic Classifier for resume sections.

Classifies paragraphs into resume sections (experience, education,
skills, projects, etc.) using text patterns and formatting cues.

This works as a post-processing step on the initial import to
validate and correct section assignments.
"""

from __future__ import annotations

import re
from typing import Optional

from backend.models.resume_content import (
    ResumeContent,
    ResumeSection,
    SectionType,
)


# --- Section title patterns ---

SECTION_PATTERNS: dict[SectionType, list[re.Pattern]] = {
    SectionType.EXPERIENCE: [
        re.compile(r"\b(work\s*)?experience\b", re.I),
        re.compile(r"\bemployment\b", re.I),
        re.compile(r"\bprofessional\s+experience\b", re.I),
        re.compile(r"\bwork\s+history\b", re.I),
    ],
    SectionType.EDUCATION: [
        re.compile(r"\beducation\b", re.I),
        re.compile(r"\bacademic\b", re.I),
    ],
    SectionType.PROJECTS: [
        re.compile(r"\bproject", re.I),
    ],
    SectionType.SKILLS: [
        re.compile(r"\bskill", re.I),
        re.compile(r"\btechnical\s+(proficienc|skill|competenc)", re.I),
        re.compile(r"\btechnolog", re.I),
    ],
    SectionType.CERTIFICATIONS: [
        re.compile(r"\bcertif", re.I),
        re.compile(r"\blicens", re.I),
    ],
    SectionType.AWARDS: [
        re.compile(r"\baward", re.I),
        re.compile(r"\bhonor", re.I),
    ],
    SectionType.VOLUNTEER: [
        re.compile(r"\bvolunteer", re.I),
        re.compile(r"\bcommunity", re.I),
        re.compile(r"\bextracurricular", re.I),
        re.compile(r"\bactivit", re.I),
        re.compile(r"\bleadership", re.I),
    ],
    SectionType.SUMMARY: [
        re.compile(r"\bsummary\b", re.I),
        re.compile(r"\bprofile\b", re.I),
        re.compile(r"\babout\b", re.I),
    ],
    SectionType.OBJECTIVE: [
        re.compile(r"\bobjective\b", re.I),
    ],
    SectionType.PUBLICATIONS: [
        re.compile(r"\bpublicat", re.I),
        re.compile(r"\bresearch\b", re.I),
    ],
}

# --- Content-based heuristics ---

DATE_PATTERN = re.compile(
    r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|"
    r"january|february|march|april|june|july|august|september|october|november|december|"
    r"spring|summer|fall|winter|present|\d{4})",
    re.I,
)

UNIVERSITY_KEYWORDS = [
    "university", "college", "institute", "school of",
    "b.s.", "b.a.", "m.s.", "m.a.", "ph.d.", "bachelor",
    "master", "gpa", "magna", "summa", "cum laude",
]

COMPANY_INDICATORS = [
    "inc.", "llc", "corp.", "ltd.", "co.",
    "intern", "engineer", "developer", "analyst",
    "manager", "director", "consultant",
]


class SemanticClassifier:
    """Validates and corrects section type assignments."""

    def classify_section_title(self, title: str) -> SectionType:
        """Classify a section by its title.

        Checks every category's patterns rather than stopping at the
        first match in SECTION_PATTERNS' dict order, and prefers
        whichever pattern matched the LONGEST span of the title — a
        longer, more specific keyword match (e.g. "certif" matching
        "Certification") is a better signal than a shorter, more
        generic one (e.g. "skill" matching "SkillBridge") that happens
        to appear as a substring. Without this, dict insertion order
        alone decided ties: a section titled "Publications & Research
        Activities" matched VOLUNTEER's broad "activit" pattern before
        ever reaching PUBLICATIONS's own patterns, purely because
        VOLUNTEER was defined earlier in the dict — silently mislabeling
        the whole publications list with no error or diagnostic.
        """
        best_type: Optional[SectionType] = None
        best_len = 0
        for section_type, patterns in SECTION_PATTERNS.items():
            for pattern in patterns:
                match = pattern.search(title)
                if match and (match.end() - match.start()) > best_len:
                    best_len = match.end() - match.start()
                    best_type = section_type
        return best_type or SectionType.CUSTOM

    def _validate_section(self, section: ResumeSection) -> Optional[SectionType]:
        """Check if a section's content matches its type."""
        # Title-based classification
        title_type = self.classify_section_title(section.title)
        if title_type != SectionType.CUSTOM:
            return title_type

        # Content-based heuristics
        if section.experience_entries:
            has_dates = any(
                e.start_date or e.end_date
                for e in section.experience_entries
            )
            has_company_indicators = any(
                any(kw in (e.company + " " + e.role).lower() for kw in COMPANY_INDICATORS)
                for e in section.experience_entries
            )
            if has_dates and has_company_indicators:
                return SectionType.EXPERIENCE

        if section.education_entries:
            has_edu_keywords = any(
                any(kw in (e.institution + " " + (e.degree or "")).lower() for kw in UNIVERSITY_KEYWORDS)
                for e in section.education_entries
            )
            if has_edu_keywords:
                return SectionType.EDUCATION

        # Unlike EXPERIENCE/EDUCATION above, these two used to fire on
        # "is the list non-empty" alone, with no corroborating check —
        # so a catch-all section (e.g. "Additional Information" mixing
        # an Eagle Scout note, a volunteering line, and one incidental
        # "Languages: Spanish" entry) got force-relabeled SKILLS just
        # because *something* parsed into a SkillCategory, discarding
        # that the section was mostly other content. Only apply these
        # when skill/project entries are the section's ONLY populated
        # content — a genuinely skills-only or projects-only ambiguous
        # section is still classified correctly, but a mixed one is left
        # CUSTOM rather than force-guessed.
        no_other_content = not any([
            section.experience_entries, section.education_entries,
            section.generic_entries, section.raw_lines,
        ])

        if section.skill_categories and not section.project_entries and no_other_content:
            return SectionType.SKILLS

        if section.project_entries and not section.skill_categories and no_other_content:
            return SectionType.PROJECTS

        return None

    def reclassify(self, content: ResumeContent) -> ResumeContent:
        """Apply classification corrections to content."""
        for section in content.sections:
            suggested = self._validate_section(section)
            if suggested:
                section.type = suggested
        return content
