"""Bullet Rewriter — Focused per-bullet LLM rewrite.

Receives planning context from the planner pass instead of
re-deriving everything. Each call is small and focused:
just the original bullet, its facts, the planning decision,
and the target keywords.

The model may NOT invent technologies, metrics, responsibilities,
projects, alter dates, employers, or job titles.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Optional

import anthropic

from backend.models.job_description import JobAnalysis
from backend.models.tailoring import BulletChange

logger = logging.getLogger(__name__)


@dataclass
class LLMCallRecord:
    """Observability record for a single LLM call."""
    stage: str  # "rewrite" | "shorten" | "plan"
    model: str = ""
    bullet_id: str = ""
    duration_ms: int = 0
    success: bool = False
    error: Optional[str] = None


REWRITE_SYSTEM = """You are an ATS resume optimizer. Make small, surgical keyword insertions.

RULES:
1. The rewritten bullet must be THE SAME LENGTH (±5%) as the original. NOT longer.
2. Add JD keywords by REPLACING existing words, not by appending.
3. Every word you add must replace a word you remove. Zero net growth.
4. Prefer one-word swaps: "Built" → "Developed", "app" → "application", "API" → "RESTful API"
5. Only add a descriptor if you remove something of equal length: "optimization engine" → "ML optimization system"
6. Do NOT pad with filler: no "software system", "production environments", "integrated solutions"
7. Do NOT add technology claims to non-tech bullets (EMT work is NOT software engineering)
8. Do NOT invent numbers, metrics, or technologies
9. If you cannot add JD keywords without making the bullet longer or less specific, return the original UNCHANGED

GOOD EXAMPLES:
- "Built a Python system" → "Developed a Python software system" (swap verb, add 1 word by dropping an article elsewhere)
- "REST API" → "RESTful API" (1-word change, big ATS win)
- "Gemini API" → "Gemini LLM API" (add "LLM" — 3 chars, high value)
- "RAG app using SentenceTransformers" → "machine learning RAG app using SentenceTransformers" (adds "machine learning" to describe what it IS)

BAD EXAMPLES (do NOT do these):
- Adding "using integrated software systems and mobile applications" to an EMT bullet — fabrication
- "optimization engine" → "mathematical optimization software system using SciPy MILP algorithms" — bloating
- Adding "cross-functional" to a bullet about solo coding work — misleading

Return ONLY a JSON object, no markdown."""


REWRITE_USER_TEMPLATE = """Optimize this resume bullet for ATS matching.

CONTEXT FOR THIS ENTRY:
{entry_context}

OVERALL RESUME STRATEGY:
{strategy_context}

ORIGINAL BULLET ({orig_chars} chars):
{bullet_text}

SOURCE FACTS (the claims you may draw from):
{facts_text}

REWRITE BRIEF:
{planning_context}

TARGET KEYWORDS TO ADD (use EXACT JD spelling):
{keywords}

KEYWORDS ALREADY COVERED BY OTHER BULLETS (don't duplicate these excessively):
{covered_keywords}

NEARBY BULLETS FROM SAME ENTRY (don't duplicate):
{nearby_bullets}

{length_constraint}

Return a JSON object:
{{
  "rewritten_text": "the optimized bullet",
  "source_fact_ids": ["fact IDs used"],
  "target_keywords": ["JD keywords now present in the bullet"],
  "changes_made": "what changed and why"
}}"""


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
    """Rewrite bullets using Claude API with planning context."""

    def __init__(self, model: Optional[str] = None):
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY environment variable not set")
        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = model or os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
        self.call_records: list[LLMCallRecord] = []

    def rewrite_bullet(
        self,
        bullet_id: str,
        bullet_text: str,
        facts: list[dict],
        jd: JobAnalysis,
        target_keywords: list[str] | None = None,
        max_chars: int | None = None,
        planning_reason: str = "",
        rewrite_guidance: str = "",
        nearby_bullets: list[str] | None = None,
        entry_context: str = "",
        strategy_context: str = "",
        covered_keywords: list[str] | None = None,
    ) -> BulletChange:
        """Rewrite a single bullet using planning context."""
        record = LLMCallRecord(stage="rewrite", model=self._model, bullet_id=bullet_id)

        # Build facts text
        facts_text = "\n".join(f"- [{f['id']}] {f['text']}" for f in facts)
        if not facts_text:
            facts_text = f"- [original] {bullet_text}"
            facts = [{"id": "original", "text": bullet_text}]

        # Build keywords
        if target_keywords is None:
            target_keywords = [s.name for s in jd.all_skills_flat()[:15]]
        keywords_text = ", ".join(target_keywords[:15])

        # Build planning context (the rewrite brief from the planner)
        planning_parts = []
        if planning_reason:
            planning_parts.append(planning_reason)
        if rewrite_guidance:
            planning_parts.append(rewrite_guidance)
        planning_context = "\n".join(planning_parts) or "General ATS optimization"

        # Build nearby bullets context
        nearby_text = "None"
        if nearby_bullets:
            nearby_text = "\n".join(f"- {b}" for b in nearby_bullets[:3])

        # Build covered keywords
        covered_text = ", ".join(covered_keywords[:15]) if covered_keywords else "None tracked"

        # Build length constraint from layout measurement
        orig_len = len(bullet_text)
        if max_chars and max_chars != orig_len:
            floor = int(orig_len * 0.80)
            ceiling = max_chars
            length_constraint = f"LENGTH: Original is {orig_len} chars. Line capacity is {ceiling} chars. Your rewrite MUST be {floor}-{ceiling} chars. Do not go shorter than {floor}."
        else:
            floor = int(orig_len * 0.80)
            ceiling = int(orig_len * 1.05)
            length_constraint = f"LENGTH: Original is {orig_len} chars. Your rewrite MUST be {floor}-{ceiling} chars."

        user_msg = REWRITE_USER_TEMPLATE.format(
            bullet_text=bullet_text,
            orig_chars=len(bullet_text),
            facts_text=facts_text,
            planning_context=planning_context,
            entry_context=entry_context or "Not specified",
            strategy_context=strategy_context or "Not specified",
            keywords=keywords_text,
            covered_keywords=covered_text,
            nearby_bullets=nearby_text,
            length_constraint=length_constraint,
        )

        start = time.time()
        try:
            response = self._client.messages.create(
                model=self._model,
                max_tokens=500,
                system=REWRITE_SYSTEM,
                messages=[{"role": "user", "content": user_msg}],
            )

            record.duration_ms = int((time.time() - start) * 1000)

            # Extract text (handle ThinkingBlock)
            response_text = None
            for block in response.content:
                if hasattr(block, "text"):
                    response_text = block.text
                    break

            if not response_text:
                record.error = "No text content in response"
                self.call_records.append(record)
                return BulletChange(
                    bullet_id=bullet_id,
                    original_text=bullet_text,
                    tailored_text=bullet_text,
                    action="keep",
                    reason="LLM returned no text content",
                )

            # Parse response
            result = self._parse_json(response_text)
            record.success = True
            self.call_records.append(record)

            return BulletChange(
                bullet_id=bullet_id,
                original_text=bullet_text,
                tailored_text=result.get("rewritten_text", bullet_text),
                action="rewrite",
                source_fact_ids=result.get("source_fact_ids", []),
                target_keywords=result.get("target_keywords", []),
                reason=result.get("changes_made", ""),
            )

        except Exception as e:
            record.duration_ms = int((time.time() - start) * 1000)
            record.error = str(e)[:200]
            self.call_records.append(record)
            logger.warning(f"Rewrite failed for {bullet_id}: {e}")
            return BulletChange(
                bullet_id=bullet_id,
                original_text=bullet_text,
                tailored_text=bullet_text,
                action="keep",
                reason=f"Rewrite failed: {str(e)[:100]}",
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
        record = LLMCallRecord(stage="shorten", model=self._model, bullet_id=bullet_id)

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

            record.duration_ms = int((time.time() - start) * 1000)

            response_text = None
            for block in response.content:
                if hasattr(block, "text"):
                    response_text = block.text
                    break

            if not response_text:
                record.error = "No text content"
                self.call_records.append(record)
                return BulletChange(
                    bullet_id=bullet_id,
                    original_text=bullet_text,
                    tailored_text=bullet_text,
                    action="keep",
                    reason="LLM returned no text content",
                )

            result = self._parse_json(response_text)
            record.success = True
            self.call_records.append(record)

            return BulletChange(
                bullet_id=bullet_id,
                original_text=bullet_text,
                tailored_text=result.get("shortened_text", bullet_text),
                action="rewrite",
                source_fact_ids=result.get("source_fact_ids", []),
                reason=f"Shortened to ~{target_chars} chars",
            )

        except Exception as e:
            record.duration_ms = int((time.time() - start) * 1000)
            record.error = str(e)[:200]
            self.call_records.append(record)
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
