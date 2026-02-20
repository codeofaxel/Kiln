"""Google Gemini Deep Think 3D generation provider.

Uses Google's Gemini API with extended thinking to generate printable
3D models from text or napkin-sketch descriptions.  Gemini reasons
deeply about the geometry ("deep think") and produces OpenSCAD code,
which is then compiled locally to STL.

This two-stage pipeline (AI reasoning → local compilation) produces
precise, parametric, watertight meshes ideal for 3D printing.

Authentication
--------------
Set ``KILN_GEMINI_API_KEY`` or pass ``api_key`` to the constructor.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import shutil
import subprocess
import sys
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

_GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models"
_DEFAULT_MODEL = "gemini-2.0-flash"
_REQUEST_TIMEOUT = 120
_MACOS_APP_PATH = "/Applications/OpenSCAD.app/Contents/MacOS/OpenSCAD" if sys.platform == "darwin" else ""

_MAX_RETRIES = 3
_RETRY_BACKOFF_BASE = 2.0  # seconds: 2, 4, 8

# System prompt that instructs Gemini to produce OpenSCAD code
_SYSTEM_PROMPT = """You are a 3D modeling expert. Given a text description or napkin-sketch description of a 3D object, generate valid OpenSCAD code that creates that object.

Requirements:
- Output ONLY valid OpenSCAD code, no explanations or markdown
- The model must be watertight (manifold) for 3D printing
- Use millimeters as the unit
- Keep polygon count reasonable (avoid extremely fine $fn values)
- Center the model at the origin when practical
- Make the model a practical size for 3D printing (typically 20-200mm)
- Use difference(), union(), intersection() for complex shapes
- Do NOT use import(), surface(), include, or use statements
- Think deeply about the geometry — consider all faces, edges, and proportions
- For organic shapes, use hull() and smooth approximations
- Aim for printability: flat bottom surface, no extreme overhangs when possible"""


def _find_openscad(explicit_path: str | None = None) -> str:
    """Locate the OpenSCAD binary.

    :param explicit_path: If provided, verify it exists and is executable.
    :returns: Absolute path to the OpenSCAD binary.
    :raises GenerationError: If no binary is found.
    """
    if explicit_path:
        if os.path.isfile(explicit_path) and os.access(explicit_path, os.X_OK):
            return explicit_path
        raise GenerationError(
            f"OpenSCAD binary not found at {explicit_path}",
            code="OPENSCAD_NOT_FOUND",
        )

    which = shutil.which("openscad")
    if which:
        return which

    if _MACOS_APP_PATH and os.path.isfile(_MACOS_APP_PATH) and os.access(_MACOS_APP_PATH, os.X_OK):
        return _MACOS_APP_PATH

    raise GenerationError(
        "OpenSCAD not found. Gemini Deep Think requires OpenSCAD to compile generated code.\n"
        "  Linux/WSL: apt install openscad\n"
        "  macOS: brew install openscad\n"
        "  Or download from https://openscad.org",
        code="OPENSCAD_NOT_FOUND",
    )


def _extract_openscad_code(text: str) -> str:
    """Extract OpenSCAD code from Gemini response.

    Handles responses that may include markdown code fences or
    plain OpenSCAD code.
    """
    # Try to extract from markdown code fence first
    match = re.search(r"```(?:openscad|scad)?\s*\n(.*?)```", text, re.DOTALL)
    if match:
        return match.group(1).strip()

    # If no code fence, try to find OpenSCAD-like content
    # Look for lines containing OpenSCAD keywords
    lines = text.strip().split("\n")
    scad_lines: list[str] = []
    in_code = False
    for line in lines:
        stripped = line.strip()
        # Detect start of OpenSCAD code
        if not in_code and re.match(
            r"^(//|/\*|\$|cube|sphere|cylinder|translate|rotate|scale|union|difference|intersection|module|linear_extrude|rotate_extrude|polygon|circle|square|hull|minkowski|color|mirror|resize|offset|text|polyhedron|for|if|let|function|echo)",
            stripped,
        ):
            in_code = True
        if in_code:
            scad_lines.append(line)

    if scad_lines:
        return "\n".join(scad_lines).strip()

    # Last resort: return the entire text and let OpenSCAD error if invalid
    return text.strip()


# Block dangerous OpenSCAD functions that could access the filesystem
_DANGEROUS_PATTERNS = [
    r"\bimport\s*\(",
    r"\bsurface\s*\(",
    r"\binclude\s*<",
    r"\buse\s*<",
]


class GeminiDeepThinkProvider(GenerationProvider):
    """Google Gemini Deep Think text-to-3D generation.

    Uses Gemini's reasoning capabilities to generate OpenSCAD code from
    natural language descriptions, then compiles it locally to STL.

    :param api_key: Google Gemini API key.  Falls back to
        ``KILN_GEMINI_API_KEY`` env var.
    :param model: Gemini model to use (default: ``gemini-2.0-flash``).
    :param openscad_path: Explicit path to the ``openscad`` binary.
    :param compile_timeout: Max OpenSCAD compilation time in seconds.
    """

    def __init__(
        self,
        api_key: str = "",
        *,
        model: str = _DEFAULT_MODEL,
        openscad_path: str | None = None,
        compile_timeout: int = 120,
    ) -> None:
        self._api_key = api_key or os.environ.get("KILN_GEMINI_API_KEY", "")
        if not self._api_key:
            raise GenerationAuthError(
                "Gemini API key required.  Set KILN_GEMINI_API_KEY or pass api_key.",
                code="AUTH_REQUIRED",
            )
        self._model = model
        self._openscad = _find_openscad(openscad_path)
        self._compile_timeout = compile_timeout
        self._session = requests.Session()
        self._jobs: dict[str, GenerationJob] = {}
        self._paths: dict[str, str] = {}
        self._prompts: dict[str, str] = {}
        self._scad_code: dict[str, str] = {}

    @property
    def name(self) -> str:
        return "gemini"

    @property
    def display_name(self) -> str:
        return "Gemini Deep Think"

    def generate(
        self,
        prompt: str,
        *,
        format: str = "stl",
        style: str | None = None,
        **kwargs: Any,
    ) -> GenerationJob:
        """Generate a 3D model from a text description via Gemini + OpenSCAD.

        Stage 1: Gemini reasons about the geometry and produces OpenSCAD code.
        Stage 2: OpenSCAD compiles the code to STL.

        :param prompt: Natural language description of the desired 3D model,
            or a description of a napkin sketch.
        :param format: Output format (only ``"stl"`` supported).
        :param style: Optional style hint (``"organic"``, ``"mechanical"``,
            ``"decorative"``).
        :returns: :class:`GenerationJob` with ``SUCCEEDED`` or ``FAILED`` status.
        """
        if format != "stl":
            raise GenerationError(
                f"Gemini Deep Think only supports STL output, got {format!r}.",
                code="UNSUPPORTED_FORMAT",
            )

        job_id = f"gemini-{uuid.uuid4().hex[:12]}"
        output_dir = kwargs.get(
            "output_dir",
            os.path.join(tempfile.gettempdir(), "kiln_generated"),
        )
        os.makedirs(output_dir, exist_ok=True)
        out_path = os.path.join(output_dir, f"{job_id}.stl")

        # Stage 1: Call Gemini API to generate OpenSCAD code
        style_hint = ""
        if style:
            style_hint = f"\nStyle preference: {style}."

        user_prompt = (
            f"Create a 3D printable model of: {prompt}{style_hint}\n\n"
            f"Think carefully about the geometry, proportions, and printability. "
            f"Output valid OpenSCAD code only."
        )

        try:
            scad_code = self._call_gemini(user_prompt)
        except GenerationError:
            raise
        except Exception as exc:
            job = GenerationJob(
                id=job_id,
                provider=self.name,
                prompt=prompt,
                status=GenerationStatus.FAILED,
                progress=0,
                created_at=time.time(),
                format=format,
                error=f"Gemini API call failed: {exc}",
            )
            self._jobs[job_id] = job
            return job

        # Safety check: block dangerous operations in the raw response
        # (before extraction, which could strip dangerous lines)
        for pattern in _DANGEROUS_PATTERNS:
            if re.search(pattern, scad_code, re.IGNORECASE):
                job = GenerationJob(
                    id=job_id,
                    provider=self.name,
                    prompt=prompt,
                    status=GenerationStatus.FAILED,
                    progress=0,
                    created_at=time.time(),
                    format=format,
                    error="Generated code contains blocked file I/O operations.",
                )
                self._jobs[job_id] = job
                return job

        # Extract and validate OpenSCAD code
        scad_code = _extract_openscad_code(scad_code)

        if not scad_code.strip():
            job = GenerationJob(
                id=job_id,
                provider=self.name,
                prompt=prompt,
                status=GenerationStatus.FAILED,
                progress=0,
                created_at=time.time(),
                format=format,
                error="Gemini returned no usable OpenSCAD code.",
            )
            self._jobs[job_id] = job
            return job

        # Double-check extracted code for safety
        for pattern in _DANGEROUS_PATTERNS:
            if re.search(pattern, scad_code, re.IGNORECASE):
                job = GenerationJob(
                    id=job_id,
                    provider=self.name,
                    prompt=prompt,
                    status=GenerationStatus.FAILED,
                    progress=0,
                    created_at=time.time(),
                    format=format,
                    error="Generated code contains blocked file I/O operations.",
                )
                self._jobs[job_id] = job
                return job

        self._scad_code[job_id] = scad_code

        # Stage 2: Compile OpenSCAD code to STL
        compile_result = self._compile_scad(scad_code, out_path, job_id, prompt, format)
        return compile_result

    def get_job_status(self, job_id: str) -> GenerationJob:
        """Return the stored job state.

        Gemini Deep Think jobs are synchronous, so this simply returns
        the result from :meth:`generate`.
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
        """Return the path to the already-generated STL.

        For Gemini Deep Think, the file is generated synchronously during
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
            format="stl",
            file_size_bytes=os.path.getsize(path),
            prompt=prompt,
        )

    def get_scad_code(self, job_id: str) -> str | None:
        """Return the generated OpenSCAD source code for a job.

        Useful for debugging or iterating on the generated geometry.
        """
        return self._scad_code.get(job_id)

    def list_styles(self) -> list[str]:
        return ["organic", "mechanical", "decorative"]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _call_gemini(self, prompt: str) -> str:
        """Call the Gemini API to generate OpenSCAD code."""
        url = f"{_GEMINI_API_URL}/{self._model}:generateContent"
        params = {"key": self._api_key}

        body: dict[str, Any] = {
            "contents": [
                {
                    "parts": [{"text": prompt}],
                }
            ],
            "systemInstruction": {
                "parts": [{"text": _SYSTEM_PROMPT}],
            },
            "generationConfig": {
                "temperature": 0.7,
                "maxOutputTokens": 8192,
            },
        }

        resp = self._request("POST", url, json_body=body, params=params)
        data = resp.json()

        # Extract text from Gemini response
        candidates = data.get("candidates", [])
        if not candidates:
            error_msg = data.get("error", {}).get("message", "No candidates returned.")
            raise GenerationError(
                f"Gemini returned no results: {error_msg}",
                code="NO_RESULT",
            )

        content = candidates[0].get("content", {})
        parts = content.get("parts", [])
        if not parts:
            raise GenerationError(
                "Gemini response contained no content parts.",
                code="EMPTY_RESPONSE",
            )

        text = parts[0].get("text", "")
        if not text:
            raise GenerationError(
                "Gemini returned empty text.",
                code="EMPTY_RESPONSE",
            )

        return text

    def _compile_scad(
        self,
        scad_code: str,
        out_path: str,
        job_id: str,
        prompt: str,
        format: str,
    ) -> GenerationJob:
        """Compile OpenSCAD code to STL."""
        scad_fd, scad_path = tempfile.mkstemp(suffix=".scad", prefix="kiln_gemini_")
        try:
            with os.fdopen(scad_fd, "w") as fh:
                fh.write(scad_code)

            cmd = [self._openscad, "-o", out_path, scad_path]
            logger.info("Gemini Deep Think: compiling OpenSCAD: %s", " ".join(cmd))

            work_dir = tempfile.mkdtemp(prefix="kiln_gemini_scad_")
            try:
                try:
                    result = subprocess.run(
                        cmd,
                        capture_output=True,
                        text=True,
                        timeout=self._compile_timeout,
                        cwd=work_dir,
                    )
                except subprocess.TimeoutExpired:
                    job = GenerationJob(
                        id=job_id,
                        provider=self.name,
                        prompt=prompt,
                        status=GenerationStatus.FAILED,
                        progress=0,
                        created_at=time.time(),
                        format=format,
                        error=f"OpenSCAD compilation timed out after {self._compile_timeout}s.",
                    )
                    self._jobs[job_id] = job
                    return job
                except OSError as exc:
                    raise GenerationError(
                        f"Failed to run OpenSCAD: {exc}",
                        code="OPENSCAD_EXEC_ERROR",
                    ) from exc
            finally:
                shutil.rmtree(work_dir, ignore_errors=True)

            if result.returncode != 0:
                stderr = (result.stderr or "").strip()[:500]
                job = GenerationJob(
                    id=job_id,
                    provider=self.name,
                    prompt=prompt,
                    status=GenerationStatus.FAILED,
                    progress=0,
                    created_at=time.time(),
                    format=format,
                    error=f"OpenSCAD compilation failed (exit {result.returncode}): {stderr}",
                )
                self._jobs[job_id] = job
                return job

            if not os.path.isfile(out_path) or os.path.getsize(out_path) == 0:
                job = GenerationJob(
                    id=job_id,
                    provider=self.name,
                    prompt=prompt,
                    status=GenerationStatus.FAILED,
                    progress=0,
                    created_at=time.time(),
                    format=format,
                    error="OpenSCAD produced no output file.",
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
                format=format,
            )
            self._jobs[job_id] = job
            return job

        finally:
            with contextlib.suppress(OSError):
                os.unlink(scad_path)

    def _request(
        self,
        method: str,
        url: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
        timeout: int | None = None,
        stream: bool = False,
    ) -> requests.Response:
        """Make an HTTP request with rate-limit retry and backoff.

        Retries up to ``_MAX_RETRIES`` times on HTTP 429 (rate limit)
        and 502/503/504 (transient server errors).  Uses exponential
        backoff: 2s, 4s, 8s.
        """
        req_timeout = timeout or _REQUEST_TIMEOUT

        for attempt in range(_MAX_RETRIES + 1):
            try:
                resp = self._session.request(
                    method,
                    url,
                    json=json_body,
                    params=params,
                    timeout=req_timeout,
                    stream=stream,
                )
            except requests.ConnectionError:
                raise GenerationError(
                    "Could not connect to Gemini API.",
                    code="CONNECTION_ERROR",
                ) from None
            except requests.Timeout:
                raise GenerationError(
                    "Gemini API request timed out.",
                    code="TIMEOUT",
                ) from None

            if resp.status_code in (429, 502, 503, 504) and attempt < _MAX_RETRIES:
                wait = _RETRY_BACKOFF_BASE * (2**attempt)
                logger.warning(
                    "Gemini API returned %d, retrying in %.0fs (attempt %d/%d)",
                    resp.status_code,
                    wait,
                    attempt + 1,
                    _MAX_RETRIES,
                )
                time.sleep(wait)
                continue

            self._handle_http_error(resp)
            return resp

        raise GenerationError(
            "Gemini API request failed after retries.",
            code="RETRY_EXHAUSTED",
        )

    def _handle_http_error(self, resp: requests.Response) -> None:
        """Raise a typed exception for non-2xx responses."""
        if resp.ok:
            return

        if resp.status_code in (401, 403):
            raise GenerationAuthError(
                "Gemini API key is invalid or expired.",
                code="AUTH_INVALID",
            )
        if resp.status_code == 429:
            raise GenerationError(
                "Gemini API rate limit exceeded.  Try again later.",
                code="RATE_LIMITED",
            )

        body = ""
        try:
            body = resp.json().get("error", {}).get("message", resp.text[:200])
        except Exception:
            body = resp.text[:200]

        raise GenerationError(
            f"Gemini API error (HTTP {resp.status_code}): {body}",
            code="API_ERROR",
        )
