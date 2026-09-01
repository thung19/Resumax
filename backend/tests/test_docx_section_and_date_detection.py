"""Tests for DocxImporter._detect_section_type and the date-abbreviation
regex in _extract_mixed_entry.

Regression: both used unbounded substring/regex matching for short date
abbreviations and the word "work", the same bug class already fixed once
for DATE_RE in _clean_lr_paragraphs (word boundaries added there to stop
"mar" matching inside "SmartNote") but not applied to these two other
call sites. Confirmed real-world consequences: a "Coursework" or
"Networking" section gets classified as SectionType.EXPERIENCE, and a
location string like "Marlboro, NJ" gets misfiled as a start_date because
"mar" matches inside "Marlboro".
"""

from backend.importers.docx_importer import DocxImporter, ParagraphFormatting


def _importer() -> DocxImporter:
    return DocxImporter.__new__(DocxImporter)


class TestDetectSectionTypeWorkBoundary:
    def test_coursework_not_misclassified_as_experience(self):
        assert _importer()._detect_section_type("Coursework") != _sectiontype_experience()

    def test_relevant_coursework_not_misclassified(self):
        assert _importer()._detect_section_type("Relevant Coursework") != _sectiontype_experience()

    def test_networking_not_misclassified(self):
        assert _importer()._detect_section_type("Networking") != _sectiontype_experience()

    def test_framework_proficiencies_classified_as_skills_not_experience(self):
        from backend.models.resume_content import SectionType
        assert _importer()._detect_section_type("Framework Proficiencies") == SectionType.SKILLS

    def test_work_experience_still_classified_correctly(self):
        assert _importer()._detect_section_type("Work Experience") == _sectiontype_experience()

    def test_employment_history_still_classified_correctly(self):
        assert _importer()._detect_section_type("Employment History") == _sectiontype_experience()

    def test_bare_experience_still_classified_correctly(self):
        assert _importer()._detect_section_type("Experience") == _sectiontype_experience()


def _sectiontype_experience():
    from backend.models.resume_content import SectionType
    return SectionType.EXPERIENCE


class TestExtractMixedEntryDateBoundary:
    def _entry(self, bold_texts: list[str]) -> dict:
        p = ParagraphFormatting()
        p.runs = [{"text": t, "bold": True} for t in bold_texts]
        return _importer()._extract_mixed_entry(p)

    def test_location_not_misfiled_as_date_via_mar_in_marlboro(self):
        result = self._entry(["Augustine Consulting Group", "   ", "Marlboro, NJ"])
        assert result["start_date"] is None
        assert result["end_date"] is None

    def test_smartnote_style_company_not_misfiled_as_date(self):
        result = self._entry(["SmartNote Inc", "   ", "Remote"])
        assert result["start_date"] is None

    def test_real_date_still_detected(self):
        result = self._entry(["Acme Corp", "   ", "Mar 2020 - Aug 2021"])
        assert result["start_date"] is not None
        assert result["end_date"] is not None

    def test_present_still_detected_as_end_date_word(self):
        result = self._entry(["Acme Corp", "   ", "Jan 2022 - Present"])
        assert result["end_date"] is not None

    def test_presentation_skills_not_misfiled_as_date(self):
        result = self._entry(["Acme Corp", "   ", "Presentation Skills"])
        assert result["start_date"] is None
