"""Content Selector.

Picks the best content for a tailored resume based on match scores.
Decides which bullets to keep, reorder, or remove.
Decides which bank bullets to add.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from backend.models.job_description import JobAnalysis
from backend.models.resume_content import (
    Bullet,
    ResumeContent,
    ResumeSection,
    SectionType,
    SkillCategory,
)
from backend.models.tailoring import ResumeBank
from backend.tailoring.matcher import BulletScore, MatchResult


@dataclass
class BulletSelection:
    """Selection decision for a bullet."""
    bullet_id: str
    entry_id: str
    text: str
    action: str  # "keep" | "rewrite" | "remove" | "add"
    relevance_score: float = 0.0
    rewrite_priority: float = 0.0  # higher = more need to rewrite
    target_keywords: list[str] = field(default_factory=list)


@dataclass
class SkillReorder:
    """Reordering suggestion for a skill category."""
    category_id: str
    category: str
    original_order: list[str]
    suggested_order: list[str]


@dataclass
class SkillAddition:
    """A skill to add to a category."""
    category: str  # which skill category to add to (e.g., "Technologies")
    skill: str     # the skill to add
    reason: str    # why it's supportable


@dataclass
class SelectionResult:
    """Complete selection result."""
    bullet_selections: list[BulletSelection] = field(default_factory=list)
    skill_reorders: list[SkillReorder] = field(default_factory=list)
    skill_additions: list[SkillAddition] = field(default_factory=list)
    missing_keywords_to_add: list[str] = field(default_factory=list)


class Selector:
    """Select content for a tailored resume."""

    def __init__(
        self,
        jd: JobAnalysis,
        content: ResumeContent,
        match_result: MatchResult,
        bank: Optional[ResumeBank] = None,
        max_bullets_per_entry: int = 4,
    ):
        self._jd = jd
        self._content = content
        self._match = match_result
        self._bank = bank
        self._max_bullets = max_bullets_per_entry

    def select(self) -> SelectionResult:
        """Run the selection pipeline."""
        result = SelectionResult()

        # Build a lookup of bullet scores
        bullet_scores: dict[str, BulletScore] = {}
        for entry_score in self._match.entry_scores:
            for bs in entry_score.bullet_scores:
                bullet_scores[bs.bullet_id] = bs

        # Process each section
        for section in self._content.sections:
            if section.type in (SectionType.EXPERIENCE, SectionType.VOLUNTEER):
                for entry in section.experience_entries:
                    selections = self._select_entry_bullets(
                        entry.id, entry.bullets, bullet_scores
                    )
                    result.bullet_selections.extend(selections)

            elif section.type == SectionType.PROJECTS:
                for entry in section.project_entries:
                    selections = self._select_entry_bullets(
                        entry.id, entry.bullets, bullet_scores
                    )
                    result.bullet_selections.extend(selections)

            elif section.type == SectionType.SKILLS:
                for cat in section.skill_categories:
                    reorder = self._reorder_skills(cat)
                    if reorder:
                        result.skill_reorders.append(reorder)

        # Identify skills that should be ADDED to the skills section
        result.skill_additions = self._find_addable_skills()

        # Identify keywords that could be added via rewriting
        result.missing_keywords_to_add = self._match.bankable_keywords[:10]

        return result

    def _select_entry_bullets(
        self,
        entry_id: str,
        bullets: list[Bullet],
        bullet_scores: dict[str, BulletScore],
    ) -> list[BulletSelection]:
        """Select and prioritize bullets for an entry."""
        selections: list[BulletSelection] = []

        jd_keywords = self._jd.all_keywords()

        for bullet in bullets:
            bs = bullet_scores.get(bullet.id)
            relevance = bs.relevance_score if bs else 0.0
            keyword_matches = bs.keyword_matches if bs else []

            # Find JD keywords NOT yet in this bullet
            text_lower = bullet.text.lower()
            unmatched_keywords = [
                kw for kw in jd_keywords
                if kw.lower() not in text_lower
            ]

            # EVERY bullet is a candidate for optimization.
            # Even high-relevance bullets might benefit from simple
            # keyword swaps (e.g., "built" → "developed").
            # Only truly perfect bullets (all JD keywords present) stay "keep".
            if relevance >= 0.7 and len(unmatched_keywords) == 0:
                action = "keep"
                rewrite_priority = 0.0
            elif relevance >= 0.5:
                # Good match but could still swap keywords for ATS
                action = "rewrite"
                rewrite_priority = 0.3
            elif relevance >= 0.2:
                action = "rewrite"
                rewrite_priority = 0.7
            else:
                action = "rewrite"
                rewrite_priority = 1.0

            selections.append(BulletSelection(
                bullet_id=bullet.id,
                entry_id=entry_id,
                text=bullet.text,
                action=action,
                relevance_score=relevance,
                rewrite_priority=rewrite_priority,
                target_keywords=keyword_matches + unmatched_keywords[:5],
            ))

        # If too many bullets, mark lowest-relevance ones for removal
        if len(selections) > self._max_bullets:
            selections.sort(
                key=lambda s: self._removal_score(s, bullet_scores),
            )
            for s in selections[: len(selections) - self._max_bullets]:
                s.action = "remove"

        return selections

    @staticmethod
    def _removal_score(
        selection: BulletSelection,
        bullet_scores: dict[str, BulletScore],
    ) -> float:
        """Rank bullets for removal while protecting evidence of impact."""
        score = selection.relevance_score
        bullet_score = bullet_scores.get(selection.bullet_id)
        if bullet_score is None:
            return score

        if bullet_score.has_metrics:
            score += 0.2
        score += min(0.2, len(bullet_score.responsibility_matches) * 0.1)
        return score

    def _reorder_skills(self, cat: SkillCategory) -> Optional[SkillReorder]:
        """Suggest reordering skills to put JD-relevant ones first."""
        jd_keywords = {kw.lower() for kw in self._jd.all_keywords()}

        # Score each skill
        scored = []
        for skill in cat.skills:
            skill_lower = skill.lower().strip()
            # Check if this skill appears in the JD
            in_jd = skill_lower in jd_keywords or any(
                skill_lower in aliases
                for aliases_list in [
                    _expand_aliases_simple(kw) for kw in jd_keywords
                ]
                for aliases in [aliases_list]
                if skill_lower in aliases
            )
            scored.append((skill, 1.0 if in_jd else 0.0))

        # Sort: JD-relevant first, then original order
        suggested = [s for s, score in sorted(scored, key=lambda x: -x[1])]

        if suggested != cat.skills:
            return SkillReorder(
                category_id=cat.id,
                category=cat.category,
                original_order=cat.skills,
                suggested_order=suggested,
            )
        return None


    # Generic words that should NOT be added as skills even if they match
    _SKIP_AS_SKILL = {
        "build", "built", "develop", "developed", "design", "designed",
        "create", "created", "implement", "implemented", "work", "working",
        "use", "used", "using", "support", "manage", "lead", "write",
        "test", "tested", "enhance", "enhanced", "features", "feature",
        "full", "stack", "software", "web", "applications", "application",
        "code", "data", "system", "systems", "services", "service",
        "team", "teams", "project", "projects", "experience",
        "strong", "good", "excellent", "proven", "demonstrated",
        "lean", "clean", "secure", "documented", "scalable",
        "modern", "new", "high", "large", "small", "fast",
    }

    def _find_addable_skills(self) -> list[SkillAddition]:
        """Find JD skills that the resume PROVES but doesn't list in skills section.

        Only adds real technologies/methodologies — not generic English words.
        Requires IMPLICATION evidence (not just substring match).
        """
        from backend.tailoring.matcher import IMPLIES, _text_contains_keyword

        all_bullet_text = ""
        for section in self._content.sections:
            for e in section.experience_entries:
                for b in e.bullets:
                    all_bullet_text += " " + b.text
            for e in section.project_entries:
                for b in e.bullets:
                    all_bullet_text += " " + b.text
        all_bullet_lower = all_bullet_text.lower()

        existing_skills: set[str] = set()
        for section in self._content.sections:
            for cat in section.skill_categories:
                for s in cat.skills:
                    existing_skills.add(s.lower().strip())

        additions: list[SkillAddition] = []
        seen_additions: set[str] = set()
        jd_skills = self._jd.all_skills_flat()  # Only real skills, not generic terms

        for skill in jd_skills:
            kw = skill.name
            kw_lower = kw.lower().strip()

            # Skip generic words
            if kw_lower in self._SKIP_AS_SKILL:
                continue
            # Skip single common words (real skills are proper nouns or multi-word)
            if len(kw_lower) <= 3 and kw_lower not in {"sql", "git", "aws", "gcp", "ai", "ml", "r"}:
                continue

            # Skip if already in skills
            if kw_lower in existing_skills:
                continue

            # Only add if there's IMPLICATION evidence (not just substring)
            has_implication = False
            evidence = []
            for canonical, evidence_terms in IMPLIES.items():
                # Flexible matching: "LLMs" matches "llms", "Machine Learning" matches "machine learning"
                # Also match if the keyword contains the canonical or vice versa
                if (kw_lower == canonical
                    or canonical in kw_lower
                    or kw_lower in canonical
                    or any(kw_lower == alias for alias in _expand_aliases_simple(canonical))):
                    for term in evidence_terms:
                        if term in all_bullet_lower:
                            has_implication = True
                            evidence.append(term)
                            break

            if has_implication:
                category = self._categorize_skill(kw)
                # Use proper casing for display
                display_name = self._proper_case_skill(kw)
                # Skip if a variant is already being added
                if display_name.lower() not in seen_additions:
                    additions.append(SkillAddition(
                        category=category,
                        skill=display_name,
                        reason=f"Supported by: {', '.join(evidence[:3])}",
                    ))
                    seen_additions.add(display_name.lower())

        return additions

    @staticmethod
    def _proper_case_skill(skill: str) -> str:
        """Proper display casing for skills being added."""
        PROPER = {
            "machine learning": "Machine Learning",
            "deep learning": "Deep Learning",
            "llms": "LLMs",
            "llm": "LLMs",
            "embeddings": "Embeddings",
            "embedding": "Embeddings",
            "nlp": "NLP",
            "natural language processing": "NLP",
            "ai": "AI",
            "full-stack": "Full-Stack",
            "full-stack development": "Full-Stack Development",
            "restful": "RESTful APIs",
            "rest api": "REST APIs",
            "api design": "API Design",
            "database design": "Database Design",
            "web services": "Web Services",
            "problem-solving": "Problem-Solving",
            "communication": "Communication",
            "collaboration": "Collaboration",
            "debugging": "Debugging",
            "testing": "Testing",
            "ci/cd": "CI/CD",
            "docker": "Docker",
            "kubernetes": "Kubernetes",
            "aws": "AWS",
            "gcp": "GCP",
        }
        return PROPER.get(skill.lower().strip(), skill)

    def _categorize_skill(self, skill: str) -> str:
        """Determine which skills category a new skill belongs in."""
        sl = skill.lower()
        lang_terms = {"python", "java", "javascript", "typescript", "c++", "sql", "r", "go", "rust"}
        tool_terms = {"git", "docker", "jira", "postman", "vercel"}
        infra_terms = {"aws", "gcp", "azure", "docker", "kubernetes"}

        if sl in lang_terms:
            return "Languages"
        if sl in infra_terms:
            return "Tools"
        if sl in tool_terms:
            return "Tools"
        # Default: Technologies for frameworks, ML, etc.
        return "Technologies"


def _expand_aliases_simple(keyword: str) -> set[str]:
    """Simple alias expansion."""
    from backend.tailoring.matcher import ALIASES
    kw_lower = keyword.lower().strip()
    for canonical, aliases in ALIASES.items():
        if kw_lower in [a.lower() for a in aliases] or kw_lower == canonical:
            return {a.lower() for a in aliases} | {canonical}
    return {kw_lower}
