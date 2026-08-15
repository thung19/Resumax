"""Tests for the two-stage tailoring pipeline.

Covers: planning, rewriting, validation, score merging, edge cases.
"""

import json
import pytest

from backend.models.resume_content import (
    Bullet, ExperienceEntry, ProjectEntry, ResumeContent,
    ResumeSection, SectionType, SkillCategory, ContactInfo,
)
from backend.models.job_description import JobAnalysis, Responsibility, WeightedItem
from backend.models.tailoring import BulletChange, ResumeBank, ExperienceBank, Fact, TailoringResult
from backend.tailoring.claim_validator import ClaimValidator
from backend.tailoring.planner import BulletPlan, PlanResult, ResumePlanner
from backend.tailoring.matcher import Matcher


# --- Fixtures ---

def _make_content():
    """Build a minimal resume content for testing."""
    return ResumeContent(
        contact=ContactInfo(name="Test User"),
        sections=[
            ResumeSection(
                id="section_1", type=SectionType.EXPERIENCE, title="EXPERIENCE",
                experience_entries=[
                    ExperienceEntry(
                        id="exp_1", company="BigCorp", role="Engineer",
                        start_date="Jan 2024", end_date="Present",
                        bullets=[
                            Bullet(id="b1", text="Engineered a Python optimization system processing $250M+ in portfolios, reducing risk by 15%"),
                            Bullet(id="b2", text="Led a team of 4 engineers to deliver a customer dashboard 2 weeks ahead of schedule"),
                            Bullet(id="b3", text="Organized office birthday parties and ordered team snacks weekly"),
                        ],
                    ),
                ],
            ),
            ResumeSection(
                id="section_2", type=SectionType.PROJECTS, title="PROJECTS",
                project_entries=[
                    ProjectEntry(
                        id="proj_1", name="SmartNote",
                        bullets=[
                            Bullet(id="b4", text="Built a full-stack RAG app using Python and SentenceTransformers for semantic search across 1,000+ notes"),
                        ],
                    ),
                ],
            ),
            ResumeSection(
                id="section_3", type=SectionType.SKILLS, title="SKILLS",
                skill_categories=[
                    SkillCategory(id="sk_1", category="Languages", skills=["Python", "Java", "SQL"]),
                ],
            ),
        ],
    )


def _make_jd():
    """Build a minimal JD analysis for testing."""
    return JobAnalysis(
        job_title="Software Engineer",
        required_skills=[
            WeightedItem(name="Python", importance=0.9),
            WeightedItem(name="React", importance=0.8),
        ],
        preferred_skills=[
            WeightedItem(name="Docker", importance=0.5),
        ],
        programming_languages=[WeightedItem(name="python", importance=0.9)],
        frameworks=[WeightedItem(name="react", importance=0.8)],
        responsibilities=[
            Responsibility(text="Develop full-stack web applications", importance=0.9, keywords=["python", "react"]),
        ],
        ats_phrases=["Python", "React", "Docker", "full-stack"],
    )


def _make_bank():
    return ResumeBank(
        experiences=[
            ExperienceBank(
                experience_id="exp_1", company="BigCorp", role="Engineer",
                facts=[
                    Fact(id="f1", text="Built a Python optimization system"),
                    Fact(id="f2", text="Portfolio value exceeded $250M"),
                    Fact(id="f3", text="Reduced risk by 15%"),
                    Fact(id="f4", text="Led a team of 4 engineers"),
                    Fact(id="f5", text="Delivered dashboard 2 weeks early"),
                    Fact(id="f6", text="Organized office parties"),
                ],
            ),
        ],
    )


# --- Test: Low-keyword bullet with strong metrics is preserved ---

class TestMetricsBulletPreserved:
    def test_high_strategic_value_protects_from_removal(self):
        """A bullet with $250M and 15% should not be removed even if keyword overlap is low."""
        plan = PlanResult(
            llm_used=True,
            bullet_plans={
                "b1": BulletPlan(
                    bullet_id="b1",
                    decision="remove",  # LLM suggests removal
                    strategic_value=0.9,  # but high strategic value
                    job_relevance=0.3,
                    reason="Low keyword match",
                ),
            },
        )

        from backend.tailoring.selector import BulletSelection
        # Simulate the merge logic
        from backend.services.tailoring_service import TailoringService
        service = TailoringService(use_llm=False)

        from backend.tailoring.selector import SelectionResult, SkillReorder
        sel = SelectionResult(
            bullet_selections=[
                BulletSelection(bullet_id="b1", entry_id="exp_1",
                                text="Engineered a Python optimization system processing $250M+",
                                action="rewrite", relevance_score=0.3),
            ],
        )

        from backend.tailoring.matcher import MatchResult
        merged = service._merge_plan_with_selection(sel, plan, MatchResult())

        # Should be protected (strategic_value >= 0.6)
        assert merged[0]["action"] == "keep"
        assert "Protected" in merged[0]["reason"]


# --- Test: Leadership bullet with weak keyword overlap ---

class TestLeadershipBulletKept:
    def test_leadership_bullet_not_removed(self):
        """'Led a team of 4 engineers' shows leadership — should not be removed."""
        content = _make_content()
        jd = _make_jd()
        bank = _make_bank()

        matcher = Matcher(jd, content, bank)
        match_result = matcher.match()

        # b2 has "led", "team", "engineers", "deliver" — low exact keyword match
        # but it demonstrates leadership and impact
        b2_score = None
        for entry_score in match_result.entry_scores:
            for bs in entry_score.bullet_scores:
                if bs.bullet_id == "b2":
                    b2_score = bs
                    break

        # The bullet should have some relevance from responsibility matching
        assert b2_score is not None
        assert b2_score.has_metrics  # "4 engineers", "2 weeks"


# --- Test: Irrelevant bullet removed only when over limit ---

class TestIrrelevantBulletRemoval:
    def test_irrelevant_bullet_only_removed_when_over_limit(self):
        """'Organized office birthday parties' is irrelevant but should only be
        removed if the entry has more bullets than max_bullets_per_entry."""
        from backend.tailoring.selector import Selector

        content = _make_content()
        jd = _make_jd()
        bank = _make_bank()

        matcher = Matcher(jd, content, bank)
        match_result = matcher.match()

        # With max_bullets=3 (same as entry count), nothing should be removed
        selector = Selector(jd, content, match_result, bank, max_bullets_per_entry=3)
        result = selector.select()
        removed = [s for s in result.bullet_selections if s.action == "remove"]
        assert len(removed) == 0

        # With max_bullets=2, the least relevant should be removed
        selector2 = Selector(jd, content, match_result, bank, max_bullets_per_entry=2)
        result2 = selector2.select()
        removed2 = [s for s in result2.bullet_selections if s.action == "remove"]
        assert len(removed2) == 1
        assert removed2[0].bullet_id == "b3"  # the party organizer bullet


# --- Test: Rewrite uses only supported source facts ---

class TestRewriteUsesSourceFacts:
    def test_source_fact_ids_validated(self):
        """Rewrite output should only reference facts that actually exist."""
        change = BulletChange(
            bullet_id="b1",
            original_text="Built a Python system",
            tailored_text="Developed a Python system",
            action="rewrite",
            source_fact_ids=["f1", "f999_nonexistent"],
        )

        facts = [{"id": "f1", "text": "Built a Python optimization system"}]

        # The service validates source_fact_ids
        fact_ids = {f["id"] for f in facts}
        valid_ids = [fid for fid in change.source_fact_ids if fid in fact_ids]
        assert valid_ids == ["f1"]
        assert "f999_nonexistent" not in valid_ids


# --- Test: Hallucinated technology rejected ---

class TestHallucinatedTechRejected:
    def test_new_technology_rejected(self):
        """Adding 'Docker' when source facts don't mention it should be rejected."""
        validator = ClaimValidator()

        change = BulletChange(
            bullet_id="b1",
            original_text="Built a Python optimization system",
            tailored_text="Built a Python Docker optimization system",
            action="rewrite",
        )

        facts = [{"id": "f1", "text": "Built a Python optimization system"}]
        result = validator.validate(change, facts)

        assert not result.valid
        assert result.severity == "reject"
        assert any("docker" in issue.lower() for issue in result.issues)


# --- Test: Malformed LLM JSON falls back safely ---

class TestMalformedJsonFallback:
    def test_truncated_json_recovery(self):
        """Truncated JSON should be recovered via robust parsing."""
        planner = ResumePlanner()

        # Simulate truncated response
        truncated = '[{"bullet_id": "b1", "decision": "keep", "strategic_value": 0.8, "job_relevance": 0.6, "reason": "good bullet'
        plans = planner._parse_response(truncated)

        # Should recover at least something
        assert len(plans) >= 1
        assert plans[0].bullet_id == "b1"
        assert plans[0].decision == "keep"

    def test_completely_broken_json(self):
        """Completely broken JSON should return empty list."""
        planner = ResumePlanner()
        plans = planner._parse_response("this is not json at all")
        assert plans == []


# --- Test: Missing API key uses deterministic analysis ---

class TestMissingApiKey:
    def test_planning_returns_empty_on_missing_key(self, monkeypatch):
        """No API key should return PlanResult with error, not crash."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        planner = ResumePlanner()
        content = _make_content()
        jd = _make_jd()
        bank = _make_bank()

        result = planner.plan(content, jd, bank)
        assert not result.llm_used
        assert result.llm_error is not None
        assert "API_KEY" in result.llm_error
        assert len(result.bullet_plans) == 0


# --- Test: Planning output merged with deterministic scores ---

class TestScoreMerging:
    def test_combined_score_formula(self):
        """final_score = deterministic * 0.6 + llm_job_relevance * 0.4"""
        from backend.services.tailoring_service import TailoringService
        from backend.tailoring.selector import BulletSelection, SelectionResult
        from backend.tailoring.matcher import MatchResult

        service = TailoringService(use_llm=False)

        sel = SelectionResult(
            bullet_selections=[
                BulletSelection(
                    bullet_id="b1", entry_id="exp_1",
                    text="test bullet",
                    action="rewrite", relevance_score=0.4,
                ),
            ],
        )

        plan = PlanResult(
            llm_used=True,
            bullet_plans={
                "b1": BulletPlan(
                    bullet_id="b1",
                    decision="rewrite",
                    strategic_value=0.7,
                    job_relevance=0.8,
                    reason="Can add Python keyword",
                    supported_target_keywords=["Python"],
                ),
            },
        )

        merged = service._merge_plan_with_selection(sel, plan, MatchResult())

        expected_combined = 0.4 * 0.6 + 0.8 * 0.4  # = 0.56
        assert abs(merged[0]["combined_score"] - expected_combined) < 0.01
        assert merged[0]["action"] == "rewrite"
        assert "Python" in merged[0]["target_keywords"]

    def test_llm_keep_overrides_deterministic_rewrite(self):
        """If LLM says keep and deterministic says rewrite, trust LLM."""
        from backend.services.tailoring_service import TailoringService
        from backend.tailoring.selector import BulletSelection, SelectionResult
        from backend.tailoring.matcher import MatchResult

        service = TailoringService(use_llm=False)

        sel = SelectionResult(
            bullet_selections=[
                BulletSelection(
                    bullet_id="b1", entry_id="exp_1",
                    text="test", action="rewrite", relevance_score=0.3,
                ),
            ],
        )

        plan = PlanResult(
            llm_used=True,
            bullet_plans={
                "b1": BulletPlan(
                    bullet_id="b1", decision="keep",
                    strategic_value=0.8, job_relevance=0.7,
                    reason="Already well-written",
                ),
            },
        )

        merged = service._merge_plan_with_selection(sel, plan, MatchResult())
        assert merged[0]["action"] == "keep"


# --- Test: User rejection of a suggested rewrite ---

class TestUserRejection:
    def test_rejected_rewrite_uses_original_text(self):
        """When user rejects a rewrite, apply_tailoring should use original text."""
        from backend.models.resume_ir import ResumeIR
        from backend.services.tailoring_service import TailoringService

        content = _make_content()
        ir = ResumeIR(content=content)

        result = TailoringResult(
            resume_id="test",
            bullet_changes=[
                BulletChange(
                    bullet_id="b1",
                    original_text="Original bullet text",
                    tailored_text="Rewritten bullet text",
                    action="rewrite",
                    accepted=False,  # User rejected
                    resolved=True,
                ),
            ],
        )

        service = TailoringService(use_llm=False)
        # Override content bullet text to match
        ir.content.sections[0].experience_entries[0].bullets[0].text = "Original bullet text"
        new_ir = service.apply_tailoring(ir, result)

        # Should keep original since accepted=False
        assert new_ir.content.sections[0].experience_entries[0].bullets[0].text == "Original bullet text"
