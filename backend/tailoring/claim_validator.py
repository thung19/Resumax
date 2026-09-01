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

# Specific technologies that need evidence.
#
# This list is inherently incomplete — anything NOT listed here is
# structurally unable to be checked at all, since _check_new_technologies
# only ever tests membership in this set. Missing a common technology
# isn't a neutral gap, it's a silent hole: a fabricated "SQL" or "Apache
# Spark" claim sails through validation with zero check, not a cautious
# one. Below adds several dozen widely-used technologies confirmed
# absent (databases/query languages, markup, big-data/observability
# tooling, and common platforms/frameworks) — still not exhaustive, but
# meaningfully less porous than before.
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
    # Query languages / markup — extremely common, previously absent
    "sql", "html", "css", "linux", "bash",
    # Big data / data engineering
    "spark", "hadoop", "airflow", "snowflake", "databricks",
    "bigquery", "redshift",
    # Observability / infra tooling
    "grafana", "prometheus", "nginx", "helm", "istio", "vagrant",
    # AI / ML platforms and tooling
    "openai", "langchain",
    # Hosting / deployment platforms
    "vercel", "heroku",
    # Frontend build tooling
    "webpack", "tailwind", "tailwindcss",
    # Testing
    "pytest", "selenium",
}


_METRIC_NUMBER_RE = re.compile(r"(?<!\w)(\d[\d,]*)([%x])?", re.IGNORECASE)


def _extract_metric_tokens(text: str) -> set[str]:
    """Extract number tokens for fabrication/preservation checks, keeping
    an immediately-attached %/x suffix (if present) as part of the token.

    Without this, a bare-digit match anywhere in the source validated
    ANY claim built from the same digits regardless of what they meant —
    e.g. "40 tickets per week" in the source would validate an entirely
    fabricated "boosting uptime by 40%" claim purely because "40" exists
    somewhere for something unrelated. Keeping a directly-attached unit
    suffix as part of the comparison token catches that: "40%" and "40"
    are now different tokens, so a rewrite that makes its unit explicit
    has to find that same unit in the source, not just the digits.
    Numbers with no attached suffix (by far the more common case, e.g.
    "a team of 8") still only compare bare digits — properly verifying
    that kind of claim needs real context/NLP understanding, out of
    scope for this thin, deterministic safety net.
    """
    tokens = set()
    for m in _METRIC_NUMBER_RE.finditer(text):
        digits = m.group(1).replace(",", "")
        suffix = (m.group(2) or "").lower()
        tokens.add(f"{digits}{suffix}")
    return tokens


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

            # (?<!\w)/(?!\w) instead of \b: \b requires a WORD/NON-WORD
            # transition on each side, which a term ending in punctuation
            # (e.g. "c++", "c#") can never satisfy against following
            # whitespace/punctuation -- both sides are non-word, so \b
            # never matches and this check was dead code for those terms,
            # silently letting fabricated "C#"/"C++" claims straight
            # through. The lookaround only requires the adjacent
            # character not be a word character, regardless of what the
            # match itself ends in.
            pattern = rf"(?<!\w){re.escape(tech)}(?!\w)"

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
        rewrite_tokens = _extract_metric_tokens(rewritten)
        source_tokens = _extract_metric_tokens(source)

        for token in rewrite_tokens:
            # No exemption for small numbers: a fabricated "team of 8" or
            # "9x faster" is just as much a fabrication as a fabricated
            # large one — the prior ">10" threshold let any invented
            # number 10-or-under through with zero check.
            if token not in source_tokens:
                result.issues.append(
                    f"[REJECT] Metric '{token}' appears in rewrite "
                    "but not in source facts"
                )

    def _check_metric_preservation(
        self, original: str, rewritten: str, result: ValidationResult,
    ):
        """Warn if too many metrics from original were dropped (not a rejection).

        Tolerance is controlled by config.metric_loss_tolerance (default 20%).
        """
        orig_tokens = _extract_metric_tokens(original)
        rewrite_tokens = _extract_metric_tokens(rewritten)

        # Count how many original metrics were preserved
        preserved = 0
        orig_numbers = {re.sub(r"\D", "", t) for t in orig_tokens}
        for token in orig_tokens:
            digits = re.sub(r"\D", "", token)
            if digits and int(digits) > 1:
                if token in rewrite_tokens:
                    preserved += 1
                else:
                    result.metric_warnings.append(
                        f"Original metric '{token}' was dropped in rewrite"
                    )

        # Check if metric loss exceeds tolerance
        if orig_numbers:
            loss_ratio = 1.0 - (preserved / len(orig_numbers))
            if loss_ratio > self.config.metric_loss_tolerance:
                result.metric_warnings.append(
                    f"Metric loss ratio {loss_ratio:.1%} exceeds tolerance "
                    f"{self.config.metric_loss_tolerance:.1%}"
                )
