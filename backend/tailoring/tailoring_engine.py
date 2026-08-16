"""Unified Tailoring Engine.

Single LLM pass that sees the full resume and full JD.
Produces all bullet rewrites, skill reorders, and skill additions.

The prompt is intentionally simple — modeled on what works
when pasting a resume + JD into ChatGPT with a one-line instruction.
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

TAILORING_SYSTEM = """You are a resume ATS optimization expert. You will receive a full resume, a job description, and source facts. Your job is to make small, surgical changes to improve ATS keyword matching.

RULES:
1. Keep each bullet approximately the same length. The current bullets fill the page — do NOT make them shorter. If anything, use available space to add a keyword.
2. Each bullet has a target character count shown as (~N chars). Your rewrite should be close to that length.
3. Change 1-5 words per bullet. Think word swaps and small insertions, not full rewrites.
4. PRESERVE all numbers, metrics, percentages, and dollar amounts exactly.
5. Do NOT invent technologies, metrics, or claims not supported by the source facts.
6. Spread different JD keywords across different bullets — don't cram the same keyword everywhere.

GOOD CHANGES:
- Add a JD verb: "Built" → "Built and tested", "Designed" → "Designed and deployed"
- Add a JD descriptor: "API pipelines" → "reliable API pipelines", "REST API" → "scalable REST API"
- Swap for JD terminology: "collaborated with" → "cross-functional collaboration with", "app" → "application", "NoSQL models" → "database models"
- Swap near-synonyms: "natural language processing" → "LLM tooling", "60+ professionals" → "60+ users"

BAD CHANGES:
- Making a bullet shorter without adding anything
- Removing words just to rephrase
- Claiming to add a keyword that isn't actually in your new_text
- Adding a technology the candidate never used

If you cannot meaningfully improve a bullet, mark it "keep". That is perfectly fine.

For skills: reorder to put JD-relevant skills first. Add technical concepts (LLMs, Embeddings, RAG, CI/CD) the candidate demonstrably used — never add soft skills as listed skills.

Return ONLY valid JSON, no markdown."""


TAILORING_USER_TEMPLATE = """Improve this resume to match the job description for ATS. Tell me what lines you would change and what the new versions would be. Keep each bullet around the same length so it goes to the end of the line.

JOB DESCRIPTION:
Title: {job_title}
Required: {required_skills}
Preferred: {preferred_skills}
Responsibilities:
{responsibilities}

RESUME:
{resume_text}

SOURCE FACTS (claims you may draw from):
{facts_text}

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
    "Category Name": ["skill1 in new order", "skill2", "skill3"]
  }},
  "skill_additions": {{
    "Category Name": ["NewSkill1", "NewSkill2"]
  }}
}}

Only include bullets you are changing — omit bullets you would keep as-is.
Only include skill categories you are reordering or adding to.
For removals: only remove if an entry has more than {max_bullets} bullets AND the bullet is irrelevant."""


def _build_resume_text(
    content: ResumeContent,
    target_chars: dict[str, int],
) -> str:
    """Build resume text with per-bullet target character counts."""
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
                tc = target_chars.get(b.id, len(b.text))
                parts.append(f"  [{b.id}] (~{tc} chars) {b.text}")

        for entry in section.education_entries:
            parts.append(f"\n{entry.institution}")
            if entry.degree:
                parts.append(f"  {entry.degree}")

        for entry in section.project_entries:
            parts.append(f"\n{entry.name}")
            for b in entry.bullets:
                tc = target_chars.get(b.id, len(b.text))
                parts.append(f"  [{b.id}] (~{tc} chars) {b.text}")

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
            "ANTHROPIC_MODEL", "claude-sonnet-4-6"
        )

    def tailor(
        self,
        content: ResumeContent,
        jd: JobAnalysis,
        bank: ResumeBank,
        target_chars: dict[str, int],
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
        resume_text = _build_resume_text(content, target_chars)
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

        user_msg = TAILORING_USER_TEMPLATE.format(
            job_title=jd.job_title or "Not specified",
            required_skills=required,
            preferred_skills=preferred,
            responsibilities=responsibilities,
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
