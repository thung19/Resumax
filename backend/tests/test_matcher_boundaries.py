"""Tests for matcher.py's word-boundary handling in
_text_contains_keyword_direct.

Regression: only aliases of length <=2 were checked with a boundary-safe
regex; anything longer fell back to a raw substring check with no
boundary at all. Confirmed at the real coverage-percentage level: a
resume that never mentions APIs could show 100% coverage of a required
"API" skill purely because the word "rapid" contains "api" as a
substring. Same class of bug for "java" inside "javascript" and "aws"
inside "draws".
"""

from backend.tailoring.matcher import _normalize, _text_contains_keyword_direct


class TestSubstringFalsePositives:
    def test_api_does_not_match_inside_rapid(self):
        text = _normalize("Delivered rapid prototyping and rapid iteration cycles")
        assert _text_contains_keyword_direct(text, "api") is False

    def test_java_does_not_match_inside_javascript(self):
        text = _normalize("Built the frontend using JavaScript and Node.js")
        assert _text_contains_keyword_direct(text, "java") is False

    def test_aws_does_not_match_inside_draws(self):
        text = _normalize("The designer draws mockups by hand")
        assert _text_contains_keyword_direct(text, "aws") is False

    def test_go_does_not_match_inside_google(self):
        # len<=2 aliases were already boundary-protected -- regression
        # guard that the fix didn't accidentally weaken this.
        text = _normalize("Used Google Analytics to track engagement")
        assert _text_contains_keyword_direct(text, "go") is False


class TestGenuineMatchesStillWork:
    def test_api_matches_as_a_real_word(self):
        text = _normalize("Built a REST API for internal services")
        assert _text_contains_keyword_direct(text, "api") is True

    def test_java_matches_as_a_real_word(self):
        text = _normalize("Wrote backend services in Java")
        assert _text_contains_keyword_direct(text, "java") is True

    def test_aws_matches_as_a_real_word(self):
        text = _normalize("Deployed the service on AWS")
        assert _text_contains_keyword_direct(text, "aws") is True

    def test_technology_ending_in_punctuation_still_matches(self):
        # C++/C# end in punctuation, so a plain \b-based fix (rather
        # than the (?<!\w)/(?!\w) lookaround actually used) would have
        # reintroduced the same dead-check bug fixed in
        # claim_validator.py for this exact reason.
        text = _normalize("Wrote performance-critical code in C++")
        assert _text_contains_keyword_direct(text, "c++") is True


class TestFullPipelineCoverageNumberNoLongerInflated:
    def test_api_coverage_is_honest_not_inflated_by_rapid(self):
        from backend.models.job_description import JobAnalysis, WeightedItem
        from backend.models.tailoring import BulletChange, TailoringResult
        from backend.services.tailoring_service import TailoringService

        jd = JobAnalysis(required_skills=[WeightedItem(name="API", importance=1.0)])
        result = TailoringResult(
            resume_id="r1",
            bullet_changes=[BulletChange(
                bullet_id="b1",
                original_text="Delivered rapid prototypes for the design team ahead of schedule",
                tailored_text="Delivered rapid prototypes for the design team ahead of schedule",
                action="keep",
            )],
        )
        svc = TailoringService()
        svc._build_bullet_snapshots(result, jd)
        svc._calculate_coverage_from_snapshots(result, jd)

        assert result.required_skill_coverage == 0.0
        assert result.required_skills_matched == []
