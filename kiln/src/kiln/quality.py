"""Standardized quality verification protocol for 3D print attestation.

Provides a protocol-level quality scoring and attestation system that
replaces subjective, agent-specific quality judgments with a standardized,
tamper-evident format.  Quality attestations use HMAC-SHA256 signatures
so that scores can be verified during dispute resolution in the 3DOS
network.

Example::

    attestation = assess_quality(
        metrics={"overall": 0.85, "surface_quality": 0.9},
        job_id="job-abc123",
        printer_id="printer-01",
        assessed_by="agent-vision-v2",
        signing_key="shared-secret",
    )
    assert attestation.passed
    assert verify_attestation(attestation, signing_key="shared-secret")
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class QualityError(Exception):
    """Raised when quality assessment or verification fails."""

    def __init__(self, message: str, *, cause: Optional[Exception] = None) -> None:
        super().__init__(message)
        self.cause = cause


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class QualityScore:
    """Standardized quality score for a completed print job.

    All scores are normalized to the 0.0 -- 1.0 range where 1.0 is
    perfect quality.  Per-dimension scores are optional; ``overall``
    is always required.
    """

    overall: float
    assessed_by: str
    assessed_at: str
    notes: str = ""
    dimensional_accuracy: Optional[float] = None
    surface_quality: Optional[float] = None
    layer_adhesion: Optional[float] = None
    structural_integrity: Optional[float] = None
    reference_model_hash: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-serialisable dictionary."""
        return asdict(self)


@dataclass
class QualityThreshold:
    """Pass/fail thresholds for quality assessment.

    Only ``min_overall`` is required.  Per-dimension thresholds are
    optional and only checked when both the threshold and the
    corresponding score are present.
    """

    min_overall: float = 0.7
    min_dimensional_accuracy: Optional[float] = None
    min_surface_quality: Optional[float] = None
    min_layer_adhesion: Optional[float] = None
    min_structural_integrity: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-serialisable dictionary."""
        return asdict(self)


@dataclass
class QualityAttestation:
    """Tamper-evident quality attestation for a print job.

    The ``attestation_hash`` is an HMAC-SHA256 digest computed over the
    canonical JSON representation of (score, job_id, printer_id).  This
    allows any party holding the signing key to verify that the
    attestation has not been altered.
    """

    job_id: str
    printer_id: str
    score: QualityScore
    threshold: QualityThreshold
    passed: bool
    attestation_hash: str

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-serialisable dictionary."""
        data = asdict(self)
        data["score"] = self.score.to_dict()
        data["threshold"] = self.threshold.to_dict()
        return data


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _canonical_payload(score: QualityScore, job_id: str, printer_id: str) -> str:
    """Build a deterministic JSON string for HMAC signing.

    Keys are sorted and no extra whitespace is added so that the same
    inputs always produce the same byte sequence.
    """
    payload = {
        "score": score.to_dict(),
        "job_id": job_id,
        "printer_id": printer_id,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _compute_hmac(payload: str, key: str) -> str:
    """Return the hex-encoded HMAC-SHA256 of *payload* using *key*."""
    return hmac.new(
        key.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _validate_score_range(value: float, name: str) -> None:
    """Raise :class:`QualityError` if *value* is outside [0.0, 1.0]."""
    if not 0.0 <= value <= 1.0:
        raise QualityError(
            f"{name} must be between 0.0 and 1.0, got {value}"
        )


def _check_threshold(
    score_value: Optional[float],
    threshold_value: Optional[float],
    name: str,
) -> bool:
    """Return ``False`` if the score is below the threshold.

    Returns ``True`` when either value is ``None`` (threshold not
    configured or score not provided).
    """
    if score_value is None or threshold_value is None:
        return True
    return score_value >= threshold_value


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def assess_quality(
    metrics: Dict[str, Any],
    job_id: str,
    printer_id: str,
    assessed_by: str,
    signing_key: str,
    *,
    threshold: Optional[QualityThreshold] = None,
    reference_model_hash: Optional[str] = None,
    notes: str = "",
) -> QualityAttestation:
    """Build a quality attestation from raw metric values.

    :param metrics: Dict with ``"overall"`` (required) and optional
        per-dimension keys (``"dimensional_accuracy"``,
        ``"surface_quality"``, ``"layer_adhesion"``,
        ``"structural_integrity"``).  All values must be floats in
        [0.0, 1.0].
    :param job_id: Unique identifier for the print job.
    :param printer_id: Identifier of the printer that produced the part.
    :param assessed_by: Agent ID or ``"operator"`` for manual assessment.
    :param signing_key: Shared secret used for HMAC-SHA256 signing.
    :param threshold: Custom pass/fail thresholds.  Defaults to
        :class:`QualityThreshold` with ``min_overall=0.7``.
    :param reference_model_hash: SHA-256 hash of the original STL file
        for reference comparison.
    :param notes: Free-text notes about the assessment.
    :returns: A signed :class:`QualityAttestation`.
    :raises QualityError: If required fields are missing or values are
        out of range.
    """
    if not job_id:
        raise QualityError("job_id is required")
    if not printer_id:
        raise QualityError("printer_id is required")
    if not assessed_by:
        raise QualityError("assessed_by is required")
    if not signing_key:
        raise QualityError("signing_key is required")

    # -- Extract and validate scores --
    overall = metrics.get("overall")
    if overall is None:
        raise QualityError("metrics must include 'overall' score")
    _validate_score_range(overall, "overall")

    dimensional_accuracy: Optional[float] = metrics.get("dimensional_accuracy")
    surface_quality: Optional[float] = metrics.get("surface_quality")
    layer_adhesion: Optional[float] = metrics.get("layer_adhesion")
    structural_integrity: Optional[float] = metrics.get("structural_integrity")

    for name, value in [
        ("dimensional_accuracy", dimensional_accuracy),
        ("surface_quality", surface_quality),
        ("layer_adhesion", layer_adhesion),
        ("structural_integrity", structural_integrity),
    ]:
        if value is not None:
            _validate_score_range(value, name)

    # -- Build score --
    score = QualityScore(
        overall=overall,
        assessed_by=assessed_by,
        assessed_at=datetime.now(timezone.utc).isoformat(),
        notes=notes,
        dimensional_accuracy=dimensional_accuracy,
        surface_quality=surface_quality,
        layer_adhesion=layer_adhesion,
        structural_integrity=structural_integrity,
        reference_model_hash=reference_model_hash,
    )

    # -- Evaluate against threshold --
    if threshold is None:
        threshold = QualityThreshold()

    passed = all([
        _check_threshold(score.overall, threshold.min_overall, "overall"),
        _check_threshold(
            score.dimensional_accuracy,
            threshold.min_dimensional_accuracy,
            "dimensional_accuracy",
        ),
        _check_threshold(
            score.surface_quality,
            threshold.min_surface_quality,
            "surface_quality",
        ),
        _check_threshold(
            score.layer_adhesion,
            threshold.min_layer_adhesion,
            "layer_adhesion",
        ),
        _check_threshold(
            score.structural_integrity,
            threshold.min_structural_integrity,
            "structural_integrity",
        ),
    ])

    # -- Sign --
    canonical = _canonical_payload(score, job_id, printer_id)
    attestation_hash = _compute_hmac(canonical, signing_key)

    attestation = QualityAttestation(
        job_id=job_id,
        printer_id=printer_id,
        score=score,
        threshold=threshold,
        passed=passed,
        attestation_hash=attestation_hash,
    )

    logger.info(
        "Quality attestation for job %s: passed=%s overall=%.2f",
        job_id,
        passed,
        overall,
    )
    return attestation


def verify_attestation(attestation: QualityAttestation, signing_key: str) -> bool:
    """Verify the HMAC integrity of a quality attestation.

    Re-computes the HMAC-SHA256 digest from the attestation's score,
    job_id, and printer_id, then compares it to the stored hash using
    constant-time comparison.

    :param attestation: The attestation to verify.
    :param signing_key: The shared secret used when the attestation was
        created.
    :returns: ``True`` if the attestation is intact, ``False`` if
        tampered.
    """
    canonical = _canonical_payload(
        attestation.score, attestation.job_id, attestation.printer_id
    )
    expected = _compute_hmac(canonical, signing_key)
    return hmac.compare_digest(expected, attestation.attestation_hash)
