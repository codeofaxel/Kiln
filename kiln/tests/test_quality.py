"""Tests for kiln.quality — standardized quality verification protocol.

Covers:
- QualityScore construction and serialization
- QualityThreshold defaults and custom values
- QualityAttestation construction and serialization
- assess_quality() happy path, threshold evaluation, validation errors
- verify_attestation() integrity check and tamper detection
- Edge cases: boundary scores, missing optional fields, all dimensions
"""

from __future__ import annotations

import json

import pytest

from kiln.quality import (
    QualityAttestation,
    QualityError,
    QualityScore,
    QualityThreshold,
    _canonical_payload,
    _check_threshold,
    _compute_hmac,
    _validate_score_range,
    assess_quality,
    verify_attestation,
)


# ---------------------------------------------------------------------------
# QualityScore
# ---------------------------------------------------------------------------


class TestQualityScore:
    """QualityScore dataclass construction and serialization."""

    def test_minimal_construction(self):
        score = QualityScore(
            overall=0.85,
            assessed_by="agent-v1",
            assessed_at="2025-01-15T12:00:00+00:00",
        )
        assert score.overall == 0.85
        assert score.assessed_by == "agent-v1"
        assert score.dimensional_accuracy is None
        assert score.surface_quality is None
        assert score.layer_adhesion is None
        assert score.structural_integrity is None
        assert score.reference_model_hash is None
        assert score.notes == ""

    def test_full_construction(self):
        score = QualityScore(
            overall=0.92,
            assessed_by="operator",
            assessed_at="2025-01-15T12:00:00+00:00",
            notes="Excellent print",
            dimensional_accuracy=0.95,
            surface_quality=0.88,
            layer_adhesion=0.91,
            structural_integrity=0.94,
            reference_model_hash="abc123def456",
        )
        assert score.dimensional_accuracy == 0.95
        assert score.reference_model_hash == "abc123def456"

    def test_to_dict(self):
        score = QualityScore(
            overall=0.8,
            assessed_by="agent-v1",
            assessed_at="2025-01-15T12:00:00+00:00",
            surface_quality=0.75,
        )
        d = score.to_dict()
        assert d["overall"] == 0.8
        assert d["assessed_by"] == "agent-v1"
        assert d["surface_quality"] == 0.75
        assert d["dimensional_accuracy"] is None
        # Must be JSON-serializable
        json.dumps(d)


# ---------------------------------------------------------------------------
# QualityThreshold
# ---------------------------------------------------------------------------


class TestQualityThreshold:
    """QualityThreshold defaults and customization."""

    def test_defaults(self):
        t = QualityThreshold()
        assert t.min_overall == 0.7
        assert t.min_dimensional_accuracy is None
        assert t.min_surface_quality is None
        assert t.min_layer_adhesion is None
        assert t.min_structural_integrity is None

    def test_custom_thresholds(self):
        t = QualityThreshold(
            min_overall=0.9,
            min_surface_quality=0.85,
        )
        assert t.min_overall == 0.9
        assert t.min_surface_quality == 0.85

    def test_to_dict(self):
        t = QualityThreshold(min_overall=0.8)
        d = t.to_dict()
        assert d["min_overall"] == 0.8
        json.dumps(d)


# ---------------------------------------------------------------------------
# QualityAttestation
# ---------------------------------------------------------------------------


class TestQualityAttestation:
    """QualityAttestation construction and serialization."""

    def test_to_dict_includes_nested(self):
        score = QualityScore(
            overall=0.85,
            assessed_by="agent-v1",
            assessed_at="2025-01-15T12:00:00+00:00",
        )
        threshold = QualityThreshold()
        att = QualityAttestation(
            job_id="job-1",
            printer_id="printer-1",
            score=score,
            threshold=threshold,
            passed=True,
            attestation_hash="deadbeef",
        )
        d = att.to_dict()
        assert d["job_id"] == "job-1"
        assert d["passed"] is True
        assert isinstance(d["score"], dict)
        assert isinstance(d["threshold"], dict)
        assert d["score"]["overall"] == 0.85
        assert d["threshold"]["min_overall"] == 0.7
        json.dumps(d)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


class TestValidateScoreRange:
    """_validate_score_range boundary checks."""

    def test_valid_boundaries(self):
        _validate_score_range(0.0, "test")
        _validate_score_range(1.0, "test")
        _validate_score_range(0.5, "test")

    def test_below_zero_raises(self):
        with pytest.raises(QualityError, match="must be between 0.0 and 1.0"):
            _validate_score_range(-0.01, "test_field")

    def test_above_one_raises(self):
        with pytest.raises(QualityError, match="must be between 0.0 and 1.0"):
            _validate_score_range(1.01, "test_field")


class TestCheckThreshold:
    """_check_threshold pass/fail logic."""

    def test_score_above_threshold_passes(self):
        assert _check_threshold(0.8, 0.7, "test") is True

    def test_score_equal_to_threshold_passes(self):
        assert _check_threshold(0.7, 0.7, "test") is True

    def test_score_below_threshold_fails(self):
        assert _check_threshold(0.6, 0.7, "test") is False

    def test_none_score_passes(self):
        assert _check_threshold(None, 0.7, "test") is True

    def test_none_threshold_passes(self):
        assert _check_threshold(0.5, None, "test") is True

    def test_both_none_passes(self):
        assert _check_threshold(None, None, "test") is True


class TestCanonicalPayload:
    """_canonical_payload determinism."""

    def test_deterministic_output(self):
        score = QualityScore(
            overall=0.85,
            assessed_by="agent-v1",
            assessed_at="2025-01-15T12:00:00+00:00",
        )
        p1 = _canonical_payload(score, "job-1", "printer-1")
        p2 = _canonical_payload(score, "job-1", "printer-1")
        assert p1 == p2

    def test_different_inputs_differ(self):
        score = QualityScore(
            overall=0.85,
            assessed_by="agent-v1",
            assessed_at="2025-01-15T12:00:00+00:00",
        )
        p1 = _canonical_payload(score, "job-1", "printer-1")
        p2 = _canonical_payload(score, "job-2", "printer-1")
        assert p1 != p2

    def test_is_valid_json(self):
        score = QualityScore(
            overall=0.85,
            assessed_by="agent-v1",
            assessed_at="2025-01-15T12:00:00+00:00",
        )
        payload = _canonical_payload(score, "job-1", "printer-1")
        parsed = json.loads(payload)
        assert parsed["job_id"] == "job-1"
        assert parsed["printer_id"] == "printer-1"


class TestComputeHmac:
    """_compute_hmac produces valid hex digests."""

    def test_returns_hex_string(self):
        result = _compute_hmac("test-payload", "secret-key")
        assert isinstance(result, str)
        assert len(result) == 64  # SHA-256 hex digest

    def test_different_keys_differ(self):
        h1 = _compute_hmac("payload", "key-a")
        h2 = _compute_hmac("payload", "key-b")
        assert h1 != h2

    def test_different_payloads_differ(self):
        h1 = _compute_hmac("payload-a", "key")
        h2 = _compute_hmac("payload-b", "key")
        assert h1 != h2


# ---------------------------------------------------------------------------
# assess_quality()
# ---------------------------------------------------------------------------


class TestAssessQuality:
    """assess_quality() happy path, thresholds, and validation."""

    def test_happy_path_passes(self):
        att = assess_quality(
            metrics={"overall": 0.85},
            job_id="job-1",
            printer_id="printer-1",
            assessed_by="agent-v1",
            signing_key="test-key",
        )
        assert att.passed is True
        assert att.job_id == "job-1"
        assert att.printer_id == "printer-1"
        assert att.score.overall == 0.85
        assert att.score.assessed_by == "agent-v1"
        assert att.score.assessed_at  # non-empty ISO timestamp
        assert att.attestation_hash  # non-empty hex string
        assert att.threshold.min_overall == 0.7

    def test_below_threshold_fails(self):
        att = assess_quality(
            metrics={"overall": 0.5},
            job_id="job-1",
            printer_id="printer-1",
            assessed_by="agent-v1",
            signing_key="test-key",
        )
        assert att.passed is False

    def test_exact_threshold_passes(self):
        att = assess_quality(
            metrics={"overall": 0.7},
            job_id="job-1",
            printer_id="printer-1",
            assessed_by="agent-v1",
            signing_key="test-key",
        )
        assert att.passed is True

    def test_custom_threshold(self):
        threshold = QualityThreshold(min_overall=0.95)
        att = assess_quality(
            metrics={"overall": 0.9},
            job_id="job-1",
            printer_id="printer-1",
            assessed_by="agent-v1",
            signing_key="test-key",
            threshold=threshold,
        )
        assert att.passed is False

    def test_per_dimension_threshold_fail(self):
        threshold = QualityThreshold(
            min_overall=0.5,
            min_surface_quality=0.9,
        )
        att = assess_quality(
            metrics={"overall": 0.8, "surface_quality": 0.7},
            job_id="job-1",
            printer_id="printer-1",
            assessed_by="agent-v1",
            signing_key="test-key",
            threshold=threshold,
        )
        assert att.passed is False

    def test_all_dimensions(self):
        att = assess_quality(
            metrics={
                "overall": 0.9,
                "dimensional_accuracy": 0.95,
                "surface_quality": 0.88,
                "layer_adhesion": 0.91,
                "structural_integrity": 0.94,
            },
            job_id="job-1",
            printer_id="printer-1",
            assessed_by="operator",
            signing_key="test-key",
            reference_model_hash="sha256abc",
            notes="Manual inspection OK",
        )
        assert att.passed is True
        assert att.score.dimensional_accuracy == 0.95
        assert att.score.reference_model_hash == "sha256abc"
        assert att.score.notes == "Manual inspection OK"

    def test_missing_overall_raises(self):
        with pytest.raises(QualityError, match="must include 'overall'"):
            assess_quality(
                metrics={"surface_quality": 0.8},
                job_id="job-1",
                printer_id="printer-1",
                assessed_by="agent-v1",
                signing_key="test-key",
            )

    def test_overall_out_of_range_raises(self):
        with pytest.raises(QualityError, match="must be between 0.0 and 1.0"):
            assess_quality(
                metrics={"overall": 1.5},
                job_id="job-1",
                printer_id="printer-1",
                assessed_by="agent-v1",
                signing_key="test-key",
            )

    def test_dimension_out_of_range_raises(self):
        with pytest.raises(QualityError, match="must be between 0.0 and 1.0"):
            assess_quality(
                metrics={"overall": 0.8, "layer_adhesion": -0.1},
                job_id="job-1",
                printer_id="printer-1",
                assessed_by="agent-v1",
                signing_key="test-key",
            )

    def test_empty_job_id_raises(self):
        with pytest.raises(QualityError, match="job_id is required"):
            assess_quality(
                metrics={"overall": 0.8},
                job_id="",
                printer_id="printer-1",
                assessed_by="agent-v1",
                signing_key="test-key",
            )

    def test_empty_printer_id_raises(self):
        with pytest.raises(QualityError, match="printer_id is required"):
            assess_quality(
                metrics={"overall": 0.8},
                job_id="job-1",
                printer_id="",
                assessed_by="agent-v1",
                signing_key="test-key",
            )

    def test_empty_assessed_by_raises(self):
        with pytest.raises(QualityError, match="assessed_by is required"):
            assess_quality(
                metrics={"overall": 0.8},
                job_id="job-1",
                printer_id="printer-1",
                assessed_by="",
                signing_key="test-key",
            )

    def test_empty_signing_key_raises(self):
        with pytest.raises(QualityError, match="signing_key is required"):
            assess_quality(
                metrics={"overall": 0.8},
                job_id="job-1",
                printer_id="printer-1",
                assessed_by="agent-v1",
                signing_key="",
            )

    def test_perfect_score(self):
        att = assess_quality(
            metrics={"overall": 1.0},
            job_id="job-1",
            printer_id="printer-1",
            assessed_by="agent-v1",
            signing_key="test-key",
        )
        assert att.passed is True
        assert att.score.overall == 1.0

    def test_zero_score(self):
        att = assess_quality(
            metrics={"overall": 0.0},
            job_id="job-1",
            printer_id="printer-1",
            assessed_by="agent-v1",
            signing_key="test-key",
        )
        assert att.passed is False
        assert att.score.overall == 0.0


# ---------------------------------------------------------------------------
# verify_attestation()
# ---------------------------------------------------------------------------


class TestVerifyAttestation:
    """verify_attestation() integrity and tamper detection."""

    def test_valid_attestation_verifies(self):
        att = assess_quality(
            metrics={"overall": 0.85},
            job_id="job-1",
            printer_id="printer-1",
            assessed_by="agent-v1",
            signing_key="shared-secret",
        )
        assert verify_attestation(att, signing_key="shared-secret") is True

    def test_wrong_key_fails(self):
        att = assess_quality(
            metrics={"overall": 0.85},
            job_id="job-1",
            printer_id="printer-1",
            assessed_by="agent-v1",
            signing_key="correct-key",
        )
        assert verify_attestation(att, signing_key="wrong-key") is False

    def test_tampered_score_fails(self):
        att = assess_quality(
            metrics={"overall": 0.85},
            job_id="job-1",
            printer_id="printer-1",
            assessed_by="agent-v1",
            signing_key="shared-secret",
        )
        # Tamper with the score
        att.score.overall = 0.99
        assert verify_attestation(att, signing_key="shared-secret") is False

    def test_tampered_job_id_fails(self):
        att = assess_quality(
            metrics={"overall": 0.85},
            job_id="job-1",
            printer_id="printer-1",
            assessed_by="agent-v1",
            signing_key="shared-secret",
        )
        att.job_id = "job-999"
        assert verify_attestation(att, signing_key="shared-secret") is False

    def test_tampered_printer_id_fails(self):
        att = assess_quality(
            metrics={"overall": 0.85},
            job_id="job-1",
            printer_id="printer-1",
            assessed_by="agent-v1",
            signing_key="shared-secret",
        )
        att.printer_id = "printer-hacked"
        assert verify_attestation(att, signing_key="shared-secret") is False

    def test_full_roundtrip_with_all_dimensions(self):
        att = assess_quality(
            metrics={
                "overall": 0.9,
                "dimensional_accuracy": 0.95,
                "surface_quality": 0.88,
                "layer_adhesion": 0.91,
                "structural_integrity": 0.94,
            },
            job_id="job-full",
            printer_id="printer-full",
            assessed_by="operator",
            signing_key="full-test-key",
            reference_model_hash="sha256:abcdef",
            notes="Full dimension test",
        )
        assert verify_attestation(att, signing_key="full-test-key") is True
        # Tamper with one dimension
        att.score.surface_quality = 0.5
        assert verify_attestation(att, signing_key="full-test-key") is False
