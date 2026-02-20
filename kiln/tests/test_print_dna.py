"""Tests for print_dna module.

Covers model fingerprinting, DNA record storage, settings prediction,
similar model search, model history, and success rate computation.
"""

from __future__ import annotations

import struct
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from kiln.persistence import KilnDB
from kiln.print_dna import (
    ModelFingerprint,
    PrintDNARecord,
    SettingsPrediction,
    find_similar_models,
    fingerprint_model,
    get_model_history,
    get_success_rate,
    predict_settings,
    record_print_dna,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db(tmp_path: Path) -> KilnDB:
    """Fresh database for each test."""
    db_path = str(tmp_path / "test.db")
    instance = KilnDB(db_path=db_path)
    yield instance
    instance.close()


@pytest.fixture(autouse=True)
def _patch_db(db: KilnDB) -> None:
    """Patch get_db to return the test database."""
    with patch("kiln.persistence.get_db", return_value=db):
        yield


def _make_fingerprint(**overrides: Any) -> ModelFingerprint:
    """Create a ModelFingerprint with sensible defaults."""
    defaults = {
        "file_hash": "abc123def456",
        "triangle_count": 100,
        "vertex_count": 50,
        "bounding_box": {"min_x": 0, "max_x": 10, "min_y": 0, "max_y": 10, "min_z": 0, "max_z": 5},
        "surface_area_mm2": 500.0,
        "volume_mm3": 250.0,
        "overhang_ratio": 0.1,
        "complexity_score": 0.3,
        "geometric_signature": "sig123abc",
    }
    defaults.update(overrides)
    return ModelFingerprint(**defaults)


def _write_binary_stl(path: Path, triangles: list[tuple]) -> None:
    """Write a minimal binary STL file with given triangles.

    Each triangle is ((nx,ny,nz), (v0x,v0y,v0z), (v1x,v1y,v1z), (v2x,v2y,v2z)).
    """
    with open(path, "wb") as f:
        f.write(b"\x00" * 80)  # header
        f.write(struct.pack("<I", len(triangles)))
        for normal, v0, v1, v2 in triangles:
            f.write(struct.pack("<fff", *normal))
            f.write(struct.pack("<fff", *v0))
            f.write(struct.pack("<fff", *v1))
            f.write(struct.pack("<fff", *v2))
            f.write(struct.pack("<H", 0))  # attribute byte count


# ---------------------------------------------------------------------------
# ModelFingerprint dataclass
# ---------------------------------------------------------------------------


class TestModelFingerprint:
    def test_to_dict_returns_all_fields(self) -> None:
        fp = _make_fingerprint()
        d = fp.to_dict()
        assert d["file_hash"] == "abc123def456"
        assert d["triangle_count"] == 100
        assert d["vertex_count"] == 50
        assert isinstance(d["bounding_box"], dict)
        assert d["surface_area_mm2"] == 500.0
        assert d["volume_mm3"] == 250.0
        assert d["overhang_ratio"] == 0.1
        assert d["complexity_score"] == 0.3
        assert d["geometric_signature"] == "sig123abc"

    def test_to_dict_is_plain_dict(self) -> None:
        fp = _make_fingerprint()
        d = fp.to_dict()
        assert isinstance(d, dict)


class TestPrintDNARecord:
    def test_to_dict_includes_fingerprint(self) -> None:
        fp = _make_fingerprint()
        record = PrintDNARecord(
            fingerprint=fp,
            printer_model="ender3",
            material="PLA",
            settings={"layer_height": 0.2},
            outcome="success",
            quality_grade="A",
            failure_mode=None,
            print_time_seconds=3600,
            timestamp=time.time(),
        )
        d = record.to_dict()
        assert d["fingerprint"]["file_hash"] == "abc123def456"
        assert d["printer_model"] == "ender3"
        assert d["outcome"] == "success"


class TestSettingsPrediction:
    def test_to_dict(self) -> None:
        pred = SettingsPrediction(
            recommended_settings={"layer_height": 0.2},
            confidence=0.85,
            based_on_prints=10,
            success_rate=0.9,
            similar_models_count=5,
            source="exact_match",
        )
        d = pred.to_dict()
        assert d["confidence"] == 0.85
        assert d["source"] == "exact_match"


# ---------------------------------------------------------------------------
# fingerprint_model — binary STL
# ---------------------------------------------------------------------------


class TestFingerprintModel:
    def test_simple_triangle(self, tmp_path: Path) -> None:
        stl_path = tmp_path / "test.stl"
        triangles = [
            ((0, 0, 1), (0, 0, 0), (10, 0, 0), (5, 10, 0)),
        ]
        _write_binary_stl(stl_path, triangles)

        fp = fingerprint_model(str(stl_path))
        assert fp.triangle_count == 1
        assert fp.vertex_count == 3
        assert len(fp.file_hash) == 64  # SHA-256 hex
        assert fp.surface_area_mm2 > 0
        assert fp.geometric_signature

    def test_cube_like_shape(self, tmp_path: Path) -> None:
        stl_path = tmp_path / "cube.stl"
        # 12 triangles for a cube
        triangles = [
            # front face
            ((0, 0, 1), (0, 0, 0), (10, 0, 0), (10, 10, 0)),
            ((0, 0, 1), (0, 0, 0), (10, 10, 0), (0, 10, 0)),
            # back face
            ((0, 0, -1), (0, 0, 10), (10, 10, 10), (10, 0, 10)),
            ((0, 0, -1), (0, 0, 10), (0, 10, 10), (10, 10, 10)),
            # top face
            ((0, 1, 0), (0, 10, 0), (10, 10, 0), (10, 10, 10)),
            ((0, 1, 0), (0, 10, 0), (10, 10, 10), (0, 10, 10)),
            # bottom face (overhanging normal)
            ((0, -1, 0), (0, 0, 0), (10, 0, 10), (10, 0, 0)),
            ((0, -1, 0), (0, 0, 0), (0, 0, 10), (10, 0, 10)),
            # left face
            ((-1, 0, 0), (0, 0, 0), (0, 10, 0), (0, 10, 10)),
            ((-1, 0, 0), (0, 0, 0), (0, 10, 10), (0, 0, 10)),
            # right face
            ((1, 0, 0), (10, 0, 0), (10, 10, 10), (10, 10, 0)),
            ((1, 0, 0), (10, 0, 0), (10, 0, 10), (10, 10, 10)),
        ]
        _write_binary_stl(stl_path, triangles)

        fp = fingerprint_model(str(stl_path))
        assert fp.triangle_count == 12
        assert fp.bounding_box["min_x"] == 0.0
        assert fp.bounding_box["max_x"] == 10.0
        assert fp.volume_mm3 > 0

    def test_file_not_found(self) -> None:
        with pytest.raises(FileNotFoundError):
            fingerprint_model("/nonexistent/path/file.stl")

    def test_empty_file(self, tmp_path: Path) -> None:
        stl_path = tmp_path / "empty.stl"
        stl_path.write_bytes(b"")
        with pytest.raises(ValueError, match="Empty file"):
            fingerprint_model(str(stl_path))

    def test_too_small_binary_stl(self, tmp_path: Path) -> None:
        stl_path = tmp_path / "small.stl"
        stl_path.write_bytes(b"\x00" * 50)
        with pytest.raises(ValueError, match="too small"):
            fingerprint_model(str(stl_path))

    def test_ascii_stl(self, tmp_path: Path) -> None:
        stl_path = tmp_path / "ascii.stl"
        stl_content = (
            "solid test\n"
            "  facet normal 0 0 1\n"
            "    outer loop\n"
            "      vertex 0 0 0\n"
            "      vertex 10 0 0\n"
            "      vertex 5 10 0\n"
            "    endloop\n"
            "  endfacet\n"
            "endsolid test\n"
        )
        stl_path.write_text(stl_content)

        fp = fingerprint_model(str(stl_path))
        assert fp.triangle_count == 1
        assert fp.vertex_count == 3

    def test_overhang_ratio(self, tmp_path: Path) -> None:
        stl_path = tmp_path / "overhang.stl"
        # Two triangles: one pointing up, one pointing down (overhang)
        triangles = [
            ((0, 0, 1), (0, 0, 0), (10, 0, 0), (5, 10, 0)),
            ((0, 0, -1), (0, 0, 5), (10, 0, 5), (5, 10, 5)),
        ]
        _write_binary_stl(stl_path, triangles)

        fp = fingerprint_model(str(stl_path))
        assert fp.overhang_ratio == 0.5

    def test_deterministic_hash(self, tmp_path: Path) -> None:
        stl_path = tmp_path / "test.stl"
        triangles = [((0, 0, 1), (0, 0, 0), (10, 0, 0), (5, 10, 0))]
        _write_binary_stl(stl_path, triangles)

        fp1 = fingerprint_model(str(stl_path))
        fp2 = fingerprint_model(str(stl_path))
        assert fp1.file_hash == fp2.file_hash
        assert fp1.geometric_signature == fp2.geometric_signature


# ---------------------------------------------------------------------------
# record_print_dna
# ---------------------------------------------------------------------------


class TestRecordPrintDNA:
    def test_record_and_retrieve(self) -> None:
        fp = _make_fingerprint()
        record_print_dna(fp, "ender3", "PLA", {"layer_height": 0.2}, "success")
        history = get_model_history(fp.file_hash)
        assert len(history) == 1
        assert history[0].outcome == "success"
        assert history[0].printer_model == "ender3"

    def test_record_multiple(self) -> None:
        fp = _make_fingerprint()
        record_print_dna(fp, "ender3", "PLA", {}, "success")
        record_print_dna(fp, "voron", "PETG", {}, "failed", failure_mode="spaghetti")
        history = get_model_history(fp.file_hash)
        assert len(history) == 2

    def test_invalid_outcome_raises(self) -> None:
        fp = _make_fingerprint()
        with pytest.raises(ValueError, match="Invalid outcome"):
            record_print_dna(fp, "ender3", "PLA", {}, "great")

    def test_invalid_grade_raises(self) -> None:
        fp = _make_fingerprint()
        with pytest.raises(ValueError, match="Invalid quality_grade"):
            record_print_dna(fp, "ender3", "PLA", {}, "success", quality_grade="Z")

    def test_record_with_failure_mode(self) -> None:
        fp = _make_fingerprint()
        record_print_dna(
            fp,
            "ender3",
            "PLA",
            {},
            "failed",
            quality_grade="F",
            failure_mode="adhesion",
        )
        history = get_model_history(fp.file_hash)
        assert history[0].failure_mode == "adhesion"
        assert history[0].quality_grade == "F"

    def test_record_preserves_settings(self) -> None:
        fp = _make_fingerprint()
        settings = {"layer_height": 0.2, "speed": 60, "temp": 210}
        record_print_dna(fp, "ender3", "PLA", settings, "success")
        history = get_model_history(fp.file_hash)
        assert history[0].settings["layer_height"] == 0.2
        assert history[0].settings["speed"] == 60


# ---------------------------------------------------------------------------
# predict_settings
# ---------------------------------------------------------------------------


class TestPredictSettings:
    def test_exact_match(self) -> None:
        fp = _make_fingerprint()
        record_print_dna(fp, "ender3", "PLA", {"layer_height": 0.2, "speed": 50}, "success")
        record_print_dna(fp, "ender3", "PLA", {"layer_height": 0.2, "speed": 60}, "success")

        prediction = predict_settings(fp, "ender3", "PLA")
        assert prediction.source == "exact_match"
        assert prediction.based_on_prints == 2
        assert "layer_height" in prediction.recommended_settings

    def test_similar_geometry_fallback(self) -> None:
        fp1 = _make_fingerprint(file_hash="hash_a")
        fp2 = _make_fingerprint(file_hash="hash_b")  # same geometric_signature

        record_print_dna(fp1, "ender3", "PLA", {"speed": 50}, "success")

        prediction = predict_settings(fp2, "ender3", "PLA")
        assert prediction.source == "similar_geometry"
        assert prediction.based_on_prints == 1

    def test_material_default_fallback(self) -> None:
        fp1 = _make_fingerprint(file_hash="hash_a", geometric_signature="sig_a")
        fp2 = _make_fingerprint(file_hash="hash_b", geometric_signature="sig_b")

        record_print_dna(fp1, "ender3", "PLA", {"speed": 50}, "success")

        prediction = predict_settings(fp2, "ender3", "PLA")
        assert prediction.source == "material_default"

    def test_no_data(self) -> None:
        fp = _make_fingerprint()
        prediction = predict_settings(fp, "ender3", "PLA")
        assert prediction.source == "no_data"
        assert prediction.based_on_prints == 0
        assert prediction.confidence == 0.0

    def test_ignores_failed_prints(self) -> None:
        fp = _make_fingerprint()
        record_print_dna(fp, "ender3", "PLA", {"speed": 50}, "failed")

        prediction = predict_settings(fp, "ender3", "PLA")
        assert prediction.source == "no_data"


# ---------------------------------------------------------------------------
# find_similar_models
# ---------------------------------------------------------------------------


class TestFindSimilarModels:
    def test_find_by_signature(self) -> None:
        fp1 = _make_fingerprint(file_hash="hash_a")
        fp2 = _make_fingerprint(file_hash="hash_b")  # same sig

        record_print_dna(fp2, "ender3", "PLA", {}, "success")

        results = find_similar_models(fp1, threshold=1.0)
        assert len(results) == 1
        assert results[0].fingerprint.file_hash == "hash_b"

    def test_no_similar_models(self) -> None:
        fp = _make_fingerprint(geometric_signature="unique_sig")
        results = find_similar_models(fp)
        assert len(results) == 0

    def test_excludes_self(self) -> None:
        fp = _make_fingerprint()
        record_print_dna(fp, "ender3", "PLA", {}, "success")

        results = find_similar_models(fp, threshold=1.0)
        assert len(results) == 0

    def test_fuzzy_threshold(self) -> None:
        fp1 = _make_fingerprint(
            file_hash="hash_a",
            geometric_signature="sig_a",
            surface_area_mm2=500.0,
            volume_mm3=250.0,
            complexity_score=0.3,
        )
        fp2 = _make_fingerprint(
            file_hash="hash_b",
            geometric_signature="sig_b",
            surface_area_mm2=510.0,
            volume_mm3=260.0,
            complexity_score=0.32,
        )
        record_print_dna(fp2, "ender3", "PLA", {}, "success")

        results = find_similar_models(fp1, threshold=0.8)
        assert len(results) == 1

    def test_limit(self) -> None:
        fp_query = _make_fingerprint(file_hash="query_hash")
        for i in range(5):
            fp = _make_fingerprint(file_hash=f"hash_{i}")
            record_print_dna(fp, "ender3", "PLA", {}, "success")

        results = find_similar_models(fp_query, limit=3, threshold=1.0)
        assert len(results) <= 3


# ---------------------------------------------------------------------------
# get_model_history
# ---------------------------------------------------------------------------


class TestGetModelHistory:
    def test_empty_history(self) -> None:
        history = get_model_history("nonexistent_hash")
        assert history == []

    def test_ordered_by_timestamp_desc(self) -> None:
        fp = _make_fingerprint()
        record_print_dna(fp, "ender3", "PLA", {}, "success")
        record_print_dna(fp, "voron", "PETG", {}, "failed")

        history = get_model_history(fp.file_hash)
        assert len(history) == 2
        assert history[0].timestamp >= history[1].timestamp


# ---------------------------------------------------------------------------
# get_success_rate
# ---------------------------------------------------------------------------


class TestGetSuccessRate:
    def test_no_prints(self) -> None:
        rate = get_success_rate("nonexistent")
        assert rate["total_prints"] == 0
        assert rate["success_rate"] == 0.0

    def test_all_successful(self) -> None:
        fp = _make_fingerprint()
        record_print_dna(fp, "ender3", "PLA", {}, "success", quality_grade="A")
        record_print_dna(fp, "ender3", "PLA", {}, "success", quality_grade="B")

        rate = get_success_rate(fp.file_hash)
        assert rate["total_prints"] == 2
        assert rate["success_rate"] == 1.0
        assert rate["outcomes"]["success"] == 2

    def test_mixed_outcomes(self) -> None:
        fp = _make_fingerprint()
        record_print_dna(fp, "ender3", "PLA", {}, "success")
        record_print_dna(fp, "ender3", "PLA", {}, "failed")

        rate = get_success_rate(fp.file_hash)
        assert rate["total_prints"] == 2
        assert rate["success_rate"] == 0.5

    def test_filter_by_printer(self) -> None:
        fp = _make_fingerprint()
        record_print_dna(fp, "ender3", "PLA", {}, "success")
        record_print_dna(fp, "voron", "PLA", {}, "failed")

        rate = get_success_rate(fp.file_hash, printer_model="ender3")
        assert rate["total_prints"] == 1
        assert rate["success_rate"] == 1.0

    def test_filter_by_material(self) -> None:
        fp = _make_fingerprint()
        record_print_dna(fp, "ender3", "PLA", {}, "success")
        record_print_dna(fp, "ender3", "PETG", {}, "failed")

        rate = get_success_rate(fp.file_hash, material="PLA")
        assert rate["total_prints"] == 1
        assert rate["success_rate"] == 1.0

    def test_grade_distribution(self) -> None:
        fp = _make_fingerprint()
        record_print_dna(fp, "ender3", "PLA", {}, "success", quality_grade="A")
        record_print_dna(fp, "ender3", "PLA", {}, "success", quality_grade="A")
        record_print_dna(fp, "ender3", "PLA", {}, "success", quality_grade="C")

        rate = get_success_rate(fp.file_hash)
        assert rate["grade_distribution"]["A"] == 2
        assert rate["grade_distribution"]["C"] == 1
