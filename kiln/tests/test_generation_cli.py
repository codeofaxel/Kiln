"""Tests for Model Generation CLI commands in kiln.cli.main.

Covers:
- generate — submit and optionally wait for a generation job
- generate-status — check job status
- generate-download — download completed model with optional validation
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from kiln.cli.main import cli
from kiln.generation.base import (
    GenerationAuthError,
    GenerationError,
    GenerationJob,
    GenerationResult,
    GenerationStatus,
    MeshValidationResult,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def runner():
    return CliRunner()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_job(
    *,
    job_id: str = "gen-abc-123",
    provider: str = "meshy",
    prompt: str = "a phone stand",
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
    job_id: str = "gen-abc-123",
    provider: str = "meshy",
    local_path: str = "/tmp/kiln_generated/model.stl",
    fmt: str = "stl",
    file_size_bytes: int = 42000,
    prompt: str = "a phone stand",
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
        triangle_count=1200,
        vertex_count=600,
        is_manifold=valid,
        bounding_box={"x": 20.0, "y": 20.0, "z": 10.0},
    )


# ---------------------------------------------------------------------------
# generate
# ---------------------------------------------------------------------------


class TestGenerate:
    """Tests for the 'kiln generate' CLI command."""

    def test_generate_meshy_no_wait_json(self, runner):
        """Submit an async job and return the job ID without waiting."""
        with patch("kiln.generation.MeshyProvider") as MockProvider:
            provider = MockProvider.return_value
            provider.display_name = "Meshy"
            provider.generate.return_value = _make_job(
                status=GenerationStatus.PENDING, progress=0,
            )

            result = runner.invoke(cli, [
                "generate", "a phone stand", "--provider", "meshy", "--json",
            ])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "success"
        assert data["data"]["job"]["id"] == "gen-abc-123"
        assert data["data"]["job"]["status"] == "pending"

    def test_generate_openscad_json(self, runner):
        """OpenSCAD completes synchronously — result returned immediately."""
        with patch("kiln.generation.OpenSCADProvider") as MockProvider, \
             patch("kiln.generation.validate_mesh") as mock_validate:
            provider = MockProvider.return_value
            provider.display_name = "OpenSCAD"
            provider.generate.return_value = _make_job(
                provider="openscad",
                prompt="cube([10,10,10]);",
                status=GenerationStatus.SUCCEEDED,
                progress=100,
            )
            provider.download_result.return_value = _make_result(
                provider="openscad",
                prompt="cube([10,10,10]);",
            )
            mock_validate.return_value = _make_validation(valid=True)

            result = runner.invoke(cli, [
                "generate", "cube([10,10,10]);", "--provider", "openscad", "--json",
            ])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "success"
        assert "result" in data["data"]
        assert "validation" in data["data"]
        assert data["data"]["validation"]["valid"] is True

    def test_generate_auth_error(self, runner):
        """Auth error returns non-zero exit and error JSON."""
        with patch("kiln.generation.MeshyProvider") as MockProvider:
            MockProvider.return_value.generate.side_effect = GenerationAuthError(
                "KILN_MESHY_API_KEY not set",
            )

            result = runner.invoke(cli, [
                "generate", "a cube", "--provider", "meshy", "--json",
            ])

        assert result.exit_code != 0
        data = json.loads(result.output)
        assert data["status"] == "error"
        assert "AUTH_ERROR" in data.get("code", "") or "API_KEY" in result.output.upper() or "AUTH" in result.output.upper()

    def test_generate_wait_timeout(self, runner):
        """--wait with a short timeout exits with error."""
        with patch("kiln.generation.MeshyProvider") as MockProvider, \
             patch("time.time") as mock_time_fn, \
             patch("time.sleep"):
            provider = MockProvider.return_value
            provider.display_name = "Meshy"
            provider.generate.return_value = _make_job(
                status=GenerationStatus.PENDING,
            )
            provider.get_job_status.return_value = _make_job(
                status=GenerationStatus.IN_PROGRESS, progress=10,
            )
            # Simulate time exceeding timeout
            mock_time_fn.side_effect = [0.0, 999.0]

            result = runner.invoke(cli, [
                "generate", "a cube", "--provider", "meshy",
                "--wait", "--timeout", "1", "--json",
            ])

        assert result.exit_code != 0

    def test_generate_text_output(self, runner):
        """Without --json flag, human-readable output is produced."""
        with patch("kiln.generation.MeshyProvider") as MockProvider:
            provider = MockProvider.return_value
            provider.display_name = "Meshy"
            provider.generate.return_value = _make_job(
                status=GenerationStatus.PENDING,
            )

            result = runner.invoke(cli, [
                "generate", "a phone stand", "--provider", "meshy",
            ])

        assert result.exit_code == 0
        assert "gen-abc-123" in result.output
        assert "submitted" in result.output.lower() or "Job" in result.output


# ---------------------------------------------------------------------------
# generate-status
# ---------------------------------------------------------------------------


class TestGenerateStatus:
    """Tests for the 'kiln generate-status' CLI command."""

    def test_status_json(self, runner):
        """Returns job info as JSON."""
        with patch("kiln.generation.MeshyProvider") as MockProvider:
            provider = MockProvider.return_value
            provider.get_job_status.return_value = _make_job(
                status=GenerationStatus.IN_PROGRESS, progress=65,
            )

            result = runner.invoke(cli, [
                "generate-status", "gen-abc-123", "--provider", "meshy", "--json",
            ])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "success"
        assert data["data"]["job"]["progress"] == 65
        assert data["data"]["job"]["status"] == "in_progress"

    def test_status_error(self, runner):
        """Provider raises error — exits non-zero."""
        with patch("kiln.generation.MeshyProvider") as MockProvider:
            provider = MockProvider.return_value
            provider.get_job_status.side_effect = GenerationError(
                "Job not found", code="NOT_FOUND",
            )

            result = runner.invoke(cli, [
                "generate-status", "bad-id", "--provider", "meshy", "--json",
            ])

        assert result.exit_code != 0

    def test_status_text_output(self, runner):
        """Human-readable text output for generate-status."""
        with patch("kiln.generation.MeshyProvider") as MockProvider:
            provider = MockProvider.return_value
            provider.get_job_status.return_value = _make_job(
                status=GenerationStatus.SUCCEEDED, progress=100,
            )

            result = runner.invoke(cli, [
                "generate-status", "gen-abc-123", "--provider", "meshy",
            ])

        assert result.exit_code == 0
        assert "gen-abc-123" in result.output
        assert "succeeded" in result.output.lower() or "100" in result.output


# ---------------------------------------------------------------------------
# generate-download
# ---------------------------------------------------------------------------


class TestGenerateDownload:
    """Tests for the 'kiln generate-download' CLI command."""

    def test_download_json_with_validation(self, runner):
        """Download with validation enabled — JSON output."""
        with patch("kiln.generation.MeshyProvider") as MockProvider, \
             patch("kiln.generation.validate_mesh") as mock_validate:
            provider = MockProvider.return_value
            provider.download_result.return_value = _make_result(fmt="stl")
            mock_validate.return_value = _make_validation(valid=True)

            result = runner.invoke(cli, [
                "generate-download", "gen-abc-123",
                "--provider", "meshy", "--json",
            ])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "success"
        assert "result" in data["data"]
        assert "validation" in data["data"]
        assert data["data"]["validation"]["valid"] is True

    def test_download_no_validate_flag(self, runner):
        """--no-validate skips mesh validation."""
        with patch("kiln.generation.MeshyProvider") as MockProvider, \
             patch("kiln.generation.validate_mesh") as mock_validate:
            provider = MockProvider.return_value
            provider.download_result.return_value = _make_result(fmt="stl")

            result = runner.invoke(cli, [
                "generate-download", "gen-abc-123",
                "--provider", "meshy", "--no-validate", "--json",
            ])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "success"
        # validation key should not be present when skipped
        assert data["data"].get("validation") is None
        mock_validate.assert_not_called()

    def test_download_error(self, runner):
        """Provider raises error during download."""
        with patch("kiln.generation.MeshyProvider") as MockProvider:
            provider = MockProvider.return_value
            provider.download_result.side_effect = GenerationError(
                "Job not complete", code="NOT_READY",
            )

            result = runner.invoke(cli, [
                "generate-download", "gen-abc-123",
                "--provider", "meshy", "--json",
            ])

        assert result.exit_code != 0
