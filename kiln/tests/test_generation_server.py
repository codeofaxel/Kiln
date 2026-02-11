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
    _generation_providers,
    _get_generation_provider,
    await_generation,
    download_generated_model,
    generate_and_print,
    generate_model,
    generation_status,
    list_generation_providers,
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
        mock_adapter.return_value = adapter

        result = generate_and_print("a cube", provider="meshy")
        assert result["success"] is True
        assert "generation" in result
        assert "slice" in result
        assert "upload" in result
        assert result["experimental"] is True
        assert result["ready_to_print"] is True
        assert "safety_notice" in result
        assert "start_print" in result["message"]
        provider.generate.assert_called_once()
        # validate_mesh called twice: once in pipeline step 4, once for preview
        assert mock_validate.call_count == 2
        mock_slice.assert_called_once()
        adapter.upload_file.assert_called_once()
        # start_print should NOT be called — requires explicit user action
        adapter.start_print.assert_not_called()

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


# ---------------------------------------------------------------------------
# list_generation_providers()
# ---------------------------------------------------------------------------


class TestListGenerationProviders:
    """Tests for the list_generation_providers MCP tool."""

    def test_returns_provider_list(self):
        result = list_generation_providers()
        assert result["success"] is True
        assert isinstance(result["providers"], list)
        assert len(result["providers"]) >= 2

    def test_meshy_provider_metadata(self):
        result = list_generation_providers()
        meshy = next(p for p in result["providers"] if p["name"] == "meshy")
        assert meshy["display_name"] == "Meshy"
        assert meshy["requires_api_key"] is True
        assert meshy["api_key_env"] == "KILN_MESHY_API_KEY"
        assert isinstance(meshy["api_key_set"], bool)
        assert "realistic" in meshy["styles"]
        assert "sculpture" in meshy["styles"]
        assert meshy["async"] is True

    def test_openscad_provider_metadata(self):
        result = list_generation_providers()
        openscad = next(p for p in result["providers"] if p["name"] == "openscad")
        assert openscad["display_name"] == "OpenSCAD"
        assert openscad["requires_api_key"] is False
        assert openscad["styles"] == []
        assert openscad["async"] is False


# ---------------------------------------------------------------------------
# Provider singleton caching
# ---------------------------------------------------------------------------


class TestProviderCaching:
    """Tests for _get_generation_provider singleton cache."""

    def test_same_provider_returns_same_instance(self):
        """Calling with the same name twice returns the cached instance."""
        _generation_providers.clear()
        with patch("kiln.server.MeshyProvider") as MockMeshy:
            instance = MagicMock()
            MockMeshy.return_value = instance
            first = _get_generation_provider("meshy")
            second = _get_generation_provider("meshy")
        assert first is second
        # Constructor called only once.
        assert MockMeshy.call_count == 1
        _generation_providers.clear()

    def test_different_providers_are_distinct(self):
        """Different provider names return different instances."""
        _generation_providers.clear()
        with patch("kiln.server.MeshyProvider") as MockMeshy, \
             patch("kiln.server.OpenSCADProvider") as MockOpenSCAD:
            MockMeshy.return_value = MagicMock()
            MockOpenSCAD.return_value = MagicMock()
            meshy = _get_generation_provider("meshy")
            openscad = _get_generation_provider("openscad")
        assert meshy is not openscad
        _generation_providers.clear()

    def test_unknown_provider_raises(self):
        """Unknown provider name raises GenerationError."""
        _generation_providers.clear()
        from kiln.generation.base import GenerationError
        with pytest.raises(GenerationError, match="Unknown generation provider"):
            _get_generation_provider("nonexistent")
        _generation_providers.clear()


# ---------------------------------------------------------------------------
# Dimensions in download response
# ---------------------------------------------------------------------------


class TestDownloadDimensions:
    """Tests for dimensions dict returned by download_generated_model."""

    @_AUTH_PATCH
    @patch("kiln.server.validate_mesh")
    @patch("kiln.server._get_generation_provider")
    def test_dimensions_present_for_stl(self, mock_get_provider, mock_validate, _auth):
        """Download of STL should include dimensions from bounding box."""
        provider = MagicMock()
        provider.download_result.return_value = _make_result(fmt="stl")
        mock_get_provider.return_value = provider

        mock_validate.return_value = MeshValidationResult(
            valid=True,
            errors=[],
            warnings=[],
            triangle_count=100,
            vertex_count=50,
            is_manifold=True,
            bounding_box={
                "x_min": 0.0, "x_max": 20.0,
                "y_min": 5.0, "y_max": 15.0,
                "z_min": 0.0, "z_max": 30.0,
            },
        )

        result = download_generated_model("test-job-123", provider="meshy")
        assert result["success"] is True
        dims = result["dimensions"]
        assert dims is not None
        assert dims["width_mm"] == 20.0
        assert dims["depth_mm"] == 10.0
        assert dims["height_mm"] == 30.0
        assert "20.0" in dims["summary"]
        assert "10.0" in dims["summary"]
        assert "30.0" in dims["summary"]

    @_AUTH_PATCH
    @patch("kiln.server.validate_mesh")
    @patch("kiln.server._get_generation_provider")
    def test_dimensions_none_for_non_stl(self, mock_get_provider, mock_validate, _auth):
        """GLB format should have no dimensions (no validation)."""
        provider = MagicMock()
        provider.download_result.return_value = _make_result(fmt="glb")
        mock_get_provider.return_value = provider

        result = download_generated_model("test-job-123")
        assert result["success"] is True
        assert result["dimensions"] is None

    @_AUTH_PATCH
    @patch("kiln.server.validate_mesh")
    @patch("kiln.server._get_generation_provider")
    def test_dimensions_none_when_no_bounding_box(self, mock_get_provider, mock_validate, _auth):
        """If validation has no bounding box, dimensions should be None."""
        provider = MagicMock()
        provider.download_result.return_value = _make_result(fmt="stl")
        mock_get_provider.return_value = provider

        mock_validate.return_value = MeshValidationResult(
            valid=True,
            errors=[],
            warnings=[],
            triangle_count=100,
            vertex_count=50,
            is_manifold=True,
            bounding_box=None,
        )

        result = download_generated_model("test-job-123")
        assert result["success"] is True
        assert result["dimensions"] is None


# ---------------------------------------------------------------------------
# OBJ -> STL auto-conversion in download pipeline
# ---------------------------------------------------------------------------


class TestObjToStlAutoConversion:
    """Tests for auto-convert OBJ to STL in download_generated_model."""

    @_AUTH_PATCH
    @patch("kiln.server.os.path.getsize", return_value=50000)
    @patch("kiln.server.convert_to_stl")
    @patch("kiln.server.validate_mesh")
    @patch("kiln.server._get_generation_provider")
    def test_obj_auto_converted_to_stl(
        self, mock_get_provider, mock_validate, mock_convert, mock_getsize, _auth,
    ):
        """When provider returns OBJ, it is automatically converted to STL."""
        provider = MagicMock()
        provider.download_result.return_value = _make_result(
            fmt="obj", local_path="/tmp/kiln_generated/model.obj",
        )
        mock_get_provider.return_value = provider
        mock_convert.return_value = "/tmp/kiln_generated/model.stl"
        mock_validate.return_value = MeshValidationResult(
            valid=True,
            errors=[],
            warnings=[],
            triangle_count=100,
            vertex_count=50,
            is_manifold=True,
            bounding_box={
                "x_min": 0.0, "x_max": 10.0,
                "y_min": 0.0, "y_max": 10.0,
                "z_min": 0.0, "z_max": 10.0,
            },
        )

        result = download_generated_model("test-job-123")
        assert result["success"] is True
        mock_convert.assert_called_once_with("/tmp/kiln_generated/model.obj")
        # After conversion, the result should reference the STL.
        assert result["result"]["format"] == "stl"
        assert result["result"]["local_path"] == "/tmp/kiln_generated/model.stl"

    @_AUTH_PATCH
    @patch("kiln.server.convert_to_stl")
    @patch("kiln.server.validate_mesh")
    @patch("kiln.server._get_generation_provider")
    def test_obj_conversion_failure_keeps_obj(
        self, mock_get_provider, mock_validate, mock_convert, _auth,
    ):
        """If OBJ->STL conversion fails, the original OBJ is kept."""
        provider = MagicMock()
        provider.download_result.return_value = _make_result(
            fmt="obj", local_path="/tmp/kiln_generated/model.obj",
        )
        mock_get_provider.return_value = provider
        mock_convert.side_effect = ValueError("parse error")
        mock_validate.return_value = MeshValidationResult(
            valid=True,
            errors=[],
            warnings=[],
            triangle_count=100,
            vertex_count=50,
            is_manifold=True,
            bounding_box={
                "x_min": 0.0, "x_max": 10.0,
                "y_min": 0.0, "y_max": 10.0,
                "z_min": 0.0, "z_max": 10.0,
            },
        )

        result = download_generated_model("test-job-123")
        assert result["success"] is True
        # Should fall back to OBJ.
        assert result["result"]["format"] == "obj"
        assert result["result"]["local_path"] == "/tmp/kiln_generated/model.obj"

    @_AUTH_PATCH
    @patch("kiln.server.convert_to_stl")
    @patch("kiln.server.validate_mesh")
    @patch("kiln.server._get_generation_provider")
    def test_stl_format_not_converted(
        self, mock_get_provider, mock_validate, mock_convert, _auth,
    ):
        """STL results are not passed through convert_to_stl."""
        provider = MagicMock()
        provider.download_result.return_value = _make_result(fmt="stl")
        mock_get_provider.return_value = provider
        mock_validate.return_value = _make_validation(valid=True)

        result = download_generated_model("test-job-123")
        assert result["success"] is True
        mock_convert.assert_not_called()
