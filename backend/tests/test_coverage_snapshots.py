"""Tests for TailoringService._build_bullet_snapshots /
_calculate_coverage_from_snapshots correctness.

Two regressions found by a proactive audit:

1. responsibility_coverage was essentially always wrong: snapshots
   stored a matched responsibility as the full text snippet, but the
   coverage calculation checked "does any single WORD of resp.text
   equal a full multi-word snippet string" -- which is almost never
   true for real (multi-word) responsibility text. A bullet that was a
   near-verbatim match of a JD responsibility still reported 0%
   coverage and listed it as "missing".

2. Headline coverage % completely ignored the resume's Skills section:
   _build_bullet_snapshots only ever scanned bullet_changes (experience/
   project bullets), never section.skill_categories. A JD skill listed
   only in the Skills section (never repeated in a bullet -- an
   extremely common resume pattern) was reported "missing" from the
   headline coverage %, even while a separate keyword list built
   elsewhere in the app could show the same skill as "matched" --
   directly contradicting itself in the UI.
"""

from backend.models.job_description import JobAnalysis, Responsibility, WeightedItem
from backend.models.resume_content import (
    ResumeContent, ResumeSection, SectionType, SkillCategory,
)
from backend.models.tailoring import BulletChange, TailoringResult
from backend.services.tailoring_service import TailoringService


def _result(bullet_id: str, text: str, action: str = "keep") -> TailoringResult:
    return TailoringResult(
        resume_id="r1",
        bullet_changes=[BulletChange(
            bullet_id=bullet_id, original_text=text, tailored_text=text, action=action,
        )],
    )


class TestResponsibilityCoverageMatchesSnapshot:
    def test_near_verbatim_match_reports_full_coverage(self):
        jd = JobAnalysis(responsibilities=[Responsibility(
            text="Design and build scalable REST APIs for internal services",
            importance=0.8, keywords=["api"],
        )])
        result = _result(
            "b1", "Designed and built scalable REST APIs for internal services",
        )
        svc = TailoringService()
        svc._build_bullet_snapshots(result, jd)
        svc._calculate_coverage_from_snapshots(result, jd)

        assert result.responsibility_coverage == 1.0
        assert result.responsibilities_matched == [
            "Design and build scalable REST APIs for internal services",
        ]
        assert result.responsibilities_missing == []

    def test_genuinely_unmatched_responsibility_still_reports_missing(self):
        jd = JobAnalysis(responsibilities=[Responsibility(
            text="Lead quarterly performance reviews for a team of designers",
            importance=0.8, keywords=["performance review"],
        )])
        result = _result("b1", "Built REST APIs for the internal billing platform")
        svc = TailoringService()
        svc._build_bullet_snapshots(result, jd)
        svc._calculate_coverage_from_snapshots(result, jd)

        assert result.responsibility_coverage == 0.0
        assert result.responsibilities_missing == [
            "Lead quarterly performance reviews for a team of designers",
        ]


class TestSkillsSectionCountsTowardCoverage:
    def _content_with_skills(self, category: str, skills: list[str]) -> ResumeContent:
        return ResumeContent(sections=[
            ResumeSection(
                id="s1", type=SectionType.SKILLS, title="Skills",
                skill_categories=[SkillCategory(id="c1", category=category, skills=skills)],
            ),
        ])

    def test_skill_listed_only_in_skills_section_counts_as_matched(self):
        jd = JobAnalysis(required_skills=[WeightedItem(name="Kubernetes", importance=1.0)])
        content = self._content_with_skills("Tools", ["Kubernetes", "Docker"])
        result = _result("b1", "Built REST APIs for the internal billing platform")

        svc = TailoringService()
        svc._build_bullet_snapshots(result, jd, content=content)
        svc._calculate_coverage_from_snapshots(result, jd)

        assert result.required_skill_coverage == 1.0
        assert result.required_skills_matched == ["Kubernetes"]
        assert result.required_skills_missing == []

    def test_skill_in_neither_bullets_nor_skills_section_still_reports_missing(self):
        jd = JobAnalysis(required_skills=[WeightedItem(name="Kubernetes", importance=1.0)])
        content = self._content_with_skills("Tools", ["Docker"])
        result = _result("b1", "Built REST APIs for the internal billing platform")

        svc = TailoringService()
        svc._build_bullet_snapshots(result, jd, content=content)
        svc._calculate_coverage_from_snapshots(result, jd)

        assert result.required_skill_coverage == 0.0
        assert result.required_skills_missing == ["Kubernetes"]

    def test_no_content_provided_is_backward_compatible_noop(self):
        # content is optional -- omitting it (existing callers that
        # haven't been updated) must not raise or change bullet-only
        # behavior.
        jd = JobAnalysis(required_skills=[WeightedItem(name="Kubernetes", importance=1.0)])
        result = _result("b1", "Built REST APIs for the internal billing platform")

        svc = TailoringService()
        svc._build_bullet_snapshots(result, jd)
        svc._calculate_coverage_from_snapshots(result, jd)

        assert result.required_skill_coverage == 0.0
        assert not any(s.bullet_id == "__skills_section__" for s in result.bullet_snapshots)
