"""Meshy text-to-3D generation provider.

Integrates with the Meshy API v2 (https://docs.meshy.ai) to generate
3D models from text prompts.  Supports both preview (geometry-only,
fast) and refine (textured, slower) modes.

**Preview mode** (default): geometry without textures, suitable for
single-color 3D printing.

**Refine mode**: full textured output with UV mapping and PNG textures.
Required for multicolor printing with ``auto_multicolor_from_texture``.
Use ``generate(prompt, refine=True)`` or call ``refine(preview_job_id)``
on a completed preview.

Authentication
--------------
Set ``KILN_MESHY_API_KEY`` or pass ``api_key`` to the constructor.
"""

from __future__ import annotations

import base64
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

_BASE_URL = "https://api.meshy.ai/openapi/v2"
_BASE_URL_V1 = "https://api.meshy.ai/openapi/v1"

_STATUS_MAP: dict[str, GenerationStatus] = {
    "PENDING": GenerationStatus.PENDING,
    "IN_PROGRESS": GenerationStatus.IN_PROGRESS,
    "SUCCEEDED": GenerationStatus.SUCCEEDED,
    "FAILED": GenerationStatus.FAILED,
    "CANCELED": GenerationStatus.CANCELLED,
    "CANCELLED": GenerationStatus.CANCELLED,
}


_MAX_RETRIES = 3
_RETRY_BACKOFF_BASE = 2.0  # seconds: 2, 4, 8


def _rewrite_mtl_texture_paths(
    mtl_path: str, output_dir: str, job_id: str
) -> None:
    """Rewrite texture filenames in MTL to match downloaded local names.

    Meshy MTL files reference ``texture_0.png`` but we download as
    ``{job_id}_texture_0.png``.  This rewrites ``map_Kd`` lines to
    point to the local filenames.
    """
    import re

    with open(mtl_path, encoding="utf-8") as f:
        content = f.read()

    def _rewrite(match: re.Match[str]) -> str:
        original = match.group(1).strip()
        # Extract the texture index from the original filename
        idx_match = re.search(r"texture[_.]?(\d+)", original)
        idx = idx_match.group(1) if idx_match else "0"
        local_name = f"{job_id}_texture_{idx}.png"
        return f"map_Kd {local_name}"

    rewritten = re.sub(r"map_Kd\s+(.+)", _rewrite, content)

    with open(mtl_path, "w", encoding="utf-8") as f:
        f.write(rewritten)


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
        self._results: dict[str, dict[str, str]] = {}
        self._prompts: dict[str, str] = {}
        # Track image-to-3D jobs (different polling endpoint).
        self._image_jobs: set[str] = set()
        # Track refine jobs and their texture URLs.
        self._refine_jobs: set[str] = set()
        self._texture_urls: dict[str, list[dict[str, str]]] = {}
        # Track retexture jobs (v1 API, different polling endpoint).
        self._retexture_jobs: set[str] = set()

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
            **kwargs: Pass ``image_url`` to use image-to-3D instead of
                text-to-3D.

        Returns:
            :class:`GenerationJob` with ``PENDING`` status.
        """
        # Route to image-to-3D if an image_url is provided.
        image_url: str | None = kwargs.get("image_url")
        if image_url:
            return self._generate_from_image(image_url, format=format, style=style)

        body: dict[str, Any] = {
            "mode": "preview",
            "prompt": prompt[:600],
            "ai_model": "meshy-6",
            "topology": "triangle",
            "target_polycount": 30000,
        }
        if style:
            body["art_style"] = style

        resp = self._request("POST", f"{_BASE_URL}/text-to-3d", json_body=body)

        data = resp.json()
        task_id = data.get("result", "")
        if not task_id:
            raise GenerationError("Meshy API returned no task ID.", code="INVALID_RESPONSE")

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

    def refine(
        self,
        preview_job_id: str,
        *,
        format: str = "obj",
    ) -> GenerationJob:
        """Refine a preview model to add textures and UV mapping.

        Takes a completed preview job and submits it for texture
        generation.  The refined model will have UV coordinates and
        PNG textures suitable for ``auto_multicolor_from_texture``.

        Args:
            preview_job_id: Task ID of a completed preview job.
            format: Desired output format for download.

        Returns:
            :class:`GenerationJob` with ``PENDING`` status for the refine task.
        """
        body: dict[str, Any] = {
            "mode": "refine",
            "preview_task_id": preview_job_id,
        }

        resp = self._request("POST", f"{_BASE_URL}/text-to-3d", json_body=body)
        data = resp.json()
        refine_id = data.get("result", "")
        if not refine_id:
            raise GenerationError(
                "Meshy refine API returned no task ID.",
                code="INVALID_RESPONSE",
            )

        prompt = self._prompts.get(preview_job_id, f"[refine] {preview_job_id}")
        self._prompts[refine_id] = prompt
        self._refine_jobs.add(refine_id)

        return GenerationJob(
            id=refine_id,
            provider=self.name,
            prompt=prompt,
            status=GenerationStatus.PENDING,
            progress=0,
            created_at=time.time(),
            format=format,
        )

    def retexture(
        self,
        mesh_path: str,
        prompt: str,
        *,
        style: str = "realistic",
        remove_lighting: bool = True,
        format: str = "obj",
    ) -> GenerationJob:
        """Apply AI-generated texture to an existing 3D mesh.

        Uses Meshy's retexture API to generate UV-mapped textures from a
        text prompt.  The mesh is uploaded and Meshy generates a texture
        map that matches the prompt description.

        :param mesh_path: Local path to STL/OBJ/GLB/FBX file.
        :param prompt: Text description of desired texture (max 600 chars).
        :param style: Art style — ``"realistic"`` or ``"2.5d-cartoon"``.
        :param remove_lighting: Strip baked lighting from albedo (cleaner for FDM).
        :param format: Desired output format.
        :returns: GenerationJob with PENDING status.
        """
        if not os.path.isfile(mesh_path):
            raise GenerationError(
                f"Mesh file not found: {mesh_path}",
                code="FILE_NOT_FOUND",
            )

        with open(mesh_path, "rb") as f:
            raw = f.read()
        b64 = base64.b64encode(raw).decode("ascii")

        body: dict[str, Any] = {
            "model_url": f"data:application/octet-stream;base64,{b64}",
            "text_style_prompt": prompt[:600],
            "enable_original_uv": True,
            "enable_pbr": False,
            "remove_lighting": remove_lighting,
        }

        resp = self._request("POST", f"{_BASE_URL_V1}/retexture", json_body=body)

        data = resp.json()
        task_id = data.get("result", "")
        if not task_id:
            raise GenerationError(
                "Meshy retexture API returned no task ID.",
                code="INVALID_RESPONSE",
            )

        self._prompts[task_id] = prompt
        self._retexture_jobs.add(task_id)
        # Mark as refine so download_result fetches textures.
        self._refine_jobs.add(task_id)

        return GenerationJob(
            id=task_id,
            provider=self.name,
            prompt=prompt,
            status=GenerationStatus.PENDING,
            progress=0,
            created_at=time.time(),
            format=format,
        )

    def _generate_from_image(
        self,
        image_url: str,
        *,
        format: str = "stl",
        style: str | None = None,
    ) -> GenerationJob:
        """Submit an image-to-3D generation task to Meshy.

        Args:
            image_url: URL of a reference image to reconstruct as 3D.
            format: Desired output format (stored for later download).
            style: Optional art style.

        Returns:
            :class:`GenerationJob` with ``PENDING`` status.
        """
        body: dict[str, Any] = {
            "image_url": image_url,
            "ai_model": "meshy-6",
            "topology": "triangle",
            "target_polycount": 30000,
        }
        if style:
            body["art_style"] = style

        resp = self._request("POST", f"{_BASE_URL}/image-to-3d", json_body=body)

        data = resp.json()
        task_id = data.get("result", "")
        if not task_id:
            raise GenerationError(
                "Meshy image-to-3D API returned no task ID.",
                code="INVALID_RESPONSE",
            )

        self._prompts[task_id] = f"[image-to-3d] {image_url}"
        self._image_jobs.add(task_id)

        return GenerationJob(
            id=task_id,
            provider=self.name,
            prompt=f"[image-to-3d] {image_url}",
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
        if job_id in self._retexture_jobs:
            resp = self._request("GET", f"{_BASE_URL_V1}/retexture/{job_id}")
        else:
            endpoint = "image-to-3d" if job_id in self._image_jobs else "text-to-3d"
            resp = self._request("GET", f"{_BASE_URL}/{endpoint}/{job_id}")

        data = resp.json()
        status_str = data.get("status", "PENDING")
        status = _STATUS_MAP.get(status_str, GenerationStatus.PENDING)
        progress = data.get("progress", 0)
        prompt = data.get("prompt", self._prompts.get(job_id, ""))

        # Cache model URLs for download.
        model_urls = data.get("model_urls")
        if model_urls and isinstance(model_urls, dict):
            self._results[job_id] = model_urls

        # Cache texture URLs for refined models.
        texture_urls = data.get("texture_urls")
        if texture_urls and isinstance(texture_urls, list):
            self._texture_urls[job_id] = texture_urls

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
            created_at=data.get("created_at", 0) / 1000.0 if data.get("created_at") else 0.0,
            format="obj",
            style=data.get("art_style"),
            error=error_msg,
        )

    def download_result(
        self,
        job_id: str,
        output_dir: str = os.path.join(tempfile.gettempdir(), "kiln_generated"),
    ) -> GenerationResult:
        """Download the generated model file and associated textures.

        Prefers OBJ format (best for 3D printing pipelines),
        falls back to GLB.  For refined jobs, also downloads the MTL
        material file and PNG textures alongside the OBJ so
        ``auto_multicolor_from_texture`` can consume them directly.

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
                    f"No model URLs available for job {job_id}.  Job status: {job.status.value}",
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

        resp = self._request("GET", url, timeout=120, stream=True)

        with open(out_path, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=65536):
                fh.write(chunk)

        file_size = os.path.getsize(out_path)
        prompt = self._prompts.get(job_id, "")

        # For refined jobs: download MTL and textures alongside the OBJ
        # so auto_multicolor_from_texture can find them by convention.
        if ext == "obj" and job_id in self._refine_jobs:
            # Download MTL
            mtl_url = model_urls.get("mtl")
            if mtl_url:
                mtl_path = os.path.join(output_dir, f"{job_id}.mtl")
                try:
                    mtl_resp = self._request("GET", mtl_url, timeout=60, stream=True)
                    with open(mtl_path, "wb") as mf:
                        for chunk in mtl_resp.iter_content(chunk_size=65536):
                            mf.write(chunk)
                    # Rewrite MTL to use local texture filename
                    _rewrite_mtl_texture_paths(mtl_path, output_dir, job_id)
                except Exception:
                    logger.debug("Failed to download MTL for %s", job_id, exc_info=True)

            # Download texture PNGs
            tex_urls = self._texture_urls.get(job_id, [])
            for i, tex_entry in enumerate(tex_urls):
                tex_url = tex_entry.get("base_color", "")
                if tex_url:
                    tex_name = f"{job_id}_texture_{i}.png"
                    tex_path = os.path.join(output_dir, tex_name)
                    try:
                        tex_resp = self._request("GET", tex_url, timeout=120, stream=True)
                        with open(tex_path, "wb") as tf:
                            for chunk in tex_resp.iter_content(chunk_size=65536):
                                tf.write(chunk)
                        logger.info("Downloaded texture: %s (%d bytes)",
                                     tex_name, os.path.getsize(tex_path))
                    except Exception:
                        logger.debug("Failed to download texture %d for %s",
                                      i, job_id, exc_info=True)

        return GenerationResult(
            job_id=job_id,
            provider=self.name,
            local_path=out_path,
            format=ext,
            file_size_bytes=file_size,
            prompt=prompt,
        )

    def list_styles(self) -> list[str]:
        return ["realistic", "sculpture"]

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
                raise GenerationError("Could not connect to Meshy API.", code="CONNECTION_ERROR") from None
            except requests.Timeout:
                raise GenerationError("Meshy API request timed out.", code="TIMEOUT") from None

            if resp.status_code in (429, 502, 503, 504) and attempt < _MAX_RETRIES:
                wait = _RETRY_BACKOFF_BASE * (2**attempt)
                logger.warning(
                    "Meshy API returned %d, retrying in %.0fs (attempt %d/%d)",
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
        raise GenerationError("Meshy API request failed after retries.", code="RETRY_EXHAUSTED")

    def _handle_http_error(self, resp: requests.Response) -> None:
        """Raise a typed exception for non-2xx responses."""
        if resp.ok:
            return

        if resp.status_code == 401:
            raise GenerationAuthError("Meshy API key is invalid or expired.", code="AUTH_INVALID")
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
