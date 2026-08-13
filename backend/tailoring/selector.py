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
class SelectionResult:
    """Complete selection result."""
    bullet_selections: list[BulletSelection] = field(default_factory=list)
    skill_reorders: list[SkillReorder] = field(default_factory=list)
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

        for bullet in bullets:
            bs = bullet_scores.get(bullet.id)
            relevance = bs.relevance_score if bs else 0.0
            keyword_matches = bs.keyword_matches if bs else []

            # Determine action
            if relevance >= 0.5:
                action = "keep"
                rewrite_priority = 0.0
            elif relevance >= 0.2:
                action = "rewrite"
                rewrite_priority = 1.0 - relevance
            else:
                # Low relevance — candidate for rewrite or removal
                action = "rewrite"
                rewrite_priority = 1.0

            # Calculate which JD keywords this bullet could target
            jd_keywords = self._jd.all_keywords()
            text_lower = bullet.text.lower()
            unmatched_keywords = [
                kw for kw in jd_keywords
                if kw.lower() not in text_lower
            ]

            selections.append(BulletSelection(
                bullet_id=bullet.id,
                entry_id=entry_id,
                text=bullet.text,
                action=action,
                relevance_score=relevance,
                rewrite_priority=rewrite_priority,
                target_keywords=keyword_matches + unmatched_keywords[:3],
            ))

        # If too many bullets, mark lowest-relevance ones for removal
        if len(selections) > self._max_bullets:
            selections.sort(key=lambda s: s.relevance_score)
            for s in selections[: len(selections) - self._max_bullets]:
                s.action = "remove"

        return selections

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


def _expand_aliases_simple(keyword: str) -> set[str]:
    """Simple alias expansion."""
    from backend.tailoring.matcher import ALIASES
    kw_lower = keyword.lower().strip()
    for canonical, aliases in ALIASES.items():
        if kw_lower in [a.lower() for a in aliases] or kw_lower == canonical:
            return {a.lower() for a in aliases} | {canonical}
    return {kw_lower}
