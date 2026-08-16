"""Bullet Rewriter — Shortening only.

Used as a follow-up when a rewrite from the tailoring engine
overflows a single rendered line. The main rewriting is handled
by the unified TailoringEngine.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Optional

import anthropic

from backend.models.tailoring import BulletChange

logger = logging.getLogger(__name__)


SHORTEN_SYSTEM = """You are a professional resume writer. You shorten resume bullets while preserving their key facts, metrics, and impact.

RULES:
1. Preserve the most important facts and metrics — NEVER drop quantified results
2. Do not invent or add any new information
3. Keep the same action verb style
4. Target the specified character count

Return ONLY a JSON object, no markdown."""


SHORTEN_USER_TEMPLATE = """Shorten this resume bullet to approximately {target_chars} characters while preserving all key facts and metrics.

CURRENT BULLET ({current_chars} chars):
{bullet_text}

SOURCE FACTS:
{facts_text}

Return a JSON object:
{{
  "shortened_text": "the shortened bullet",
  "source_fact_ids": ["fact IDs preserved"],
  "facts_removed": ["any fact IDs dropped to save space"]
}}"""


class BulletRewriter:
    """Shorten bullets that overflow a single rendered line."""

    def __init__(self, model: Optional[str] = None):
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY environment variable not set")
        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = model or os.environ.get(
            "ANTHROPIC_MODEL", "claude-haiku-4-5-20251001"
        )

    def shorten_bullet(
        self,
        bullet_id: str,
        bullet_text: str,
        facts: list[dict],
        target_lines: int = 1,
        chars_per_line: int = 115,
    ) -> BulletChange:
        """Shorten a bullet to fit a target character count."""
        facts_text = "\n".join(f"- [{f['id']}] {f['text']}" for f in facts)
        if not facts_text:
            facts_text = f"- [original] {bullet_text}"

        target_chars = target_lines * chars_per_line

        user_msg = SHORTEN_USER_TEMPLATE.format(
            bullet_text=bullet_text,
            current_chars=len(bullet_text),
            facts_text=facts_text,
            target_chars=target_chars,
        )

        start = time.time()
        try:
            response = self._client.messages.create(
                model=self._model,
                max_tokens=400,
                system=SHORTEN_SYSTEM,
                messages=[{"role": "user", "content": user_msg}],
            )

            response_text = None
            for block in response.content:
                if hasattr(block, "text"):
                    response_text = block.text
                    break

            if not response_text:
                return BulletChange(
                    bullet_id=bullet_id,
                    original_text=bullet_text,
                    tailored_text=bullet_text,
                    action="keep",
                    reason="LLM returned no text content",
                )

            result = self._parse_json(response_text)

            return BulletChange(
                bullet_id=bullet_id,
                original_text=bullet_text,
                tailored_text=result.get("shortened_text", bullet_text),
                action="rewrite",
                source_fact_ids=result.get("source_fact_ids", []),
                reason=f"Shortened to ~{target_chars} chars",
            )

        except Exception as e:
            logger.warning(f"Shorten failed for {bullet_id}: {e}")
            return BulletChange(
                bullet_id=bullet_id,
                original_text=bullet_text,
                tailored_text=bullet_text,
                action="keep",
                reason=f"Shorten failed: {str(e)[:100]}",
            )

    def _parse_json(self, text: str) -> dict:
        """Parse JSON response with code block handling."""
        text = text.strip()
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0]
        elif "```" in text:
            text = text.split("```")[1].split("```")[0]
        return json.loads(text.strip())
