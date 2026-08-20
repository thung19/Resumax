"""Claim Validator.

Validates that rewritten bullets don't contain unsupported claims.
Rejects bullets with fabricated technologies or metrics.
Warns (but doesn't reject) when original metrics are dropped.

This is a thin safety net — not a quality gate.
Validation strictness is controlled by ValidationConfig.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from backend.config import ValidationConfig, get_config
from backend.models.tailoring import BulletChange


@dataclass
class ValidationResult:
    """Result of validating a rewritten bullet."""
    valid: bool = True
    issues: list[str] = field(default_factory=list)
    metric_warnings: list[str] = field(default_factory=list)
    severity: str = "ok"  # ok | warning | reject


# Words that can be freely added to any software bullet — never flag these
ALWAYS_ALLOWED = {
    # Generic software descriptors
    "software", "application", "applications", "system", "systems",
    "web", "web application", "web service", "web services",
    # Action verbs
    "developed", "development", "implemented", "implementation",
    "designed", "engineered", "built", "deployed", "tested", "testing",
    "debugged", "debugging", "automated", "optimized", "maintained",
    "architected", "integrated", "configured", "managed",
    "communicated", "collaborated", "coordinated",
    # Architecture / methodology
    "scalable", "production", "end-to-end", "cross-functional",
    "technical", "agile", "scrum", "utilized", "leveraged",
    "full-stack", "fullstack", "backend", "frontend",
    "ci/cd", "devops", "microservices", "serverless",
    # AI / ML descriptors
    "machine learning", "ml", "ai", "llm", "llms",
    "nlp", "natural language processing",
    "deep learning", "neural", "embeddings", "embedding",
    # API descriptors
    "restful", "rest", "api", "apis", "rest api", "restful api",
    "data pipeline", "data engineering", "etl",
    # Collaboration
    "collaboration", "stakeholder",
    "cross-functional", "team",
}

# Specific technologies that need evidence
TECH_TERMS = {
    "python", "java", "javascript", "typescript", "c++", "c#", "go", "rust",
    "ruby", "php", "swift", "kotlin", "scala", "r", "matlab",
    "react", "angular", "vue", "svelte", "next.js", "nuxt",
    "django", "flask", "fastapi", "spring", "express", "nest.js",
    "node.js", "deno", "bun",
    "tensorflow", "pytorch", "keras", "scikit-learn",
    "pandas", "numpy", "scipy", "matplotlib", "d3.js",
    "postgresql", "mysql", "mongodb", "redis", "elasticsearch",
    "dynamodb", "cassandra", "neo4j", "sqlite",
    "docker", "kubernetes", "terraform", "ansible",
    "aws", "gcp", "azure", "lambda", "s3", "ec2",
    "git", "github", "gitlab", "jenkins", "circleci",
    "graphql", "grpc", "kafka", "rabbitmq", "celery",
    "blockchain", "solidity", "ethereum",
}


class ClaimValidator:
    """Validate rewritten bullets against source facts."""

    def __init__(self, config: Optional[ValidationConfig] = None):
        """Initialize with validation configuration.

        Args:
            config: ValidationConfig instance. If None, uses global config.
        """
        self.config = config or get_config().validation

    def validate(
        self,
        change: BulletChange,
        source_facts: list[dict],
    ) -> ValidationResult:
        """Validate a bullet change according to configured thresholds."""
        result = ValidationResult()

        if change.action in ("keep", "remove"):
            return result

        original = change.original_text.lower()
        rewritten = change.tailored_text.lower()
        facts_text = " ".join(f["text"].lower() for f in source_facts)
        all_source = f"{original} {facts_text}"

        # Check 1: No fabricated technologies
        self._check_new_technologies(rewritten, all_source, result)

        # Check 2: No fabricated metrics
        self._check_fabricated_metrics(rewritten, all_source, result)

        # Check 3: Metric preservation (warning only, no rejection)
        self._check_metric_preservation(original, rewritten, result)

        # Set severity
        if result.issues:
            rejects = [i for i in result.issues if i.startswith("[REJECT]")]
            if rejects:
                result.valid = False
                result.severity = "reject"
            else:
                result.severity = "warning"

        return result

    def _check_new_technologies(
        self, rewritten: str, source: str, result: ValidationResult,
    ):
        """Reject if rewrite introduces specific technologies not in source."""
        for tech in TECH_TERMS:
            if tech.lower() in ALWAYS_ALLOWED:
                continue

            pattern = rf"\b{re.escape(tech)}\b"

            in_rewrite = bool(re.search(pattern, rewritten, re.IGNORECASE))
            in_source = bool(re.search(pattern, source, re.IGNORECASE))

            if in_rewrite and not in_source:
                result.issues.append(
                    f"[REJECT] Technology '{tech}' appears in rewrite "
                    "but not in source facts"
                )

    def _check_fabricated_metrics(
        self, rewritten: str, source: str, result: ValidationResult,
    ):
        """Reject if rewrite introduces new metrics not in source."""
        rewrite_numbers = set(re.findall(r"(?<!\w)(\d[\d,]*)", rewritten))
        source_numbers = set(re.findall(r"(?<!\w)(\d[\d,]*)", source))

        for num in rewrite_numbers:
            norm_num = num.replace(",", "")
            found = any(
                norm_num == src_num.replace(",", "")
                for src_num in source_numbers
            )
            if not found and int(norm_num) > 10:
                result.issues.append(
                    f"[REJECT] Metric '{num}' appears in rewrite "
                    "but not in source facts"
                )

    def _check_metric_preservation(
        self, original: str, rewritten: str, result: ValidationResult,
    ):
        """Warn if too many metrics from original were dropped (not a rejection).

        Tolerance is controlled by config.metric_loss_tolerance (default 20%).
        """
        orig_numbers = set(re.findall(r"(?<!\w)(\d[\d,]*)", original))
        rewrite_numbers = set(re.findall(r"(?<!\w)(\d[\d,]*)", rewritten))

        # Count how many original metrics were preserved
        preserved = 0
        for num in orig_numbers:
            norm = num.replace(",", "")
            if int(norm) > 1:
                found = any(
                    norm == rn.replace(",", "") for rn in rewrite_numbers
                )
                if found:
                    preserved += 1
                else:
                    result.metric_warnings.append(
                        f"Original metric '{num}' was dropped in rewrite"
                    )

        # Check if metric loss exceeds tolerance
        if orig_numbers:
            loss_ratio = 1.0 - (preserved / len(orig_numbers))
            if loss_ratio > self.config.metric_loss_tolerance:
                result.metric_warnings.append(
                    f"Metric loss ratio {loss_ratio:.1%} exceeds tolerance "
                    f"{self.config.metric_loss_tolerance:.1%}"
                )
