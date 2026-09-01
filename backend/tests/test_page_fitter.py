"""Tests for PageFitter, including the whitespace-filling pass.

Covers: the existing shrink-to-fit ladder still works, plus the new
"resume fits with room to spare, restore some shortened content back"
direction added alongside it.
"""

import pytest

from backend.models.resume_content import (
    Bullet, ContactInfo, ExperienceEntry, ResumeContent, ResumeSection,
    SectionType,
)
from backend.models.resume_ir import ResumeIR
from backend.models.tailoring import BulletChange, TailoringResult
from backend.tailoring.page_fitter import PageFitter


def _ir_with_bullets(bullets_text: list[str]) -> ResumeIR:
    content = ResumeContent(
        contact=ContactInfo(name="Test User", email="test@test.com"),
        sections=[
            ResumeSection(
                id="s1", type=SectionType.EXPERIENCE, title="Experience",
                experience_entries=[
                    ExperienceEntry(
                        id="e1", company="Acme", role="Engineer",
                        start_date="2020", end_date="2022",
                        bullets=[
                            Bullet(id=f"b{i}", text=t)
                            for i, t in enumerate(bullets_text)
                        ],
                    ),
                ],
            ),
        ],
    )
    return ResumeIR(content=content)


class TestFitWhenAlreadyFits:
    def test_no_tailoring_result_does_nothing(self):
        # Without a TailoringResult there's no "fuller" text to restore
        # toward — the whitespace pass should just no-op, not error.
        ir = _ir_with_bullets(["Did a thing.", "Did another thing."])
        fitter = PageFitter(ir, target_pages=1)
        new_ir, report = fitter.fit()

        assert report.fits
        assert report.bullets_restored == []


class TestFillWhitespace:
    def test_shortened_bullet_restored_when_page_has_room(self):
        original = (
            "Architected and deployed a distributed data processing pipeline "
            "using Kubernetes to handle over ten thousand events per second "
            "across multiple production regions"
        )
        shortened = "Deployed Kubernetes data pipeline"

        ir = _ir_with_bullets([shortened, "Did another thing."])
        result = TailoringResult(resume_id="r1")
        result.bullet_changes = [
            BulletChange(
                bullet_id="b0",
                action="rewrite",
                original_text=original,
                tailored_text=shortened,
            ),
        ]

        fitter = PageFitter(ir, target_pages=1, tailoring_result=result)
        new_ir, report = fitter.fit()

        assert report.fits
        restored_text = (
            new_ir.content.sections[0].experience_entries[0].bullets[0].text
        )
        assert restored_text == original
        assert "b0" in report.bullets_restored
        assert any("Restored bullet b0" in a for a in report.actions_taken)

    def test_layout_element_stays_in_sync_after_restore(self):
        from backend.models.resume_layout import (
            ElementType, LayoutElement, ParagraphFormat, ResumeLayout, RunFormat,
        )

        original = (
            "Architected and deployed a distributed data processing pipeline "
            "using Kubernetes to handle over ten thousand events per second"
        )
        shortened = "Deployed Kubernetes pipeline"

        ir = _ir_with_bullets([shortened])
        ir.layout = ResumeLayout(elements=[
            LayoutElement(
                element_type=ElementType.BULLET,
                paragraph_format=ParagraphFormat(
                    runs=[RunFormat(text=f"• {shortened}")]
                ),
            ),
        ])
        result = TailoringResult(resume_id="r1")
        result.bullet_changes = [
            BulletChange(
                bullet_id="b0", action="rewrite",
                original_text=original, tailored_text=shortened,
            ),
        ]

        fitter = PageFitter(ir, target_pages=1, tailoring_result=result)
        new_ir, report = fitter.fit()

        el = new_ir.layout.elements[0]
        el_text = "".join(r.text for r in el.paragraph_format.runs)
        assert original in el_text

    def test_does_not_restore_past_target_page_count(self):
        # A "fuller" text long enough that restoring it would overflow the
        # page must be skipped, not force the resume over the target.
        huge_original = "Did a thing. " * 3000  # absurdly long on purpose
        shortened = "Did a thing."

        ir = _ir_with_bullets([shortened])
        result = TailoringResult(resume_id="r1")
        result.bullet_changes = [
            BulletChange(
                bullet_id="b0", action="rewrite",
                original_text=huge_original, tailored_text=shortened,
            ),
        ]

        fitter = PageFitter(ir, target_pages=1, tailoring_result=result)
        new_ir, report = fitter.fit()

        assert report.fits
        assert report.page_count <= 1
        assert "b0" not in report.bullets_restored
        restored_text = (
            new_ir.content.sections[0].experience_entries[0].bullets[0].text
        )
        assert restored_text == shortened

    def test_kept_and_removed_bullets_are_not_candidates(self):
        # Only "rewrite" changes carry a fuller pre-tailoring text to
        # restore toward — "keep"/"remove" changes must be ignored.
        ir = _ir_with_bullets(["Did a thing."])
        result = TailoringResult(resume_id="r1")
        result.bullet_changes = [
            BulletChange(
                bullet_id="b0", action="keep",
                original_text="Did a thing.", tailored_text="Did a thing.",
            ),
            BulletChange(
                bullet_id="ghost", action="remove",
                original_text="Something removed entirely", tailored_text="",
            ),
        ]

        fitter = PageFitter(ir, target_pages=1, tailoring_result=result)
        new_ir, report = fitter.fit()

        assert report.fits
        assert report.bullets_restored == []
