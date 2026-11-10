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

A task id from Meshy's own app is read and downloaded like one Kiln
started: Kiln asks each kind of Meshy task for it and keeps the kind that
answers.

Authentication
--------------
Set ``KILN_MESHY_API_KEY`` or pass ``api_key`` to the constructor.
"""

from __future__ import annotations

import base64
import logging
import os
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
from kiln.generation.job_records import JobRecord, JobRecordStore, MemoryJobRecords, made_from_part

logger = logging.getLogger(__name__)

#: How an image-to-3D job's prompt names the picture it was made from.
_IMAGE_PROMPT_PREFIX = "[image-to-3d] "


def _source_image(is_image_job: bool, prompt: str) -> str:
    """The picture an image-to-3D job was made from, read back off the
    prompt it recorded; "" for any other job."""
    if is_image_job and prompt.startswith(_IMAGE_PROMPT_PREFIX):
        return prompt[len(_IMAGE_PROMPT_PREFIX):]
    return ""

_BASE_URL = "https://api.meshy.ai/openapi/v2"
_BASE_URL_V1 = "https://api.meshy.ai/openapi/v1"

#: Where each kind of task is read, by route (:data:`kiln.generation.job_records.ROUTES`).
#: An image task is read at v1 first and at v2 when v1 does not hold it.
_ROUTE_ENDPOINTS: dict[str, tuple[str, ...]] = {
    "": (f"{_BASE_URL}/text-to-3d",),
    "text": (f"{_BASE_URL}/text-to-3d",),
    "refine": (f"{_BASE_URL}/text-to-3d",),
    "image": (f"{_BASE_URL_V1}/image-to-3d", f"{_BASE_URL}/image-to-3d"),
    "multi_image": (f"{_BASE_URL_V1}/multi-image-to-3d",),
    "retexture": (f"{_BASE_URL_V1}/retexture",),
    "remesh": (f"{_BASE_URL_V1}/remesh",),
}

#: A task id Kiln holds no record of is asked about at each kind in turn.
_PROBE_ORDER: tuple[tuple[str, str], ...] = (
    ("text", f"{_BASE_URL}/text-to-3d"),
    ("image", f"{_BASE_URL_V1}/image-to-3d"),
    ("multi_image", f"{_BASE_URL_V1}/multi-image-to-3d"),
    ("retexture", f"{_BASE_URL_V1}/retexture"),
    ("remesh", f"{_BASE_URL_V1}/remesh"),
)

#: A task's own ``type`` names its route.
_ROUTE_BY_TYPE: dict[str, str] = {
    "text-to-3d-preview": "text",
    "text-to-3d-refine": "refine",
    "image-to-3d": "image",
    "multi-image-to-3d": "multi_image",
    "retexture": "retexture",
    "remesh": "remesh",
}

#: The answers that mean "not a task of this kind": the next kind is asked.
#: Any other refusal (the key, the account, the rate, the service) stops.
_NOT_THIS_KIND = frozenset({400, 404, 422})

#: The model formats Kiln takes, best first.
_FORMAT_PREFERENCE = ("obj", "glb", "stl", "fbx")

#: A signed download link that answers one of these has expired.
_EXPIRED_LINK = frozenset({403, 404, 410})

_EXPIRED_MESSAGE = (
    "Meshy's download links for this task have expired. "
    "Export it again from Meshy, or make it again."
)


def _task_not_found(job_id: str) -> GenerationError:
    return GenerationError(
        f"Meshy has no task {job_id} under this API key. It may have been made "
        "under a different Meshy account (use that account's key), the id may "
        "be mistyped, or it may be older than Meshy keeps finished models "
        "(about 3 days).",
        code="TASK_NOT_FOUND",
    )


def _task_words(data: dict[str, Any]) -> str:
    """What the task was asked for, in Meshy's own response: the prompt, else
    the retexture's style prompt, else the image task's texture prompt.  A
    value that is a link is not words and is skipped."""
    for key in ("prompt", "text_style_prompt", "texture_prompt"):
        value = data.get(key)
        text = value.strip() if isinstance(value, str) else ""
        if text and not text.lower().startswith(("http://", "https://")):
            return text
    return ""


def _pick_format(model_urls: dict[str, Any]) -> str:
    """The first format in :data:`_FORMAT_PREFERENCE` the task offers, or ""."""
    for fmt in _FORMAT_PREFERENCE:
        if model_urls.get(fmt):
            return fmt
    return ""


class _MeshyHTTPError(GenerationError):
    """A refusal from Meshy's API, carrying its HTTP status."""

    def __init__(self, message: str, *, code: str, status: int) -> None:
        super().__init__(message, code=code)
        self.status = status


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
        # Every job: its words, and which kind of task it is, which names
        # the endpoint it is read at (_ROUTE_ENDPOINTS,
        # kiln.generation.job_records).
        self._local_jobs = MemoryJobRecords()
        # Download URLs from the last poll on this machine.  Signed by Meshy
        # and asked for again on a miss, so never kept anywhere else.
        self._results: dict[str, dict[str, str]] = {}
        self._texture_urls: dict[str, list[dict[str, str]]] = {}
        # When those links stop working (ms since the epoch), and the words
        # the task was asked for, as Meshy gave them on the last poll.
        self._expires_at: dict[str, int] = {}
        self._words: dict[str, str] = {}

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
        # Resolved first: a store that cannot keep the job refuses before
        # anything is asked of Meshy.
        records = self._job_records()
        # Route to image-to-3D if an image_url is provided.
        image_url: str | None = kwargs.get("image_url")
        if image_url:
            return self._generate_from_image(records, image_url, format=format, style=style)

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

        job = GenerationJob(
            id=task_id,
            provider=self.name,
            prompt=prompt,
            status=GenerationStatus.PENDING,
            progress=0,
            created_at=time.time(),
            format=format,
            style=style,
        )
        records.put(JobRecord.from_job(job, route="text"))
        return job

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
        records = self._job_records()
        # Only a preview this caller started can be refined: Meshy would
        # refine any preview its key owns.
        preview = self._known_job(records, preview_job_id)
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

        prompt = preview.prompt if preview is not None else f"[refine] {preview_job_id}"
        job = GenerationJob(
            id=refine_id,
            provider=self.name,
            prompt=prompt,
            status=GenerationStatus.PENDING,
            progress=0,
            created_at=time.time(),
            format=format,
        )
        records.put(JobRecord.from_job(
            job, route="refine", made_from=preview.made_from if preview is not None else None,
        ))
        return job

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

        # Resolved first: a store that cannot keep the job refuses before
        # anything is asked of Meshy.
        records = self._job_records()
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

        job = GenerationJob(
            id=task_id,
            provider=self.name,
            prompt=prompt,
            status=GenerationStatus.PENDING,
            progress=0,
            created_at=time.time(),
            format=format,
        )
        # The part it was made from, by content: the textured model that
        # comes back is that part's next version.
        records.put(JobRecord.from_job(job, route="retexture", made_from=made_from_part(raw)))
        return job

    def _generate_from_image(
        self,
        records: JobRecordStore,
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

        job = GenerationJob(
            id=task_id,
            provider=self.name,
            prompt=f"{_IMAGE_PROMPT_PREFIX}{image_url}",
            status=GenerationStatus.PENDING,
            progress=0,
            created_at=time.time(),
            format=format,
            style=style,
        )
        records.put(JobRecord.from_job(job, route="image"))
        return job

    def get_job_status(self, job_id: str) -> GenerationJob:
        """Poll a Meshy task.

        A task Kiln started is read where its kind is kept.  A task id with
        no record (made in Meshy's own app, or by Kiln in another process)
        is asked about at each kind in turn; the kind that answers is kept
        as the job's record, so the next poll asks once.  A strict store's
        missing record is ``JOB_NOT_FOUND`` and Meshy is not asked.

        Args:
            job_id: Task ID from :meth:`generate`, or from Meshy's own app.

        Returns:
            Updated :class:`GenerationJob`.

        Raises:
            GenerationError: ``TASK_NOT_FOUND`` when no kind holds the id;
                ``AUTH_INVALID``, ``NO_CREDITS``, ``FORBIDDEN`` or
                ``RATE_LIMITED`` as Meshy answers.
        """
        records = self._job_records()
        record = self._known_job(records, job_id)
        if record is not None:
            endpoints = _ROUTE_ENDPOINTS.get(record.route, _ROUTE_ENDPOINTS[""])
            data, route = self._first_answer(job_id, [(record.route, e) for e in endpoints])
        else:
            data, route = self._first_answer(job_id, _PROBE_ORDER)
            route = _ROUTE_BY_TYPE.get(str(data.get("type") or ""), route)

        status_str = data.get("status", "PENDING")
        status = _STATUS_MAP.get(status_str, GenerationStatus.PENDING)
        progress = data.get("progress", 0)
        words = _task_words(data)
        if words:
            self._words[job_id] = words
        prompt = (record.prompt if record is not None and record.prompt else "") or words

        # Cache model URLs for download.
        model_urls = data.get("model_urls")
        if model_urls and isinstance(model_urls, dict):
            self._results[job_id] = model_urls

        # Cache texture URLs for refined models.
        texture_urls = data.get("texture_urls")
        if texture_urls and isinstance(texture_urls, list):
            self._texture_urls[job_id] = texture_urls

        expires_at = data.get("expires_at")
        if isinstance(expires_at, (int, float)) and expires_at > 0:
            self._expires_at[job_id] = int(expires_at)

        error_msg: str | None = None
        task_error = data.get("task_error")
        if task_error and isinstance(task_error, dict):
            error_msg = task_error.get("message") or None

        offered = _pick_format(model_urls) if isinstance(model_urls, dict) else ""
        job = GenerationJob(
            id=job_id,
            provider=self.name,
            prompt=prompt,
            status=status,
            progress=progress,
            created_at=data.get("created_at", 0) / 1000.0 if data.get("created_at") else 0.0,
            format=offered or "obj",
            style=data.get("art_style"),
            error=error_msg,
        )
        if record is None:
            # Remembered by the kind that answered: the next poll asks once.
            records.put(JobRecord.from_job(job, route=route))
        return job

    def download_result(
        self,
        job_id: str,
        output_dir: str | None = None,
    ) -> GenerationResult:
        """Download the generated model file and associated textures.

        The task is polled first, so its links are fresh.  The format is
        the first the task offers of OBJ, GLB, STL and FBX.  An OBJ that
        comes with an MTL brings its MTL and PNG textures alongside, so
        ``auto_multicolor_from_texture`` can consume them directly.  Files
        are fetched from Meshy's signed links without the API key.

        Args:
            job_id: Task ID of a completed task, Kiln's or from Meshy's own app.
            output_dir: Directory to save the file.

        Raises:
            GenerationError: ``NOT_READY`` while the task is still being
                made; ``GENERATION_FAILED`` when it failed or was canceled;
                ``RESULT_EXPIRED`` when its download links have expired;
                any code :meth:`get_job_status` raises.
        """
        records = self._job_records()
        self._known_job(records, job_id)
        job = self.get_job_status(job_id)
        record = records.get(self.name, job_id)

        if job.status in (GenerationStatus.PENDING, GenerationStatus.IN_PROGRESS):
            raise GenerationError(
                f"Meshy is still making task {job_id} ({int(job.progress or 0)}% done). "
                "Wait for it to finish (await_generation), then download it.",
                code="NOT_READY",
            )
        if job.status in (GenerationStatus.FAILED, GenerationStatus.CANCELLED):
            ended = "failed" if job.status is GenerationStatus.FAILED else "was canceled"
            detail = f": {job.error}" if job.error else "."
            raise GenerationError(f"Meshy task {job_id} {ended}{detail}", code="GENERATION_FAILED")

        model_urls = self._results.get(job_id)
        if not model_urls:
            raise GenerationError(
                f"No model URLs available for job {job_id}.  Job status: {job.status.value}",
                code="NO_RESULT",
            )
        expires_at = self._expires_at.get(job_id)
        if expires_at is not None and expires_at <= time.time() * 1000:
            raise GenerationError(_EXPIRED_MESSAGE, code="RESULT_EXPIRED")

        ext = _pick_format(model_urls)
        if not ext:
            raise GenerationError(
                "No downloadable format found in Meshy results.",
                code="NO_FORMAT",
            )

        from kiln.output_paths import kiln_output_dir

        output_dir = output_dir or kiln_output_dir("kiln_generated")
        os.makedirs(output_dir, exist_ok=True)
        out_path = os.path.join(output_dir, f"{job_id}.{ext}")

        self._fetch_asset(model_urls[ext], out_path, timeout=120)

        if ext == "obj":
            # Meshy writes its OBJ in glTF's frame (+Y up), so read as
            # written every model lies on its back.  Its GLB is turned by
            # load_mesh; the OBJ is turned here, as it lands.
            from kiln.mesh_frame import stand_up_gltf_frame_obj

            stand_up_gltf_frame_obj(out_path)

        file_size = os.path.getsize(out_path)
        prompt = (record.prompt if record is not None and record.prompt else "") or self._words.get(job_id, "")

        # An OBJ's material and textures land beside it, so
        # auto_multicolor_from_texture can find them by convention.
        if ext == "obj":
            mtl_url = model_urls.get("mtl")
            if mtl_url:
                mtl_path = os.path.join(output_dir, f"{job_id}.mtl")
                try:
                    self._fetch_asset(mtl_url, mtl_path, timeout=60)
                    # Rewrite MTL to use local texture filename
                    _rewrite_mtl_texture_paths(mtl_path, output_dir, job_id)
                except Exception:
                    logger.debug("Failed to download MTL for %s", job_id, exc_info=True)

            for i, tex_entry in enumerate(self._texture_urls.get(job_id, [])):
                tex_url = tex_entry.get("base_color", "") if isinstance(tex_entry, dict) else ""
                if tex_url:
                    tex_name = f"{job_id}_texture_{i}.png"
                    tex_path = os.path.join(output_dir, tex_name)
                    try:
                        self._fetch_asset(tex_url, tex_path, timeout=120)
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
            source_image=_source_image(record is not None and record.route == "image", prompt),
        )

    def list_styles(self) -> list[str]:
        return ["realistic", "sculpture"]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _first_answer(
        self, job_id: str, candidates: Any,
    ) -> tuple[dict[str, Any], str]:
        """The task's data from the first of *candidates* (route, endpoint)
        that holds it, and that route.  An answer meaning "not this kind"
        moves to the next; any other refusal is raised as it stands."""
        for route, endpoint in candidates:
            try:
                resp = self._request("GET", f"{endpoint}/{job_id}")
            except _MeshyHTTPError as exc:
                if exc.status in _NOT_THIS_KIND:
                    continue
                raise
            data = resp.json()
            return (data if isinstance(data, dict) else {}), route
        raise _task_not_found(job_id)

    def _fetch_asset(self, url: str, path: str, *, timeout: int) -> None:
        """Write the file at Meshy's signed *url* to *path*.  The link
        carries its own signature, so the API key is not sent with it."""
        try:
            resp = requests.get(url, timeout=timeout, stream=True)
        except requests.ConnectionError:
            raise GenerationError("Could not connect to Meshy's file host.", code="CONNECTION_ERROR") from None
        except requests.Timeout:
            raise GenerationError("Meshy's file host timed out.", code="TIMEOUT") from None
        if resp.status_code in _EXPIRED_LINK:
            raise GenerationError(_EXPIRED_MESSAGE, code="RESULT_EXPIRED")
        if not resp.ok:
            raise GenerationError(
                f"Meshy's file host answered HTTP {resp.status_code}.",
                code="DOWNLOAD_ERROR",
            )
        with open(path, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=65536):
                fh.write(chunk)

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
            raise _MeshyHTTPError(
                "Meshy API rate limit exceeded.  Try again later.",
                code="RATE_LIMITED",
                status=429,
            )

        body = ""
        try:
            body = resp.json().get("message", resp.text[:200])
        except Exception:
            body = resp.text[:200]

        if resp.status_code == 402:
            raise _MeshyHTTPError(
                "The Meshy account for this API key is out of credits. "
                "Add credits in Meshy, then try again.",
                code="NO_CREDITS",
                status=402,
            )
        if resp.status_code == 403:
            raise _MeshyHTTPError(
                f"Meshy refused this request for this API key: {body}",
                code="FORBIDDEN",
                status=403,
            )

        raise _MeshyHTTPError(
            f"Meshy API error (HTTP {resp.status_code}): {body}",
            code="API_ERROR",
            status=resp.status_code,
        )
