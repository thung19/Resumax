"""Unified Tailoring Engine.

Single LLM pass that sees the full resume and the raw JD text.
Produces all bullet rewrites, skill reorders, and skill additions.

The prompt is intentionally simple — modeled on what works
when pasting a resume + JD into ChatGPT with a one-line instruction.
The LLM sees the raw JD (not a pre-digested summary) so it can
pick up on phrasing, emphasis, and qualitative language.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Optional

from backend.models.job_description import JobAnalysis
from backend.models.resume_content import ResumeContent, SectionType
from backend.models.tailoring import BulletChange, ResumeBank
from backend.prompts import (
    TAILORING_SYSTEM_V2,
    TAILORING_USER_V2,
    BATCH_TRIM_SYSTEM_V2,
    BATCH_TRIM_USER_V2,
    BATCH_TRIM_RETRY_HINT,
    REJECTION_RETRY_SYSTEM_V1,
    REJECTION_RETRY_USER_TEMPLATE,
    FREEFORM_EDIT_SYSTEM_V1,
    FREEFORM_EDIT_USER_TEMPLATE,
)

logger = logging.getLogger(__name__)


def _sanitize_error(e: Exception) -> str:
    """Sanitize error message to prevent leaking secrets."""
    msg = str(e)
    # Strip anything that looks like an API key (sk-ant-..., sk-...)
    import re
    msg = re.sub(r"sk-[a-zA-Z0-9_-]{10,}", "[REDACTED]", msg)
    return msg[:200]


@dataclass
class TailoringEngineResult:
    """Output of the unified tailoring pass."""
    bullet_changes: list[BulletChange] = field(default_factory=list)
    skill_reorders: dict[str, list[str]] = field(default_factory=dict)
    skill_additions: dict[str, list[str]] = field(default_factory=dict)
    llm_used: bool = False
    llm_error: Optional[str] = None
    duration_ms: int = 0




def _compute_char_cap(
    text: str,
    available_width_pt: float,
    font_name: str,
    font_size: float,
) -> int:
    """Compute how many chars fit on one line for text with this character mix."""
    from reportlab.pdfbase.pdfmetrics import stringWidth

    if not text:
        return 120
    text_width = stringWidth(text, font_name, font_size)
    if text_width <= 0:
        return 120
    avg_char_width = text_width / len(text)
    # Subtract bullet prefix width
    prefix_width = stringWidth("\u2022 ", font_name, font_size)
    usable = available_width_pt - prefix_width
    return max(40, int(usable / avg_char_width))


def _build_resume_text(
    content: ResumeContent,
    max_chars: int,
    available_width_pt: float = 0,
    font_name: str = "",
    font_size: float = 0,
) -> str:
    """Build resume text with bullet IDs and per-bullet char caps."""
    use_per_bullet = available_width_pt > 0 and font_name and font_size > 0
    parts: list[str] = []

    for section in content.sections:
        if section.title:
            parts.append(f"\n=== {section.title} ===")

        for entry in section.experience_entries:
            date = ""
            if entry.start_date and entry.end_date:
                date = f" ({entry.start_date} – {entry.end_date})"
            parts.append(f"\n{entry.company}{date}")
            parts.append(f"{entry.role} | {entry.location or ''}")
            for b in entry.bullets:
                cap = (
                    _compute_char_cap(b.text, available_width_pt, font_name, font_size)
                    if use_per_bullet else max_chars
                )
                parts.append(f"  [{b.id}] ({len(b.text)}/{cap} chars) {b.text}")

        for entry in section.education_entries:
            parts.append(f"\n{entry.institution}")
            if entry.degree:
                parts.append(f"  {entry.degree}")

        for entry in section.project_entries:
            parts.append(f"\n{entry.name}")
            for b in entry.bullets:
                cap = (
                    _compute_char_cap(b.text, available_width_pt, font_name, font_size)
                    if use_per_bullet else max_chars
                )
                parts.append(f"  [{b.id}] ({len(b.text)}/{cap} chars) {b.text}")

        for cat in section.skill_categories:
            parts.append(f"{cat.category}: {', '.join(cat.skills)}")

    return "\n".join(parts)


class TailoringEngine:
    """Unified LLM tailoring pass."""

    def __init__(self, model: Optional[str] = None):
        self._model = model or os.environ.get(
            "ANTHROPIC_MODEL", "claude-sonnet-4-6"
        )

    def tailor(
        self,
        content: ResumeContent,
        jd_raw_text: str,
        max_chars_per_line: int = 120,
        max_bullets_per_entry: int = 4,
        available_width_pt: float = 0,
        font_name: str = "",
        font_size: float = 0,
    ) -> TailoringEngineResult:
        """Run the unified tailoring pass."""
        result = TailoringEngineResult()

        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            result.llm_error = "ANTHROPIC_API_KEY not set"
            return result

        try:
            import anthropic
        except ImportError:
            result.llm_error = "anthropic package not installed"
            return result

        # Build prompt with per-bullet char caps
        resume_text = _build_resume_text(
            content, max_chars_per_line,
            available_width_pt=available_width_pt,
            font_name=font_name,
            font_size=font_size,
        )

        user_msg = TAILORING_USER_V2.format(
            jd_text=jd_raw_text,
            resume_text=resume_text,
            max_bullets=max_bullets_per_entry,
        )

        # Call Claude
        client = anthropic.Anthropic(api_key=api_key)
        start = time.time()
        try:
            response = client.messages.create(
                model=self._model,
                max_tokens=4096,
                system=TAILORING_SYSTEM_V2,
                messages=[{"role": "user", "content": user_msg}],
            )

            result.duration_ms = int((time.time() - start) * 1000)

            # Extract text
            response_text = None
            for block in response.content:
                if hasattr(block, "text"):
                    response_text = block.text
                    break

            if not response_text:
                result.llm_error = "LLM returned no text content"
                return result

            # Parse response
            logger.info(f"LLM raw response ({len(response_text)} chars): {response_text[:500]}")
            parsed = self._parse_response(response_text, content)
            result.bullet_changes = parsed["bullet_changes"]
            result.skill_reorders = parsed["skill_reorders"]
            result.skill_additions = parsed["skill_additions"]
            result.llm_used = True

            # Log stats
            rewrites = sum(1 for c in result.bullet_changes if c.action == "rewrite")
            keeps = sum(1 for c in result.bullet_changes if c.action == "keep")
            logger.info(f"Tailoring result: {rewrites} rewrites, {keeps} keeps")

        except Exception as e:
            result.duration_ms = int((time.time() - start) * 1000)
            result.llm_error = _sanitize_error(e)
            logger.warning(f"Tailoring engine failed: {_sanitize_error(e)}")

        return result

    def freeform_edit(
        self,
        content: ResumeContent,
        user_message: str,
    ) -> TailoringEngineResult:
        """Apply a freeform user instruction to the resume.

        The user types whatever they want — the LLM sees the full
        resume and the instruction, and returns bullet rewrites.
        No char limits enforced here.
        """
        result = TailoringEngineResult()

        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            result.llm_error = "ANTHROPIC_API_KEY not set"
            return result

        try:
            import anthropic
        except ImportError:
            result.llm_error = "anthropic package not installed"
            return result

        # Build resume text (no char caps — freeform)
        resume_text = _build_resume_text(content, max_chars=999)

        user_msg = FREEFORM_EDIT_USER_TEMPLATE.format(
            user_instruction=user_message,
            resume_text=resume_text,
        )

        client = anthropic.Anthropic(api_key=api_key)
        start = time.time()
        try:
            response = client.messages.create(
                model=self._model,
                max_tokens=4096,
                system=FREEFORM_EDIT_SYSTEM_V1,
                messages=[{"role": "user", "content": user_msg}],
            )

            result.duration_ms = int((time.time() - start) * 1000)

            response_text = None
            for block in response.content:
                if hasattr(block, "text"):
                    response_text = block.text
                    break

            if not response_text:
                result.llm_error = "LLM returned no text content"
                return result

            parsed = self._parse_response(response_text, content)
            result.bullet_changes = parsed["bullet_changes"]
            result.skill_reorders = parsed.get("skill_reorders", {})
            result.skill_additions = parsed.get("skill_additions", {})
            result.llm_used = True

        except Exception as e:
            result.duration_ms = int((time.time() - start) * 1000)
            result.llm_error = _sanitize_error(e)
            logger.warning(f"Freeform edit failed: {_sanitize_error(e)}")

        return result

    def batch_trim_bullets(
        self,
        bullets: list[dict],
        is_retry: bool = False,
    ) -> dict[str, Optional[str]]:
        """Trim multiple overflowing bullets in a single LLM call.

        bullets: list of dicts with keys:
            - bullet_id: str
            - text: str (current bullet text)
            - break_index: int (where the line overflows)
            - max_chars: int (per-bullet char cap)
            - keywords: list[str] (keywords to preserve)
        is_retry: if True, nudge toward rephrasing rather than just trimming.

        Returns: dict mapping bullet_id → trimmed text (or None if failed).
        """
        if not bullets:
            return {}

        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            return {b["bullet_id"]: None for b in bullets}

        try:
            import anthropic
        except ImportError:
            return {b["bullet_id"]: None for b in bullets}

        # Build per-bullet entries
        entries: list[str] = []
        for b in bullets:
            fits = b["text"][:b["break_index"]]
            overflows = b["text"][b["break_index"]:]
            chars_over = len(b["text"]) - b["max_chars"]
            kw_text = ", ".join(b["keywords"]) if b["keywords"] else "the key terms"
            entries.append(
                f"[{b['bullet_id']}] (max {b['max_chars']} chars, "
                f"currently {len(b['text'])}, cut at least {chars_over})\n"
                f"  Keep: {kw_text}\n"
                f"  {fits}|{overflows}"
            )

        bullet_list = "\n\n".join(entries)

        retry_hint = f"\n\n{BATCH_TRIM_RETRY_HINT}" if is_retry else ""

        user_msg = BATCH_TRIM_USER_V2.format(bullet_list=bullet_list) + retry_hint

        try:
            client = anthropic.Anthropic(api_key=api_key)
            response = client.messages.create(
                model=self._model,
                max_tokens=2048,
                system=BATCH_TRIM_SYSTEM_V2,
                messages=[{"role": "user", "content": user_msg}],
            )
            response_text = None
            for block in response.content:
                if hasattr(block, "text"):
                    response_text = block.text.strip()
                    break
            if not response_text:
                return {b["bullet_id"]: None for b in bullets}

            # Parse JSON
            if "```json" in response_text:
                response_text = response_text.split("```json")[1].split("```")[0]
            elif "```" in response_text:
                response_text = response_text.split("```")[1].split("```")[0]
            response_text = response_text.strip()

            try:
                data = json.loads(response_text)
            except json.JSONDecodeError:
                data = _parse_json_robust(response_text)

            # Handle both old format {"trimmed": {...}} and new format {"trimmed_bullets": [...]}
            trimmed_map: dict[str, Optional[str]] = {}

            # New format: {"trimmed_bullets": [{"bullet_id": "...", "trimmed_text": "..."}, ...]}
            if "trimmed_bullets" in data:
                for item in data.get("trimmed_bullets", []):
                    if isinstance(item, dict):
                        bid = item.get("bullet_id", "")
                        text = item.get("trimmed_text", "")
                        if bid and text:
                            trimmed_map[bid] = text

            # Old format: {"trimmed": {"bullet_id": "text", ...}}
            if not trimmed_map:
                trimmed_map = data.get("trimmed", {})

            if not isinstance(trimmed_map, dict):
                return {b["bullet_id"]: None for b in bullets}

            results: dict[str, Optional[str]] = {}
            for b in bullets:
                bid = b["bullet_id"]
                text = trimmed_map.get(bid)
                if not text or not isinstance(text, str):
                    results[bid] = None
                    continue

                # Clean LLM artifacts
                text = text.strip()
                if text.startswith('"') and text.endswith('"'):
                    text = text[1:-1]
                if text.startswith("\u2022"):
                    text = text.lstrip("\u2022 ").strip()
                if "\n" in text:
                    text = text.split("\n")[0].strip()

                # Reject if longer than input
                if len(text) > len(b["text"]):
                    results[bid] = None
                else:
                    results[bid] = text

            return results

        except Exception as e:
            logger.warning(f"Batch trim failed: {_sanitize_error(e)}")
            return {b["bullet_id"]: None for b in bullets}

    def _parse_response(
        self, text: str, content: ResumeContent,
    ) -> dict:
        """Parse the LLM response into structured output."""
        text = text.strip()
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0]
        elif "```" in text:
            text = text.split("```")[1].split("```")[0]
        text = text.strip()

        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = _parse_json_robust(text)

        if not isinstance(data, dict):
            data = {}

        # Build bullet ID → original text lookup
        bullet_texts: dict[str, str] = {}
        for section in content.sections:
            for entry in section.experience_entries:
                for b in entry.bullets:
                    bullet_texts[b.id] = b.text
            for entry in section.project_entries:
                for b in entry.bullets:
                    bullet_texts[b.id] = b.text

        # Parse bullet changes from LLM (only changed bullets)
        bullet_changes: list[BulletChange] = []
        for item in data.get("bullet_changes", []):
            if not isinstance(item, dict):
                continue
            bid = item.get("bullet_id", "")
            if not bid or bid not in bullet_texts:
                continue

            action = item.get("action", "keep")
            original = bullet_texts[bid]

            if action == "rewrite":
                new_text = item.get("new_text", original)
                if not new_text or new_text.strip() == original.strip():
                    action = "keep"
                    new_text = original
            else:
                new_text = original

            bullet_changes.append(BulletChange(
                bullet_id=bid,
                original_text=original,
                tailored_text=new_text,
                action=action,
                target_keywords=item.get("keywords_added", []),
                reason=item.get("reason", ""),
            ))

        # Fill in keeps for bullets the LLM didn't mention
        seen_ids = {c.bullet_id for c in bullet_changes}
        for bid, btext in bullet_texts.items():
            if bid not in seen_ids:
                bullet_changes.append(BulletChange(
                    bullet_id=bid,
                    original_text=btext,
                    tailored_text=btext,
                    action="keep",
                    reason="Already well-written for this JD",
                ))

        return {
            "bullet_changes": bullet_changes,
            "skill_reorders": data.get("skill_reorders", {}),
            "skill_additions": data.get("skill_additions", {}),
        }


def _parse_json_robust(text: str) -> dict:
    """Attempt to recover malformed/truncated JSON."""
    text = text.strip()
    in_string = False
    escape_next = False
    open_braces = 0
    open_brackets = 0

    for ch in text:
        if escape_next:
            escape_next = False
            continue
        if ch == '\\':
            escape_next = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == '{':
            open_braces += 1
        elif ch == '}':
            open_braces -= 1
        elif ch == '[':
            open_brackets += 1
        elif ch == ']':
            open_brackets -= 1

    fixed = text
    if in_string:
        fixed += '"'
    fixed += '}' * max(0, open_braces)
    fixed += ']' * max(0, open_brackets)

    try:
        return json.loads(fixed)
    except json.JSONDecodeError:
        pass

    for trim in range(1, min(500, len(text))):
        candidate = text[:-trim]
        ob = candidate.count('{') - candidate.count('}')
        ab = candidate.count('[') - candidate.count(']')
        attempt = candidate + '}' * max(0, ob) + ']' * max(0, ab)
        try:
            return json.loads(attempt)
        except json.JSONDecodeError:
            continue

    return {}
