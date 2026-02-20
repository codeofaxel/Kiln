"""Stability AI 3D generation provider.

Integrates with the Stability AI API (https://platform.stability.ai) to
generate 3D models from text prompts.  Uses the synchronous v2beta 3D
endpoint which returns the model directly in the response body.

Authentication
--------------
Set ``KILN_STABILITY_API_KEY`` or pass ``api_key`` to the constructor.
"""

from __future__ import annotations

import logging
import os
import tempfile
import time
import uuid
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

_BASE_URL = "https://api.stability.ai/v2beta/3d"

_MAX_RETRIES = 3
_RETRY_BACKOFF_BASE = 2.0  # seconds: 2, 4, 8


class StabilityProvider(GenerationProvider):
    """Stability AI 3D generation via REST API.

    The Stability 3D endpoint is synchronous — it returns the generated
    model directly in the response body.  The ``generate`` method
    therefore completes the entire workflow (submit + download) in one
    call and returns a ``SUCCEEDED`` job immediately.

    Args:
        api_key: Stability API key.  Falls back to ``KILN_STABILITY_API_KEY``.
        timeout: HTTP request timeout in seconds (generous default for
            synchronous generation).
    """

    def __init__(
        self,
        api_key: str = "",
        *,
        timeout: int = 120,
    ) -> None:
        self._api_key = api_key or os.environ.get("KILN_STABILITY_API_KEY", "")
        if not self._api_key:
            raise GenerationAuthError(
                "Stability API key required.  Set KILN_STABILITY_API_KEY or pass api_key.",
                code="AUTH_REQUIRED",
            )
        self._timeout = timeout
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {self._api_key}",
            }
        )
        # Store completed jobs and file paths.
        self._jobs: dict[str, GenerationJob] = {}
        self._paths: dict[str, str] = {}
        self._prompts: dict[str, str] = {}

    @property
    def name(self) -> str:
        return "stability"

    @property
    def display_name(self) -> str:
        return "Stability AI"

    def generate(
        self,
        prompt: str,
        *,
        format: str = "stl",
        style: str | None = None,
        **kwargs: Any,
    ) -> GenerationJob:
        """Generate a 3D model synchronously via Stability AI.

        The Stability 3D endpoint returns the model in the response body,
        so this method downloads the result immediately and returns a
        ``SUCCEEDED`` job.

        Args:
            prompt: Text description of the desired 3D model.
            format: Desired output format (stored for metadata).
            style: Ignored for Stability AI.

        Returns:
            :class:`GenerationJob` with ``SUCCEEDED`` status.
        """
        job_id = f"stability-{uuid.uuid4().hex[:12]}"
        output_dir = kwargs.get(
            "output_dir",
            os.path.join(tempfile.gettempdir(), "kiln_generated"),
        )
        os.makedirs(output_dir, exist_ok=True)
        out_path = os.path.join(output_dir, f"{job_id}.glb")

        # Stability 3D uses multipart form data.
        form_data = {
            "prompt": (None, prompt),
            "output_format": (None, "glb"),
        }

        resp = self._request(
            "POST",
            f"{_BASE_URL}/generate",
            files=form_data,
            timeout=self._timeout,
        )

        # Write the response body (the model file) to disk.
        with open(out_path, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=65536):
                fh.write(chunk)

        file_size = os.path.getsize(out_path)
        if file_size == 0:
            job = GenerationJob(
                id=job_id,
                provider=self.name,
                prompt=prompt,
                status=GenerationStatus.FAILED,
                progress=0,
                created_at=time.time(),
                format="glb",
                error="Stability AI returned an empty response.",
            )
            self._jobs[job_id] = job
            return job

        self._paths[job_id] = out_path
        self._prompts[job_id] = prompt

        job = GenerationJob(
            id=job_id,
            provider=self.name,
            prompt=prompt,
            status=GenerationStatus.SUCCEEDED,
            progress=100,
            created_at=time.time(),
            format="glb",
            style=style,
        )
        self._jobs[job_id] = job
        return job

    def get_job_status(self, job_id: str) -> GenerationJob:
        """Return the stored job state.

        Stability AI jobs are synchronous, so this simply returns the
        result from :meth:`generate`.
        """
        job = self._jobs.get(job_id)
        if not job:
            raise GenerationError(
                f"Job {job_id!r} not found.",
                code="JOB_NOT_FOUND",
            )
        return job

    def download_result(
        self,
        job_id: str,
        output_dir: str = os.path.join(tempfile.gettempdir(), "kiln_generated"),
    ) -> GenerationResult:
        """Return the path to the already-generated model.

        For Stability AI, the file is generated synchronously during
        :meth:`generate`, so this just verifies the file exists.
        """
        path = self._paths.get(job_id)
        if not path or not os.path.isfile(path):
            raise GenerationError(
                f"No generated file for job {job_id!r}.",
                code="NO_RESULT",
            )

        prompt = self._prompts.get(job_id, "")

        return GenerationResult(
            job_id=job_id,
            provider=self.name,
            local_path=path,
            format="glb",
            file_size_bytes=os.path.getsize(path),
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
        files: dict[str, Any] | None = None,
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
                    files=files,
                    json=json_body,
                    timeout=req_timeout,
                    stream=stream,
                )
            except requests.ConnectionError:
                raise GenerationError(
                    "Could not connect to Stability AI API.",
                    code="CONNECTION_ERROR",
                ) from None
            except requests.Timeout:
                raise GenerationError(
                    "Stability AI API request timed out.",
                    code="TIMEOUT",
                ) from None

            if resp.status_code in (429, 502, 503, 504) and attempt < _MAX_RETRIES:
                wait = _RETRY_BACKOFF_BASE * (2**attempt)
                logger.warning(
                    "Stability AI API returned %d, retrying in %.0fs (attempt %d/%d)",
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
            "Stability AI API request failed after retries.",
            code="RETRY_EXHAUSTED",
        )

    def _handle_http_error(self, resp: requests.Response) -> None:
        """Raise a typed exception for non-2xx responses."""
        if resp.ok:
            return

        if resp.status_code == 401:
            raise GenerationAuthError(
                "Stability AI API key is invalid or expired.",
                code="AUTH_INVALID",
            )
        if resp.status_code == 429:
            raise GenerationError(
                "Stability AI API rate limit exceeded.  Try again later.",
                code="RATE_LIMITED",
            )

        body = ""
        try:
            body = resp.json().get("message", resp.text[:200])
        except Exception:
            body = resp.text[:200]

        raise GenerationError(
            f"Stability AI API error (HTTP {resp.status_code}): {body}",
            code="API_ERROR",
        )
