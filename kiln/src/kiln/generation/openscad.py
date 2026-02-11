"""OpenSCAD local model generation provider.

Compiles OpenSCAD scripts into STL files using the ``openscad`` CLI.
The agent writes the ``.scad`` code; Kiln compiles it locally.  This
is deterministic, parametric, free, and ideal for geometric or
mechanical parts.

Requires OpenSCAD installed on the system.  Auto-detects the binary
from ``PATH`` or macOS application bundle.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from typing import Any, Dict, List, Optional

from kiln.generation.base import (
    GenerationError,
    GenerationJob,
    GenerationProvider,
    GenerationResult,
    GenerationStatus,
)

logger = logging.getLogger(__name__)

_MACOS_APP_PATH = "/Applications/OpenSCAD.app/Contents/MacOS/OpenSCAD"


def _find_openscad(explicit_path: str | None = None) -> str:
    """Locate the OpenSCAD binary.

    Args:
        explicit_path: If provided, verify it exists and is executable.

    Returns:
        Absolute path to the OpenSCAD binary.

    Raises:
        GenerationError: If no binary is found.
    """
    if explicit_path:
        if os.path.isfile(explicit_path) and os.access(explicit_path, os.X_OK):
            return explicit_path
        raise GenerationError(
            f"OpenSCAD binary not found at {explicit_path}",
            code="OPENSCAD_NOT_FOUND",
        )

    # Check PATH.
    which = shutil.which("openscad")
    if which:
        return which

    # Check macOS application bundle.
    if os.path.isfile(_MACOS_APP_PATH) and os.access(_MACOS_APP_PATH, os.X_OK):
        return _MACOS_APP_PATH

    raise GenerationError(
        "OpenSCAD not found.  Install it from https://openscad.org or set "
        "the binary path explicitly.",
        code="OPENSCAD_NOT_FOUND",
    )


class OpenSCADProvider(GenerationProvider):
    """Local OpenSCAD model generation.

    The ``prompt`` argument to :meth:`generate` should contain valid
    OpenSCAD code.  The provider writes it to a temporary ``.scad``
    file, compiles it to STL, and returns immediately (synchronous).

    Args:
        binary_path: Explicit path to the ``openscad`` binary.
            Auto-detected if omitted.
        timeout: Maximum compilation time in seconds.
    """

    def __init__(
        self,
        binary_path: str | None = None,
        *,
        timeout: int = 120,
    ) -> None:
        self._binary = _find_openscad(binary_path)
        self._timeout = timeout
        self._jobs: Dict[str, GenerationJob] = {}
        self._paths: Dict[str, str] = {}

    @property
    def name(self) -> str:
        return "openscad"

    @property
    def display_name(self) -> str:
        return "OpenSCAD"

    def generate(
        self,
        prompt: str,
        *,
        format: str = "stl",
        style: str | None = None,
        **kwargs: Any,
    ) -> GenerationJob:
        """Compile OpenSCAD code to an STL file.

        Args:
            prompt: Valid OpenSCAD code.
            format: Output format (only ``"stl"`` supported).
            style: Ignored for OpenSCAD.

        Returns:
            :class:`GenerationJob` with ``SUCCEEDED`` or ``FAILED`` status.
        """
        if format != "stl":
            raise GenerationError(
                f"OpenSCAD only supports STL output, got {format!r}.",
                code="UNSUPPORTED_FORMAT",
            )

        job_id = f"openscad-{uuid.uuid4().hex[:12]}"
        output_dir = kwargs.get("output_dir", "/tmp/kiln_generated")
        os.makedirs(output_dir, exist_ok=True)
        out_path = os.path.join(output_dir, f"{job_id}.stl")

        # Write .scad source to a temp file.
        scad_fd, scad_path = tempfile.mkstemp(suffix=".scad", prefix="kiln_")
        try:
            with os.fdopen(scad_fd, "w") as fh:
                fh.write(prompt)

            cmd = [self._binary, "-o", out_path, scad_path]
            logger.info("OpenSCAD: %s", " ".join(cmd))

            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=self._timeout,
                )
            except subprocess.TimeoutExpired:
                job = GenerationJob(
                    id=job_id,
                    provider=self.name,
                    prompt=prompt[:200],
                    status=GenerationStatus.FAILED,
                    progress=0,
                    created_at=time.time(),
                    format=format,
                    error=f"OpenSCAD compilation timed out after {self._timeout}s.",
                )
                self._jobs[job_id] = job
                return job
            except OSError as exc:
                raise GenerationError(
                    f"Failed to run OpenSCAD: {exc}",
                    code="OPENSCAD_EXEC_ERROR",
                )

            if result.returncode != 0:
                stderr = (result.stderr or "").strip()[:500]
                job = GenerationJob(
                    id=job_id,
                    provider=self.name,
                    prompt=prompt[:200],
                    status=GenerationStatus.FAILED,
                    progress=0,
                    created_at=time.time(),
                    format=format,
                    error=f"OpenSCAD exited with code {result.returncode}: {stderr}",
                )
                self._jobs[job_id] = job
                return job

            if not os.path.isfile(out_path) or os.path.getsize(out_path) == 0:
                job = GenerationJob(
                    id=job_id,
                    provider=self.name,
                    prompt=prompt[:200],
                    status=GenerationStatus.FAILED,
                    progress=0,
                    created_at=time.time(),
                    format=format,
                    error="OpenSCAD produced no output file.",
                )
                self._jobs[job_id] = job
                return job

            self._paths[job_id] = out_path
            job = GenerationJob(
                id=job_id,
                provider=self.name,
                prompt=prompt[:200],
                status=GenerationStatus.SUCCEEDED,
                progress=100,
                created_at=time.time(),
                format=format,
            )
            self._jobs[job_id] = job
            return job

        finally:
            # Clean up temp .scad file.
            try:
                os.unlink(scad_path)
            except OSError:
                pass

    def get_job_status(self, job_id: str) -> GenerationJob:
        """Return the stored job state.

        OpenSCAD jobs are synchronous, so this simply returns the
        result from :meth:`generate`.
        """
        job = self._jobs.get(job_id)
        if not job:
            raise GenerationError(
                f"Job {job_id!r} not found.", code="JOB_NOT_FOUND"
            )
        return job

    def download_result(
        self,
        job_id: str,
        output_dir: str = "/tmp/kiln_generated",
    ) -> GenerationResult:
        """Return the path to the already-generated STL.

        For OpenSCAD, the file is generated synchronously during
        :meth:`generate`, so this just verifies the file exists.
        """
        path = self._paths.get(job_id)
        if not path or not os.path.isfile(path):
            raise GenerationError(
                f"No generated file for job {job_id!r}.", code="NO_RESULT"
            )

        job = self._jobs.get(job_id)
        prompt = job.prompt if job else ""

        return GenerationResult(
            job_id=job_id,
            provider=self.name,
            local_path=path,
            format="stl",
            file_size_bytes=os.path.getsize(path),
            prompt=prompt,
        )

    def list_styles(self) -> List[str]:
        return []
