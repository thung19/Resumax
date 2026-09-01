"""Tests for JobAnalyzer._classify_required_preferred's inline
requirement detection (deterministic path).

Regression: the classifier only extracted keywords from lines AFTER a
"required"/"must have" trigger line -- it set the in_required flag and
`continue`d past the trigger line itself without extracting anything
from it. Real JDs frequently state the requirement and the skill in the
same sentence ("Must have Kubernetes experience..."), so that line's
skill was never classified as required at all. This only matters when
LLM enrichment is unavailable (analyze() falls back to this deterministic
result on any LLM error) -- when it IS available, it independently
re-derives required/preferred classification from full context.
"""

from backend.analysis.job_analyzer import JobAnalyzer

JD_TEXT = """Senior Backend Engineer
Requirements:
Must have Kubernetes experience and strong Python skills to succeed in this role.
Experience with PostgreSQL is required for this position.

Nice to have:
Familiarity with Docker containers.
"""


class TestInlineRequirementClassification:
    def test_inline_required_skills_get_boosted_importance(self):
        analyzer = JobAnalyzer(use_llm=False)
        analysis = analyzer._deterministic_analysis(JD_TEXT)

        required_names = {i.name.lower() for i in analysis.required_skills}
        assert "kubernetes" in required_names
        assert "python" in required_names
        assert "postgresql" in required_names

    def test_preferred_skill_not_classified_as_required(self):
        analyzer = JobAnalyzer(use_llm=False)
        analysis = analyzer._deterministic_analysis(JD_TEXT)

        required_names = {i.name.lower() for i in analysis.required_skills}
        assert "docker" not in required_names

    def test_required_skill_scores_higher_than_merely_preferred(self):
        analyzer = JobAnalyzer(use_llm=False)
        analysis = analyzer._deterministic_analysis(JD_TEXT)

        by_name = {i.name.lower(): i.importance for i in analysis.infrastructure}
        # Kubernetes (inline "must have") must outrank Docker (nice to
        # have) -- this was inverted before the fix, since Kubernetes
        # never got its required-boost at all.
        assert by_name["kubernetes"] > by_name["docker"]

    def test_requirement_on_its_own_line_still_works(self):
        # Regression guard: the fix must not break the already-working
        # "trigger line, then separate requirement lines" shape.
        text = (
            "Required Skills:\n"
            "Kubernetes\n"
            "Python\n"
            "\n"
            "Nice to have:\n"
            "Docker\n"
        )
        analyzer = JobAnalyzer(use_llm=False)
        analysis = analyzer._deterministic_analysis(text)
        required_names = {i.name.lower() for i in analysis.required_skills}
        assert "kubernetes" in required_names
        assert "python" in required_names

    def test_bare_requirements_header_now_triggers_too(self):
        # Separate, pre-existing gap found and fixed alongside this one:
        # "required" isn't actually a substring of "requirements"
        # (require-MENTS vs require-D), so a JD section literally
        # titled "Requirements:" (an extremely common header) never
        # triggered in_required at all before.
        text = "Requirements:\nKubernetes\nPython\n"
        analyzer = JobAnalyzer(use_llm=False)
        analysis = analyzer._deterministic_analysis(text)
        required_names = {i.name.lower() for i in analysis.required_skills}
        assert "kubernetes" in required_names
        assert "python" in required_names
