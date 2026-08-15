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


REWRITE_SYSTEM = """You are an expert resume optimizer for Applicant Tracking Systems (ATS).

ATS scans for exact keyword matches against the job description. Maximize match rate while keeping the bullet truthful and professional.

APPROACH (smallest changes first):
1. KEYWORD SWAPS: Replace synonyms with exact JD terms ("built" → "developed", "app" → "application")
2. KEYWORD INSERTION: Add JD terms the work already implies ("API" → "RESTful API", add "full-stack" if the work was full-stack)
3. VERB UPGRADES: Use action verbs from the JD responsibilities
4. CLARIFYING TERMS: Add descriptors that common sense supports from the context

USE COMMON SENSE — think in tiers:

FREELY ADD (no evidence needed — these describe ANY software work):
- "software" (before engineering, development, system, etc.)
- "software engineer/development" framing
- Action verbs: developed, implemented, designed, built, engineered, tested, debugged, deployed
- Generic descriptors: scalable, production, end-to-end, cross-functional, technical
- "application" instead of "app", "utilized" instead of "used"

ADD IF THE WORK IMPLIES IT (reasonable inference from context):
- "full-stack" if the bullet mentions both frontend and backend work
- "RESTful" if the bullet mentions API
- "data pipeline" if the bullet describes processing/transforming data
- "CI/CD" if deployment is mentioned
- "Agile" if the role context suggests it (most tech internships are Agile)
- "collaboration" or "cross-functional" if working with other teams is described
- "machine learning" if the bullet mentions embeddings, models, NLP, or training

USE DISCRETION — only add if genuinely evidenced:
- Specific frameworks (Kubernetes, Docker, Terraform) — only if the work clearly involved them
- Specific databases — only if data storage/querying is part of the described work
- Specific cloud providers (AWS, GCP) — only if deployment or cloud services are mentioned
- Programming languages — only if the language was plausibly used in the described work

NEVER ADD:
- Technologies the candidate clearly never used in this role
- Metrics, numbers, or quantified results that don't exist
- Responsibilities or projects that weren't part of this work
- Company names, titles, or dates that aren't accurate

LENGTH RULES:
- The result MUST be approximately the same length as the original (within 10%)
- Do NOT drop words just to shorten — every removal needs justification
- NEVER remove metrics, numbers, or quantified results
- If you can't meaningfully improve it, return the original text UNCHANGED

Return ONLY a JSON object, no markdown."""


REWRITE_USER_TEMPLATE = """Optimize this resume bullet for ATS matching.

ORIGINAL BULLET ({orig_chars} chars):
{bullet_text}

SOURCE FACTS (the ONLY claims you may use):
{facts_text}

PLANNING CONTEXT:
{planning_context}

TARGET KEYWORDS (use EXACT JD spelling when adding):
{keywords}

NEARBY BULLETS (for context — do NOT duplicate their content):
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

        # Build planning context
        planning_parts = []
        if planning_reason:
            planning_parts.append(f"Reason for rewrite: {planning_reason}")
        if rewrite_guidance:
            planning_parts.append(f"Guidance: {rewrite_guidance}")
        planning_context = "\n".join(planning_parts) or "General ATS optimization"

        # Build nearby bullets context
        nearby_text = "None"
        if nearby_bullets:
            nearby_text = "\n".join(f"- {b}" for b in nearby_bullets[:3])

        # Build length constraint
        length_constraint = ""
        if max_chars:
            length_constraint = f"LENGTH CONSTRAINT: The rewritten bullet MUST be {max_chars} characters or fewer. Current is {len(bullet_text)} chars."

        user_msg = REWRITE_USER_TEMPLATE.format(
            bullet_text=bullet_text,
            orig_chars=len(bullet_text),
            facts_text=facts_text,
            planning_context=planning_context,
            keywords=keywords_text,
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
