"""Meshy text-to-3D generation provider.

Integrates with the Meshy API (https://docs.meshy.ai) to generate
3D models from text prompts.  Uses the preview mode which produces
geometry without textures — suitable for 3D printing.

Authentication
--------------
Set ``KILN_MESHY_API_KEY`` or pass ``api_key`` to the constructor.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, List, Optional

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

_BASE_URL = "https://api.meshy.ai/openapi/v2"

_STATUS_MAP: Dict[str, GenerationStatus] = {
    "PENDING": GenerationStatus.PENDING,
    "IN_PROGRESS": GenerationStatus.IN_PROGRESS,
    "SUCCEEDED": GenerationStatus.SUCCEEDED,
    "FAILED": GenerationStatus.FAILED,
    "CANCELED": GenerationStatus.CANCELLED,
    "CANCELLED": GenerationStatus.CANCELLED,
}


class MeshyProvider(GenerationProvider):
    """Meshy text-to-3D generation via REST API.

    Args:
        api_key: Meshy API key.  Falls back to ``KILN_MESHY_API_KEY``.
        timeout: HTTP request timeout in seconds.
    """

    def __init__(
        self,
        api_key: str = "",
        *,
        timeout: int = 30,
    ) -> None:
        self._api_key = api_key or os.environ.get("KILN_MESHY_API_KEY", "")
        if not self._api_key:
            raise GenerationAuthError(
                "Meshy API key required.  Set KILN_MESHY_API_KEY or pass api_key.",
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
        self._results: Dict[str, Dict[str, str]] = {}
        self._prompts: Dict[str, str] = {}

    @property
    def name(self) -> str:
        return "meshy"

    @property
    def display_name(self) -> str:
        return "Meshy"

    def generate(
        self,
        prompt: str,
        *,
        format: str = "stl",
        style: str | None = None,
        **kwargs: Any,
    ) -> GenerationJob:
        """Submit a preview generation task to Meshy.

        Args:
            prompt: Text description (max 600 characters).
            format: Desired output format (stored for later download).
            style: Optional art style (``"realistic"`` or ``"sculpture"``).

        Returns:
            :class:`GenerationJob` with ``PENDING`` status.
        """
        body: Dict[str, Any] = {
            "mode": "preview",
            "prompt": prompt[:600],
            "ai_model": "meshy-6",
            "topology": "triangle",
            "target_polycount": 30000,
        }
        if style:
            body["art_style"] = style

        try:
            resp = self._session.post(
                f"{_BASE_URL}/text-to-3d",
                json=body,
                timeout=self._timeout,
            )
        except requests.ConnectionError:
            raise GenerationError(
                "Could not connect to Meshy API.", code="CONNECTION_ERROR"
            )
        except requests.Timeout:
            raise GenerationError(
                "Meshy API request timed out.", code="TIMEOUT"
            )

        self._handle_http_error(resp)

        data = resp.json()
        task_id = data.get("result", "")
        if not task_id:
            raise GenerationError(
                "Meshy API returned no task ID.", code="INVALID_RESPONSE"
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
        """Poll a Meshy generation task.

        Args:
            job_id: Task ID from :meth:`generate`.

        Returns:
            Updated :class:`GenerationJob`.
        """
        try:
            resp = self._session.get(
                f"{_BASE_URL}/text-to-3d/{job_id}",
                timeout=self._timeout,
            )
        except requests.ConnectionError:
            raise GenerationError(
                "Could not connect to Meshy API.", code="CONNECTION_ERROR"
            )
        except requests.Timeout:
            raise GenerationError(
                "Meshy API request timed out.", code="TIMEOUT"
            )

        self._handle_http_error(resp)

        data = resp.json()
        status_str = data.get("status", "PENDING")
        status = _STATUS_MAP.get(status_str, GenerationStatus.PENDING)
        progress = data.get("progress", 0)
        prompt = data.get("prompt", self._prompts.get(job_id, ""))

        # Cache model URLs for download.
        model_urls = data.get("model_urls")
        if model_urls and isinstance(model_urls, dict):
            self._results[job_id] = model_urls

        error_msg: str | None = None
        task_error = data.get("task_error")
        if task_error and isinstance(task_error, dict):
            error_msg = task_error.get("message") or None

        return GenerationJob(
            id=job_id,
            provider=self.name,
            prompt=prompt,
            status=status,
            progress=progress,
            created_at=data.get("created_at", 0) / 1000.0
            if data.get("created_at")
            else 0.0,
            format="obj",
            style=data.get("art_style"),
            error=error_msg,
        )

    def download_result(
        self,
        job_id: str,
        output_dir: str = "/tmp/kiln_generated",
    ) -> GenerationResult:
        """Download the generated model file.

        Prefers OBJ format (best for 3D printing pipelines),
        falls back to GLB.

        Args:
            job_id: Task ID of a completed job.
            output_dir: Directory to save the file.
        """
        model_urls = self._results.get(job_id)
        if not model_urls:
            # Try polling once to populate.
            job = self.get_job_status(job_id)
            model_urls = self._results.get(job_id)
            if not model_urls:
                raise GenerationError(
                    f"No model URLs available for job {job_id}.  "
                    f"Job status: {job.status.value}",
                    code="NO_RESULT",
                )

        # Prefer OBJ > GLB for slicing compatibility.
        for fmt in ("obj", "glb", "fbx"):
            url = model_urls.get(fmt)
            if url:
                ext = fmt
                break
        else:
            raise GenerationError(
                "No downloadable format found in Meshy results.",
                code="NO_FORMAT",
            )

        os.makedirs(output_dir, exist_ok=True)
        out_path = os.path.join(output_dir, f"{job_id}.{ext}")

        try:
            resp = self._session.get(url, timeout=120, stream=True)
            resp.raise_for_status()
        except requests.ConnectionError:
            raise GenerationError(
                "Could not download model from Meshy.", code="CONNECTION_ERROR"
            )
        except requests.HTTPError as exc:
            raise GenerationError(
                f"Download failed: HTTP {exc.response.status_code}",
                code="DOWNLOAD_ERROR",
            )

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

    def list_styles(self) -> List[str]:
        return ["realistic", "sculpture"]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _handle_http_error(self, resp: requests.Response) -> None:
        """Raise a typed exception for non-2xx responses."""
        if resp.ok:
            return

        if resp.status_code == 401:
            raise GenerationAuthError(
                "Meshy API key is invalid or expired.", code="AUTH_INVALID"
            )
        if resp.status_code == 429:
            raise GenerationError(
                "Meshy API rate limit exceeded.  Try again later.",
                code="RATE_LIMITED",
            )

        body = ""
        try:
            body = resp.json().get("message", resp.text[:200])
        except Exception:
            body = resp.text[:200]

        raise GenerationError(
            f"Meshy API error (HTTP {resp.status_code}): {body}",
            code="API_ERROR",
        )
