"""Resume Planner — Full-resume LLM evaluation pass.

Sends Claude the complete structured resume + JD analysis in ONE call.
Returns per-bullet decisions (keep/rewrite/remove) with strategic value
and job relevance scores, so the rewriter has global context without
needing the full resume again.

Falls back to deterministic-only scoring if LLM is unavailable.
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
from backend.models.tailoring import ResumeBank

logger = logging.getLogger(__name__)


@dataclass
class BulletPlan:
    """LLM planning decision for a single bullet."""
    bullet_id: str
    decision: str = "keep"  # keep | rewrite | remove
    strategic_value: float = 0.5  # 0.0–1.0
    job_relevance: float = 0.5  # 0.0–1.0
    reason: str = ""
    supported_target_keywords: list[str] = field(default_factory=list)
    source_fact_ids: list[str] = field(default_factory=list)
    rewrite_guidance: str = ""  # specific instruction for the rewriter


@dataclass
class PlanResult:
    """Full planning pass output."""
    bullet_plans: dict[str, BulletPlan] = field(default_factory=dict)
    llm_used: bool = False
    llm_error: Optional[str] = None
    duration_ms: int = 0


PLANNING_PROMPT = """You are evaluating a resume against a job description to decide which bullets to keep, rewrite, or remove.

JOB DESCRIPTION ANALYSIS:
Title: {job_title}
Required Skills: {required_skills}
Preferred Skills: {preferred_skills}
Key Responsibilities: {responsibilities}
ATS Keywords: {ats_keywords}

FULL RESUME:
{resume_text}

SOURCE FACTS BANK:
{facts_text}

For EVERY bullet listed above, return a JSON array. Each entry must have:
{{
  "bullet_id": "the bullet ID",
  "decision": "keep" | "rewrite" | "remove",
  "strategic_value": 0.0 to 1.0,
  "job_relevance": 0.0 to 1.0,
  "reason": "why this decision",
  "supported_target_keywords": ["JD keywords this bullet can honestly include"],
  "rewrite_guidance": "specific instruction for how to improve this bullet, if rewrite"
}}

DECISION CRITERIA:
- "keep": Bullet already contains strong JD-relevant keywords OR has high strategic value (metrics, leadership, scope, impact). Do NOT rewrite bullets that are already well-written and relevant.
- "rewrite": Bullet has potential to incorporate JD terminology without fabricating facts. Only suggest if you can identify SPECIFIC keyword swaps or insertions that would improve ATS matching.
- "remove": Bullet is genuinely irrelevant AND there are other bullets in the same entry that are more valuable. Removing should be rare — only when an entry has too many bullets and this one adds the least value.

STRATEGIC VALUE (independent of keyword match):
- 1.0: Strong quantified impact ($250M, 60+ users, 10k+ data points)
- 0.8: Clear ownership and scope (designed, architected, led)
- 0.6: Technical depth (specific algorithms, system design)
- 0.4: Standard contribution (collaborated, participated)
- 0.2: Generic or context-light claim

CRITICAL RULES:
- A bullet with strong metrics or impact should have strategic_value >= 0.7 even if job_relevance is low
- Do NOT recommend removing a bullet just because it lacks exact keyword matches
- supported_target_keywords must ONLY include keywords that the bullet's source facts actually support
- If a bullet uses "React" and the JD says "React", that IS a match — do not suggest rewriting what already works
- For rewrite_guidance, be SPECIFIC: "swap 'built' for 'developed', add 'RESTful' before 'API'" not "make it more relevant"
- If no honest improvement is possible, decision should be "keep"

Return ONLY a JSON array, no markdown or explanation outside the array."""


def _build_resume_text(content: ResumeContent) -> str:
    """Build a structured text representation of the resume for the planner."""
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
                parts.append(f"  [{b.id}] {b.text}")

        for entry in section.education_entries:
            parts.append(f"\n{entry.institution}")
            if entry.degree:
                parts.append(f"  {entry.degree}")

        for entry in section.project_entries:
            parts.append(f"\n{entry.name}")
            for b in entry.bullets:
                parts.append(f"  [{b.id}] {b.text}")

        for cat in section.skill_categories:
            parts.append(f"{cat.category}: {', '.join(cat.skills)}")

    return "\n".join(parts)


def _build_facts_text(bank: ResumeBank) -> str:
    """Build a text listing of all source facts."""
    parts: list[str] = []
    for exp in bank.experiences:
        parts.append(f"\n{exp.company} — {exp.role}:")
        for f in exp.facts:
            parts.append(f"  [{f.id}] {f.text}")
    for proj in bank.projects:
        parts.append(f"\n{proj.name}:")
        for f in proj.facts:
            parts.append(f"  [{f.id}] {f.text}")
    return "\n".join(parts) if parts else "No additional facts available."


class ResumePlanner:
    """Full-resume LLM planning pass."""

    def __init__(self, model: Optional[str] = None):
        self._model = model or os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")

    def plan(
        self,
        content: ResumeContent,
        jd: JobAnalysis,
        bank: ResumeBank,
    ) -> PlanResult:
        """Send full resume + JD to Claude for global evaluation."""
        result = PlanResult()

        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            result.llm_error = "ANTHROPIC_API_KEY not set"
            return result

        try:
            import anthropic
        except ImportError:
            result.llm_error = "anthropic package not installed"
            return result

        # Build prompt
        resume_text = _build_resume_text(content)
        facts_text = _build_facts_text(bank)

        required = ", ".join(s.name for s in jd.required_skills[:15]) or "Not specified"
        preferred = ", ".join(s.name for s in jd.preferred_skills[:15]) or "Not specified"
        responsibilities = "\n".join(
            f"- {r.text}" for r in jd.responsibilities[:10]
        ) or "Not specified"
        ats_kws = ", ".join(jd.ats_phrases[:20]) or "Not specified"

        prompt = PLANNING_PROMPT.format(
            job_title=jd.job_title or "Not specified",
            required_skills=required,
            preferred_skills=preferred,
            responsibilities=responsibilities,
            ats_keywords=ats_kws,
            resume_text=resume_text,
            facts_text=facts_text,
        )

        # Call Claude
        client = anthropic.Anthropic(api_key=api_key)
        start = time.time()
        try:
            response = client.messages.create(
                model=self._model,
                max_tokens=4096,
                messages=[{"role": "user", "content": prompt}],
            )

            result.duration_ms = int((time.time() - start) * 1000)

            # Extract text from response (handle ThinkingBlock)
            response_text = None
            for block in response.content:
                if hasattr(block, "text"):
                    response_text = block.text
                    break

            if not response_text:
                result.llm_error = "LLM returned no text content"
                return result

            # Parse JSON
            plans = self._parse_response(response_text)
            for plan in plans:
                result.bullet_plans[plan.bullet_id] = plan

            result.llm_used = True

        except Exception as e:
            result.duration_ms = int((time.time() - start) * 1000)
            result.llm_error = str(e)[:200]
            logger.warning(f"Planning pass failed: {e}")

        return result

    def _parse_response(self, text: str) -> list[BulletPlan]:
        """Parse the LLM planning response with fallback for malformed JSON."""
        text = text.strip()

        # Strip markdown code blocks
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0]
        elif "```" in text:
            text = text.split("```")[1].split("```")[0]

        text = text.strip()

        # Try direct parse
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            # Try fixing truncated JSON
            data = self._parse_json_robust(text)

        if not isinstance(data, list):
            data = [data] if isinstance(data, dict) else []

        plans: list[BulletPlan] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            bid = item.get("bullet_id", "")
            if not bid:
                continue
            plans.append(BulletPlan(
                bullet_id=bid,
                decision=item.get("decision", "keep"),
                strategic_value=min(1.0, max(0.0, float(item.get("strategic_value", 0.5)))),
                job_relevance=min(1.0, max(0.0, float(item.get("job_relevance", 0.5)))),
                reason=item.get("reason", ""),
                supported_target_keywords=item.get("supported_target_keywords", []),
                source_fact_ids=item.get("source_fact_ids", []),
                rewrite_guidance=item.get("rewrite_guidance", ""),
            ))
        return plans

    @staticmethod
    def _parse_json_robust(text: str) -> list:
        """Attempt to recover truncated JSON arrays."""
        # Try closing open structures
        fixed = text
        in_string = False
        escape_next = False
        open_braces = 0
        open_brackets = 0

        for ch in fixed:
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

        if in_string:
            fixed += '"'
        fixed += '}' * max(0, open_braces)
        fixed += ']' * max(0, open_brackets)

        try:
            return json.loads(fixed)
        except json.JSONDecodeError:
            pass

        # Try trimming from the end
        for trim in range(1, min(500, len(text))):
            candidate = text[:-trim]
            ob = candidate.count('{') - candidate.count('}')
            ab = candidate.count('[') - candidate.count(']')
            attempt = candidate + '}' * max(0, ob) + ']' * max(0, ab)
            try:
                return json.loads(attempt)
            except json.JSONDecodeError:
                continue

        return []
