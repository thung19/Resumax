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

    # Extract metrics/numbers as separate facts
    metrics = re.findall(r"\b\d+[\d,]*\+?\b[^.]*", bullet_text)
    for i, metric in enumerate(metrics):
        metric = metric.strip().rstrip(",;")
        if len(metric) > 5:
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
        if term in text_lower:
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
    """Save a resume bank to disk."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = DATA_DIR / f"{resume_id}_bank.json"
    path.write_text(bank.model_dump_json(indent=2))


def load_bank(resume_id: str) -> Optional[ResumeBank]:
    """Load a resume bank from disk."""
    path = DATA_DIR / f"{resume_id}_bank.json"
    if path.exists():
        return ResumeBank.model_validate_json(path.read_text())
    return None
