"""Bullet Rewriter.

Uses Claude API to rewrite resume bullets emphasizing JD-relevant
keywords while preserving factual accuracy.

Each rewritten bullet must return source_fact_ids and target_keywords.
The model may NOT invent technologies, metrics, responsibilities,
projects, alter dates, employers, or job titles.
"""

from __future__ import annotations

import json
import os
from typing import Optional

import anthropic

from backend.models.job_description import JobAnalysis
from backend.models.tailoring import BulletChange, ResumeBank


REWRITE_SYSTEM = """You are a professional resume writer. You rewrite resume bullets to better match a target job description.

STRICT RULES:
1. You may reword, restructure, and emphasize different aspects of a bullet
2. You may incorporate keywords from the job description IF the original bullet's facts support them
3. You MUST preserve ALL factual claims — do not invent technologies, metrics, responsibilities, or projects
4. You MUST NOT add any technology, framework, or tool that is not mentioned in the source facts
5. You MUST NOT change numbers, dates, or quantitative claims unless a source fact supports a different number
6. You MUST NOT fabricate metrics (e.g., adding "reduced latency by 40%" when no performance metric exists)
7. Keep bullets concise — aim for 1-2 lines when printed
8. Start each bullet with a strong action verb
9. Naturally integrate relevant keywords without keyword-stuffing

Return your response as a JSON object."""

REWRITE_USER_TEMPLATE = """Rewrite the following resume bullet to better match the target job description.

ORIGINAL BULLET:
{bullet_text}

SOURCE FACTS (the ONLY facts you may use):
{facts_text}

TARGET JOB KEYWORDS TO EMPHASIZE (only if supported by facts):
{keywords}

TARGET JOB RESPONSIBILITIES:
{responsibilities}

Return a JSON object:
{{
  "rewritten_text": "the rewritten bullet",
  "source_fact_ids": ["list of fact IDs used"],
  "target_keywords": ["keywords from JD that appear in the rewrite"],
  "changes_made": "brief explanation of what changed and why"
}}"""

SHORTEN_SYSTEM = """You are a professional resume writer. You shorten resume bullets while preserving their key facts and impact.

STRICT RULES:
1. Preserve the most important facts and metrics
2. Do not invent or add any new information
3. Keep the same action verb style
4. Target the specified line count

Return your response as a JSON object."""

SHORTEN_USER_TEMPLATE = """Shorten this resume bullet to fit in approximately {target_lines} line(s) (roughly {target_chars} characters).

CURRENT BULLET:
{bullet_text}

SOURCE FACTS:
{facts_text}

Return a JSON object:
{{
  "shortened_text": "the shortened bullet",
  "source_fact_ids": ["fact IDs preserved"],
  "facts_removed": ["any fact IDs that were dropped to save space"]
}}"""


class BulletRewriter:
    """Rewrite bullets using Claude API."""

    def __init__(self, model: str = "claude-sonnet-4-20250514"):
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY environment variable not set")
        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = model

    def rewrite_bullet(
        self,
        bullet_id: str,
        bullet_text: str,
        facts: list[dict],  # [{id, text}]
        jd: JobAnalysis,
        target_keywords: list[str] | None = None,
    ) -> BulletChange:
        """Rewrite a single bullet to better match the JD."""
        # Build facts text
        facts_text = "\n".join(f"- [{f['id']}] {f['text']}" for f in facts)
        if not facts_text:
            facts_text = f"- [original] {bullet_text}"
            facts = [{"id": "original", "text": bullet_text}]

        # Build keywords
        if target_keywords is None:
            target_keywords = [s.name for s in jd.all_skills_flat()[:15]]
        keywords_text = ", ".join(target_keywords[:15])

        # Build responsibilities
        resp_text = "\n".join(
            f"- {r.text}" for r in jd.responsibilities[:5]
        ) or "Not specified"

        user_msg = REWRITE_USER_TEMPLATE.format(
            bullet_text=bullet_text,
            facts_text=facts_text,
            keywords=keywords_text,
            responsibilities=resp_text,
        )

        response = self._client.messages.create(
            model=self._model,
            max_tokens=500,
            system=REWRITE_SYSTEM,
            messages=[{"role": "user", "content": user_msg}],
        )

        # Parse response
        response_text = response.content[0].text
        try:
            # Handle markdown code blocks
            if "```json" in response_text:
                response_text = response_text.split("```json")[1].split("```")[0]
            elif "```" in response_text:
                response_text = response_text.split("```")[1].split("```")[0]
            result = json.loads(response_text)
        except (json.JSONDecodeError, IndexError):
            # Fallback: keep original
            return BulletChange(
                bullet_id=bullet_id,
                original_text=bullet_text,
                tailored_text=bullet_text,
                action="keep",
                reason="Failed to parse rewrite response",
            )

        return BulletChange(
            bullet_id=bullet_id,
            original_text=bullet_text,
            tailored_text=result.get("rewritten_text", bullet_text),
            action="rewrite",
            source_fact_ids=result.get("source_fact_ids", []),
            target_keywords=result.get("target_keywords", []),
            reason=result.get("changes_made", ""),
        )

    def shorten_bullet(
        self,
        bullet_id: str,
        bullet_text: str,
        facts: list[dict],
        target_lines: int = 2,
        chars_per_line: int = 95,
    ) -> BulletChange:
        """Shorten a bullet to fit a target line count."""
        facts_text = "\n".join(f"- [{f['id']}] {f['text']}" for f in facts)
        if not facts_text:
            facts_text = f"- [original] {bullet_text}"

        target_chars = target_lines * chars_per_line

        user_msg = SHORTEN_USER_TEMPLATE.format(
            bullet_text=bullet_text,
            facts_text=facts_text,
            target_lines=target_lines,
            target_chars=target_chars,
        )

        response = self._client.messages.create(
            model=self._model,
            max_tokens=400,
            system=SHORTEN_SYSTEM,
            messages=[{"role": "user", "content": user_msg}],
        )

        response_text = response.content[0].text
        try:
            if "```json" in response_text:
                response_text = response_text.split("```json")[1].split("```")[0]
            elif "```" in response_text:
                response_text = response_text.split("```")[1].split("```")[0]
            result = json.loads(response_text)
        except (json.JSONDecodeError, IndexError):
            return BulletChange(
                bullet_id=bullet_id,
                original_text=bullet_text,
                tailored_text=bullet_text,
                action="keep",
                reason="Failed to parse shorten response",
            )

        return BulletChange(
            bullet_id=bullet_id,
            original_text=bullet_text,
            tailored_text=result.get("shortened_text", bullet_text),
            action="rewrite",
            source_fact_ids=result.get("source_fact_ids", []),
            reason=f"Shortened to ~{target_lines} lines",
        )
