"""Tests for kiln.generation.tripo3d -- Tripo3D provider (Tripo API v3).

URLs and payloads below are written out literally from Tripo's v3
reference rather than imported from the provider, so a wrong base URL or
endpoint in the provider fails here instead of agreeing with itself.

Fixture shapes and where each one comes from:

* Base URL, Bearer auth, ``{"code": 0, "data": {...}}`` success envelope:
  https://developers.tripo3d.ai/en/docs/introduction
* ``POST /generation/text-to-model`` request (``prompt`` and ``model``
  both required, no ``type`` field) and its ``{"task_id": ...}`` reply:
  https://developers.tripo3d.ai/en/docs/generation-text-to-model/standard
* ``GET /tasks/{task_id}`` reply (``status``, ``progress``, ``output``
  with ``model_url``, ``error_code``, ``error_message``, ISO 8601
  ``created_at``): https://developers.tripo3d.ai/en/docs/task-query
* Status vocabulary including ``banned`` and ``expired``:
  https://developers.tripo3d.ai/en/docs/task-lifecycle
* Error envelope (``code``, ``message``, ``suggestion``, ``request_id``)
  and error codes: https://developers.tripo3d.ai/en/docs/error-handling
* Rate-limit envelope (code 1007):
  https://developers.tripo3d.ai/en/docs/rate-limits
* v2 → v3 endpoint and field mapping:
  https://developers.tripo3d.ai/en/docs/migration-v2-to-v3

Coverage: constructor and model selection, task creation, status polling
for every documented status plus unrecognised ones, download, retry, and
error envelopes.
"""

from __future__ import annotations

import json
import os
import tempfile

import pytest
import requests as requests_lib
import responses

import kiln.generation.tripo3d as tripo_mod
from kiln.generation.base import (
    GenerationAuthError,
    GenerationError,
    GenerationJob,
    GenerationStatus,
)
from kiln.generation.tripo3d import Tripo3DProvider

_V3 = "https://openapi.tripo3d.ai/v3"
_CREATE_URL = f"{_V3}/generation/text-to-model"
_DEFAULT_MODEL = "v3.1-20260211"
_MODEL_URL = "https://cdn.tripo3d.ai/output/model_pbr.glb"

# 2026-04-28T12:00:00Z, the ``created_at`` in Tripo's task-query example.
_CREATED_AT_ISO = "2026-04-28T12:00:00Z"
_CREATED_AT_EPOCH = 1777377600.0


def _task_url(task_id: str) -> str:
    return f"{_V3}/tasks/{task_id}"


def _task(task_id: str, status: str, **fields) -> dict:
    """A ``GET /tasks/{task_id}`` reply in the documented v3 shape."""
    data = {
        "task_id": task_id,
        "type": "text_to_model",
        "status": status,
        "progress": 0,
        "created_at": _CREATED_AT_ISO,
    }
    data.update(fields)
    return {"code": 0, "data": data}


def _success_task(task_id: str, **output) -> dict:
    return _task(
        task_id,
        "success",
        progress=100,
        output=output or {"model_url": _MODEL_URL, "rendered_image_url": "https://cdn.tripo3d.ai/output/preview.png"},
        credits_consumed=100.00,
        completed_at="2026-04-28T12:01:30Z",
    )


@pytest.fixture()
def no_sleep(monkeypatch):
    monkeypatch.setattr(tripo_mod.time, "sleep", lambda _: None)


@pytest.fixture(autouse=True)
def _no_model_env(monkeypatch):
    monkeypatch.delenv("KILN_TRIPO3D_MODEL", raising=False)


def _request_methods() -> list[str]:
    return [call.request.method for call in responses.calls]


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

    def test_base_url_is_v3(self):
        assert tripo_mod._BASE_URL == _V3


# ---------------------------------------------------------------------------
# TestTripo3DProviderGenerate
# ---------------------------------------------------------------------------


class TestTripo3DProviderGenerate:
    @responses.activate
    def test_generate_success(self):
        responses.add(
            responses.POST,
            _CREATE_URL,
            json={"code": 0, "data": {"task_id": "task_abc123"}},
            status=200,
        )
        p = Tripo3DProvider(api_key="test-key")
        job = p.generate("a small vase")
        assert isinstance(job, GenerationJob)
        assert job.id == "task_abc123"
        assert job.provider == "tripo3d"
        assert job.status == GenerationStatus.PENDING
        assert job.prompt == "a small vase"

    @responses.activate
    def test_generate_sends_v3_body_and_bearer_auth(self):
        responses.add(
            responses.POST,
            _CREATE_URL,
            json={"code": 0, "data": {"task_id": "task_abc123"}},
            status=200,
        )
        Tripo3DProvider(api_key="test-key").generate("a small vase")
        request = responses.calls[0].request
        assert json.loads(request.body) == {"prompt": "a small vase", "model": _DEFAULT_MODEL}
        assert request.headers["Authorization"] == "Bearer test-key"

    @responses.activate
    def test_generate_model_from_env(self, monkeypatch):
        monkeypatch.setenv("KILN_TRIPO3D_MODEL", "v3.0-20250812")
        responses.add(
            responses.POST,
            _CREATE_URL,
            json={"code": 0, "data": {"task_id": "task_env"}},
            status=200,
        )
        Tripo3DProvider(api_key="test-key").generate("a small vase")
        assert json.loads(responses.calls[0].request.body)["model"] == "v3.0-20250812"

    @responses.activate
    def test_generate_model_argument_beats_env(self, monkeypatch):
        monkeypatch.setenv("KILN_TRIPO3D_MODEL", "v3.0-20250812")
        responses.add(
            responses.POST,
            _CREATE_URL,
            json={"code": 0, "data": {"task_id": "task_arg"}},
            status=200,
        )
        Tripo3DProvider(api_key="test-key", model="v2.5-20250123").generate("a small vase")
        assert json.loads(responses.calls[0].request.body)["model"] == "v2.5-20250123"

    @responses.activate
    def test_generate_no_task_id_raises(self):
        responses.add(
            responses.POST,
            _CREATE_URL,
            json={"code": 0, "data": {}},
            status=200,
        )
        p = Tripo3DProvider(api_key="test-key")
        with pytest.raises(GenerationError, match="no task ID"):
            p.generate("test prompt")

    @responses.activate
    def test_generate_auth_error(self):
        # Envelope as served by openapi.tripo3d.ai/v3 for a bad key
        # (observed 2026-09-30); the documented shape omits ``status``.
        responses.add(
            responses.POST,
            _CREATE_URL,
            json={
                "code": 2,
                "status": "error",
                "message": "Invalid API key",
                "suggestion": "Check if your credentials is valid",
            },
            status=401,
        )
        p = Tripo3DProvider(api_key="bad-key")
        with pytest.raises(GenerationAuthError, match="invalid or expired"):
            p.generate("test prompt")

    @responses.activate
    def test_generate_insufficient_credits_names_the_reason(self):
        responses.add(
            responses.POST,
            _CREATE_URL,
            json={
                "code": 2010,
                "message": "Insufficient credits",
                "suggestion": "Please top up your account at https://platform.tripo3d.ai",
                "request_id": "req_abc123",
            },
            status=403,
        )
        p = Tripo3DProvider(api_key="test-key")
        with pytest.raises(GenerationError, match="HTTP 403.*Insufficient credits"):
            p.generate("test prompt")

    @responses.activate
    def test_generate_error_envelope_on_http_200_raises(self):
        # The docs define any non-zero ``code`` as an error; the pairing with
        # HTTP 200 is not documented and is covered defensively.
        responses.add(
            responses.POST,
            _CREATE_URL,
            json={"code": 2008, "message": "Content policy violation", "suggestion": "Modify the input content"},
            status=200,
        )
        p = Tripo3DProvider(api_key="test-key")
        with pytest.raises(GenerationError, match="Content policy violation"):
            p.generate("test prompt")


# ---------------------------------------------------------------------------
# TestTripo3DProviderGetJobStatus
# ---------------------------------------------------------------------------


class TestTripo3DProviderGetJobStatus:
    @responses.activate
    def test_get_job_status_running(self):
        responses.add(responses.GET, _task_url("task_123"), json=_task("task_123", "running", progress=50), status=200)
        p = Tripo3DProvider(api_key="test-key")
        job = p.get_job_status("task_123")
        assert job.status == GenerationStatus.IN_PROGRESS
        assert job.progress == 50

    @responses.activate
    def test_get_job_status_parses_iso_created_at(self):
        responses.add(responses.GET, _task_url("task_123"), json=_task("task_123", "running", progress=50), status=200)
        job = Tripo3DProvider(api_key="test-key").get_job_status("task_123")
        assert job.created_at == _CREATED_AT_EPOCH

    @responses.activate
    def test_get_job_status_unparseable_created_at_is_zero(self):
        responses.add(
            responses.GET,
            _task_url("task_123"),
            json=_task("task_123", "running", created_at="not a timestamp"),
            status=200,
        )
        job = Tripo3DProvider(api_key="test-key").get_job_status("task_123")
        assert job.created_at == 0.0

    @responses.activate
    def test_get_job_status_success(self):
        responses.add(responses.GET, _task_url("task_456"), json=_success_task("task_456"), status=200)
        p = Tripo3DProvider(api_key="test-key")
        job = p.get_job_status("task_456")
        assert job.status == GenerationStatus.SUCCEEDED
        assert job.progress == 100
        assert job.error is None

    @responses.activate
    def test_get_job_status_failed_uses_error_message(self):
        responses.add(
            responses.GET,
            _task_url("task_789"),
            json=_task("task_789", "failed", error_code=2018, error_message="Model too complex"),
            status=200,
        )
        p = Tripo3DProvider(api_key="test-key")
        job = p.get_job_status("task_789")
        assert job.status == GenerationStatus.FAILED
        assert job.error == "Model too complex"

    @responses.activate
    def test_get_job_status_failed_without_message(self):
        responses.add(responses.GET, _task_url("task_789"), json=_task("task_789", "failed"), status=200)
        job = Tripo3DProvider(api_key="test-key").get_job_status("task_789")
        assert job.status == GenerationStatus.FAILED
        assert job.error == "Generation failed."

    @responses.activate
    def test_get_job_status_banned_is_failed_with_reason(self):
        responses.add(responses.GET, _task_url("task_ban"), json=_task("task_ban", "banned"), status=200)
        job = Tripo3DProvider(api_key="test-key").get_job_status("task_ban")
        assert job.status == GenerationStatus.FAILED
        assert "content policy" in job.error

    @responses.activate
    def test_get_job_status_expired_is_failed_with_reason(self):
        responses.add(responses.GET, _task_url("task_exp"), json=_task("task_exp", "expired"), status=200)
        job = Tripo3DProvider(api_key="test-key").get_job_status("task_exp")
        assert job.status == GenerationStatus.FAILED
        assert "expired" in job.error

    @pytest.mark.parametrize(
        ("tripo_status", "expected"),
        [
            ("queued", GenerationStatus.PENDING),
            ("running", GenerationStatus.IN_PROGRESS),
            ("success", GenerationStatus.SUCCEEDED),
            ("failed", GenerationStatus.FAILED),
            ("cancelled", GenerationStatus.CANCELLED),
            ("banned", GenerationStatus.FAILED),
            ("expired", GenerationStatus.FAILED),
        ],
    )
    @responses.activate
    def test_every_documented_status_maps(self, tripo_status, expected):
        responses.add(responses.GET, _task_url("task_s"), json=_task("task_s", tripo_status), status=200)
        job = Tripo3DProvider(api_key="test-key").get_job_status("task_s")
        assert job.status == expected

    @pytest.mark.parametrize("tripo_status", ["unknown", "some_future_state", "", None])
    @responses.activate
    def test_unrecognised_status_is_pending_never_succeeded(self, tripo_status):
        responses.add(
            responses.GET,
            _task_url("task_u"),
            json=_task("task_u", tripo_status, output={"model_url": _MODEL_URL}),
            status=200,
        )
        job = Tripo3DProvider(api_key="test-key").get_job_status("task_u")
        assert job.status == GenerationStatus.PENDING
        assert job.error is None

    @responses.activate
    def test_null_progress_reads_as_zero(self):
        responses.add(responses.GET, _task_url("task_p"), json=_task("task_p", "failed", progress=None), status=200)
        job = Tripo3DProvider(api_key="test-key").get_job_status("task_p")
        assert job.progress == 0

    @responses.activate
    def test_error_envelope_on_poll_raises_instead_of_pending(self):
        responses.add(
            responses.GET,
            _task_url("task_e"),
            json={"code": 2015, "message": "Version deprecated", "suggestion": "Upgrade to the latest API version"},
            status=200,
        )
        p = Tripo3DProvider(api_key="test-key")
        with pytest.raises(GenerationError, match="Version deprecated"):
            p.get_job_status("task_e")

    @responses.activate
    def test_non_json_body_raises_invalid_response(self):
        responses.add(responses.GET, _task_url("task_html"), body="<html>gateway</html>", status=200)
        p = Tripo3DProvider(api_key="test-key")
        with pytest.raises(GenerationError, match="not a JSON object") as excinfo:
            p.get_job_status("task_html")
        assert excinfo.value.code == "INVALID_RESPONSE"

    @responses.activate
    def test_unknown_task_raises_with_status(self):
        # The docs list HTTP 404 for a missing task but no error code or
        # message for it, so the body is left empty rather than guessed.
        responses.add(responses.GET, _task_url("task_missing"), status=404)
        p = Tripo3DProvider(api_key="test-key")
        with pytest.raises(GenerationError, match="HTTP 404"):
            p.get_job_status("task_missing")

    @responses.activate
    def test_poll_timeout_raises_and_never_creates_a_task(self):
        responses.add(responses.GET, _task_url("task_slow"), body=requests_lib.Timeout("slow"))
        p = Tripo3DProvider(api_key="test-key")
        with pytest.raises(GenerationError, match="timed out") as excinfo:
            p.get_job_status("task_slow")
        assert excinfo.value.code == "TIMEOUT"
        assert _request_methods() == ["GET"]


# ---------------------------------------------------------------------------
# TestTripo3DProviderDownload
# ---------------------------------------------------------------------------


class TestTripo3DProviderDownload:
    @responses.activate
    def test_download_result_success(self):
        responses.add(responses.GET, _task_url("task_dl"), json=_success_task("task_dl"), status=200)
        responses.add(responses.GET, _MODEL_URL, body=b"\x00" * 1024, status=200)

        p = Tripo3DProvider(api_key="test-key")
        with tempfile.TemporaryDirectory() as tmpdir:
            result = p.download_result("task_dl", output_dir=tmpdir)
            assert result.provider == "tripo3d"
            assert result.format == "glb"
            assert result.file_size_bytes == 1024
            assert os.path.isfile(result.local_path)
        assert [call.request.url for call in responses.calls] == [_task_url("task_dl"), _MODEL_URL]

    @responses.activate
    def test_download_reads_older_output_keys(self):
        # Tripo's own v3 client falls back to these names after
        # ``model_url`` (tripo-js-sdk, ``extractModelUrl``); tasks created
        # on v2 stay queryable on v3 per the migration guide.
        legacy_url = "https://cdn.tripo3d.ai/output/legacy.glb"
        responses.add(
            responses.GET, _task_url("task_old"), json=_success_task("task_old", pbr_model=legacy_url), status=200
        )
        responses.add(responses.GET, legacy_url, body=b"\x01" * 16, status=200)

        p = Tripo3DProvider(api_key="test-key")
        with tempfile.TemporaryDirectory() as tmpdir:
            result = p.download_result("task_old", output_dir=tmpdir)
            assert result.file_size_bytes == 16

    @responses.activate
    def test_download_success_without_model_url_raises(self):
        responses.add(
            responses.GET,
            _task_url("task_img"),
            json=_success_task("task_img", rendered_image_url="https://cdn.tripo3d.ai/output/preview.png"),
            status=200,
        )
        p = Tripo3DProvider(api_key="test-key")
        with pytest.raises(GenerationError, match="No downloadable model URL"):
            p.download_result("task_img")

    @responses.activate
    def test_download_no_output_raises_without_creating_a_task(self):
        responses.add(responses.GET, _task_url("task_no"), json=_task("task_no", "queued"), status=200)
        p = Tripo3DProvider(api_key="test-key")
        with pytest.raises(GenerationError, match="No output available"):
            p.download_result("task_no")
        assert _request_methods() == ["GET"]


# ---------------------------------------------------------------------------
# TestTripo3DProviderRetry
# ---------------------------------------------------------------------------


class TestTripo3DProviderRetry:
    @responses.activate
    def test_retry_on_429(self, no_sleep):
        responses.add(
            responses.POST,
            _CREATE_URL,
            json={
                "code": 1007,
                "message": "Rate limit exceeded, you've generated too many requests in a short amount of time",
                "suggestion": "Please wait for a while and try again",
            },
            status=429,
        )
        responses.add(responses.POST, _CREATE_URL, json={"code": 0, "data": {"task_id": "retry-ok"}}, status=200)
        p = Tripo3DProvider(api_key="test-key")
        job = p.generate("retry test")
        assert job.id == "retry-ok"

    @responses.activate
    def test_retry_on_502(self, no_sleep):
        responses.add(responses.POST, _CREATE_URL, status=502)
        responses.add(responses.POST, _CREATE_URL, status=502)
        responses.add(responses.POST, _CREATE_URL, json={"code": 0, "data": {"task_id": "retry-502"}}, status=200)
        p = Tripo3DProvider(api_key="test-key")
        job = p.generate("retry 502")
        assert job.id == "retry-502"

    @responses.activate
    def test_rate_limit_after_retries_raises(self, no_sleep):
        for _ in range(4):
            responses.add(responses.POST, _CREATE_URL, status=429)
        p = Tripo3DProvider(api_key="test-key")
        with pytest.raises(GenerationError, match="rate limit"):
            p.generate("rate limited")


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
            _CREATE_URL,
            body=requests_lib.ConnectionError("Network down"),
        )
        p = Tripo3DProvider(api_key="test-key")
        with pytest.raises(GenerationError, match="Could not connect"):
            p.generate("connection test")

    @responses.activate
    def test_api_error_includes_status(self):
        responses.add(
            responses.POST,
            _CREATE_URL,
            json={"code": 2002, "message": "Unsupported request parameter", "suggestion": "Check the request body"},
            status=400,
        )
        p = Tripo3DProvider(api_key="test-key")
        with pytest.raises(GenerationError, match="HTTP 400.*Unsupported request parameter"):
            p.generate("bad request")
