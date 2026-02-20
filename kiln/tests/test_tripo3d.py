"""Tests for kiln.generation.tripo3d -- Tripo3D provider."""

from __future__ import annotations

import os
import tempfile

import pytest
import requests as requests_lib
import responses

from kiln.generation.base import (
    GenerationAuthError,
    GenerationError,
    GenerationJob,
    GenerationStatus,
)
from kiln.generation.tripo3d import _BASE_URL, Tripo3DProvider

# ---------------------------------------------------------------------------
# TestTripo3DProviderConstructor
# ---------------------------------------------------------------------------


class TestTripo3DProviderConstructor:
    def test_api_key_from_argument(self):
        p = Tripo3DProvider(api_key="test-key")
        assert p.name == "tripo3d"
        assert p.display_name == "Tripo3D"

    def test_api_key_from_env(self, monkeypatch):
        monkeypatch.setenv("KILN_TRIPO3D_API_KEY", "env-key")
        p = Tripo3DProvider()
        assert p.name == "tripo3d"

    def test_missing_api_key_raises(self, monkeypatch):
        monkeypatch.delenv("KILN_TRIPO3D_API_KEY", raising=False)
        with pytest.raises(GenerationAuthError, match="Tripo3D API key required"):
            Tripo3DProvider()

    def test_empty_api_key_raises(self, monkeypatch):
        monkeypatch.delenv("KILN_TRIPO3D_API_KEY", raising=False)
        with pytest.raises(GenerationAuthError, match="Tripo3D API key required"):
            Tripo3DProvider(api_key="")


# ---------------------------------------------------------------------------
# TestTripo3DProviderGenerate
# ---------------------------------------------------------------------------


class TestTripo3DProviderGenerate:
    @responses.activate
    def test_generate_success(self):
        responses.add(
            responses.POST,
            f"{_BASE_URL}/task",
            json={"data": {"task_id": "task-abc123"}},
            status=200,
        )
        p = Tripo3DProvider(api_key="test-key")
        job = p.generate("a small vase")
        assert isinstance(job, GenerationJob)
        assert job.id == "task-abc123"
        assert job.provider == "tripo3d"
        assert job.status == GenerationStatus.PENDING
        assert job.prompt == "a small vase"

    @responses.activate
    def test_generate_no_task_id_raises(self):
        responses.add(
            responses.POST,
            f"{_BASE_URL}/task",
            json={"data": {}},
            status=200,
        )
        p = Tripo3DProvider(api_key="test-key")
        with pytest.raises(GenerationError, match="no task ID"):
            p.generate("test prompt")

    @responses.activate
    def test_generate_auth_error(self):
        responses.add(
            responses.POST,
            f"{_BASE_URL}/task",
            json={"message": "Unauthorized"},
            status=401,
        )
        p = Tripo3DProvider(api_key="bad-key")
        with pytest.raises(GenerationAuthError, match="invalid or expired"):
            p.generate("test prompt")


# ---------------------------------------------------------------------------
# TestTripo3DProviderGetJobStatus
# ---------------------------------------------------------------------------


class TestTripo3DProviderGetJobStatus:
    @responses.activate
    def test_get_job_status_running(self):
        responses.add(
            responses.GET,
            f"{_BASE_URL}/task/task-123",
            json={
                "data": {
                    "status": "running",
                    "progress": 50,
                    "create_time": 1700000000.0,
                }
            },
            status=200,
        )
        p = Tripo3DProvider(api_key="test-key")
        job = p.get_job_status("task-123")
        assert job.status == GenerationStatus.IN_PROGRESS
        assert job.progress == 50

    @responses.activate
    def test_get_job_status_success(self):
        responses.add(
            responses.GET,
            f"{_BASE_URL}/task/task-456",
            json={
                "data": {
                    "status": "success",
                    "progress": 100,
                    "output": {"model": "https://example.com/model.glb"},
                }
            },
            status=200,
        )
        p = Tripo3DProvider(api_key="test-key")
        job = p.get_job_status("task-456")
        assert job.status == GenerationStatus.SUCCEEDED
        assert job.progress == 100

    @responses.activate
    def test_get_job_status_failed(self):
        responses.add(
            responses.GET,
            f"{_BASE_URL}/task/task-789",
            json={
                "data": {
                    "status": "failed",
                    "message": "Model generation failed",
                }
            },
            status=200,
        )
        p = Tripo3DProvider(api_key="test-key")
        job = p.get_job_status("task-789")
        assert job.status == GenerationStatus.FAILED
        assert job.error == "Model generation failed"

    @responses.activate
    def test_get_job_status_queued(self):
        responses.add(
            responses.GET,
            f"{_BASE_URL}/task/task-q",
            json={"data": {"status": "queued", "progress": 0}},
            status=200,
        )
        p = Tripo3DProvider(api_key="test-key")
        job = p.get_job_status("task-q")
        assert job.status == GenerationStatus.PENDING


# ---------------------------------------------------------------------------
# TestTripo3DProviderDownload
# ---------------------------------------------------------------------------


class TestTripo3DProviderDownload:
    @responses.activate
    def test_download_result_success(self):
        # First poll to populate results.
        responses.add(
            responses.GET,
            f"{_BASE_URL}/task/task-dl",
            json={
                "data": {
                    "status": "success",
                    "progress": 100,
                    "output": {"model": "https://cdn.tripo3d.ai/model.glb"},
                }
            },
            status=200,
        )
        # Download the model.
        responses.add(
            responses.GET,
            "https://cdn.tripo3d.ai/model.glb",
            body=b"\x00" * 1024,
            status=200,
        )

        p = Tripo3DProvider(api_key="test-key")
        with tempfile.TemporaryDirectory() as tmpdir:
            result = p.download_result("task-dl", output_dir=tmpdir)
            assert result.provider == "tripo3d"
            assert result.format == "glb"
            assert result.file_size_bytes == 1024
            assert os.path.isfile(result.local_path)

    @responses.activate
    def test_download_no_output_raises(self):
        responses.add(
            responses.GET,
            f"{_BASE_URL}/task/task-no",
            json={"data": {"status": "queued", "progress": 0}},
            status=200,
        )
        p = Tripo3DProvider(api_key="test-key")
        with pytest.raises(GenerationError, match="No output available"):
            p.download_result("task-no")


# ---------------------------------------------------------------------------
# TestTripo3DProviderRetry
# ---------------------------------------------------------------------------


class TestTripo3DProviderRetry:
    @responses.activate
    def test_retry_on_429(self):
        responses.add(
            responses.POST,
            f"{_BASE_URL}/task",
            status=429,
        )
        responses.add(
            responses.POST,
            f"{_BASE_URL}/task",
            json={"data": {"task_id": "retry-ok"}},
            status=200,
        )
        p = Tripo3DProvider(api_key="test-key")
        # Patch sleep to avoid delays.
        import kiln.generation.tripo3d as tripo_mod

        original_sleep = tripo_mod.time.sleep
        tripo_mod.time.sleep = lambda _: None
        try:
            job = p.generate("retry test")
            assert job.id == "retry-ok"
        finally:
            tripo_mod.time.sleep = original_sleep

    @responses.activate
    def test_retry_on_502(self):
        responses.add(responses.POST, f"{_BASE_URL}/task", status=502)
        responses.add(responses.POST, f"{_BASE_URL}/task", status=502)
        responses.add(
            responses.POST,
            f"{_BASE_URL}/task",
            json={"data": {"task_id": "retry-502"}},
            status=200,
        )
        p = Tripo3DProvider(api_key="test-key")
        import kiln.generation.tripo3d as tripo_mod

        original_sleep = tripo_mod.time.sleep
        tripo_mod.time.sleep = lambda _: None
        try:
            job = p.generate("retry 502")
            assert job.id == "retry-502"
        finally:
            tripo_mod.time.sleep = original_sleep

    @responses.activate
    def test_rate_limit_after_retries_raises(self):
        for _ in range(4):
            responses.add(responses.POST, f"{_BASE_URL}/task", status=429)
        p = Tripo3DProvider(api_key="test-key")
        import kiln.generation.tripo3d as tripo_mod

        original_sleep = tripo_mod.time.sleep
        tripo_mod.time.sleep = lambda _: None
        try:
            with pytest.raises(GenerationError, match="rate limit"):
                p.generate("rate limited")
        finally:
            tripo_mod.time.sleep = original_sleep


# ---------------------------------------------------------------------------
# TestTripo3DProviderMisc
# ---------------------------------------------------------------------------


class TestTripo3DProviderMisc:
    def test_list_styles_empty(self):
        p = Tripo3DProvider(api_key="test-key")
        assert p.list_styles() == []

    @responses.activate
    def test_connection_error(self):
        responses.add(
            responses.POST,
            f"{_BASE_URL}/task",
            body=requests_lib.ConnectionError("Network down"),
        )
        p = Tripo3DProvider(api_key="test-key")
        with pytest.raises(GenerationError, match="Could not connect"):
            p.generate("connection test")

    @responses.activate
    def test_api_error_includes_status(self):
        responses.add(
            responses.POST,
            f"{_BASE_URL}/task",
            json={"message": "Bad request"},
            status=400,
        )
        p = Tripo3DProvider(api_key="test-key")
        with pytest.raises(GenerationError, match="HTTP 400"):
            p.generate("bad request")
