"""Bullet Fitter — measure-compress-validate-retry loop.

Orchestrates:
1. Measure each bullet with BulletMeasurer (ReportLab layout engine)
2. Identify overflows
3. Request LLM compression with calculated compression ratio
4. Validate rewrite via ClaimValidator
5. Re-measure to confirm fit
6. Retry up to MAX_RETRIES with increasing compression
7. Final validation: report any bullets that still don't fit

Responsibilities are separated:
- BulletMeasurer: layout measurement
- BulletRewriter: LLM compression
- ClaimValidator: factual safety
- BulletFitter: orchestration and retry logic
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from backend.models.resume_ir import ResumeIR
from backend.models.resume_layout import ResumeLayout
from backend.models.tailoring import BulletChange, ResumeBank, TailoringResult
from backend.tailoring.bullet_measurer import BulletMeasurer, BulletMeasurement
from backend.tailoring.claim_validator import ClaimValidator

logger = logging.getLogger(__name__)

MAX_RETRIES = 3


@dataclass
class FitAttempt:
    """Record of a single fitting attempt for a bullet."""
    attempt: int
    compression_target: float
    original_chars: int
    result_chars: int
    result_lines: int
    accepted: bool
    reason: str = ""


@dataclass
class BulletFitResult:
    """Result of fitting a single bullet."""
    bullet_id: str
    original_text: str
    fitted_text: str
    original_lines: int
    fitted_lines: int
    changed: bool = False
    fits: bool = True
    attempts: list[FitAttempt] = field(default_factory=list)
    error: Optional[str] = None


@dataclass
class FittingReport:
    """Report from the full bullet fitting pass."""
    total_bullets: int = 0
    overflows_detected: int = 0
    successfully_fitted: int = 0
    failed_to_fit: int = 0
    unchanged: int = 0
    results: list[BulletFitResult] = field(default_factory=list)
    all_fit: bool = True


class BulletFitter:
    """Orchestrate bullet line-fitting with layout-accurate measurement.

    When one_line_bullets=True, every bullet must render on exactly one
    line. Uses BulletMeasurer for actual rendered measurement, LLM rewriter
    for intelligent compression, and ClaimValidator for factual safety.
    """

    def __init__(
        self,
        layout: ResumeLayout,
        bank: Optional[ResumeBank] = None,
        use_llm: bool = True,
        safety_margin: float = 0.03,
    ):
        self._measurer = BulletMeasurer(layout, safety_margin=safety_margin)
        self._validator = ClaimValidator()
        self._bank = bank
        self._use_llm = use_llm
        self._rewriter = None

    def _get_rewriter(self):
        if self._rewriter is None:
            from backend.tailoring.rewriter import BulletRewriter
            self._rewriter = BulletRewriter()
        return self._rewriter

    def fit_bullets(self, result: TailoringResult) -> FittingReport:
        """Fit all bullets to one line. Modifies result.bullet_changes in place."""
        report = FittingReport()

        for change in result.bullet_changes:
            if change.action == "remove":
                continue

            report.total_bullets += 1
            text = change.tailored_text

            # Measure current bullet
            measurement = self._measurer.measure(text)

            if measurement.fits_one_line:
                report.unchanged += 1
                report.results.append(BulletFitResult(
                    bullet_id=change.bullet_id,
                    original_text=text,
                    fitted_text=text,
                    original_lines=1,
                    fitted_lines=1,
                ))
                continue

            # Overflow detected
            report.overflows_detected += 1
            fit_result = self._fit_single_bullet(change, measurement)
            report.results.append(fit_result)

            if fit_result.fits:
                report.successfully_fitted += 1
                change.tailored_text = fit_result.fitted_text
                if fit_result.changed:
                    change.reason = (change.reason or "") + f" | Fitted to 1 line ({fit_result.original_lines}→1)"
                    if change.action == "keep":
                        change.action = "rewrite"
            else:
                report.failed_to_fit += 1
                report.all_fit = False

        return report

    def _fit_single_bullet(
        self, change: BulletChange, initial_measurement: BulletMeasurement
    ) -> BulletFitResult:
        """Try to fit a single bullet to one line with retry loop."""
        fit_result = BulletFitResult(
            bullet_id=change.bullet_id,
            original_text=change.tailored_text,
            fitted_text=change.tailored_text,
            original_lines=initial_measurement.line_count,
            fitted_lines=initial_measurement.line_count,
        )

        current_text = change.tailored_text
        current_measurement = initial_measurement

        for attempt_num in range(1, MAX_RETRIES + 1):
            compression = self._measurer.compute_compression_target(current_measurement)
            target_chars = int(len(current_text) * compression)

            attempt = FitAttempt(
                attempt=attempt_num,
                compression_target=compression,
                original_chars=len(current_text),
                result_chars=0,
                result_lines=0,
                accepted=False,
            )

            # Try LLM compression
            if self._use_llm:
                try:
                    rewriter = self._get_rewriter()
                    facts = self._get_facts(change.bullet_id)

                    shortened = rewriter.shorten_bullet(
                        bullet_id=change.bullet_id,
                        bullet_text=current_text,
                        facts=facts,
                        target_lines=1,
                        chars_per_line=target_chars,
                    )

                    candidate = shortened.tailored_text

                except Exception as e:
                    logger.warning(f"LLM shorten failed for {change.bullet_id}: {e}")
                    candidate = self._deterministic_shorten(current_text, target_chars)
            else:
                candidate = self._deterministic_shorten(current_text, target_chars)

            # Validate factually
            validation_change = BulletChange(
                bullet_id=change.bullet_id,
                original_text=change.original_text,
                tailored_text=candidate,
                action="rewrite",
            )
            validation = self._validator.validate(
                validation_change,
                self._get_facts(change.bullet_id),
            )

            if not validation.valid:
                attempt.reason = f"Validation failed: {'; '.join(validation.issues[:2])}"
                attempt.result_chars = len(candidate)
                fit_result.attempts.append(attempt)
                # Try again with a tighter target using the original text
                compression *= 0.9
                target_chars = int(len(change.tailored_text) * compression)
                continue

            # Re-measure
            new_measurement = self._measurer.measure(candidate)
            attempt.result_chars = len(candidate)
            attempt.result_lines = new_measurement.line_count

            if new_measurement.fits_one_line:
                attempt.accepted = True
                attempt.reason = "Fits on one line"
                fit_result.attempts.append(attempt)
                fit_result.fitted_text = candidate
                fit_result.fitted_lines = 1
                fit_result.changed = True
                fit_result.fits = True
                return fit_result
            else:
                attempt.reason = f"Still {new_measurement.line_count} lines after compression"
                fit_result.attempts.append(attempt)
                # Use the compressed version as starting point for next attempt
                current_text = candidate
                current_measurement = new_measurement

        # Exhausted retries
        fit_result.error = f"Could not fit to 1 line after {MAX_RETRIES} attempts"
        fit_result.fits = False
        return fit_result

    def _deterministic_shorten(self, text: str, target_chars: int) -> str:
        """Shorten text to target_chars at a word boundary.
        Only used as LLM fallback — preserves grammatical completeness."""
        if len(text) <= target_chars:
            return text

        # Find the last complete clause/phrase before the target
        truncated = text[:target_chars]

        # Try to break at a comma or semicolon for natural phrasing
        for sep in [", ", "; ", " — ", " - "]:
            last_sep = truncated.rfind(sep)
            if last_sep > target_chars * 0.6:
                return truncated[:last_sep].rstrip(",;: ")

        # Fall back to word boundary
        last_space = truncated.rfind(" ")
        if last_space > target_chars * 0.5:
            return truncated[:last_space].rstrip(",;: ")

        return truncated.rstrip(",;: ")

    def _get_facts(self, bullet_id: str) -> list[dict]:
        """Get source facts for a bullet."""
        if not self._bank:
            return []
        for exp in self._bank.experiences:
            for ab in exp.approved_bullets:
                if ab.id == bullet_id:
                    return [{"id": f.id, "text": f.text} for f in exp.facts]
            # Check if any bullet matches
            for f in exp.facts:
                if bullet_id in f.id:
                    return [{"id": ff.id, "text": ff.text} for ff in exp.facts]
        for proj in self._bank.projects:
            for ab in proj.approved_bullets:
                if ab.id == bullet_id:
                    return [{"id": f.id, "text": f.text} for f in proj.facts]
        return []

    def validate_all_fit(self, result: TailoringResult) -> list[str]:
        """Final validation: check that ALL bullets fit on one line.

        Returns a list of bullet_ids that still overflow.
        Called at the end of the pipeline as a hard constraint check.
        """
        violations: list[str] = []
        for change in result.bullet_changes:
            if change.action == "remove":
                continue
            measurement = self._measurer.measure(change.tailored_text)
            if not measurement.fits_one_line:
                violations.append(change.bullet_id)
        return violations
