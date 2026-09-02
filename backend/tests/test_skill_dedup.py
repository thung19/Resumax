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
from backend.analysis.skill_dedup import (
    dedupe_skill_names, find_redundant_variants, is_duplicate_skill,
)
from backend.models.job_description import JobAnalysis, WeightedItem
from backend.models.resume_content import (
    ResumeContent, ResumeSection, SectionType, SkillCategory,
)
from backend.models.resume_ir import ResumeIR
from backend.models.tailoring import TailoringResult
from backend.services.tailoring_service import (
    TailoringService, _dedupe_additions_across_categories,
)


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

    def test_c_cpp_csharp_stay_distinct(self):
        # Regression: normalize_skill_name used to strip ALL punctuation,
        # including "+" and "#" — collapsing "C", "C++", and "C#" (three
        # genuinely distinct, extremely common languages) to the same
        # normalized "c". Listing more than one silently dropped all but
        # one; an explicitly user-accepted "C++" addition could vanish
        # with no error and no way to notice.
        assert dedupe_skill_names(["C", "C++", "C#"]) == ["C", "C++", "C#"]
        assert is_duplicate_skill("C++", ["C"]) is False
        assert is_duplicate_skill("C#", ["C++"]) is False

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

    def test_accepted_cpp_addition_survives_alongside_existing_c(self):
        # Full-pipeline regression for the C/C++/C# normalize_skill_name
        # bug: a user explicitly accepting a "C++" addition, when the
        # resume already lists plain "C", must not silently vanish.
        ir = _skills_ir("Languages", ["C", "Python"])
        result = TailoringResult(resume_id="r1")
        result.added_skills["Languages"] = ["C++"]
        result.additions_accepted["Languages:C++"] = True

        service = TailoringService()
        new_ir = service.apply_tailoring(ir, result, fit_skills=False)

        skills = new_ir.content.sections[0].skill_categories[0].skills
        assert "[LLM]C++" in skills
        assert "C" in skills

    def test_genuinely_new_addition_still_appended(self):
        ir = _skills_ir("Languages", ["Python"])
        result = TailoringResult(resume_id="r1")
        result.added_skills["Languages"] = ["Kubernetes"]

        service = TailoringService()
        new_ir = service.apply_tailoring(ir, result, fit_skills=False)

        skills = new_ir.content.sections[0].skill_categories[0].skills
        assert skills == ["Python", "[LLM]Kubernetes"]

    def test_pre_existing_duplicate_cleaned_even_without_llm_changes(self):
        # Regression: a category the LLM never touches (no skill_reorders/
        # skill_additions entry for it at all) previously passed through
        # apply_tailoring completely untouched — so a duplicate already in
        # the *source* resume (e.g. "GraphQL" listed twice, or "TypeScript"
        # alongside "JavaScript/TypeScript") survived tailoring unchanged.
        content = ResumeContent(
            sections=[
                ResumeSection(
                    id="sec-1",
                    type=SectionType.SKILLS,
                    title="Skills",
                    skill_categories=[
                        SkillCategory(
                            id="cat-1", category="Languages",
                            skills=[
                                "Python", "JavaScript/TypeScript", "Java", "SQL",
                                "HTML", "C++", "R", "TypeScript", "JavaScript",
                            ],
                        ),
                        SkillCategory(
                            id="cat-2", category="Concepts",
                            skills=[
                                "REST APIs", "GraphQL", "HTTP", "Testing",
                                "Debugging", "OOP", "Data Structures",
                                "Algorithms", "AI/ML", "GraphQL",
                            ],
                        ),
                    ],
                ),
            ],
        )
        ir = ResumeIR(content=content)
        result = TailoringResult(resume_id="r1")  # no LLM changes at all

        service = TailoringService()
        new_ir = service.apply_tailoring(ir, result, fit_skills=False)

        languages, concepts = new_ir.content.sections[0].skill_categories
        assert languages.skills == [
            "Python", "JavaScript/TypeScript", "Java", "SQL", "HTML", "C++", "R",
        ]
        assert concepts.skills == [
            "REST APIs", "GraphQL", "HTTP", "Testing", "Debugging", "OOP",
            "Data Structures", "Algorithms", "AI/ML",
        ]

    def test_same_skill_via_addition_and_reorder_not_duplicated(self):
        # Regression: the main tailor() pass adds "GraphQL" via
        # skill_additions (stored as "[LLM]GraphQL"), while the separate
        # optimize_skills() pass independently returns a skill_reorders
        # list for the same category that already includes plain
        # "GraphQL". The marker prefix previously hid the fact that these
        # are the same skill from the reorder-merge's duplicate check —
        # both survived until `_fit_skills_to_line` later stripped the
        # marker, at which point they became two identical, visibly
        # duplicated "GraphQL" entries.
        ir = _skills_ir("Concepts", [
            "REST APIs", "Data Structures", "Algorithms", "OOP",
            "AI/ML", "Testing", "Debugging", "Cloud Computing",
        ])
        result = TailoringResult(resume_id="r1")
        result.added_skills["Concepts"] = ["GraphQL"]
        result.reordered_skills["Concepts"] = [
            "REST APIs", "GraphQL", "HTTP", "Testing", "Debugging",
            "OOP", "Data Structures", "Algorithms", "AI/ML",
        ]

        service = TailoringService()
        new_ir = service.apply_tailoring(ir, result, fit_skills=False)

        skills = new_ir.content.sections[0].skill_categories[0].skills
        graphql_count = sum(1 for s in skills if s.replace("[LLM]", "") == "GraphQL")
        assert graphql_count == 1
        assert "Cloud Computing" in skills

    def test_same_skill_across_two_categories_kept_only_in_the_first(self):
        # Regression: the same skill can be claimed by two DIFFERENT
        # categories (e.g. "GraphQL" ends up in both "Technologies" and
        # "Concepts") since every prior dedup check only ever compared a
        # category against itself. First category in the resume's own
        # order should win; later occurrences are dropped, not renamed.
        content = ResumeContent(
            sections=[
                ResumeSection(
                    id="sec-1",
                    type=SectionType.SKILLS,
                    title="Skills",
                    skill_categories=[
                        SkillCategory(
                            id="cat-1", category="Technologies",
                            skills=["React", "Docker", "GraphQL"],
                        ),
                        SkillCategory(
                            id="cat-2", category="Concepts",
                            skills=["REST APIs", "GraphQL", "OOP"],
                        ),
                    ],
                ),
            ],
        )
        ir = ResumeIR(content=content)
        result = TailoringResult(resume_id="r1")

        service = TailoringService()
        new_ir = service.apply_tailoring(ir, result, fit_skills=False)

        technologies, concepts = new_ir.content.sections[0].skill_categories
        assert technologies.skills == ["React", "Docker", "GraphQL"]
        assert concepts.skills == ["REST APIs", "OOP"]

    def test_rejected_addition_does_not_reappear_via_accepted_reorder(self):
        # Regression: additions and reorders are tracked independently
        # (`additions_accepted` vs `reorder_accepted`). Rejecting a skill
        # as an ADDITION did nothing to stop it reappearing if the
        # separate `skill_reorders` list for that category also happened
        # to include it — accepting the reorder (e.g. via "Accept All")
        # silently brought the rejected skill straight back.
        ir = _skills_ir("Concepts", ["REST APIs", "OOP", "Testing"])
        result = TailoringResult(resume_id="r1")
        result.added_skills["Concepts"] = ["GraphQL", "Kubernetes"]
        result.reordered_skills["Concepts"] = ["GraphQL", "REST APIs", "OOP", "Testing"]

        # User explicitly rejects the GraphQL addition...
        result.additions_accepted["Concepts:GraphQL"] = False
        # ...then hits "Accept All" for everything else still pending.
        for cat, skills in result.added_skills.items():
            for skill in skills:
                key = f"{cat}:{skill}"
                result.additions_accepted.setdefault(key, True)
        for cat in result.reordered_skills:
            result.reorder_accepted.setdefault(cat, True)

        service = TailoringService()
        new_ir = service.apply_tailoring(ir, result, fit_skills=False)

        skills = new_ir.content.sections[0].skill_categories[0].skills
        assert "GraphQL" not in skills
        assert "[LLM]Kubernetes" in skills

    def test_rejected_addition_does_not_remove_a_pre_existing_skill(self):
        # A skill already on the resume must survive even if a redundant
        # "addition" of the same name was separately rejected — rejection
        # only ever removes something new, never something that was
        # already there.
        ir = _skills_ir("Languages", ["Python", "SQL"])
        result = TailoringResult(resume_id="r1")
        result.reordered_skills["Languages"] = ["Python", "SQL"]
        result.additions_accepted["Languages:Python"] = False
        result.reorder_accepted["Languages"] = True

        service = TailoringService()
        new_ir = service.apply_tailoring(ir, result, fit_skills=False)

        skills = new_ir.content.sections[0].skill_categories[0].skills
        assert skills == ["Python", "SQL"]

    def test_rejecting_the_reorder_also_rejects_the_paired_addition(self):
        # Regression (live bug report): user saw "Jenkins" appended at the
        # end of "Databases & Tools", rejected the "Skills Reordered" card
        # for that category (which visually included Jenkins) — and Jenkins
        # came right back on the next render. Cause: the reorder decision
        # only gates the *reorder* branch; the separate *addition* loop
        # still defaulted an untouched "Jenkins" addition to accepted=True
        # and appended it regardless, since the user never clicked
        # Accept/Reject on the addition card specifically. From the user's
        # point of view there was only one decision to make, not two.
        ir = _skills_ir(
            "Databases & Tools",
            ["Git", "PostgreSQL", "MongoDB", "SQLite"],
        )
        result = TailoringResult(resume_id="r1")
        result.added_skills["Databases & Tools"] = ["Jenkins"]
        result.reordered_skills["Databases & Tools"] = [
            "Git", "Jenkins", "PostgreSQL", "MongoDB", "SQLite",
        ]
        # User rejects only the reorder card; the addition card for
        # "Jenkins" is never explicitly touched (no additions_accepted key).
        result.reorder_accepted["Databases & Tools"] = False

        service = TailoringService()
        new_ir = service.apply_tailoring(ir, result, fit_skills=False)

        skills = new_ir.content.sections[0].skill_categories[0].skills
        assert "Jenkins" not in skills
        assert skills == ["Git", "PostgreSQL", "MongoDB", "SQLite"]

    def test_explicitly_accepted_addition_survives_rejected_reorder(self):
        # The fix above must not become "reject reorder => always drop new
        # skills in that category" — an addition the user explicitly
        # accepted stays, even if the paired reorder was rejected.
        ir = _skills_ir("Databases & Tools", ["Git", "PostgreSQL"])
        result = TailoringResult(resume_id="r1")
        result.added_skills["Databases & Tools"] = ["Jenkins"]
        result.reordered_skills["Databases & Tools"] = ["Git", "Jenkins", "PostgreSQL"]
        result.additions_accepted["Databases & Tools:Jenkins"] = True
        result.reorder_accepted["Databases & Tools"] = False

        service = TailoringService()
        new_ir = service.apply_tailoring(ir, result, fit_skills=False)

        skills = new_ir.content.sections[0].skill_categories[0].skills
        assert "[LLM]Jenkins" in skills

    def test_category_name_mismatch_logs_a_debug_warning(self):
        # Regression: if the LLM's proposed category name doesn't match
        # any of the resume's actual skill categories exactly (e.g. it
        # said "Programming Languages" but the resume's category is
        # "Languages"), the whole addition/reorder for that category
        # silently no-ops -- apply_tailoring only ever looks things up
        # by "cat.category in result.X". There's no safe way to
        # auto-recover this (no reliable way to guess which existing
        # category was meant), but it must not be entirely silent.
        ir = _skills_ir("Languages", ["Python", "Java"])
        result = TailoringResult(resume_id="r1")
        result.added_skills["Programming Languages"] = ["Go", "Rust"]
        result.reordered_skills["Programming Languages"] = ["Go", "Python", "Java", "Rust"]

        service = TailoringService()
        new_ir = service.apply_tailoring(ir, result, fit_skills=False)

        # Confirms the no-op: nothing crashes, nothing silently
        # "fuzzy-matches" onto the wrong category.
        skills = new_ir.content.sections[0].skill_categories[0].skills
        assert skills == ["Python", "Java"]
        assert any(
            "Programming Languages" in line and "MISMATCH" in line
            for line in result.debug_log
        )

    def test_matching_category_name_does_not_log_a_mismatch(self):
        ir = _skills_ir("Languages", ["Python"])
        result = TailoringResult(resume_id="r1")
        result.added_skills["Languages"] = ["Go"]
        result.additions_accepted["Languages:Go"] = True

        service = TailoringService()
        service.apply_tailoring(ir, result, fit_skills=False)

        assert not any("MISMATCH" in line for line in result.debug_log)


class TestDedupeAdditionsAcrossCategories:
    """Regression (live bug report): `tailor()`'s two independent LLM
    calls (`.tailor()` and `.optimize_skills()`) each independently
    proposed adding "Jenkins" — one under "Databases & Tools", the other
    under "Technologies". The per-category merge guard doesn't catch
    this, so both survived into `added_skills` as two separate review
    cards. The user rejected the one they saw; the other, never shown to
    them as the same decision, silently made it into the rendered resume
    anyway. `_dedupe_additions_across_categories` runs right after both
    LLM calls are merged, before the user ever gets to accept/reject
    anything, so there's only ever one card — and one decision — per
    skill.
    """

    def test_same_skill_proposed_under_two_categories_kept_in_first(self):
        added = {
            "Databases & Tools": ["Jenkins"],
            "Technologies": ["Jenkins"],
        }
        deduped = _dedupe_additions_across_categories(added)
        assert deduped == {"Databases & Tools": ["Jenkins"]}

    def test_variant_pair_across_categories_also_caught(self):
        added = {
            "Databases & Tools": ["GitHub"],
            "Technologies": ["Git"],
        }
        deduped = _dedupe_additions_across_categories(added)
        assert deduped == {"Databases & Tools": ["GitHub"]}

    def test_unrelated_additions_in_different_categories_all_kept(self):
        added = {
            "Databases & Tools": ["Jenkins"],
            "Technologies": ["GraphQL"],
        }
        deduped = _dedupe_additions_across_categories(added)
        assert deduped == added

    def test_category_left_empty_after_dedupe_is_dropped(self):
        added = {
            "Databases & Tools": ["Jenkins"],
            "Technologies": ["Jenkins", "GraphQL"],
        }
        deduped = _dedupe_additions_across_categories(added)
        assert deduped == {
            "Databases & Tools": ["Jenkins"],
            "Technologies": ["GraphQL"],
        }


class TestComboSkillOverlap:
    """A slash-combined entry like "JavaScript/TypeScript" already covers
    its individual parts — those shouldn't also be listed standalone.
    """

    def test_dedupe_collapses_standalone_entries_into_combo(self):
        assert dedupe_skill_names(
            ["Python", "JavaScript/TypeScript", "TypeScript", "JavaScript"]
        ) == ["Python", "JavaScript/TypeScript"]

    def test_is_duplicate_skill_catches_part_of_existing_combo(self):
        assert is_duplicate_skill("JavaScript", ["Python", "JavaScript/TypeScript"])
        assert is_duplicate_skill("TypeScript", ["JavaScript/TypeScript"])

    def test_is_duplicate_skill_catches_combo_whose_parts_already_exist(self):
        assert is_duplicate_skill(
            "JavaScript/TypeScript", ["JavaScript", "TypeScript", "Python"]
        )

    def test_combo_with_only_one_part_present_is_not_dropped(self):
        # "TypeScript" alone doesn't cover "JavaScript" too — don't merge
        # unless the combo's parts are already fully represented.
        assert not is_duplicate_skill("JavaScript/Rust", ["JavaScript", "Python"])
