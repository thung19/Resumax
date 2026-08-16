"""Unified Tailoring Engine.

Single LLM pass that sees the full resume, full JD analysis, matcher
coverage report, source facts bank, and per-bullet line capacity.
Returns all bullet changes, skill reorders, and skill additions in
one structured response.

Replaces the old planner → rewriter × N pipeline.
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

logger = logging.getLogger(__name__)


@dataclass
class TailoringEngineResult:
    """Output of the unified tailoring pass."""
    bullet_changes: list[BulletChange] = field(default_factory=list)
    skill_reorders: dict[str, list[str]] = field(default_factory=dict)
    skill_additions: dict[str, list[str]] = field(default_factory=dict)
    llm_used: bool = False
    llm_error: Optional[str] = None
    duration_ms: int = 0


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

TAILORING_SYSTEM = """You are a senior resume strategist and ATS optimization expert. You see the COMPLETE resume and COMPLETE job description. Your job is to make surgical, high-impact changes that improve ATS keyword matching while keeping the resume honest and natural-sounding.

RULES:
1. PRESERVE all quantified metrics (numbers, percentages, dollar amounts) — these are your strongest content
2. PRESERVE specific technical details — only replace if swapping for an equivalent JD term
3. Do NOT invent technologies, metrics, responsibilities, projects, or claims not supported by source facts
4. Most rewrites should change 2-5 words. Think word swaps, not full rewrites
5. Each bullet has a CHARACTER BUDGET (floor and ceiling). Stay within it. Aim close to the ceiling to maximize keyword space
6. Verb and framing swaps are high-value: "collaborated" → "communicated", "built" → "developed", "made" → "engineered"
7. Think about keyword DISTRIBUTION — spread different JD keywords across different bullets instead of cramming the same ones everywhere
8. For skills: only suggest adding skills the candidate demonstrably has based on their experience bullets. Place additions in the correct existing category. Never add soft skills or activity descriptions (debugging, testing, collaboration) as listed skills
9. A "keep" decision means the bullet is already strong for this JD — do not default to keep. Most bullets can benefit from at least a small keyword swap

Return ONLY valid JSON, no markdown code blocks."""


TAILORING_USER_TEMPLATE = """Tailor this resume for the following job description.

JOB DESCRIPTION ANALYSIS:
Title: {job_title}
Required Skills: {required_skills}
Preferred Skills: {preferred_skills}
Key Responsibilities:
{responsibilities}
ATS Keywords: {ats_keywords}

KEYWORD COVERAGE (from deterministic analysis):
Already in resume: {covered_keywords}
Missing from resume: {missing_keywords}

FULL RESUME:
{resume_text}

SOURCE FACTS BANK (claims you may draw from — do NOT go beyond these):
{facts_text}

For each bullet, I've included its character budget: [floor-ceiling]. Your rewrite MUST fall within this range. Aim close to the ceiling.

Return a JSON object:
{{
  "bullet_changes": [
    {{
      "bullet_id": "the bullet ID",
      "action": "keep" | "rewrite" | "remove",
      "new_text": "rewritten text (only if action is rewrite)",
      "reason": "brief explanation of what changed and why",
      "keywords_added": ["JD keywords now present that weren't before"]
    }}
  ],
  "skill_reorders": {{
    "Category Name": ["skill1", "skill2", "skill3"]
  }},
  "skill_additions": {{
    "Category Name": ["NewSkill1", "NewSkill2"]
  }}
}}

DECISION GUIDANCE:
- "rewrite": The bullet can be improved by swapping words for JD terminology. This is the default for most bullets.
- "keep": The bullet already has strong keyword coverage AND good metrics/impact. Should be rare.
- "remove": ONLY if the entry has more bullets than {max_bullets} AND this bullet is genuinely irrelevant to the JD. Very rare.

For skill_reorders: put JD-relevant skills first in each category. Only include categories that need reordering.
For skill_additions: only add named technologies/methodologies the candidate demonstrably used. Place in the correct existing category. Never add soft skills or generic terms."""


def _build_resume_text(
    content: ResumeContent,
    char_budgets: dict[str, tuple[int, int]],
) -> str:
    """Build structured resume text with per-bullet character budgets."""
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
                budget = char_budgets.get(b.id, (80, 120))
                parts.append(f"  [{b.id}] (chars: {budget[0]}-{budget[1]}) {b.text}")

        for entry in section.education_entries:
            parts.append(f"\n{entry.institution}")
            if entry.degree:
                parts.append(f"  {entry.degree}")

        for entry in section.project_entries:
            parts.append(f"\n{entry.name}")
            for b in entry.bullets:
                budget = char_budgets.get(b.id, (80, 120))
                parts.append(f"  [{b.id}] (chars: {budget[0]}-{budget[1]}) {b.text}")

        for cat in section.skill_categories:
            parts.append(f"{cat.category}: {', '.join(cat.skills)}")

    return "\n".join(parts)


def _build_facts_text(bank: ResumeBank) -> str:
    """Build text listing of all source facts."""
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


class TailoringEngine:
    """Unified LLM tailoring pass."""

    def __init__(self, model: Optional[str] = None):
        self._model = model or os.environ.get(
            "ANTHROPIC_MODEL", "claude-sonnet-4-5-20241022"
        )

    def tailor(
        self,
        content: ResumeContent,
        jd: JobAnalysis,
        bank: ResumeBank,
        covered_keywords: list[str],
        missing_keywords: list[str],
        char_budgets: dict[str, tuple[int, int]],
        max_bullets_per_entry: int = 4,
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

        # Build prompt
        resume_text = _build_resume_text(content, char_budgets)
        facts_text = _build_facts_text(bank)

        required = ", ".join(
            s.name for s in jd.required_skills[:15]
        ) or "Not specified"
        preferred = ", ".join(
            s.name for s in jd.preferred_skills[:15]
        ) or "Not specified"
        responsibilities = "\n".join(
            f"- {r.text}" for r in jd.responsibilities[:10]
        ) or "Not specified"
        ats_kws = ", ".join(jd.ats_phrases[:20]) or "Not specified"

        user_msg = TAILORING_USER_TEMPLATE.format(
            job_title=jd.job_title or "Not specified",
            required_skills=required,
            preferred_skills=preferred,
            responsibilities=responsibilities,
            ats_keywords=ats_kws,
            covered_keywords=", ".join(covered_keywords[:20]) or "None",
            missing_keywords=", ".join(missing_keywords[:20]) or "None",
            resume_text=resume_text,
            facts_text=facts_text,
            max_bullets=max_bullets_per_entry,
        )

        # Call Claude
        client = anthropic.Anthropic(api_key=api_key)
        start = time.time()
        try:
            response = client.messages.create(
                model=self._model,
                max_tokens=4096,
                system=TAILORING_SYSTEM,
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
            parsed = self._parse_response(response_text, content)
            result.bullet_changes = parsed["bullet_changes"]
            result.skill_reorders = parsed["skill_reorders"]
            result.skill_additions = parsed["skill_additions"]
            result.llm_used = True

        except Exception as e:
            result.duration_ms = int((time.time() - start) * 1000)
            result.llm_error = str(e)[:200]
            logger.warning(f"Tailoring engine failed: {e}")

        return result

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

        # Parse bullet changes
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

        # Ensure all bullets have a change entry (default to keep)
        seen_ids = {c.bullet_id for c in bullet_changes}
        for bid, text in bullet_texts.items():
            if bid not in seen_ids:
                bullet_changes.append(BulletChange(
                    bullet_id=bid,
                    original_text=text,
                    tailored_text=text,
                    action="keep",
                    reason="Not addressed by LLM",
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

    # Trim from end until valid
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
