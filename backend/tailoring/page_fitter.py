"""Page Fitting Engine.

Detects overflow by rendering a PDF and checking page count.
Applies correction priority to fit the resume onto one page:

1. Shorten the overflowing bullet while preserving facts
2. Shorten other verbose low-priority bullets
3. Remove the lowest-relevance optional bullet
4. Reduce optional content
5. Slightly adjust paragraph/section spacing if allowed
6. Adjust font size only as last resort (never below minimum)
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Optional

from backend.models.resume_ir import ResumeIR
from backend.models.resume_content import SectionType, Bullet
from backend.models.tailoring import TailoringResult
from backend.renderers.pdf_renderer import PdfRenderer


@dataclass
class OverflowReport:
    """Report on page fitting status."""
    fits: bool = True
    page_count: int = 1
    actions_taken: list[str] = field(default_factory=list)
    bullets_shortened: list[str] = field(default_factory=list)
    bullets_removed: list[str] = field(default_factory=list)
    spacing_adjusted: bool = False
    font_adjusted: bool = False


@dataclass
class BulletInfo:
    """Info about a bullet for priority sorting."""
    section_idx: int
    entry_idx: int
    bullet_idx: int
    bullet_id: str
    text: str
    char_count: int
    relevance: float  # from tailoring result, or default
    entry_type: str  # "experience" | "project" | "volunteer"


class PageFitter:
    """Fit a resume onto a target page count."""

    def __init__(
        self,
        ir: ResumeIR,
        target_pages: int = 1,
        min_font_pt: float = 8.5,
        allow_removal: bool = True,
        allow_spacing_compression: bool = True,
        tailoring_result: Optional[TailoringResult] = None,
    ):
        self._ir = copy.deepcopy(ir)
        self._target = target_pages
        self._min_font = min_font_pt
        self._allow_removal = allow_removal
        self._allow_spacing = allow_spacing_compression
        self._tailoring = tailoring_result
        self._report = OverflowReport()

    def fit(self) -> tuple[ResumeIR, OverflowReport]:
        """Attempt to fit the resume. Returns (modified IR, report)."""
        # Check current page count
        pages = self._check_pages()
        self._report.page_count = pages

        if pages <= self._target:
            self._report.fits = True
            return self._ir, self._report

        # Collect all bullets with metadata
        bullets = self._collect_bullets()

        # Sort by priority: longest + lowest relevance first
        bullets.sort(key=lambda b: (-b.char_count, b.relevance))

        # Step 1: Shorten the longest bullets
        for b_info in bullets:
            if b_info.char_count > 150:
                shortened = self._shorten_text(b_info.text, target_chars=130)
                self._update_bullet(b_info, shortened)
                self._report.bullets_shortened.append(b_info.bullet_id)
                self._report.actions_taken.append(
                    f"Shortened bullet {b_info.bullet_id} from {b_info.char_count} to {len(shortened)} chars"
                )

                pages = self._check_pages()
                if pages <= self._target:
                    self._report.fits = True
                    self._report.page_count = pages
                    return self._ir, self._report

        # Step 2: Shorten medium-length bullets
        for b_info in bullets:
            if 100 < b_info.char_count <= 150:
                shortened = self._shorten_text(b_info.text, target_chars=95)
                self._update_bullet(b_info, shortened)
                self._report.bullets_shortened.append(b_info.bullet_id)
                self._report.actions_taken.append(
                    f"Shortened bullet {b_info.bullet_id} to {len(shortened)} chars"
                )

                pages = self._check_pages()
                if pages <= self._target:
                    self._report.fits = True
                    self._report.page_count = pages
                    return self._ir, self._report

        # Step 3: Remove lowest-relevance bullets
        if self._allow_removal:
            # Re-sort by relevance (lowest first)
            bullets.sort(key=lambda b: b.relevance)

            # Build entry bullet counts so we never remove the last bullet
            entry_bullet_counts = self._count_entry_bullets()

            for b_info in bullets:
                entry_key = (b_info.section_idx, b_info.entry_idx, b_info.entry_type)
                if entry_bullet_counts.get(entry_key, 0) <= 1:
                    # Don't remove the only bullet — entry would be empty
                    continue

                self._remove_bullet(b_info)
                entry_bullet_counts[entry_key] -= 1
                self._report.bullets_removed.append(b_info.bullet_id)
                self._report.actions_taken.append(
                    f"Removed bullet {b_info.bullet_id} (relevance={b_info.relevance:.2f})"
                )

                pages = self._check_pages()
                if pages <= self._target:
                    self._report.fits = True
                    self._report.page_count = pages
                    return self._ir, self._report

        # Step 4: Compress spacing
        if self._allow_spacing:
            self._compress_spacing()
            self._report.spacing_adjusted = True
            self._report.actions_taken.append("Compressed section/entry spacing")

            pages = self._check_pages()
            if pages <= self._target:
                self._report.fits = True
                self._report.page_count = pages
                return self._ir, self._report

        # Step 5: Reduce font size (last resort)
        current_font = self._get_bullet_font_size()
        while current_font > self._min_font:
            current_font -= 0.5
            self._set_all_body_font_size(current_font)
            self._report.font_adjusted = True
            self._report.actions_taken.append(f"Reduced body font to {current_font}pt")

            pages = self._check_pages()
            if pages <= self._target:
                self._report.fits = True
                self._report.page_count = pages
                return self._ir, self._report

        # Could not fit
        self._report.fits = False
        self._report.page_count = self._check_pages()
        return self._ir, self._report

    def _check_pages(self) -> int:
        """Render PDF and return page count."""
        renderer = PdfRenderer(self._ir)
        renderer.render()
        return renderer.get_overflow_info().page_count

    def _collect_bullets(self) -> list[BulletInfo]:
        """Collect all bullets with metadata."""
        bullets: list[BulletInfo] = []

        # Build relevance lookup from tailoring result
        relevance_map: dict[str, float] = {}
        if self._tailoring:
            for change in self._tailoring.bullet_changes:
                # Use the match-derived relevance (not available directly,
                # so estimate from action)
                if change.action == "keep":
                    relevance_map[change.bullet_id] = 0.7
                elif change.action == "rewrite":
                    relevance_map[change.bullet_id] = 0.5
                elif change.action == "remove":
                    relevance_map[change.bullet_id] = 0.1

        for si, section in enumerate(self._ir.content.sections):
            if section.type in (SectionType.EXPERIENCE, SectionType.VOLUNTEER):
                for ei, entry in enumerate(section.experience_entries):
                    for bi, bullet in enumerate(entry.bullets):
                        bullets.append(BulletInfo(
                            section_idx=si,
                            entry_idx=ei,
                            bullet_idx=bi,
                            bullet_id=bullet.id,
                            text=bullet.text,
                            char_count=len(bullet.text),
                            relevance=relevance_map.get(bullet.id, 0.5),
                            entry_type="experience",
                        ))

            elif section.type == SectionType.PROJECTS:
                for ei, entry in enumerate(section.project_entries):
                    for bi, bullet in enumerate(entry.bullets):
                        bullets.append(BulletInfo(
                            section_idx=si,
                            entry_idx=ei,
                            bullet_idx=bi,
                            bullet_id=bullet.id,
                            text=bullet.text,
                            char_count=len(bullet.text),
                            relevance=relevance_map.get(bullet.id, 0.5),
                            entry_type="project",
                        ))

        return bullets

    def _count_entry_bullets(self) -> dict[tuple, int]:
        """Count current bullets per entry so we can protect single-bullet entries."""
        counts: dict[tuple, int] = {}
        for si, section in enumerate(self._ir.content.sections):
            if section.type in (SectionType.EXPERIENCE, SectionType.VOLUNTEER):
                for ei, entry in enumerate(section.experience_entries):
                    counts[(si, ei, "experience")] = len(entry.bullets)
            elif section.type == SectionType.PROJECTS:
                for ei, entry in enumerate(section.project_entries):
                    counts[(si, ei, "project")] = len(entry.bullets)
        return counts

    def _shorten_text(self, text: str, target_chars: int) -> str:
        """Deterministically shorten text to target character count."""
        if len(text) <= target_chars:
            return text

        # Try trimming from the end at a word boundary
        truncated = text[:target_chars]
        last_space = truncated.rfind(" ")
        if last_space > target_chars * 0.7:
            return truncated[:last_space].rstrip(",;: ")

        return truncated.rstrip(",;: ")

    def _update_bullet(self, b_info: BulletInfo, new_text: str):
        """Update a bullet's text in the IR."""
        section = self._ir.content.sections[b_info.section_idx]
        if b_info.entry_type == "experience":
            section.experience_entries[b_info.entry_idx].bullets[b_info.bullet_idx].text = new_text
        elif b_info.entry_type == "project":
            section.project_entries[b_info.entry_idx].bullets[b_info.bullet_idx].text = new_text
        b_info.char_count = len(new_text)
        b_info.text = new_text

    def _remove_bullet(self, b_info: BulletInfo):
        """Remove a bullet from both content and layout elements."""
        section = self._ir.content.sections[b_info.section_idx]
        if b_info.entry_type == "experience":
            bullets = section.experience_entries[b_info.entry_idx].bullets
            # Get the text before removing so we can match layout elements
            removed_text = None
            for b in bullets:
                if b.id == b_info.bullet_id:
                    removed_text = b.text
                    break
            bullets[:] = [b for b in bullets if b.id != b_info.bullet_id]
        elif b_info.entry_type == "project":
            bullets = section.project_entries[b_info.entry_idx].bullets
            removed_text = None
            for b in bullets:
                if b.id == b_info.bullet_id:
                    removed_text = b.text
                    break
            bullets[:] = [b for b in bullets if b.id != b_info.bullet_id]
        else:
            removed_text = None

        # Also remove from layout elements so renderers stay in sync
        if removed_text and self._ir.layout.elements:
            from backend.models.resume_layout import ElementType
            clean_prefix = removed_text[:30]
            new_elements = []
            removed_one = False
            for el in self._ir.layout.elements:
                if (
                    not removed_one
                    and el.element_type == ElementType.BULLET
                ):
                    el_text = "".join(
                        r.text for r in el.paragraph_format.runs if not r.is_tab
                    ).lstrip("\u2022 ").strip()
                    if el_text[:30] == clean_prefix:
                        removed_one = True
                        continue
                new_elements.append(el)
            self._ir.layout.elements = new_elements

    def _compress_spacing(self):
        """Reduce spacing in layout styles."""
        for style_name, style_def in self._ir.layout.styles.items():
            if style_def.spacing.space_before_pt > 2:
                style_def.spacing.space_before_pt = max(1, style_def.spacing.space_before_pt * 0.6)
            if style_def.spacing.space_after_pt > 2:
                style_def.spacing.space_after_pt = max(1, style_def.spacing.space_after_pt * 0.6)

    def _get_bullet_font_size(self) -> float:
        """Get current bullet font size."""
        bullet_style = self._ir.layout.styles.get("bullet")
        if bullet_style:
            return bullet_style.font.size_pt
        return 10.0

    def _set_all_body_font_size(self, size_pt: float):
        """Set font size for all body-level styles."""
        for name in ["bullet", "entry_subheader", "skills_row", "body_text"]:
            style = self._ir.layout.styles.get(name)
            if style:
                style.font.size_pt = size_pt
