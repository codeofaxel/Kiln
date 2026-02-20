"""Tests for kiln.generation.stability -- Stability AI provider."""

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
from kiln.generation.stability import _BASE_URL, StabilityProvider

# ---------------------------------------------------------------------------
# TestStabilityProviderConstructor
# ---------------------------------------------------------------------------


class TestStabilityProviderConstructor:
    def test_api_key_from_argument(self):
        p = StabilityProvider(api_key="test-key")
        assert p.name == "stability"
        assert p.display_name == "Stability AI"

    def test_api_key_from_env(self, monkeypatch):
        monkeypatch.setenv("KILN_STABILITY_API_KEY", "env-key")
        p = StabilityProvider()
        assert p.name == "stability"

    def test_missing_api_key_raises(self, monkeypatch):
        monkeypatch.delenv("KILN_STABILITY_API_KEY", raising=False)
        with pytest.raises(GenerationAuthError, match="Stability API key required"):
            StabilityProvider()

    def test_empty_api_key_raises(self, monkeypatch):
        monkeypatch.delenv("KILN_STABILITY_API_KEY", raising=False)
        with pytest.raises(GenerationAuthError, match="Stability API key required"):
            StabilityProvider(api_key="")


# ---------------------------------------------------------------------------
# TestStabilityProviderGenerate
# ---------------------------------------------------------------------------


class TestStabilityProviderGenerate:
    @responses.activate
    def test_generate_success(self):
        responses.add(
            responses.POST,
            f"{_BASE_URL}/generate",
            body=b"\x00glb_model_data" * 100,
            status=200,
        )
        p = StabilityProvider(api_key="test-key")
        with tempfile.TemporaryDirectory() as tmpdir:
            job = p.generate("a small vase", output_dir=tmpdir)
            assert isinstance(job, GenerationJob)
            assert job.provider == "stability"
            assert job.status == GenerationStatus.SUCCEEDED
            assert job.progress == 100
            assert job.format == "glb"

    @responses.activate
    def test_generate_empty_response_fails(self):
        responses.add(
            responses.POST,
            f"{_BASE_URL}/generate",
            body=b"",
            status=200,
        )
        p = StabilityProvider(api_key="test-key")
        with tempfile.TemporaryDirectory() as tmpdir:
            job = p.generate("empty model", output_dir=tmpdir)
            assert job.status == GenerationStatus.FAILED
            assert job.error is not None

    @responses.activate
    def test_generate_auth_error(self):
        responses.add(
            responses.POST,
            f"{_BASE_URL}/generate",
            json={"message": "Invalid key"},
            status=401,
        )
        p = StabilityProvider(api_key="bad-key")
        with pytest.raises(GenerationAuthError, match="invalid or expired"):
            p.generate("auth test")


# ---------------------------------------------------------------------------
# TestStabilityProviderGetJobStatus
# ---------------------------------------------------------------------------


class TestStabilityProviderGetJobStatus:
    @responses.activate
    def test_get_job_status_after_generate(self):
        responses.add(
            responses.POST,
            f"{_BASE_URL}/generate",
            body=b"\x00" * 512,
            status=200,
        )
        p = StabilityProvider(api_key="test-key")
        with tempfile.TemporaryDirectory() as tmpdir:
            job = p.generate("status test", output_dir=tmpdir)
            status = p.get_job_status(job.id)
            assert status.id == job.id
            assert status.status == GenerationStatus.SUCCEEDED

    def test_get_job_status_unknown_id_raises(self):
        p = StabilityProvider(api_key="test-key")
        with pytest.raises(GenerationError, match="not found"):
            p.get_job_status("nonexistent-id")


# ---------------------------------------------------------------------------
# TestStabilityProviderDownload
# ---------------------------------------------------------------------------


class TestStabilityProviderDownload:
    @responses.activate
    def test_download_result_success(self):
        responses.add(
            responses.POST,
            f"{_BASE_URL}/generate",
            body=b"\x00model_data" * 100,
            status=200,
        )
        p = StabilityProvider(api_key="test-key")
        with tempfile.TemporaryDirectory() as tmpdir:
            job = p.generate("download test", output_dir=tmpdir)
            result = p.download_result(job.id, output_dir=tmpdir)
            assert result.provider == "stability"
            assert result.format == "glb"
            assert os.path.isfile(result.local_path)
            assert result.file_size_bytes > 0

    def test_download_unknown_id_raises(self):
        p = StabilityProvider(api_key="test-key")
        with pytest.raises(GenerationError, match="No generated file"):
            p.download_result("missing-id")


# ---------------------------------------------------------------------------
# TestStabilityProviderMisc
# ---------------------------------------------------------------------------


class TestStabilityProviderMisc:
    def test_list_styles_empty(self):
        p = StabilityProvider(api_key="test-key")
        assert p.list_styles() == []

    @responses.activate
    def test_connection_error(self):
        responses.add(
            responses.POST,
            f"{_BASE_URL}/generate",
            body=requests_lib.ConnectionError("Network down"),
        )
        p = StabilityProvider(api_key="test-key")
        with pytest.raises(GenerationError, match="Could not connect"):
            p.generate("connection test")

    @responses.activate
    def test_api_error_includes_status(self):
        responses.add(
            responses.POST,
            f"{_BASE_URL}/generate",
            json={"message": "Server error"},
            status=500,
        )
        p = StabilityProvider(api_key="test-key")
        with pytest.raises(GenerationError, match="HTTP 500"):
            p.generate("server error")
