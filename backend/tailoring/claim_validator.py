"""Claim Validator.

Validates that rewritten bullets don't contain unsupported claims.
Rejects bullets whose claims cannot be traced to source facts.

Uses deterministic checks first, then optionally Claude for deeper validation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from backend.models.tailoring import BulletChange


@dataclass
class ValidationResult:
    """Result of validating a rewritten bullet."""
    valid: bool = True
    issues: list[str] = field(default_factory=list)
    severity: str = "ok"  # ok | warning | reject


# Common technology terms to check for fabrication
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

    def validate(
        self,
        change: BulletChange,
        source_facts: list[dict],  # [{id, text}]
    ) -> ValidationResult:
        """Validate a bullet change."""
        result = ValidationResult()

        if change.action in ("keep", "remove"):
            return result

        original = change.original_text.lower()
        rewritten = change.tailored_text.lower()
        facts_text = " ".join(f["text"].lower() for f in source_facts)
        all_source = f"{original} {facts_text}"

        # Check 1: No new technologies
        self._check_new_technologies(rewritten, all_source, result)

        # Check 2: No fabricated metrics
        self._check_fabricated_metrics(rewritten, all_source, result)

        # Check 3: Preserved key claims
        self._check_preserved_claims(original, rewritten, result)

        # Check 4: Reasonable length
        self._check_length(rewritten, result)

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
        self, rewritten: str, source: str, result: ValidationResult
    ):
        """Check if rewritten text introduces technologies not in source."""
        for tech in TECH_TERMS:
            # Use word boundary matching
            if len(tech) <= 2:
                pattern = rf"\b{re.escape(tech)}\b"
            else:
                pattern = re.escape(tech)

            in_rewrite = bool(re.search(pattern, rewritten, re.IGNORECASE))
            in_source = bool(re.search(pattern, source, re.IGNORECASE))

            if in_rewrite and not in_source:
                result.issues.append(
                    f"[REJECT] Technology '{tech}' appears in rewrite but not in source facts"
                )

    def _check_fabricated_metrics(
        self, rewritten: str, source: str, result: ValidationResult
    ):
        """Check if rewritten text introduces new metrics not in source."""
        # Extract all numbers from rewritten
        rewrite_numbers = set(re.findall(r"\b(\d[\d,]*)\b", rewritten))
        source_numbers = set(re.findall(r"\b(\d[\d,]*)\b", source))

        for num in rewrite_numbers:
            # Normalize: remove commas
            norm_num = num.replace(",", "")
            found = False
            for src_num in source_numbers:
                if norm_num == src_num.replace(",", ""):
                    found = True
                    break
            if not found and int(norm_num) > 1:
                # Allow small numbers (could be generic)
                if int(norm_num) > 10:
                    result.issues.append(
                        f"[REJECT] Metric '{num}' appears in rewrite but not in source facts"
                    )
                else:
                    result.issues.append(
                        f"[WARNING] Number '{num}' in rewrite not found in source — verify"
                    )

    def _check_preserved_claims(
        self, original: str, rewritten: str, result: ValidationResult
    ):
        """Warn if key claims from the original were dropped."""
        # Check that numbers from original are preserved
        orig_numbers = set(re.findall(r"\b(\d[\d,]*)\b", original))
        rewrite_numbers = set(re.findall(r"\b(\d[\d,]*)\b", rewritten))

        for num in orig_numbers:
            norm = num.replace(",", "")
            if int(norm) > 1:
                found = any(
                    norm == rn.replace(",", "") for rn in rewrite_numbers
                )
                if not found:
                    result.issues.append(
                        f"[WARNING] Original metric '{num}' was dropped in rewrite"
                    )

    def _check_length(self, rewritten: str, result: ValidationResult):
        """Check if rewritten bullet is unreasonably long or short."""
        if len(rewritten) < 20:
            result.issues.append("[WARNING] Rewritten bullet is very short")
        elif len(rewritten) > 300:
            result.issues.append("[WARNING] Rewritten bullet is very long (>300 chars)")
