"""Tests for resume_bank_service.py.

Found by a design/structure audit hunting for the same "substring match
without a word boundary" bug class already found and fixed this session in
matcher.py (_text_contains_keyword_direct) and claim_validator.py
(_check_new_technologies). This file's `_extract_tags` had the exact same
bug, unfixed, and even more permissive than matcher.py's pre-fix behavior:
a plain `term in text_lower` substring check, so single-letter/short terms
like "r", "git", "api" false-matched inside ordinary words ("reporting",
"digital", "rapid"). These tags are persisted into the resume bank JSON
(the source-of-truth file for tailoring), so false tags corrupt real data
even though nothing currently consumes them to make fabrication claims.

Also covers: the metric-extraction regex used to run unbounded to the next
period (`[^.]*`), producing a "metric" fact that was really the whole
bullet again; and save_bank/load_bank's atomic-write and corrupt-file
handling.
"""

import json

from backend.models.resume_content import (
    Bullet, ContactInfo, ExperienceEntry, ResumeContent, ResumeSection,
    SectionType,
)
from backend.models.resume_ir import ResumeIR
from backend.services.resume_bank_service import (
    _extract_facts_from_bullet, _extract_tags, generate_bank_from_ir,
    load_bank, save_bank,
)


class TestExtractTagsWordBoundary:
    def test_single_letter_r_does_not_match_inside_reporting(self):
        assert "r" not in _extract_tags("Improved reporting accuracy")

    def test_git_does_not_match_inside_digital(self):
        assert "git" not in _extract_tags("Led digital marketing campaigns")

    def test_api_does_not_match_inside_rapid(self):
        assert "api" not in _extract_tags("Delivered rapid prototypes")

    def test_real_standalone_terms_still_match(self):
        tags = _extract_tags("Built a REST API in Python using Git and Docker")
        assert "rest api" in tags
        assert "python" in tags
        assert "git" in tags
        assert "docker" in tags

    def test_r_language_still_matches_as_standalone_word(self):
        assert "r" in _extract_tags("Analyzed data using R for statistics")

    def test_multiword_term_with_punctuation_still_matches(self):
        assert "c++" in _extract_tags("Wrote performance-critical code in C++")
        assert "next.js" in _extract_tags("Built the frontend in Next.js")


class TestMetricExtractionNotWholeSentence:
    def test_metric_fact_is_short_not_the_whole_bullet(self):
        bullet = (
            "Increased conversion by 25% through A/B testing and "
            "analytics dashboards"
        )
        facts = _extract_facts_from_bullet(bullet, "e1_b1")
        metric_facts = [f for f in facts if "metric" in f.tags]
        assert metric_facts, "expected at least one metric fact"
        for f in metric_facts:
            assert len(f.text) < len(bullet)
            assert f.text != bullet


class TestSaveLoadBankRoundtrip:
    def test_roundtrip(self, tmp_path, monkeypatch):
        import backend.services.resume_bank_service as svc
        monkeypatch.setattr(svc, "DATA_DIR", tmp_path)

        content = ResumeContent(
            contact=ContactInfo(name="Jane Doe"),
            sections=[ResumeSection(
                id="s1", type=SectionType.EXPERIENCE, title="Experience",
                experience_entries=[ExperienceEntry(
                    id="e1", company="Acme", role="Engineer",
                    bullets=[Bullet(id="b1", text="Built a REST API in Python")],
                )],
            )],
        )
        bank = generate_bank_from_ir(ResumeIR(content=content))
        save_bank(bank, "abc123456789")

        loaded = load_bank("abc123456789")
        assert loaded is not None
        assert loaded.experiences[0].company == "Acme"

    def test_load_missing_returns_none(self, tmp_path, monkeypatch):
        import backend.services.resume_bank_service as svc
        monkeypatch.setattr(svc, "DATA_DIR", tmp_path)
        assert load_bank("doesnotexist0") is None

    def test_load_corrupt_file_returns_none_instead_of_raising(self, tmp_path, monkeypatch):
        import backend.services.resume_bank_service as svc
        monkeypatch.setattr(svc, "DATA_DIR", tmp_path)
        tmp_path.mkdir(parents=True, exist_ok=True)
        (tmp_path / "corrupt1234_bank.json").write_text("{not valid json")
        assert load_bank("corrupt1234") is None

    def test_save_does_not_leave_tmp_file_behind(self, tmp_path, monkeypatch):
        import backend.services.resume_bank_service as svc
        monkeypatch.setattr(svc, "DATA_DIR", tmp_path)
        content = ResumeContent(contact=ContactInfo(name="Jane Doe"))
        save_bank(generate_bank_from_ir(ResumeIR(content=content)), "xyz987654321")
        assert (tmp_path / "xyz987654321_bank.json").exists()
        assert not (tmp_path / "xyz987654321_bank.json.tmp").exists()
