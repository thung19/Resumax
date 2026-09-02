"""Tests for SemanticClassifier.

Regression 1: classify_section_title returned on the FIRST matching
pattern across SECTION_PATTERNS' dict iteration order, not the most
specific match. VOLUNTEER's broad "\\bactivit" pattern was defined
before PUBLICATIONS's own patterns, so a section literally titled
"Publications & Research Activities" (a common academic-CV phrasing)
matched VOLUNTEER first and the whole publications list was silently
mislabeled. Same class of bug: SKILLS's "\\bskill" matched
"SkillBridge Certification" before CERTIFICATIONS's "\\bcertif" ever
got a chance.

Regression 2: _validate_section's content-based fallback fired on "is
skill_categories/project_entries non-empty" alone, with none of the
corroborating checks the EXPERIENCE/EDUCATION branches use -- a
catch-all section mixing an Eagle Scout note, a volunteering line, and
one incidental "Languages: Spanish" entry got force-relabeled SKILLS
just because *something* parsed into a SkillCategory.
"""

from backend.analysis.semantic_classifier import SemanticClassifier
from backend.models.resume_content import (
    ResumeContent, ResumeSection, SectionType, SkillCategory,
)


class TestClassifySectionTitleLongestMatchWins:
    def setup_method(self):
        self.classifier = SemanticClassifier()

    def test_publications_and_research_activities_not_misfiled_as_volunteer(self):
        assert (
            self.classifier.classify_section_title("Publications & Research Activities")
            == SectionType.PUBLICATIONS
        )

    def test_research_activities_alone_still_publications(self):
        assert (
            self.classifier.classify_section_title("Research Activities")
            == SectionType.PUBLICATIONS
        )

    def test_skillbridge_certification_not_misfiled_as_skills(self):
        assert (
            self.classifier.classify_section_title("SkillBridge Certification")
            == SectionType.CERTIFICATIONS
        )

    def test_ordinary_titles_still_classify_correctly(self):
        assert self.classifier.classify_section_title("Work Experience") == SectionType.EXPERIENCE
        assert self.classifier.classify_section_title("Technical Skills") == SectionType.SKILLS
        assert self.classifier.classify_section_title("Education") == SectionType.EDUCATION
        assert self.classifier.classify_section_title("Community Involvement") == SectionType.VOLUNTEER
        assert self.classifier.classify_section_title("Awards & Honors") == SectionType.AWARDS

    def test_genuinely_ambiguous_title_falls_back_to_custom(self):
        assert self.classifier.classify_section_title("Additional Information") == SectionType.CUSTOM


class TestValidateSectionSkillsProjectsOverride:
    def test_mixed_catchall_section_not_force_relabeled_skills(self):
        section = ResumeSection(
            id="s1", title="Additional Information", type=SectionType.CUSTOM,
            skill_categories=[SkillCategory(
                id="sc1", category="Languages", skills=["Spanish (conversational)"],
            )],
            raw_lines=[
                "Eagle Scout, Class of 2015",
                "Enjoys distance running and volunteer tutoring",
            ],
        )
        content = ResumeContent(sections=[section])
        SemanticClassifier().reclassify(content)
        assert content.sections[0].type == SectionType.CUSTOM

    def test_genuinely_skills_only_section_still_classified(self):
        section = ResumeSection(
            id="s2", title="Additional Info", type=SectionType.CUSTOM,
            skill_categories=[SkillCategory(id="sc2", category="Languages", skills=["French"])],
        )
        content = ResumeContent(sections=[section])
        SemanticClassifier().reclassify(content)
        assert content.sections[0].type == SectionType.SKILLS

    def test_genuinely_projects_only_section_still_classified(self):
        from backend.models.resume_content import ProjectEntry
        section = ResumeSection(
            id="s3", title="Misc", type=SectionType.CUSTOM,
            project_entries=[ProjectEntry(id="p1", name="Side Project")],
        )
        content = ResumeContent(sections=[section])
        SemanticClassifier().reclassify(content)
        assert content.sections[0].type == SectionType.PROJECTS
