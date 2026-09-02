"""Resume Bank Service.

Manages loading, saving, and auto-generating resume banks.
Can create an initial bank from a parsed ResumeIR.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

from backend.models.resume_content import ResumeContent
from backend.models.resume_ir import ResumeIR
from backend.models.tailoring import (
    ApprovedBullet,
    ExperienceBank,
    Fact,
    ProjectBank,
    ResumeBank,
)

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "resume_bank"


def _extract_facts_from_bullet(bullet_text: str, prefix: str) -> list[Fact]:
    """Extract atomic facts from a bullet point text."""
    facts = []
    # The full bullet is itself a fact
    facts.append(Fact(
        id=f"{prefix}_full",
        text=bullet_text,
        tags=_extract_tags(bullet_text),
    ))

    # Extract metrics/numbers as separate facts. Capture the number plus a
    # short trailing unit/context (e.g. "25%", "3 years", "$250k"), not the
    # rest of the sentence -- `[^.]*` used to run unbounded to the next
    # period, so a bullet with no interior period produced a "metric" fact
    # that was really just the whole bullet again (already captured above
    # as the "_full" fact), defeating the point of extracting an atomic fact.
    metrics = re.findall(r"\b\d+[\d,]*\+?%?\s*(?:[a-zA-Z]+\.?){0,3}", bullet_text)
    for i, metric in enumerate(metrics):
        metric = metric.strip().rstrip(",;")
        if len(metric) > 2:
            facts.append(Fact(
                id=f"{prefix}_metric_{i}",
                text=metric,
                tags=["metric"],
            ))

    return facts


def _extract_tags(text: str) -> list[str]:
    """Extract technology/keyword tags from text."""
    text_lower = text.lower()
    tags = []

    tech_terms = {
        "python", "java", "javascript", "typescript", "c++", "sql", "r",
        "react", "next.js", "vue", "fastapi", "node.js", "scipy", "d3.js",
        "postgresql", "mongodb", "sqlite", "redis",
        "git", "docker", "aws", "gcp", "google cloud", "vercel",
        "rest api", "api", "machine learning", "optimization",
        "bloomberg", "data pipeline",
    }

    for term in tech_terms:
        # Word-boundary match, not substring -- a plain `term in text_lower`
        # check false-matches "r" in nearly every sentence, "git" inside
        # "digital", "api" inside "rapid", etc. Same bug class already
        # fixed in matcher.py's _text_contains_keyword_direct; (?<!\w)/
        # (?!\w) is used instead of \b so multi-word/punctuated terms like
        # "c++", "next.js", "rest api" still match correctly.
        if re.search(rf"(?<!\w){re.escape(term)}(?!\w)", text_lower):
            tags.append(term)

    return tags


def generate_bank_from_ir(ir: ResumeIR) -> ResumeBank:
    """Auto-generate a resume bank from a parsed ResumeIR."""
    bank = ResumeBank()

    for section in ir.content.sections:
        for entry in section.experience_entries:
            exp_bank = ExperienceBank(
                experience_id=entry.id,
                company=entry.company,
                role=entry.role,
                technologies=_extract_tags(" ".join(b.text for b in entry.bullets)),
            )

            for bullet in entry.bullets:
                # Create facts from bullet
                facts = _extract_facts_from_bullet(bullet.text, f"{entry.id}_{bullet.id}")
                exp_bank.facts.extend(facts)

                # The original bullet is an approved bullet
                exp_bank.approved_bullets.append(
                    ApprovedBullet(
                        id=bullet.id,
                        text=bullet.text,
                        source_fact_ids=[f.id for f in facts],
                        tags=_extract_tags(bullet.text),
                    )
                )

            bank.experiences.append(exp_bank)

        for entry in section.project_entries:
            proj_bank = ProjectBank(
                project_id=entry.id,
                name=entry.name,
                technologies=_extract_tags(" ".join(b.text for b in entry.bullets)),
            )

            for bullet in entry.bullets:
                facts = _extract_facts_from_bullet(bullet.text, f"{entry.id}_{bullet.id}")
                proj_bank.facts.extend(facts)

                proj_bank.approved_bullets.append(
                    ApprovedBullet(
                        id=bullet.id,
                        text=bullet.text,
                        source_fact_ids=[f.id for f in facts],
                        tags=_extract_tags(bullet.text),
                    )
                )

            bank.projects.append(proj_bank)

    # Extract all skills
    for section in ir.content.sections:
        for cat in section.skill_categories:
            bank.additional_skills.extend(cat.skills)

    return bank


def save_bank(bank: ResumeBank, resume_id: str):
    """Save a resume bank to disk.

    Writes to a temp file and renames into place so a concurrent load_bank
    (e.g. a double-submitted request for the same resume_id) never observes
    a partially-written file -- os.replace is atomic on both POSIX and
    Windows.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = DATA_DIR / f"{resume_id}_bank.json"
    tmp_path = path.with_suffix(".json.tmp")
    tmp_path.write_text(bank.model_dump_json(indent=2))
    tmp_path.replace(path)


def load_bank(resume_id: str) -> Optional[ResumeBank]:
    """Load a resume bank from disk.

    Returns None (triggering regeneration by the caller) rather than
    propagating a raw parse error if the file is missing, empty, or
    corrupt/torn -- e.g. from a read racing an old, non-atomic write.
    """
    path = DATA_DIR / f"{resume_id}_bank.json"
    if not path.exists():
        return None
    try:
        return ResumeBank.model_validate_json(path.read_text())
    except (json.JSONDecodeError, ValueError):
        return None
