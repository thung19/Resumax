"""Tests for the tailoring pipeline.

Covers: claim validation, matcher scoring, deterministic fallback,
user rejection, tailoring engine parsing.
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


# --- Test: Fabricated metrics rejected ---

class TestFabricatedMetrics:
    def test_new_metric_rejected(self):
        """Adding a metric not in source facts should be rejected."""
        validator = ClaimValidator()

        change = BulletChange(
            bullet_id="b1",
            original_text="Built a Python system",
            tailored_text="Built a Python system serving 500 users",
            action="rewrite",
        )

        facts = [{"id": "f1", "text": "Built a Python system"}]
        result = validator.validate(change, facts)

        assert not result.valid
        assert any("500" in issue for issue in result.issues)


# --- Test: Metric preservation warning ---

class TestMetricPreservation:
    def test_dropped_metric_warns(self):
        """Dropping a metric from original should produce a warning, not rejection."""
        validator = ClaimValidator()

        change = BulletChange(
            bullet_id="b1",
            original_text="Processed $250M in portfolios reducing risk by 15%",
            tailored_text="Processed portfolios and reduced risk significantly",
            action="rewrite",
        )

        facts = [{"id": "f1", "text": "Processed $250M in portfolios, reduced risk by 15%"}]
        result = validator.validate(change, facts)

        # Should be valid (no fabrication) but have warnings
        assert result.valid
        assert len(result.metric_warnings) > 0
        assert any("250" in w for w in result.metric_warnings)


# --- Test: Allowed terms don't trigger rejection ---

class TestAllowedTerms:
    def test_generic_descriptors_allowed(self):
        """Generic terms like 'software', 'developed', 'RESTful' should never be flagged."""
        validator = ClaimValidator()

        change = BulletChange(
            bullet_id="b1",
            original_text="Built a Python system",
            tailored_text="Developed a Python software system with RESTful API",
            action="rewrite",
        )

        facts = [{"id": "f1", "text": "Built a Python system with API"}]
        result = validator.validate(change, facts)

        assert result.valid


# --- Regression: fabrication safety-net holes found by a proactive audit ---
# _check_new_technologies' \b-based regex could never match a term ending
# in punctuation ("c#", "c++") against following whitespace, since \b needs
# a word/non-word transition and both sides are non-word there -- the
# check was structurally dead code for those two extremely common
# languages. _check_fabricated_metrics separately exempted any fabricated
# number 10-or-under, and only ever compared bare digit identity with no
# concept of what a number was attached to.

class TestCPlusPlusAndCSharpFabricationCaught:
    def test_fabricated_csharp_rejected(self):
        validator = ClaimValidator()
        change = BulletChange(
            bullet_id="b1",
            original_text="Built internal tools for the data team using Python scripts.",
            tailored_text="Built internal tools for the data team using C# and Python scripts.",
            action="rewrite",
        )
        result = validator.validate(change, [{"id": "f1", "text": change.original_text}])
        assert not result.valid
        assert any("c#" in issue.lower() for issue in result.issues)

    def test_fabricated_cpp_rejected(self):
        validator = ClaimValidator()
        change = BulletChange(
            bullet_id="b1",
            original_text="Built internal tools for the data team using Python scripts.",
            tailored_text="Built internal tools for the data team using C++ and Python scripts.",
            action="rewrite",
        )
        result = validator.validate(change, [{"id": "f1", "text": change.original_text}])
        assert not result.valid
        assert any("c++" in issue.lower() for issue in result.issues)

    def test_genuinely_present_csharp_still_allowed(self):
        # The fix must not become "always reject C#/C++" -- it should
        # still pass when genuinely present in source.
        validator = ClaimValidator()
        change = BulletChange(
            bullet_id="b1",
            original_text="Built a C# desktop application for internal use.",
            tailored_text="Engineered a C# desktop application streamlining internal workflows.",
            action="rewrite",
        )
        result = validator.validate(change, [{"id": "f1", "text": change.original_text}])
        assert result.valid


class TestSmallFabricatedNumbersCaught:
    def test_fabricated_team_size_under_eleven_rejected(self):
        validator = ClaimValidator()
        change = BulletChange(
            bullet_id="b1",
            original_text="Led a project to migrate the reporting service.",
            tailored_text="Led a team of 8 engineers to migrate the reporting service.",
            action="rewrite",
        )
        result = validator.validate(change, [{"id": "f1", "text": change.original_text}])
        assert not result.valid
        assert any("8" in issue for issue in result.issues)

    def test_fabricated_multiplier_under_eleven_rejected(self):
        validator = ClaimValidator()
        change = BulletChange(
            bullet_id="b1",
            original_text="Migrated the reporting service to a new pipeline.",
            tailored_text="Migrated the reporting service, cutting deploy time by 9x.",
            action="rewrite",
        )
        result = validator.validate(change, [{"id": "f1", "text": change.original_text}])
        assert not result.valid
        assert any("9x" in issue for issue in result.issues)

    def test_genuine_small_number_from_source_still_allowed(self):
        validator = ClaimValidator()
        change = BulletChange(
            bullet_id="b1",
            original_text="Led a team of 4 engineers on the migration.",
            tailored_text="Directed a team of 4 engineers through the service migration.",
            action="rewrite",
        )
        result = validator.validate(change, [{"id": "f1", "text": change.original_text}])
        assert result.valid


class TestMetricUnitMismatchCaught:
    def test_percent_claim_not_validated_by_unrelated_bare_number(self):
        """A number appearing anywhere in source for something unrelated
        must not validate a differently-unitted, fabricated claim built
        from the same digits."""
        validator = ClaimValidator()
        change = BulletChange(
            bullet_id="b1",
            original_text="Managed a support queue handling 40 tickets per week.",
            tailored_text="Improved system reliability, boosting uptime by 40%.",
            action="rewrite",
        )
        result = validator.validate(change, [{"id": "f1", "text": change.original_text}])
        assert not result.valid
        assert any("40%" in issue for issue in result.issues)

    def test_genuine_percent_claim_from_source_still_allowed(self):
        validator = ClaimValidator()
        change = BulletChange(
            bullet_id="b1",
            original_text="Improved system reliability, boosting uptime by 40%.",
            tailored_text="Increased platform reliability, lifting uptime 40%.",
            action="rewrite",
        )
        result = validator.validate(change, [{"id": "f1", "text": change.original_text}])
        assert result.valid

    def test_number_with_letter_suffix_like_250m_still_extracted(self):
        # Regression guard: an earlier version of this fix required a
        # word-boundary after the digits, which broke extraction of
        # numbers immediately followed by a letter unit (e.g. "$250M")
        # since neither side of that boundary is a word/non-word
        # transition-free zone. Dropping "$250M" entirely from a rewrite
        # must still warn as a preservation loss, not go unnoticed.
        validator = ClaimValidator()
        change = BulletChange(
            bullet_id="b1",
            original_text="Processed $250M in portfolios reducing risk by 15%",
            tailored_text="Processed portfolios and reduced risk significantly",
            action="rewrite",
        )
        result = validator.validate(
            change, [{"id": "f1", "text": "Processed $250M in portfolios, reduced risk by 15%"}],
        )
        assert result.valid
        assert any("250" in w for w in result.metric_warnings)


class TestExpandedTechTerms:
    """A handful of common technologies confirmed absent from TECH_TERMS
    -- previously structurally undetectable as fabrications, since
    _check_new_technologies only ever tests membership in that set."""

    @pytest.mark.parametrize("tech", ["SQL", "Spark", "Airflow", "Grafana", "Snowflake"])
    def test_fabricated_technology_now_caught(self, tech):
        validator = ClaimValidator()
        change = BulletChange(
            bullet_id="b1",
            original_text="Built internal tools for the data team.",
            tailored_text=f"Built internal tools using {tech} for the data team.",
            action="rewrite",
        )
        result = validator.validate(change, [{"id": "f1", "text": change.original_text}])
        assert not result.valid
        assert any(tech.lower() in issue.lower() for issue in result.issues)


# --- Test: Matcher direct scoring (no IMPLIES inflation) ---

class TestMatcherScoring:
    def test_bullet_with_exact_keyword_scores_high(self):
        """A bullet containing exact JD keywords should score well."""
        content = _make_content()
        jd = _make_jd()
        bank = _make_bank()

        matcher = Matcher(jd, content, bank)
        match_result = matcher.match()

        # b1 mentions "Python" directly
        b1_score = None
        for entry_score in match_result.entry_scores:
            for bs in entry_score.bullet_scores:
                if bs.bullet_id == "b1":
                    b1_score = bs
                    break

        assert b1_score is not None
        assert "python" in [k.lower() for k in b1_score.keyword_matches]

    def test_irrelevant_bullet_scores_low(self):
        """'Organized office birthday parties' has no JD keywords."""
        content = _make_content()
        jd = _make_jd()
        bank = _make_bank()

        matcher = Matcher(jd, content, bank)
        match_result = matcher.match()

        b3_score = None
        for entry_score in match_result.entry_scores:
            for bs in entry_score.bullet_scores:
                if bs.bullet_id == "b3":
                    b3_score = bs
                    break

        assert b3_score is not None
        assert b3_score.relevance_score < 0.3

    def test_coverage_report(self):
        """Matcher should report which keywords are covered vs missing."""
        content = _make_content()
        jd = _make_jd()
        bank = _make_bank()

        matcher = Matcher(jd, content, bank)
        match_result = matcher.match()

        # Python should be matched
        assert "python" in [k.lower() for k in match_result.matched_keywords]
        # Docker should be missing (not in resume)
        docker_missing = any(
            "docker" in k.lower()
            for k in match_result.missing_keywords
        )
        docker_bankable = any(
            "docker" in k.lower()
            for k in match_result.bankable_keywords
        )
        assert docker_missing or docker_bankable


# --- Test: Metrics bullet has has_metrics flag ---

class TestMetricsDetection:
    def test_metrics_detected(self):
        """Bullets with numbers should have has_metrics=True."""
        content = _make_content()
        jd = _make_jd()

        matcher = Matcher(jd, content)
        match_result = matcher.match()

        # b1 has "$250M+" and "15%"
        b1_score = None
        for entry_score in match_result.entry_scores:
            for bs in entry_score.bullet_scores:
                if bs.bullet_id == "b1":
                    b1_score = bs
                    break

        assert b1_score is not None
        assert b1_score.has_metrics


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
        ir.content.sections[0].experience_entries[0].bullets[0].text = "Original bullet text"
        new_ir = service.apply_tailoring(ir, result)

        assert new_ir.content.sections[0].experience_entries[0].bullets[0].text == "Original bullet text"


# --- Test: Deterministic fallback when LLM unavailable ---

class TestDeterministicFallback:
    def test_no_llm_keeps_all_bullets(self):
        """Without LLM, all bullets should be kept unchanged."""
        from backend.services.tailoring_service import TailoringService

        service = TailoringService(use_llm=False)
        content = _make_content()

        changes = service._deterministic_fallback(content)

        assert len(changes) == 4  # b1, b2, b3, b4
        for change in changes:
            assert change.action == "keep"
            assert change.original_text == change.tailored_text


# --- Test: Tailoring engine JSON parsing ---

class TestEngineJsonParsing:
    def test_parse_valid_response(self):
        """Engine should parse well-formed JSON response."""
        from backend.tailoring.tailoring_engine import TailoringEngine

        engine = TailoringEngine()
        content = _make_content()

        response = json.dumps({
            "bullet_changes": [
                {
                    "bullet_id": "b1",
                    "action": "rewrite",
                    "new_text": "Developed a Python optimization software system processing $250M+, reducing risk by 15%",
                    "reason": "Added 'software' descriptor",
                    "keywords_added": ["software"],
                },
                {
                    "bullet_id": "b2",
                    "action": "keep",
                    "reason": "Already strong",
                },
                {
                    "bullet_id": "b3",
                    "action": "remove",
                    "reason": "Irrelevant to JD",
                },
            ],
            "skill_reorders": {
                "Languages": ["Python", "SQL", "Java"],
            },
            "skill_additions": {
                "Languages": ["React"],
            },
        })

        parsed = engine._parse_response(response, content)

        # All 4 bullets should have entries (b4 gets default keep)
        assert len(parsed["bullet_changes"]) == 4

        # Check specific actions
        by_id = {c.bullet_id: c for c in parsed["bullet_changes"]}
        assert by_id["b1"].action == "rewrite"
        assert "software" in by_id["b1"].tailored_text.lower()
        assert by_id["b2"].action == "keep"
        assert by_id["b3"].action == "remove"
        assert by_id["b4"].action == "keep"  # default

        # Check skills
        assert parsed["skill_reorders"]["Languages"] == ["Python", "SQL", "Java"]
        assert parsed["skill_additions"]["Languages"] == ["React"]

    def test_parse_truncated_json(self):
        """Engine should handle truncated JSON gracefully."""
        from backend.tailoring.tailoring_engine import TailoringEngine

        engine = TailoringEngine()
        content = _make_content()

        # Truncated response
        truncated = '{"bullet_changes": [{"bullet_id": "b1", "action": "keep", "reason": "good'
        parsed = engine._parse_response(truncated, content)

        # Should still return all bullets with defaults
        assert len(parsed["bullet_changes"]) >= 1

    def test_empty_response_returns_keeps(self):
        """Empty/broken response should default all bullets to keep."""
        from backend.tailoring.tailoring_engine import TailoringEngine

        engine = TailoringEngine()
        content = _make_content()

        parsed = engine._parse_response("not json", content)

        assert len(parsed["bullet_changes"]) == 4
        for change in parsed["bullet_changes"]:
            assert change.action == "keep"


class TestTechTermsPresent:
    """`_tech_terms_present` protects pre-existing skill/tech words (not just
    JD keywords the main pass newly added) from being silently dropped by
    the batch-trim "aggressive rephrasing" pass.

    Regression coverage for: "Enhanced a Vue and Vite JavaScript QA tool..."
    getting trimmed down to "Enhanced Vue/Vite web application..." — losing
    "JavaScript" entirely because it was never in `target_keywords`.
    """

    def test_finds_known_tech_terms_with_original_casing(self):
        from backend.services.tailoring_service import _tech_terms_present

        text = "Enhanced a Vue and Vite JavaScript QA tool improving D3.js visuals"
        found = _tech_terms_present(text)
        assert "JavaScript" in found
        assert "Vue" in found
        assert "D3.js" in found

    def test_word_boundaries_avoid_false_positives(self):
        from backend.services.tailoring_service import _tech_terms_present

        # "r" and "go" are TECH_TERMS (R, Go the languages) but must not
        # match inside unrelated words like "for" or "going".
        text = "Translated researcher requirements for going forward"
        found = _tech_terms_present(text)
        assert found == []

    def test_no_tech_terms_returns_empty(self):
        from backend.services.tailoring_service import _tech_terms_present

        assert _tech_terms_present("Led weekly standups with stakeholders") == []
