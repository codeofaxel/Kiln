"""Tripo3D text-to-3D generation provider.

Integrates with the Tripo3D API (https://platform.tripo3d.ai) to generate
3D models from text prompts.  Uses the v2 OpenAPI endpoint for submitting
tasks and polling results.

Authentication
--------------
Set ``KILN_TRIPO3D_API_KEY`` or pass ``api_key`` to the constructor.
"""

from __future__ import annotations

import logging
import os
import tempfile
import time
from typing import Any

import requests

from kiln.generation.base import (
    GenerationAuthError,
    GenerationError,
    GenerationJob,
    GenerationProvider,
    GenerationResult,
    GenerationStatus,
)

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.tripo3d.ai/v2/openapi"

_STATUS_MAP: dict[str, GenerationStatus] = {
    "queued": GenerationStatus.PENDING,
    "running": GenerationStatus.IN_PROGRESS,
    "success": GenerationStatus.SUCCEEDED,
    "failed": GenerationStatus.FAILED,
    "cancelled": GenerationStatus.CANCELLED,
}


_MAX_RETRIES = 3
_RETRY_BACKOFF_BASE = 2.0  # seconds: 2, 4, 8


class Tripo3DProvider(GenerationProvider):
    """Tripo3D text-to-3D generation via REST API.

    Args:
        api_key: Tripo3D API key.  Falls back to ``KILN_TRIPO3D_API_KEY``.
        timeout: HTTP request timeout in seconds.
    """

    def __init__(
        self,
        api_key: str = "",
        *,
        timeout: int = 30,
    ) -> None:
        self._api_key = api_key or os.environ.get("KILN_TRIPO3D_API_KEY", "")
        if not self._api_key:
            raise GenerationAuthError(
                "Tripo3D API key required.  Set KILN_TRIPO3D_API_KEY or pass api_key.",
                code="AUTH_REQUIRED",
            )
        self._timeout = timeout
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            }
        )
        # Cache model URLs for download after polling.
        self._results: dict[str, dict[str, Any]] = {}
        self._prompts: dict[str, str] = {}

    @property
    def name(self) -> str:
        return "tripo3d"

    @property
    def display_name(self) -> str:
        return "Tripo3D"

    def generate(
        self,
        prompt: str,
        *,
        format: str = "stl",
        style: str | None = None,
        **kwargs: Any,
    ) -> GenerationJob:
        """Submit a text-to-model generation task to Tripo3D.

        Args:
            prompt: Text description of the desired 3D model.
            format: Desired output format (stored for later download).
            style: Ignored for Tripo3D.

        Returns:
            :class:`GenerationJob` with ``PENDING`` status.
        """
        body: dict[str, Any] = {
            "type": "text_to_model",
            "prompt": prompt,
        }

        resp = self._request("POST", f"{_BASE_URL}/task", json_body=body)

        data = resp.json()
        task_data = data.get("data", {})
        task_id = task_data.get("task_id", "")
        if not task_id:
            raise GenerationError(
                "Tripo3D API returned no task ID.",
                code="INVALID_RESPONSE",
            )

        self._prompts[task_id] = prompt

        return GenerationJob(
            id=task_id,
            provider=self.name,
            prompt=prompt,
            status=GenerationStatus.PENDING,
            progress=0,
            created_at=time.time(),
            format=format,
            style=style,
        )

    def get_job_status(self, job_id: str) -> GenerationJob:
        """Poll a Tripo3D generation task.

        Args:
            job_id: Task ID from :meth:`generate`.

        Returns:
            Updated :class:`GenerationJob`.
        """
        resp = self._request("GET", f"{_BASE_URL}/task/{job_id}")

        data = resp.json()
        task_data = data.get("data", {})
        status_str = task_data.get("status", "queued")
        status = _STATUS_MAP.get(status_str, GenerationStatus.PENDING)
        progress = task_data.get("progress", 0)
        prompt = self._prompts.get(job_id, "")

        # Cache output for download.
        output = task_data.get("output")
        if output and isinstance(output, dict):
            self._results[job_id] = output

        error_msg: str | None = None
        if status == GenerationStatus.FAILED:
            error_msg = task_data.get("message") or "Generation failed."

        return GenerationJob(
            id=job_id,
            provider=self.name,
            prompt=prompt,
            status=status,
            progress=progress,
            created_at=task_data.get("create_time", 0.0),
            format="glb",
            style=None,
            error=error_msg,
        )

    def download_result(
        self,
        job_id: str,
        output_dir: str = os.path.join(tempfile.gettempdir(), "kiln_generated"),
    ) -> GenerationResult:
        """Download the generated model file.

        Prefers GLB format from the Tripo3D output.

        Args:
            job_id: Task ID of a completed job.
            output_dir: Directory to save the file.
        """
        output = self._results.get(job_id)
        if not output:
            # Try polling once to populate.
            job = self.get_job_status(job_id)
            output = self._results.get(job_id)
            if not output:
                raise GenerationError(
                    f"No output available for job {job_id}.  Job status: {job.status.value}",
                    code="NO_RESULT",
                )

        # Tripo3D returns a model URL in the output dict.
        url = output.get("model") or output.get("pbr_model") or output.get("base_model")
        if not url:
            raise GenerationError(
                "No downloadable model URL found in Tripo3D results.",
                code="NO_FORMAT",
            )

        ext = "glb"
        os.makedirs(output_dir, exist_ok=True)
        out_path = os.path.join(output_dir, f"{job_id}.{ext}")

        resp = self._request("GET", url, timeout=120, stream=True)

        with open(out_path, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=65536):
                fh.write(chunk)

        file_size = os.path.getsize(out_path)
        prompt = self._prompts.get(job_id, "")

        return GenerationResult(
            job_id=job_id,
            provider=self.name,
            local_path=out_path,
            format=ext,
            file_size_bytes=file_size,
            prompt=prompt,
        )

    def list_styles(self) -> list[str]:
        return []

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _request(
        self,
        method: str,
        url: str,
        *,
        json_body: dict[str, Any] | None = None,
        timeout: int | None = None,
        stream: bool = False,
    ) -> requests.Response:
        """Make an HTTP request with rate-limit retry and backoff.

        Retries up to ``_MAX_RETRIES`` times on HTTP 429 (rate limit)
        and 502/503/504 (transient server errors).  Uses exponential
        backoff: 2s, 4s, 8s.
        """
        req_timeout = timeout or self._timeout

        for attempt in range(_MAX_RETRIES + 1):
            try:
                resp = self._session.request(
                    method,
                    url,
                    json=json_body,
                    timeout=req_timeout,
                    stream=stream,
                )
            except requests.ConnectionError:
                raise GenerationError(
                    "Could not connect to Tripo3D API.",
                    code="CONNECTION_ERROR",
                ) from None
            except requests.Timeout:
                raise GenerationError(
                    "Tripo3D API request timed out.",
                    code="TIMEOUT",
                ) from None

            if resp.status_code in (429, 502, 503, 504) and attempt < _MAX_RETRIES:
                wait = _RETRY_BACKOFF_BASE * (2**attempt)
                logger.warning(
                    "Tripo3D API returned %d, retrying in %.0fs (attempt %d/%d)",
                    resp.status_code,
                    wait,
                    attempt + 1,
                    _MAX_RETRIES,
                )
                time.sleep(wait)
                continue

            self._handle_http_error(resp)
            return resp

        # Should not reach here, but just in case.
        raise GenerationError(
            "Tripo3D API request failed after retries.",
            code="RETRY_EXHAUSTED",
        )

    def _handle_http_error(self, resp: requests.Response) -> None:
        """Raise a typed exception for non-2xx responses."""
        if resp.ok:
            return

        if resp.status_code == 401:
            raise GenerationAuthError(
                "Tripo3D API key is invalid or expired.",
                code="AUTH_INVALID",
            )
        if resp.status_code == 429:
            raise GenerationError(
                "Tripo3D API rate limit exceeded.  Try again later.",
                code="RATE_LIMITED",
            )

        body = ""
        try:
            body = resp.json().get("message", resp.text[:200])
        except Exception:
            body = resp.text[:200]

        raise GenerationError(
            f"Tripo3D API error (HTTP {resp.status_code}): {body}",
            code="API_ERROR",
        )
