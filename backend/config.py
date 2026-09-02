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
class TrimmingConfig:
    """Configuration for bullet trimming behavior.

    The system calculates character limits dynamically based on precise point
    measurements. For variable-width fonts (e.g., Garamond), it conservatively
    applies a safety factor to account for character composition differences.

    When LLM rewrites text, the character mix changes (more M's/W's, fewer i's/l's),
    affecting the actual pixel width. The safety factor provides a conservative margin.
    """

    # Enable trimming at all (can disable if causing too many reverts)
    enable_trimming: bool = True

    # Maximum trim rounds before giving up (hardcoded to 3 in implementation)
    max_trim_rounds: int = 3

    # Character width safety factor (0.0-1.0)
    # Multiplied by usable width when calculating per-bullet char limits
    # 1.0 = no reduction, 0.85 = 15% reduction for safety
    # Default: 0.85 for Garamond (very conservative for variable-width font)
    # Lower values = tighter char budgets = more revert-on-overflow
    # Higher values = looser budgets = more trim failures
    char_width_safety_factor: float = 0.85

    @classmethod
    def from_env(cls) -> "TrimmingConfig":
        """Load configuration from environment variables."""
        return cls(
            enable_trimming=os.environ.get(
                "TRIM_ENABLE", "true"
            ).lower() == "true",
            max_trim_rounds=int(
                os.environ.get("TRIM_MAX_ROUNDS", "3")
            ),
            char_width_safety_factor=float(
                os.environ.get("TRIM_SAFETY_FACTOR", "0.85")
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

    # Max tokens for JD analysis. Matches the value the analysis call site
    # actually used before this field was wired up (LLM_ANALYSIS_MAX_TOKENS
    # was previously read here but never applied to any call).
    analysis_max_tokens: int = 4096

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
                os.environ.get("LLM_ANALYSIS_MAX_TOKENS", "4096")
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
    trimming: TrimmingConfig
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
            trimming=TrimmingConfig.from_env(),
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
            trimming=TrimmingConfig(),
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
