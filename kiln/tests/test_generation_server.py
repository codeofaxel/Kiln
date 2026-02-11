"""Tests for Model Generation MCP tools in kiln.server.

Covers:
- generate_model() — submit generation jobs
- generation_status() — poll job status
- download_generated_model() — download + optional validation
- await_generation() — blocking wait for completion
- generate_and_print() — full end-to-end pipeline
- validate_generated_mesh() — mesh validation tool
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

from kiln.generation.base import (
    GenerationAuthError,
    GenerationError,
    GenerationJob,
    GenerationResult,
    GenerationStatus,
    MeshValidationResult,
)
from kiln.printers.base import (
    PrinterError,
    PrintResult,
    UploadResult,
)
from kiln.server import (
    _error_dict,
    _get_generation_provider,
    await_generation,
    download_generated_model,
    generate_and_print,
    generate_model,
    generation_status,
    validate_generated_mesh,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_job(
    *,
    job_id: str = "test-job-123",
    provider: str = "meshy",
    prompt: str = "a cube",
    status: GenerationStatus = GenerationStatus.PENDING,
    progress: int = 0,
    fmt: str = "stl",
    error: str | None = None,
) -> GenerationJob:
    return GenerationJob(
        id=job_id,
        provider=provider,
        prompt=prompt,
        status=status,
        progress=progress,
        created_at=1000.0,
        format=fmt,
        error=error,
    )


def _make_result(
    *,
    job_id: str = "test-job-123",
    provider: str = "meshy",
    local_path: str = "/tmp/kiln_generated/model.stl",
    fmt: str = "stl",
    file_size_bytes: int = 42000,
    prompt: str = "a cube",
) -> GenerationResult:
    return GenerationResult(
        job_id=job_id,
        provider=provider,
        local_path=local_path,
        format=fmt,
        file_size_bytes=file_size_bytes,
        prompt=prompt,
    )


def _make_validation(*, valid: bool = True, errors: list | None = None) -> MeshValidationResult:
    return MeshValidationResult(
        valid=valid,
        errors=errors or [],
        warnings=[],
        triangle_count=1000,
        vertex_count=500,
        is_manifold=valid,
        bounding_box={"x": 10.0, "y": 10.0, "z": 10.0},
    )


# We disable auth for all tests via _check_auth returning None.
_AUTH_PATCH = patch("kiln.server._check_auth", return_value=None)


# ---------------------------------------------------------------------------
# generate_model()
# ---------------------------------------------------------------------------


class TestGenerateModel:
    """Tests for the generate_model MCP tool."""

    @_AUTH_PATCH
    @patch("kiln.server._get_generation_provider")
    def test_success(self, mock_get_provider, _auth):
        provider = MagicMock()
        provider.display_name = "Meshy"
        provider.generate.return_value = _make_job()
        mock_get_provider.return_value = provider

        result = generate_model("a cube", provider="meshy")
        assert result["success"] is True
        assert result["job"]["id"] == "test-job-123"
        assert result["job"]["status"] == "pending"
        assert "Meshy" in result["message"]
        provider.generate.assert_called_once_with("a cube", format="stl", style=None)

    @_AUTH_PATCH
    @patch("kiln.server._get_generation_provider")
    def test_auth_error(self, mock_get_provider, _auth):
        mock_get_provider.side_effect = GenerationAuthError("Missing API key")

        result = generate_model("a cube")
        assert result["success"] is False
        assert result["error"]["code"] == "AUTH_ERROR"
        assert "Missing API key" in result["error"]["message"]

    @_AUTH_PATCH
    @patch("kiln.server._get_generation_provider")
    def test_generation_error(self, mock_get_provider, _auth):
        mock_get_provider.side_effect = GenerationError("rate limit", code="RATE_LIMIT")

        result = generate_model("a cube")
        assert result["success"] is False
        assert result["error"]["code"] == "RATE_LIMIT"

    @_AUTH_PATCH
    @patch("kiln.server._get_generation_provider")
    def test_unexpected_error(self, mock_get_provider, _auth):
        mock_get_provider.side_effect = ValueError("something weird")

        result = generate_model("a cube")
        assert result["success"] is False
        assert result["error"]["code"] == "INTERNAL_ERROR"


# ---------------------------------------------------------------------------
# generation_status()
# ---------------------------------------------------------------------------


class TestGenerationStatus:
    """Tests for the generation_status MCP tool."""

    @_AUTH_PATCH
    @patch("kiln.server._get_generation_provider")
    def test_success(self, mock_get_provider, _auth):
        provider = MagicMock()
        provider.get_job_status.return_value = _make_job(
            status=GenerationStatus.SUCCEEDED, progress=100,
        )
        mock_get_provider.return_value = provider

        result = generation_status("test-job-123", provider="meshy")
        assert result["success"] is True
        assert result["job"]["status"] == "succeeded"
        assert result["job"]["progress"] == 100

    @_AUTH_PATCH
    @patch("kiln.server._get_generation_provider")
    def test_job_not_found(self, mock_get_provider, _auth):
        provider = MagicMock()
        provider.get_job_status.side_effect = GenerationError(
            "Job not found", code="NOT_FOUND",
        )
        mock_get_provider.return_value = provider

        result = generation_status("bad-id")
        assert result["success"] is False
        assert result["error"]["code"] == "NOT_FOUND"

    @_AUTH_PATCH
    @patch("kiln.server._get_generation_provider")
    def test_auth_error(self, mock_get_provider, _auth):
        mock_get_provider.side_effect = GenerationAuthError("Invalid key")

        result = generation_status("test-job-123")
        assert result["success"] is False
        assert result["error"]["code"] == "AUTH_ERROR"


# ---------------------------------------------------------------------------
# download_generated_model()
# ---------------------------------------------------------------------------


class TestDownloadGeneratedModel:
    """Tests for the download_generated_model MCP tool."""

    @_AUTH_PATCH
    @patch("kiln.server.validate_mesh")
    @patch("kiln.server._get_generation_provider")
    def test_success_with_validation(self, mock_get_provider, mock_validate, _auth):
        provider = MagicMock()
        provider.download_result.return_value = _make_result(fmt="stl")
        mock_get_provider.return_value = provider
        mock_validate.return_value = _make_validation(valid=True)

        result = download_generated_model("test-job-123", provider="meshy")
        assert result["success"] is True
        assert result["result"]["local_path"] == "/tmp/kiln_generated/model.stl"
        assert result["validation"] is not None
        assert result["validation"]["valid"] is True
        mock_validate.assert_called_once_with("/tmp/kiln_generated/model.stl")

    @_AUTH_PATCH
    @patch("kiln.server.validate_mesh")
    @patch("kiln.server._get_generation_provider")
    def test_success_non_stl_skips_validation(self, mock_get_provider, mock_validate, _auth):
        provider = MagicMock()
        provider.download_result.return_value = _make_result(fmt="glb")
        mock_get_provider.return_value = provider

        result = download_generated_model("test-job-123")
        assert result["success"] is True
        assert result["validation"] is None
        mock_validate.assert_not_called()

    @_AUTH_PATCH
    @patch("kiln.server._get_generation_provider")
    def test_download_error(self, mock_get_provider, _auth):
        provider = MagicMock()
        provider.download_result.side_effect = GenerationError(
            "Download failed", code="DOWNLOAD_ERROR",
        )
        mock_get_provider.return_value = provider

        result = download_generated_model("test-job-123")
        assert result["success"] is False
        assert result["error"]["code"] == "DOWNLOAD_ERROR"

    @_AUTH_PATCH
    @patch("kiln.server._get_generation_provider")
    def test_auth_error(self, mock_get_provider, _auth):
        mock_get_provider.side_effect = GenerationAuthError("Expired key")

        result = download_generated_model("test-job-123")
        assert result["success"] is False
        assert result["error"]["code"] == "AUTH_ERROR"


# ---------------------------------------------------------------------------
# await_generation()
# ---------------------------------------------------------------------------


class TestAwaitGeneration:
    """Tests for the await_generation MCP tool."""

    @_AUTH_PATCH
    @patch("kiln.server.time")
    @patch("kiln.server._get_generation_provider")
    def test_completed(self, mock_get_provider, mock_time, _auth):
        """Job transitions from IN_PROGRESS to SUCCEEDED."""
        provider = MagicMock()
        provider.get_job_status.side_effect = [
            _make_job(status=GenerationStatus.IN_PROGRESS, progress=50),
            _make_job(status=GenerationStatus.SUCCEEDED, progress=100),
        ]
        mock_get_provider.return_value = provider
        # Simulate time passing: start=0, first check=0.1, second check=0.2
        mock_time.time.side_effect = [0.0, 0.1, 0.2]
        mock_time.sleep = MagicMock()

        result = await_generation("test-job-123", timeout=60, poll_interval=1)
        assert result["success"] is True
        assert result["outcome"] == "completed"
        assert result["job"]["status"] == "succeeded"
        assert len(result["progress_log"]) == 2

    @_AUTH_PATCH
    @patch("kiln.server.time")
    @patch("kiln.server._get_generation_provider")
    def test_failed(self, mock_get_provider, mock_time, _auth):
        """Job fails immediately."""
        provider = MagicMock()
        provider.get_job_status.return_value = _make_job(
            status=GenerationStatus.FAILED,
            error="Server error",
        )
        mock_get_provider.return_value = provider
        mock_time.time.side_effect = [0.0, 0.1]
        mock_time.sleep = MagicMock()

        result = await_generation("test-job-123", timeout=60)
        assert result["success"] is True
        assert result["outcome"] == "failed"
        assert result["error"] == "Server error"

    @_AUTH_PATCH
    @patch("kiln.server.time")
    @patch("kiln.server._get_generation_provider")
    def test_timeout(self, mock_get_provider, mock_time, _auth):
        """Job never completes within the timeout."""
        provider = MagicMock()
        provider.get_job_status.return_value = _make_job(
            status=GenerationStatus.IN_PROGRESS, progress=30,
        )
        mock_get_provider.return_value = provider
        # First call is start, then elapsed is past timeout on the while-loop check
        mock_time.time.side_effect = [0.0, 999.0]
        mock_time.sleep = MagicMock()

        result = await_generation("test-job-123", timeout=5)
        assert result["success"] is True
        assert result["outcome"] == "timeout"
        assert "Timed out" in result["message"]

    @_AUTH_PATCH
    @patch("kiln.server.time")
    @patch("kiln.server._get_generation_provider")
    def test_cancelled(self, mock_get_provider, mock_time, _auth):
        """Job is cancelled."""
        provider = MagicMock()
        provider.get_job_status.return_value = _make_job(
            status=GenerationStatus.CANCELLED,
        )
        mock_get_provider.return_value = provider
        mock_time.time.side_effect = [0.0, 0.1]
        mock_time.sleep = MagicMock()

        result = await_generation("test-job-123", timeout=60)
        assert result["success"] is True
        assert result["outcome"] == "cancelled"


# ---------------------------------------------------------------------------
# generate_and_print()
# ---------------------------------------------------------------------------


class TestGenerateAndPrint:
    """Tests for the generate_and_print MCP tool (full pipeline)."""

    @_AUTH_PATCH
    @patch("kiln.server._get_adapter")
    @patch("kiln.slicer.slice_file")
    @patch("kiln.server.validate_mesh")
    @patch("kiln.server._get_generation_provider")
    def test_full_pipeline(self, mock_get_provider, mock_validate, mock_slice, mock_adapter, _auth):
        """Synchronous provider (OpenSCAD-like) completes immediately."""
        provider = MagicMock()
        provider.display_name = "OpenSCAD"
        provider.generate.return_value = _make_job(status=GenerationStatus.SUCCEEDED)
        provider.download_result.return_value = _make_result()
        mock_get_provider.return_value = provider

        mock_validate.return_value = _make_validation(valid=True)

        slice_result = MagicMock()
        slice_result.output_path = "/tmp/sliced/model.gcode"
        slice_result.to_dict.return_value = {"success": True, "output_path": "/tmp/sliced/model.gcode"}
        mock_slice.return_value = slice_result

        adapter = MagicMock()
        adapter.upload_file.return_value = UploadResult(
            success=True, file_name="model.gcode", message="Uploaded",
        )
        adapter.start_print.return_value = PrintResult(success=True, message="Printing")
        mock_adapter.return_value = adapter

        result = generate_and_print("a cube", provider="meshy")
        assert result["success"] is True
        assert "generation" in result
        assert "slice" in result
        assert "upload" in result
        assert "print" in result
        provider.generate.assert_called_once()
        mock_validate.assert_called_once()
        mock_slice.assert_called_once()
        adapter.upload_file.assert_called_once()
        adapter.start_print.assert_called_once()

    @_AUTH_PATCH
    @patch("kiln.server.time")
    @patch("kiln.server._get_generation_provider")
    def test_generation_fails(self, mock_get_provider, mock_time, _auth):
        """Async provider returns FAILED during polling."""
        provider = MagicMock()
        provider.display_name = "Meshy"
        provider.generate.return_value = _make_job(status=GenerationStatus.PENDING)
        provider.get_job_status.return_value = _make_job(
            status=GenerationStatus.FAILED,
            error="GPU OOM",
        )
        mock_get_provider.return_value = provider
        mock_time.time.side_effect = [0.0, 0.1]
        mock_time.sleep = MagicMock()

        result = generate_and_print("complex model")
        assert result["success"] is False
        assert result["error"]["code"] == "GENERATION_FAILED"

    @_AUTH_PATCH
    @patch("kiln.server.validate_mesh")
    @patch("kiln.server._get_generation_provider")
    def test_validation_fails(self, mock_get_provider, mock_validate, _auth):
        """Generated mesh fails validation — should not proceed to slice."""
        provider = MagicMock()
        provider.display_name = "Meshy"
        provider.generate.return_value = _make_job(status=GenerationStatus.SUCCEEDED)
        provider.download_result.return_value = _make_result()
        mock_get_provider.return_value = provider

        mock_validate.return_value = _make_validation(
            valid=False, errors=["Non-manifold edges"],
        )

        result = generate_and_print("a cube")
        assert result["success"] is False
        assert result["error"]["code"] == "VALIDATION_FAILED"
        assert "Non-manifold" in result["error"]["message"]

    @_AUTH_PATCH
    @patch("kiln.server._registry")
    @patch("kiln.server._get_generation_provider")
    def test_printer_not_found(self, mock_get_provider, mock_registry, _auth):
        from kiln.registry import PrinterNotFoundError

        provider = MagicMock()
        provider.display_name = "Meshy"
        provider.generate.return_value = _make_job(status=GenerationStatus.SUCCEEDED)
        provider.download_result.return_value = _make_result()
        mock_get_provider.return_value = provider

        mock_registry.get.side_effect = PrinterNotFoundError("no-printer")

        # We also need validate_mesh to pass so the pipeline reaches the printer step
        with patch("kiln.server.validate_mesh", return_value=_make_validation(valid=True)), \
             patch("kiln.slicer.slice_file") as mock_slice:
            slice_result = MagicMock()
            slice_result.output_path = "/tmp/sliced/model.gcode"
            mock_slice.return_value = slice_result

            result = generate_and_print("a cube", printer_name="no-printer")

        assert result["success"] is False
        assert result["error"]["code"] == "NOT_FOUND"

    @_AUTH_PATCH
    @patch("kiln.server.time")
    @patch("kiln.server._get_generation_provider")
    def test_generation_timeout(self, mock_get_provider, mock_time, _auth):
        """Generation times out during polling in generate_and_print."""
        provider = MagicMock()
        provider.display_name = "Meshy"
        provider.generate.return_value = _make_job(status=GenerationStatus.PENDING)
        provider.get_job_status.return_value = _make_job(
            status=GenerationStatus.IN_PROGRESS, progress=10,
        )
        mock_get_provider.return_value = provider
        # First time.time() is start, second exceeds timeout
        mock_time.time.side_effect = [0.0, 999.0]
        mock_time.sleep = MagicMock()

        result = generate_and_print("a cube", timeout=5)
        assert result["success"] is False
        assert result["error"]["code"] == "GENERATION_TIMEOUT"


# ---------------------------------------------------------------------------
# validate_generated_mesh()
# ---------------------------------------------------------------------------


class TestValidateGeneratedMesh:
    """Tests for the validate_generated_mesh MCP tool."""

    @patch("kiln.server.validate_mesh")
    def test_valid_mesh(self, mock_validate):
        mock_validate.return_value = _make_validation(valid=True)

        result = validate_generated_mesh("/tmp/model.stl")
        assert result["success"] is True
        assert result["validation"]["valid"] is True
        assert "valid" in result["message"].lower()

    @patch("kiln.server.validate_mesh")
    def test_invalid_mesh(self, mock_validate):
        mock_validate.return_value = _make_validation(
            valid=False, errors=["Zero volume", "Non-manifold edges"],
        )

        result = validate_generated_mesh("/tmp/model.stl")
        assert result["success"] is True
        assert result["validation"]["valid"] is False
        assert "Zero volume" in result["message"]
        assert "Non-manifold" in result["message"]
