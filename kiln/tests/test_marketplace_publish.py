"""Tests for kiln.marketplace_publish -- marketplace publishing pipeline.

Covers:
- PublishRequest, PublishResult, PrintCertificate, PublishPipelineResult dataclasses
- validate_publish_request with valid/invalid inputs
- generate_print_certificate with/without print history
- format_certificate_markdown output
- publish_model pipeline end-to-end
- list_published_models query
- _file_hash helper
- Edge cases: missing file, empty tags, invalid license, no marketplaces
"""

from __future__ import annotations

import json
import time
from unittest.mock import patch

import pytest

from kiln.marketplace_publish import (
    PrintCertificate,
    PublishPipelineResult,
    PublishRequest,
    PublishResult,
    _file_hash,
    format_certificate_markdown,
    generate_print_certificate,
    list_published_models,
    publish_model,
    validate_publish_request,
)
from kiln.persistence import KilnDB

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db(tmp_path):
    """Return a KilnDB backed by a temporary file."""
    db_path = str(tmp_path / "test_kiln.db")
    instance = KilnDB(db_path=db_path)
    yield instance
    instance.close()


@pytest.fixture()
def model_file(tmp_path):
    """Create a temporary STL file."""
    p = tmp_path / "test_model.stl"
    p.write_bytes(b"solid test\nendsolid test\n")
    return str(p)


@pytest.fixture()
def image_file(tmp_path):
    """Create a temporary image file."""
    p = tmp_path / "preview.png"
    p.write_bytes(b"\x89PNG\r\n")
    return str(p)


def _make_request(model_file, **overrides):
    """Return a valid PublishRequest with optional overrides."""
    defaults = {
        "file_path": model_file,
        "title": "Test Model",
        "description": "A test model for unit tests.",
        "tags": ["test", "benchy"],
        "category": "tools",
        "license": "cc-by",
        "target_marketplaces": ["thingiverse"],
    }
    defaults.update(overrides)
    return PublishRequest(**defaults)


# ---------------------------------------------------------------------------
# Dataclass tests
# ---------------------------------------------------------------------------


class TestPrintCertificate:
    """PrintCertificate dataclass."""

    def test_to_dict_round_trip(self):
        cert = PrintCertificate(
            file_hash="abc123",
            printer_models_tested=["prusa_mini"],
            materials_tested=["pla"],
            recommended_settings={"layer_height": 0.2},
            success_rate=0.95,
            total_prints=20,
            best_orientation=None,
            print_time_range=(600, 1200),
            created_at=1000.0,
        )
        d = cert.to_dict()
        assert d["file_hash"] == "abc123"
        assert d["success_rate"] == 0.95
        assert d["total_prints"] == 20
        assert d["print_time_range"] == (600, 1200)

    def test_to_dict_with_orientation(self):
        cert = PrintCertificate(
            file_hash="xyz",
            printer_models_tested=[],
            materials_tested=[],
            recommended_settings={},
            success_rate=1.0,
            total_prints=1,
            best_orientation={"x": 0.0, "y": 90.0, "z": 0.0},
            print_time_range=(100, 100),
            created_at=1000.0,
        )
        d = cert.to_dict()
        assert d["best_orientation"] == {"x": 0.0, "y": 90.0, "z": 0.0}


class TestPublishRequest:
    """PublishRequest dataclass."""

    def test_to_dict(self, model_file):
        req = _make_request(model_file)
        d = req.to_dict()
        assert d["title"] == "Test Model"
        assert d["license"] == "cc-by"
        assert d["include_certificate"] is True

    def test_defaults(self, model_file):
        req = _make_request(model_file)
        assert req.include_certificate is True
        assert req.include_print_settings is True
        assert req.images is None


class TestPublishResult:
    """PublishResult dataclass."""

    def test_success_result(self):
        r = PublishResult(
            marketplace="thingiverse",
            success=True,
            listing_url="https://thingiverse.com/thing:12345",
            listing_id="12345",
            error=None,
        )
        d = r.to_dict()
        assert d["success"] is True
        assert d["listing_url"] == "https://thingiverse.com/thing:12345"

    def test_failure_result(self):
        r = PublishResult(
            marketplace="cults3d",
            success=False,
            listing_url=None,
            listing_id=None,
            error="Upload not supported",
        )
        d = r.to_dict()
        assert d["success"] is False
        assert d["error"] == "Upload not supported"


class TestPublishPipelineResult:
    """PublishPipelineResult dataclass."""

    def test_to_dict_with_certificate(self):
        cert = PrintCertificate(
            file_hash="abc",
            printer_models_tested=["ender3"],
            materials_tested=["pla"],
            recommended_settings={},
            success_rate=1.0,
            total_prints=5,
            best_orientation=None,
            print_time_range=(300, 600),
            created_at=1000.0,
        )
        r = PublishPipelineResult(
            results=[
                PublishResult("thingiverse", True, "https://example.com", "123", None),
            ],
            certificate=cert,
            successful_count=1,
            failed_count=0,
        )
        d = r.to_dict()
        assert d["successful_count"] == 1
        assert d["certificate"]["file_hash"] == "abc"
        assert len(d["results"]) == 1

    def test_to_dict_without_certificate(self):
        r = PublishPipelineResult(
            results=[],
            certificate=None,
            successful_count=0,
            failed_count=0,
        )
        d = r.to_dict()
        assert d["certificate"] is None


# ---------------------------------------------------------------------------
# validate_publish_request
# ---------------------------------------------------------------------------


class TestValidatePublishRequest:
    """validate_publish_request validation logic."""

    def test_valid_request(self, model_file):
        req = _make_request(model_file)
        errors = validate_publish_request(req)
        assert errors == []

    def test_missing_file(self, tmp_path):
        req = _make_request(str(tmp_path / "nonexistent.stl"))
        errors = validate_publish_request(req)
        assert any("not found" in e.lower() or "File not found" in e for e in errors)

    def test_empty_file_path(self, model_file):
        req = _make_request(model_file, file_path="")
        errors = validate_publish_request(req)
        assert any("file_path" in e for e in errors)

    def test_empty_title(self, model_file):
        req = _make_request(model_file, title="")
        errors = validate_publish_request(req)
        assert any("title" in e for e in errors)

    def test_empty_description(self, model_file):
        req = _make_request(model_file, description="")
        errors = validate_publish_request(req)
        assert any("description" in e for e in errors)

    def test_no_tags(self, model_file):
        req = _make_request(model_file, tags=[])
        errors = validate_publish_request(req)
        assert any("tag" in e.lower() for e in errors)

    def test_empty_category(self, model_file):
        req = _make_request(model_file, category="")
        errors = validate_publish_request(req)
        assert any("category" in e for e in errors)

    def test_invalid_license(self, model_file):
        req = _make_request(model_file, license="mit")
        errors = validate_publish_request(req)
        assert any("license" in e.lower() or "Invalid" in e for e in errors)

    def test_valid_licenses(self, model_file):
        for lic in ("cc-by", "cc-by-sa", "cc-by-nc", "gpl", "public_domain"):
            req = _make_request(model_file, license=lic)
            errors = validate_publish_request(req)
            assert not any("license" in e.lower() for e in errors)

    def test_no_marketplaces(self, model_file):
        req = _make_request(model_file, target_marketplaces=[])
        errors = validate_publish_request(req)
        assert any("marketplace" in e.lower() for e in errors)

    def test_missing_image_file(self, model_file):
        req = _make_request(model_file, images=["/nonexistent/image.png"])
        errors = validate_publish_request(req)
        assert any("Image not found" in e for e in errors)

    def test_valid_images(self, model_file, image_file):
        req = _make_request(model_file, images=[image_file])
        errors = validate_publish_request(req)
        assert errors == []


# ---------------------------------------------------------------------------
# generate_print_certificate
# ---------------------------------------------------------------------------


class TestGeneratePrintCertificate:
    """generate_print_certificate with mocked DB."""

    def test_no_history_returns_none(self, db, model_file):
        with patch("kiln.persistence.get_db", return_value=db):
            cert = generate_print_certificate(model_file)
        assert cert is None

    def test_file_not_found_returns_none(self):
        cert = generate_print_certificate("/nonexistent/file.stl")
        assert cert is None

    def test_with_print_history(self, db, model_file):
        fhash = _file_hash(model_file)
        # Insert print outcomes.
        with db._write_lock:
            db._conn.execute(
                """INSERT INTO print_outcomes
                   (job_id, printer_name, file_hash, material_type, outcome,
                    settings, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                ("j1", "prusa_mini", fhash, "pla", "success",
                 json.dumps({"layer_height": 0.2}), time.time()),
            )
            db._conn.execute(
                """INSERT INTO print_outcomes
                   (job_id, printer_name, file_hash, material_type, outcome,
                    settings, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                ("j2", "ender3", fhash, "petg", "failure", None, time.time()),
            )
            db._conn.commit()

        with patch("kiln.persistence.get_db", return_value=db):
            cert = generate_print_certificate(model_file)

        assert cert is not None
        assert cert.file_hash == fhash
        assert cert.total_prints == 2
        assert cert.success_rate == 0.5
        assert "prusa_mini" in cert.printer_models_tested
        assert "pla" in cert.materials_tested


# ---------------------------------------------------------------------------
# format_certificate_markdown
# ---------------------------------------------------------------------------


class TestFormatCertificateMarkdown:
    """format_certificate_markdown output."""

    def test_basic_output(self):
        cert = PrintCertificate(
            file_hash="abc",
            printer_models_tested=["prusa_mini", "ender3"],
            materials_tested=["pla", "petg"],
            recommended_settings={"layer_height": 0.2},
            success_rate=0.9,
            total_prints=10,
            best_orientation=None,
            print_time_range=(600, 1200),
            created_at=1000.0,
        )
        md = format_certificate_markdown(cert)
        assert "## Print Certificate" in md
        assert "90%" in md
        assert "prusa_mini" in md
        assert "layer_height" in md
        assert "Powered by [Kiln](https://kiln3d.com)" in md
        assert "open-source bridge between AI and 3D printing" in md

    def test_no_settings(self):
        cert = PrintCertificate(
            file_hash="abc",
            printer_models_tested=[],
            materials_tested=[],
            recommended_settings={},
            success_rate=1.0,
            total_prints=1,
            best_orientation=None,
            print_time_range=(0, 0),
            created_at=1000.0,
        )
        md = format_certificate_markdown(cert)
        assert "## Print Certificate" in md
        assert "100%" in md


# ---------------------------------------------------------------------------
# publish_model
# ---------------------------------------------------------------------------


class TestPublishModel:
    """publish_model pipeline."""

    def test_validation_errors_returned(self, tmp_path):
        req = PublishRequest(
            file_path="",
            title="",
            description="",
            tags=[],
            category="",
            license="invalid",
            target_marketplaces=[],
        )
        result = publish_model(req)
        assert result.failed_count == 1
        assert result.successful_count == 0
        assert result.results[0].marketplace == "validation"
        assert result.results[0].error is not None

    @patch("kiln.marketplace_publish._publish_to_marketplace")
    @patch("kiln.marketplace_publish.generate_print_certificate")
    def test_publish_success(self, mock_cert, mock_publish, db, model_file):
        mock_cert.return_value = None
        mock_publish.return_value = PublishResult(
            marketplace="thingiverse",
            success=True,
            listing_url="https://example.com/12345",
            listing_id="12345",
            error=None,
        )

        req = _make_request(model_file)
        with patch("kiln.persistence.get_db", return_value=db):
            result = publish_model(req)

        assert result.successful_count == 1
        assert result.failed_count == 0

    @patch("kiln.marketplace_publish._publish_to_marketplace")
    @patch("kiln.marketplace_publish.generate_print_certificate")
    def test_publish_failure(self, mock_cert, mock_publish, model_file):
        mock_cert.return_value = None
        mock_publish.return_value = PublishResult(
            marketplace="thingiverse",
            success=False,
            listing_url=None,
            listing_id=None,
            error="Upload failed",
        )

        req = _make_request(model_file)
        result = publish_model(req)
        assert result.successful_count == 0
        assert result.failed_count == 1


# ---------------------------------------------------------------------------
# list_published_models
# ---------------------------------------------------------------------------


class TestListPublishedModels:
    """list_published_models query."""

    def test_empty_db(self, db):
        with patch("kiln.persistence.get_db", return_value=db):
            models = list_published_models()
        assert models == []

    def test_with_data(self, db):
        with db._write_lock:
            db._conn.execute(
                """INSERT INTO published_models
                   (file_hash, marketplace, listing_id, listing_url, title,
                    published_at, certificate)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                ("hash1", "thingiverse", "123", "https://example.com", "Model A",
                 time.time(), None),
            )
            db._conn.commit()

        with patch("kiln.persistence.get_db", return_value=db):
            models = list_published_models()
        assert len(models) == 1
        assert models[0]["title"] == "Model A"

    def test_filter_by_marketplace(self, db):
        now = time.time()
        with db._write_lock:
            db._conn.execute(
                """INSERT INTO published_models
                   (file_hash, marketplace, title, published_at)
                   VALUES (?, ?, ?, ?)""",
                ("h1", "thingiverse", "A", now),
            )
            db._conn.execute(
                """INSERT INTO published_models
                   (file_hash, marketplace, title, published_at)
                   VALUES (?, ?, ?, ?)""",
                ("h2", "myminifactory", "B", now),
            )
            db._conn.commit()

        with patch("kiln.persistence.get_db", return_value=db):
            models = list_published_models(marketplace="thingiverse")
        assert len(models) == 1
        assert models[0]["marketplace"] == "thingiverse"


# ---------------------------------------------------------------------------
# _file_hash helper
# ---------------------------------------------------------------------------


class TestFileHash:
    """_file_hash SHA-256 computation."""

    def test_deterministic(self, model_file):
        h1 = _file_hash(model_file)
        h2 = _file_hash(model_file)
        assert h1 == h2
        assert len(h1) == 64  # SHA-256 hex digest

    def test_different_content(self, tmp_path):
        f1 = tmp_path / "a.stl"
        f2 = tmp_path / "b.stl"
        f1.write_bytes(b"content_a")
        f2.write_bytes(b"content_b")
        assert _file_hash(str(f1)) != _file_hash(str(f2))
