"""Resume-to-JD Matcher.

Scores resume facts, bullets, experiences, projects, and skills
against an analyzed job description using deterministic scoring.

Combines:
- Exact keyword matches
- Normalized keyword matches (case-insensitive, alias-aware)
- Technology relevance
- Responsibility relevance
- Demonstrated impact (metrics present)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from backend.models.job_description import JobAnalysis, WeightedItem
from backend.models.resume_content import (
    Bullet,
    ExperienceEntry,
    ProjectEntry,
    ResumeContent,
    ResumeSection,
    SkillCategory,
)
from backend.models.tailoring import ResumeBank


# --- Normalization aliases ---
# Direct name aliases: JD keyword -> [strings that ARE the same thing]

ALIASES: dict[str, list[str]] = {
    "javascript": ["js", "javascript"],
    "typescript": ["ts", "typescript"],
    "python": ["python", "py"],
    "react": ["react", "react.js", "reactjs"],
    "next.js": ["next.js", "nextjs", "next"],
    "vue": ["vue", "vue.js", "vuejs"],
    "node.js": ["node.js", "nodejs", "node"],
    "fastapi": ["fastapi", "fast api"],
    "postgresql": ["postgresql", "postgres", "psql"],
    "mongodb": ["mongodb", "mongo"],
    "d3.js": ["d3.js", "d3"],
    "c++": ["c++", "cpp"],
    "c#": ["c#", "csharp"],
    "google cloud": ["google cloud", "gcp"],
    "amazon web services": ["aws", "amazon web services"],
    "ci/cd": ["ci/cd", "ci cd", "cicd"],
    "rest api": ["rest api", "restful api", "rest apis", "restful"],
    "machine learning": ["machine learning", "ml"],
    "sql": ["sql"],
    "git": ["git"],
    "docker": ["docker"],
    "kubernetes": ["kubernetes", "k8s"],
    "full-stack": ["full-stack", "full stack", "fullstack"],
    "software": ["software"],
}

# Implication mappings: JD keyword -> [resume phrases that PROVE that skill]
# If any of these appear in the resume, the candidate demonstrably has the skill
# even if the exact JD keyword isn't written.

IMPLIES: dict[str, list[str]] = {
    "machine learning": [
        "sentencetransformers", "sentence-transformers", "scikit-learn",
        "sklearn", "tensorflow", "pytorch", "neural network", "classification",
        "regression", "training model", "trained model", "model training",
        "embeddings", "embedding", "rag", "vector search", "cosine similarity",
        "nlp", "natural language",
    ],
    "llms": [
        "rag", "langchain", "gemini api", "openai", "claude", "gpt",
        "llm", "large language model", "sentencetransformers",
        "sentence-transformers", "prompt",
    ],
    "embeddings": [
        "sentencetransformers", "sentence-transformers", "embedding",
        "vector search", "vector-search", "cosine similarity",
        "semantic search",
    ],
    "ai": [
        "machine learning", "ml", "rag", "llm", "neural", "embedding",
        "sentencetransformers", "gemini api", "gpt", "nlp",
    ],
    "full-stack development": [
        "full-stack", "full stack", "react", "frontend", "backend",
        "next.js", "fastapi", "node.js",
    ],
    "software": [
        "software", "engineer", "developer", "development", "application",
        "system", "codebase", "debugging", "testing",
    ],
    "debugging": [
        "debug", "debugged", "troubleshoot", "fix", "issue", "bug",
    ],
    "testing": [
        "test", "tested", "testing", "unit test", "integration test",
        "pytest", "jest", "cypress",
    ],
    "collaboration": [
        "collaborated", "partnering", "cross-functional", "team",
        "researchers", "stakeholder",
    ],
    "communication": [
        "presentation", "stakeholder", "translating requirements",
        "cross-functional", "collaborated",
    ],
    "problem-solving": [
        "optimiz", "debug", "troubleshoot", "architected", "designed",
        "engineered", "algorithms",
    ],
    "database design": [
        "postgresql", "mongodb", "data model", "nosql", "sql",
        "sqlite", "dynamodb", "database",
    ],
    "web services": [
        "rest api", "restful", "api", "endpoint", "fastapi",
        "node.js", "express",
    ],
    "api design": [
        "rest api", "restful api", "api", "fastapi", "endpoint",
        "graphql",
    ],
}


def _normalize(text: str) -> str:
    return text.lower().strip()


def _expand_aliases(keyword: str) -> set[str]:
    """Get all aliases for a keyword."""
    kw_lower = _normalize(keyword)
    for canonical, aliases in ALIASES.items():
        if kw_lower in [a.lower() for a in aliases] or kw_lower == canonical:
            return {a.lower() for a in aliases} | {canonical}
    return {kw_lower}


def _text_contains_keyword_direct(text_lower: str, keyword: str) -> bool:
    """Check if text contains a keyword via direct/alias match only.

    Used for bullet scoring — no IMPLIES inflation.
    """
    for alias in _expand_aliases(keyword):
        if len(alias) <= 2:
            if re.search(rf"\b{re.escape(alias)}\b", text_lower):
                return True
        else:
            if alias in text_lower:
                return True
    return False


def _text_contains_keyword(text_lower: str, keyword: str) -> bool:
    """Check if text contains a keyword via aliases OR implication.

    Used for overall coverage reporting (resume-wide, not per-bullet scoring).
    """
    if _text_contains_keyword_direct(text_lower, keyword):
        return True

    # Implication match: does the text contain evidence that proves this skill?
    kw_lower = keyword.lower().strip()
    for canonical, evidence_terms in IMPLIES.items():
        if kw_lower == canonical or kw_lower in _expand_aliases(canonical):
            for evidence in evidence_terms:
                if evidence in text_lower:
                    return True

    return False


@dataclass
class BulletScore:
    """Score for a single bullet against a JD."""
    bullet_id: str
    text: str
    entry_id: str
    entry_type: str  # "experience" or "project"

    keyword_matches: list[str] = field(default_factory=list)
    responsibility_matches: list[str] = field(default_factory=list)
    has_metrics: bool = False

    relevance_score: float = 0.0  # 0.0 - 1.0


@dataclass
class EntryScore:
    """Score for an experience or project entry."""
    entry_id: str
    entry_type: str
    company_or_name: str

    keyword_matches: list[str] = field(default_factory=list)
    bullet_scores: list[BulletScore] = field(default_factory=list)
    overall_relevance: float = 0.0


@dataclass
class SkillMatch:
    """Match result for a single skill."""
    skill: str
    jd_importance: float
    present_in_resume: bool
    present_in_bank: bool
    category: str = ""


@dataclass
class MatchResult:
    """Complete matching result."""
    entry_scores: list[EntryScore] = field(default_factory=list)
    skill_matches: list[SkillMatch] = field(default_factory=list)

    matched_keywords: list[str] = field(default_factory=list)
    missing_keywords: list[str] = field(default_factory=list)
    bankable_keywords: list[str] = field(default_factory=list)  # in bank but not resume

    required_coverage: float = 0.0
    technical_coverage: float = 0.0
    responsibility_coverage: float = 0.0


class Matcher:
    """Score resume content against a job analysis."""

    def __init__(self, jd: JobAnalysis, content: ResumeContent, bank: ResumeBank | None = None):
        self._jd = jd
        self._content = content
        self._bank = bank

        # Pre-compute full resume text for matching
        self._resume_text = self._build_resume_text()
        self._resume_text_lower = _normalize(self._resume_text)

        # Pre-compute bank text
        self._bank_text = self._build_bank_text() if bank else ""
        self._bank_text_lower = _normalize(self._bank_text)

    def match(self) -> MatchResult:
        """Run the full matching pipeline."""
        result = MatchResult()

        # Score each entry
        for section in self._content.sections:
            for entry in section.experience_entries:
                score = self._score_experience(entry)
                result.entry_scores.append(score)

            for entry in section.project_entries:
                score = self._score_project(entry)
                result.entry_scores.append(score)

        # Sort entries by relevance
        result.entry_scores.sort(key=lambda e: e.overall_relevance, reverse=True)

        # Skill matching
        result.skill_matches = self._match_skills()

        # Keyword coverage
        all_keywords = self._jd.all_keywords()
        for kw in all_keywords:
            if _text_contains_keyword(self._resume_text_lower, kw):
                result.matched_keywords.append(kw)
            elif self._bank_text_lower and _text_contains_keyword(self._bank_text_lower, kw):
                result.bankable_keywords.append(kw)
            else:
                result.missing_keywords.append(kw)

        # Coverage metrics
        all_skills = self._jd.all_skills_flat()
        if all_skills:
            matched_count = sum(
                1 for s in all_skills
                if _text_contains_keyword(self._resume_text_lower, s.name)
            )
            result.technical_coverage = matched_count / len(all_skills)

        required = self._jd.required_skills
        if required:
            matched_req = sum(
                1 for s in required
                if _text_contains_keyword(self._resume_text_lower, s.name)
            )
            result.required_coverage = matched_req / len(required)

        responsibilities = self._jd.responsibilities
        if responsibilities:
            matched_resp = sum(
                1 for r in responsibilities
                if any(_text_contains_keyword(self._resume_text_lower, kw) for kw in r.keywords)
                or any(_text_contains_keyword(self._resume_text_lower, word)
                       for word in r.text.split() if len(word) > 4)
            )
            result.responsibility_coverage = matched_resp / len(responsibilities)

        return result

    def _score_experience(self, entry: ExperienceEntry) -> EntryScore:
        score = EntryScore(
            entry_id=entry.id,
            entry_type="experience",
            company_or_name=entry.company,
        )

        for bullet in entry.bullets:
            bs = self._score_bullet(bullet, entry.id, "experience")
            score.bullet_scores.append(bs)
            score.keyword_matches.extend(bs.keyword_matches)

        # Deduplicate keyword matches
        score.keyword_matches = list(dict.fromkeys(score.keyword_matches))

        # Overall relevance = weighted average of bullet scores
        if score.bullet_scores:
            score.overall_relevance = sum(
                bs.relevance_score for bs in score.bullet_scores
            ) / len(score.bullet_scores)

        return score

    def _score_project(self, entry: ProjectEntry) -> EntryScore:
        score = EntryScore(
            entry_id=entry.id,
            entry_type="project",
            company_or_name=entry.name,
        )

        for bullet in entry.bullets:
            bs = self._score_bullet(bullet, entry.id, "project")
            score.bullet_scores.append(bs)
            score.keyword_matches.extend(bs.keyword_matches)

        score.keyword_matches = list(dict.fromkeys(score.keyword_matches))

        if score.bullet_scores:
            score.overall_relevance = sum(
                bs.relevance_score for bs in score.bullet_scores
            ) / len(score.bullet_scores)

        return score

    def _score_bullet(self, bullet: Bullet, entry_id: str, entry_type: str) -> BulletScore:
        bs = BulletScore(
            bullet_id=bullet.id,
            text=bullet.text,
            entry_id=entry_id,
            entry_type=entry_type,
        )

        text_lower = _normalize(bullet.text)

        # Keyword matching — direct/alias only (no IMPLIES inflation)
        all_skills = self._jd.all_skills_flat()
        for skill in all_skills:
            if _text_contains_keyword_direct(text_lower, skill.name):
                bs.keyword_matches.append(skill.name)

        # Responsibility matching
        for resp in self._jd.responsibilities:
            resp_lower = _normalize(resp.text)
            # Check if bullet addresses similar concepts
            overlap = sum(
                1 for word in resp_lower.split()
                if len(word) > 4 and word in text_lower
            )
            if overlap >= 2 or any(
                _text_contains_keyword_direct(text_lower, kw)
                for kw in resp.keywords
            ):
                bs.responsibility_matches.append(resp.text[:60])

        # Metrics detection
        bs.has_metrics = bool(re.search(r"\b\d+[,\d]*\+?\s*([\w]+)", bullet.text))

        # Calculate relevance score
        keyword_score = min(1.0, len(bs.keyword_matches) * 0.2)
        resp_score = min(1.0, len(bs.responsibility_matches) * 0.3)
        metric_bonus = 0.1 if bs.has_metrics else 0.0

        # Weight by skill importance
        if bs.keyword_matches:
            importance_sum = sum(
                skill.importance
                for skill in all_skills
                if skill.name in bs.keyword_matches
            )
            keyword_score = min(1.0, importance_sum / max(len(bs.keyword_matches), 1) * keyword_score * 2)

        bs.relevance_score = min(1.0, keyword_score + resp_score + metric_bonus)

        return bs

    def _match_skills(self) -> list[SkillMatch]:
        """Match JD skills against resume and bank."""
        matches = []
        for skill in self._jd.all_skills_flat():
            present_resume = _text_contains_keyword(self._resume_text_lower, skill.name)
            present_bank = (
                _text_contains_keyword(self._bank_text_lower, skill.name)
                if self._bank_text_lower else False
            )
            matches.append(SkillMatch(
                skill=skill.name,
                jd_importance=skill.importance,
                present_in_resume=present_resume,
                present_in_bank=present_bank or present_resume,
                category=skill.category,
            ))
        return matches

    def _build_resume_text(self) -> str:
        """Build a single text from all resume content."""
        parts = []
        for section in self._content.sections:
            for entry in section.experience_entries:
                parts.append(f"{entry.company} {entry.role}")
                for b in entry.bullets:
                    parts.append(b.text)
            for entry in section.project_entries:
                parts.append(entry.name)
                for b in entry.bullets:
                    parts.append(b.text)
            for cat in section.skill_categories:
                parts.append(f"{cat.category} {' '.join(cat.skills)}")
        return " ".join(parts)

    def _build_bank_text(self) -> str:
        """Build a single text from the resume bank."""
        if not self._bank:
            return ""
        parts = []
        for exp in self._bank.experiences:
            for f in exp.facts:
                parts.append(f.text)
            for b in exp.approved_bullets:
                parts.append(b.text)
            for b in exp.alternative_bullets:
                parts.append(b.text)
            parts.extend(exp.technologies)
            parts.extend(exp.keywords)
        for proj in self._bank.projects:
            for f in proj.facts:
                parts.append(f.text)
            for b in proj.approved_bullets:
                parts.append(b.text)
            parts.extend(proj.technologies)
        parts.extend(self._bank.additional_skills)
        return " ".join(parts)
