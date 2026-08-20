"""Tailoring Configuration - Control validation strictness and pipeline behavior.

Environment variables and defaults for tuning the tailoring pipeline.
"""

import os
from dataclasses import dataclass


@dataclass
class ValidationConfig:
    """Configuration for claim validation strictness."""

    # Claim validation thresholds
    # 0.0 = allow anything, 1.0 = reject everything
    # 0.5 = moderate (default: allow claims with at least 50% confidence)
    claim_confidence_threshold: float = 0.5

    # Reject if more than N% of bullet is fabricated
    max_fabrication_ratio: float = 0.3  # allow up to 30% new words

    # Number of rejection retries before giving up
    max_rejection_retries: int = 5

    # Reject changes that lose more than N% of original metrics
    metric_loss_tolerance: float = 0.2  # allow losing up to 20% of metrics

    @classmethod
    def from_env(cls) -> "ValidationConfig":
        """Load configuration from environment variables."""
        return cls(
            claim_confidence_threshold=float(
                os.environ.get("VALIDATION_CONFIDENCE_THRESHOLD", "0.5")
            ),
            max_fabrication_ratio=float(
                os.environ.get("VALIDATION_MAX_FABRICATION", "0.3")
            ),
            max_rejection_retries=int(
                os.environ.get("VALIDATION_MAX_RETRIES", "5")
            ),
            metric_loss_tolerance=float(
                os.environ.get("VALIDATION_METRIC_LOSS_TOLERANCE", "0.2")
            ),
        )


@dataclass
class LayoutConfig:
    """Configuration for layout and rendering."""

    # Maximum characters per bullet (default from font measurement)
    max_chars_per_line: int = 115

    # Maximum bullets per experience entry (allow LLM to remove below this)
    max_bullets_per_entry: int = 4

    # Force single-line bullets (no wraps)
    enforce_single_line_bullets: bool = True

    # Page fitting: allow removing entire entries if needed
    allow_remove_entries: bool = True

    # Page fitting: minimum bullets to preserve in entry
    min_bullets_to_preserve: int = 1

    @classmethod
    def from_env(cls) -> "LayoutConfig":
        """Load configuration from environment variables."""
        return cls(
            max_chars_per_line=int(
                os.environ.get("LAYOUT_MAX_CHARS", "115")
            ),
            max_bullets_per_entry=int(
                os.environ.get("LAYOUT_MAX_BULLETS", "4")
            ),
            enforce_single_line_bullets=os.environ.get(
                "LAYOUT_SINGLE_LINE", "true"
            ).lower() == "true",
            allow_remove_entries=os.environ.get(
                "LAYOUT_ALLOW_REMOVE_ENTRIES", "true"
            ).lower() == "true",
            min_bullets_to_preserve=int(
                os.environ.get("LAYOUT_MIN_BULLETS", "1")
            ),
        )


@dataclass
class LLMConfig:
    """Configuration for LLM behavior."""

    # Model to use for tailoring
    tailoring_model: str = "claude-sonnet-4-6"

    # Model for JD analysis
    analysis_model: str = "claude-haiku-4-5-20251001"

    # Max tokens for tailoring pass
    tailoring_max_tokens: int = 4096

    # Max tokens for JD analysis
    analysis_max_tokens: int = 2048

    # Timeout for LLM calls (seconds)
    request_timeout: int = 60

    # Use LLM for any enrichment (set False to disable all LLM)
    enabled: bool = True

    @classmethod
    def from_env(cls) -> "LLMConfig":
        """Load configuration from environment variables."""
        return cls(
            tailoring_model=os.environ.get(
                "LLM_TAILORING_MODEL", "claude-sonnet-4-6"
            ),
            analysis_model=os.environ.get(
                "LLM_ANALYSIS_MODEL", "claude-haiku-4-5-20251001"
            ),
            tailoring_max_tokens=int(
                os.environ.get("LLM_TAILORING_MAX_TOKENS", "4096")
            ),
            analysis_max_tokens=int(
                os.environ.get("LLM_ANALYSIS_MAX_TOKENS", "2048")
            ),
            request_timeout=int(
                os.environ.get("LLM_REQUEST_TIMEOUT", "60")
            ),
            enabled=os.environ.get(
                "LLM_ENABLED", "true"
            ).lower() == "true",
        )


@dataclass
class PipelineConfig:
    """Complete tailoring pipeline configuration."""

    validation: ValidationConfig
    layout: LayoutConfig
    llm: LLMConfig

    # Debug: enable detailed logging
    debug_logging: bool = False

    # Debug: preserve intermediate results in debug_log
    preserve_debug_artifacts: bool = False

    @classmethod
    def from_env(cls) -> "PipelineConfig":
        """Load complete configuration from environment variables."""
        return cls(
            validation=ValidationConfig.from_env(),
            layout=LayoutConfig.from_env(),
            llm=LLMConfig.from_env(),
            debug_logging=os.environ.get(
                "DEBUG_LOGGING", "false"
            ).lower() == "true",
            preserve_debug_artifacts=os.environ.get(
                "PRESERVE_DEBUG_ARTIFACTS", "false"
            ).lower() == "true",
        )

    @classmethod
    def default(cls) -> "PipelineConfig":
        """Return default configuration."""
        return cls(
            validation=ValidationConfig(),
            layout=LayoutConfig(),
            llm=LLMConfig(),
        )


# Global instance (lazily initialized)
_global_config: PipelineConfig | None = None


def get_config() -> PipelineConfig:
    """Get the global configuration (load from env on first access)."""
    global _global_config
    if _global_config is None:
        _global_config = PipelineConfig.from_env()
    return _global_config


def reset_config(config: PipelineConfig | None = None) -> None:
    """Reset the global configuration (used for testing)."""
    global _global_config
    if config is None:
        _global_config = PipelineConfig.default()
    else:
        _global_config = config
