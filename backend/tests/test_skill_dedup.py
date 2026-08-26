"""Tests for skill/interest deduplication.

Covers:
- The shared `dedupe_skill_names` / `find_redundant_variants` utility
  (backend/analysis/skill_dedup.py), used on both the JD-extraction side
  and the resume-tailoring side.
- The actual bug this was written for: `TailoringService.apply_tailoring`
  writing an LLM's `skill_reorders`/`skill_additions` into a resume's
  Skills (and "Skills & Interests") section without deduplicating them,
  causing the same item to appear twice.
"""

import pytest

from backend.analysis.job_analyzer import JobAnalyzer
from backend.analysis.skill_dedup import dedupe_skill_names, find_redundant_variants
from backend.models.job_description import JobAnalysis, WeightedItem
from backend.models.resume_content import (
    ResumeContent, ResumeSection, SectionType, SkillCategory,
)
from backend.models.resume_ir import ResumeIR
from backend.models.tailoring import TailoringResult
from backend.services.tailoring_service import TailoringService


def _skills_ir(category: str, skills: list[str]) -> ResumeIR:
    content = ResumeContent(
        sections=[
            ResumeSection(
                id="sec-1",
                type=SectionType.SKILLS,
                title="Skills & Interests",
                skill_categories=[
                    SkillCategory(id="cat-1", category=category, skills=list(skills)),
                ],
            ),
        ],
    )
    return ResumeIR(content=content)


class TestDedupeSkillNames:
    """Unit tests for the shared dedup utility."""

    def test_exact_case_insensitive_duplicate_removed(self):
        assert dedupe_skill_names(["Python", "python", "SQL"]) == ["Python", "SQL"]

    def test_known_variant_pair_keeps_more_specific(self):
        assert dedupe_skill_names(["Git", "GitHub", "Docker"]) == ["GitHub", "Docker"]
        assert dedupe_skill_names(["HTML", "CSS", "HTML5"]) == ["CSS", "HTML5"]
        assert dedupe_skill_names(["Tailwind", "TailwindCSS"]) == ["TailwindCSS"]

    def test_punctuation_only_variant_collapsed(self):
        assert dedupe_skill_names(["Node.js", "nodejs", "React"]) == ["Node.js", "React"]
        assert dedupe_skill_names(["CI/CD", "CI CD", "Docker"]) == ["CI/CD", "Docker"]

    def test_three_way_variant_keeps_canonical_form(self):
        # Regression: an earlier version of this collapsed "Next"/"Nextjs" by
        # punctuation first and lost "Next.js" entirely, since the leftover
        # ("Nextjs") didn't literally match any VARIANT_PAIRS entry.
        assert dedupe_skill_names(["Next", "Nextjs", "Next.js"]) == ["Next.js"]

    def test_unrelated_substrings_not_merged(self):
        # "React" is a substring of "React Native" but they are distinct skills.
        assert dedupe_skill_names(["React", "React Native"]) == ["React", "React Native"]
        assert dedupe_skill_names(["Java", "JavaScript"]) == ["Java", "JavaScript"]

    def test_works_for_non_technical_interests(self):
        # The bug shows up under "Interests" too, since a heading like
        # "Skills & Interests" is classified as SectionType.SKILLS and its
        # items go through the exact same SkillCategory/dedup path.
        assert dedupe_skill_names(["Reading", "Hiking", "reading"]) == ["Reading", "Hiking"]

    def test_empty_and_blank_entries_ignored(self):
        assert dedupe_skill_names([]) == []
        assert dedupe_skill_names(["Python", "  ", ""]) == ["Python"]

    def test_order_and_casing_of_first_occurrence_preserved(self):
        assert dedupe_skill_names(["sql", "SQL", "Sql"]) == ["sql"]


class TestFindRedundantVariants:
    def test_returns_lowercased_less_specific_form(self):
        assert find_redundant_variants(["Git", "GitHub"]) == {"git"}

    def test_no_pair_present_returns_empty(self):
        assert find_redundant_variants(["Python", "SQL"]) == set()


class TestJobAnalyzerUsesSharedDedup:
    """The JD-extraction side should share the exact same variant table."""

    def test_deduplicate_variants_prefers_specific_form(self):
        analyzer = JobAnalyzer()
        analysis = JobAnalysis(raw_text="test")
        analysis.frameworks = [
            WeightedItem(name="Git", importance=1.0),
            WeightedItem(name="GitHub", importance=1.0),
            WeightedItem(name="Docker", importance=1.0),
        ]
        analyzer._deduplicate_variants(analysis)
        assert [f.name for f in analysis.frameworks] == ["GitHub", "Docker"]


class TestApplyTailoringSkillsMerge:
    """Integration test on the actual bug site: apply_tailoring's SKILLS merge.

    Before the fix, `skill_reorders` was written to `cat.skills` verbatim
    with no dedup at all, and `skill_additions` only checked exact
    case-insensitive matches (missing "HTML"/"HTML5"-style variants).
    """

    def test_reordered_skills_with_internal_duplicate_are_deduped(self):
        ir = _skills_ir("Languages", ["Python", "SQL", "AWS"])
        result = TailoringResult(resume_id="r1")
        # LLM repeats "AWS" and returns a Git/GitHub variant pair.
        result.reordered_skills["Languages"] = ["AWS", "Python", "AWS", "Git", "GitHub"]

        service = TailoringService()
        new_ir = service.apply_tailoring(ir, result, fit_skills=False)

        skills = new_ir.content.sections[0].skill_categories[0].skills
        assert skills == ["AWS", "Python", "GitHub", "SQL"]

    def test_added_skill_that_is_a_variant_of_existing_is_not_appended(self):
        ir = _skills_ir("Interests", ["Photography", "HTML5"])
        result = TailoringResult(resume_id="r1")
        # "HTML" is a less-specific variant of the already-present "HTML5".
        result.added_skills["Interests"] = ["HTML"]

        service = TailoringService()
        new_ir = service.apply_tailoring(ir, result, fit_skills=False)

        skills = new_ir.content.sections[0].skill_categories[0].skills
        assert skills == ["Photography", "HTML5"]

    def test_genuinely_new_addition_still_appended(self):
        ir = _skills_ir("Languages", ["Python"])
        result = TailoringResult(resume_id="r1")
        result.added_skills["Languages"] = ["Kubernetes"]

        service = TailoringService()
        new_ir = service.apply_tailoring(ir, result, fit_skills=False)

        skills = new_ir.content.sections[0].skill_categories[0].skills
        assert skills == ["Python", "[LLM]Kubernetes"]
