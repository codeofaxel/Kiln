"""OpenSCAD local model generation provider.

Compiles OpenSCAD scripts into STL files using the ``openscad`` CLI.
The agent writes the ``.scad`` code; Kiln compiles it locally.  This
is deterministic, parametric, free, and ideal for geometric or
mechanical parts.

Requires OpenSCAD installed on the system.  Auto-detects the binary
from ``PATH`` or macOS application bundle.
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
from pathlib import Path
from typing import Any

from kiln.generation.base import (
    GenerationError,
    GenerationJob,
    GenerationProvider,
    GenerationResult,
    GenerationStatus,
)

logger = logging.getLogger(__name__)

_MACOS_APP_PATH = "/Applications/OpenSCAD.app/Contents/MacOS/OpenSCAD" if sys.platform == "darwin" else ""
# Versioned installs (e.g., OpenSCAD-2021.01.app)
_MACOS_VERSIONED_PATTERN = "/Applications/OpenSCAD-*.app/Contents/MacOS/OpenSCAD" if sys.platform == "darwin" else ""


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
    if _MACOS_APP_PATH and os.path.isfile(_MACOS_APP_PATH) and os.access(_MACOS_APP_PATH, os.X_OK):
        return _MACOS_APP_PATH

    # Check versioned macOS app bundles (e.g., OpenSCAD-2021.01.app).
    if _MACOS_VERSIONED_PATTERN:
        import glob as _glob

        matches = sorted(_glob.glob(_MACOS_VERSIONED_PATTERN), reverse=True)
        for match in matches:
            if os.path.isfile(match) and os.access(match, os.X_OK):
                return match
        # If versioned app exists but isn't executable, likely Gatekeeper
        if matches:
            app_path = matches[0].split("/Contents/")[0]
            raise GenerationError(
                f"OpenSCAD found at {app_path} but cannot execute.\n"
                f"macOS Gatekeeper may be blocking it. Fix with:\n"
                f'  xattr -dr com.apple.quarantine "{app_path}"\n'
                f"Then retry the operation.",
                code="OPENSCAD_QUARANTINED",
            )

    raise GenerationError(
        "OpenSCAD not found. Install it:\n"
        "  Linux/WSL: apt install openscad\n"
        "  macOS: brew install openscad\n"
        "  Or download from https://openscad.org\n"
        "Or set the binary path explicitly.",
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
        self._jobs: dict[str, GenerationJob] = {}
        self._paths: dict[str, str] = {}

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
        output_dir = kwargs.get("output_dir", os.path.join(tempfile.gettempdir(), "kiln_generated"))
        os.makedirs(output_dir, exist_ok=True)
        out_path = os.path.join(output_dir, f"{job_id}.stl")

        # Basic safety checks on OpenSCAD input
        if len(prompt) > 100_000:
            raise ValueError("OpenSCAD code too large (max 100KB).")

        # Block dangerous OpenSCAD functions that could access the filesystem
        _DANGEROUS_PATTERNS = [
            r"\bimport\s*\(",  # import() can read arbitrary files
            r"\bsurface\s*\(",  # surface() reads files from disk
            r"\binclude\s*<",  # include <file> reads files
            r"\buse\s*<",  # use <file> reads files
        ]
        for pattern in _DANGEROUS_PATTERNS:
            if re.search(pattern, prompt, re.IGNORECASE):
                raise ValueError(
                    f"OpenSCAD code contains blocked operation matching {pattern}. "
                    f"File I/O operations are not allowed for security."
                )

        # Write .scad source to a temp file.
        scad_fd, scad_path = tempfile.mkstemp(suffix=".scad", prefix="kiln_")
        try:
            with os.fdopen(scad_fd, "w") as fh:
                fh.write(prompt)

            cmd = [self._binary, "-o", out_path, scad_path]
            logger.info("OpenSCAD: %s", " ".join(cmd))

            work_dir = tempfile.mkdtemp(prefix="kiln_scad_")
            try:
                try:
                    result = subprocess.run(
                        cmd,
                        capture_output=True,
                        text=True,
                        timeout=self._timeout,
                        cwd=work_dir,
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
                    ) from exc
            finally:
                shutil.rmtree(work_dir, ignore_errors=True)

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
            with contextlib.suppress(OSError):
                os.unlink(scad_path)

    def get_job_status(self, job_id: str) -> GenerationJob:
        """Return the stored job state.

        OpenSCAD jobs are synchronous, so this simply returns the
        result from :meth:`generate`.
        """
        job = self._jobs.get(job_id)
        if not job:
            raise GenerationError(f"Job {job_id!r} not found.", code="JOB_NOT_FOUND")
        return job

    def download_result(
        self,
        job_id: str,
        output_dir: str = os.path.join(tempfile.gettempdir(), "kiln_generated"),
    ) -> GenerationResult:
        """Return the path to the already-generated STL.

        For OpenSCAD, the file is generated synchronously during
        :meth:`generate`, so this just verifies the file exists.
        """
        path = self._paths.get(job_id)
        if not path or not os.path.isfile(path):
            raise GenerationError(f"No generated file for job {job_id!r}.", code="NO_RESULT")

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

    def render_preview(
        self,
        file_path: str,
        *,
        output_path: str | None = None,
        width: int = 800,
        height: int = 600,
        camera: str | None = None,
    ) -> str:
        """Render a PNG preview of an STL or OpenSCAD file.

        Uses OpenSCAD's rendering pipeline to produce a visual preview
        image.  Agents can use this to visually inspect generated geometry
        before sending it to the printer.

        Args:
            file_path: Path to ``.stl`` or ``.scad`` file to render.
            output_path: Path for the output PNG.  Defaults to a temp file.
            width: Image width in pixels.
            height: Image height in pixels.
            camera: OpenSCAD camera spec.  Defaults to auto-centering.

        Returns:
            Path to the rendered PNG file.

        Raises:
            GenerationError: If rendering fails.
        """
        src = Path(file_path)
        ext = src.suffix.lower()

        if ext == ".stl":
            # Wrap the STL in an OpenSCAD import statement.
            # This is safe here because we control the file path — the
            # security sandbox in generate() blocks agent-supplied imports.
            scad_code = f'import("{src.resolve()}");'
            scad_fd, scad_path = tempfile.mkstemp(suffix=".scad", prefix="kiln_preview_")
            try:
                with os.fdopen(scad_fd, "w") as fh:
                    fh.write(scad_code)
                return self._render_scad_to_png(
                    scad_path, output_path=output_path,
                    width=width, height=height, camera=camera,
                )
            finally:
                with contextlib.suppress(OSError):
                    os.unlink(scad_path)
        elif ext == ".scad":
            return self._render_scad_to_png(
                file_path, output_path=output_path,
                width=width, height=height, camera=camera,
            )
        else:
            raise GenerationError(
                f"Cannot render preview for {ext!r} files. Supported: .stl, .scad",
                code="UNSUPPORTED_FORMAT",
            )

    def _render_scad_to_png(
        self,
        scad_path: str,
        *,
        output_path: str | None = None,
        width: int = 800,
        height: int = 600,
        camera: str | None = None,
    ) -> str:
        """Render a .scad file to PNG using the OpenSCAD CLI."""
        if output_path is None:
            out_fd, output_path = tempfile.mkstemp(suffix=".png", prefix="kiln_preview_")
            os.close(out_fd)

        cmd = [
            self._binary,
            "-o", output_path,
            f"--imgsize={width},{height}",
            "--render",
        ]
        if camera:
            cmd.extend(["--camera", camera])
        else:
            cmd.append("--autocenter")
            cmd.append("--viewall")
        cmd.append(scad_path)

        work_dir = tempfile.mkdtemp(prefix="kiln_preview_")
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=60,
                cwd=work_dir,
            )
        except subprocess.TimeoutExpired:
            raise GenerationError(
                "OpenSCAD preview render timed out after 60s.",
                code="RENDER_TIMEOUT",
            ) from None
        except OSError as exc:
            raise GenerationError(
                f"Failed to run OpenSCAD for preview: {exc}",
                code="OPENSCAD_EXEC_ERROR",
            ) from exc
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

        if result.returncode != 0:
            stderr = (result.stderr or "").strip()[:500]
            raise GenerationError(
                f"OpenSCAD preview failed (exit {result.returncode}): {stderr}",
                code="RENDER_FAILED",
            )

        if not os.path.isfile(output_path) or os.path.getsize(output_path) == 0:
            raise GenerationError(
                "OpenSCAD preview produced no output image.",
                code="RENDER_EMPTY",
            )

        return output_path

    def validate_scad(self, code: str) -> dict[str, Any]:
        """Validate OpenSCAD code without generating output.

        Compiles the code with ``--export-format=echo`` to check for
        syntax/semantic errors without producing geometry.

        Args:
            code: OpenSCAD source code to validate.

        Returns:
            Dict with ``valid``, ``errors``, and ``warnings`` keys.
        """
        scad_fd, scad_path = tempfile.mkstemp(suffix=".scad", prefix="kiln_validate_")
        try:
            with os.fdopen(scad_fd, "w") as fh:
                fh.write(code)

            # Use /dev/null as output — we only care about stderr
            null_out = os.path.join(tempfile.gettempdir(), f"kiln_null_{uuid.uuid4().hex[:8]}.stl")
            cmd = [self._binary, "-o", null_out, scad_path]

            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=30,
                    cwd=tempfile.gettempdir(),
                )
            except subprocess.TimeoutExpired:
                return {
                    "valid": False,
                    "errors": [{"message": "Validation timed out after 30s"}],
                    "warnings": [],
                }
            finally:
                with contextlib.suppress(OSError):
                    os.unlink(null_out)

            return _parse_openscad_output(result.stderr or "", result.returncode)
        finally:
            with contextlib.suppress(OSError):
                os.unlink(scad_path)

    def boolean_operation(
        self,
        operation: str,
        file_paths: list[str],
        *,
        output_path: str | None = None,
    ) -> str:
        """Perform a CSG boolean operation on two or more STL files.

        Uses OpenSCAD's boolean engine to compute the result of
        ``union()``, ``difference()``, or ``intersection()`` on
        the given meshes.

        Args:
            operation: One of ``"union"``, ``"difference"``, or
                ``"intersection"``.
            file_paths: List of STL file paths (minimum 2).
            output_path: Output STL path.  Defaults to a temp file.

        Returns:
            Path to the resulting STL file.

        Raises:
            GenerationError: If the operation fails.
            ValueError: If arguments are invalid.
        """
        operation = operation.lower()
        if operation not in ("union", "difference", "intersection"):
            raise ValueError(
                f"operation must be 'union', 'difference', or 'intersection', "
                f"got {operation!r}"
            )
        if len(file_paths) < 2:
            raise ValueError("boolean_operation requires at least 2 file paths")

        for fp in file_paths:
            if not os.path.isfile(fp):
                raise FileNotFoundError(f"STL file not found: {fp}")

        # Build OpenSCAD code that imports and booleans the meshes.
        # We use absolute resolved paths to avoid path confusion.
        imports = []
        for fp in file_paths:
            resolved = os.path.abspath(fp)
            imports.append(f'  import("{resolved}");')

        scad_code = f"{operation}() {{\n" + "\n".join(imports) + "\n}"

        if output_path is None:
            out_fd, output_path = tempfile.mkstemp(
                suffix=".stl", prefix=f"kiln_{operation}_"
            )
            os.close(out_fd)

        scad_fd, scad_path = tempfile.mkstemp(suffix=".scad", prefix="kiln_bool_")
        work_dir = tempfile.mkdtemp(prefix="kiln_bool_")
        try:
            with os.fdopen(scad_fd, "w") as fh:
                fh.write(scad_code)

            cmd = [self._binary, "-o", output_path, scad_path]
            logger.info("OpenSCAD boolean: %s", " ".join(cmd))

            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=self._timeout,
                    cwd=work_dir,
                )
            except subprocess.TimeoutExpired:
                raise GenerationError(
                    f"Boolean {operation} timed out after {self._timeout}s.",
                    code="BOOL_TIMEOUT",
                ) from None
            except OSError as exc:
                raise GenerationError(
                    f"Failed to run OpenSCAD for boolean: {exc}",
                    code="OPENSCAD_EXEC_ERROR",
                ) from exc

            if result.returncode != 0:
                stderr = (result.stderr or "").strip()[:500]
                raise GenerationError(
                    f"Boolean {operation} failed (exit {result.returncode}): {stderr}",
                    code="BOOL_FAILED",
                )

            if not os.path.isfile(output_path) or os.path.getsize(output_path) == 0:
                raise GenerationError(
                    f"Boolean {operation} produced no output.",
                    code="BOOL_EMPTY",
                )

            return output_path
        finally:
            with contextlib.suppress(OSError):
                os.unlink(scad_path)
            shutil.rmtree(work_dir, ignore_errors=True)

    def list_styles(self) -> list[str]:
        return []


def boolean_mesh_operation(
    operation: str,
    file_paths: list[str],
    *,
    output_path: str | None = None,
) -> dict[str, Any]:
    """Perform a CSG boolean operation on STL meshes via OpenSCAD.

    Convenience function that auto-discovers OpenSCAD and delegates
    to :meth:`OpenSCADProvider.boolean_operation`.

    Args:
        operation: ``"union"``, ``"difference"``, or ``"intersection"``.
        file_paths: List of STL file paths (minimum 2).
        output_path: Output STL path.

    Returns:
        Dict with result path, operation, and triangle count.

    Raises:
        GenerationError: If OpenSCAD is not found or the operation fails.
    """
    binary = _find_openscad()
    if binary is None:
        raise GenerationError(
            "OpenSCAD not found. Install it from https://openscad.org/",
            code="OPENSCAD_NOT_FOUND",
        )

    provider = OpenSCADProvider(binary_path=binary)
    result_path = provider.boolean_operation(
        operation, file_paths, output_path=output_path,
    )

    # Count triangles in result
    tri_count = 0
    try:
        import struct as _struct

        with open(result_path, "rb") as fh:
            fh.seek(80)
            data = fh.read(4)
            if len(data) == 4:
                tri_count = _struct.unpack("<I", data)[0]
    except Exception:
        pass

    return {
        "path": result_path,
        "operation": operation,
        "input_files": file_paths,
        "triangle_count": tri_count,
    }


def compose_from_primitives(
    operations: list[dict[str, Any]],
    *,
    output_path: str | None = None,
) -> dict[str, Any]:
    """Build a functional part by composing geometric primitives with booleans.

    This is the **CAD-aware generation path** — instead of asking a text-to-mesh
    AI to guess at geometry, agents describe parts as a tree of primitives
    (cubes, cylinders, spheres) combined with boolean operations.

    Each operation in the list is a dict with:
        - ``type``: ``"primitive"`` or ``"boolean"``
        - For primitives:
            - ``shape``: ``"cube"``, ``"cylinder"``, ``"sphere"``, ``"cone"``
            - ``params``: shape-specific parameters (see below)
            - ``translate``: optional ``[x, y, z]`` translation
            - ``rotate``: optional ``[rx, ry, rz]`` rotation in degrees
            - ``id``: optional string ID to reference later
        - For booleans:
            - ``operation``: ``"union"``, ``"difference"``, ``"intersection"``
            - ``children``: list of operation dicts (recursive)

    Primitive params:
        - cube: ``{"size": [x,y,z]}`` or ``{"size": scalar}``
        - cylinder: ``{"h": height, "r": radius}`` or ``{"h", "r1", "r2"}``
        - sphere: ``{"r": radius}``
        - cone: ``{"h": height, "r1": bottom_radius, "r2": top_radius}``

    Example — bracket with mounting hole::

        compose_from_primitives([
            {"type": "boolean", "operation": "difference", "children": [
                {"type": "boolean", "operation": "union", "children": [
                    {"type": "primitive", "shape": "cube",
                     "params": {"size": [40, 5, 30]}},
                    {"type": "primitive", "shape": "cube",
                     "params": {"size": [5, 30, 30]}},
                ]},
                {"type": "primitive", "shape": "cylinder",
                 "params": {"h": 10, "r": 3},
                 "translate": [20, -1, 15], "rotate": [-90, 0, 0]},
            ]},
        ])

    :param operations: List of operation dicts (primitive or boolean tree).
    :param output_path: Output STL path (defaults to temp file).
    :returns: Dict with path, triangle_count, and the generated SCAD code.
    :raises GenerationError: If OpenSCAD is not found or compilation fails.
    """
    binary = _find_openscad()
    if binary is None:
        raise GenerationError(
            "OpenSCAD not found. Install it from https://openscad.org/",
            code="OPENSCAD_NOT_FOUND",
        )

    if not operations:
        raise ValueError("Operations list cannot be empty")

    # Generate OpenSCAD code from the operation tree
    scad_lines: list[str] = ["// Generated by Kiln compose_from_primitives"]
    scad_lines.append("$fn = 50;")
    scad_lines.append("")

    for op in operations:
        scad_lines.append(_op_to_scad(op, indent=0))

    scad_code = "\n".join(scad_lines)

    # Compile with OpenSCAD
    if output_path is None:
        output_path = os.path.join(tempfile.gettempdir(), f"kiln_composed_{uuid.uuid4().hex[:8]}.stl")

    # Compile directly rather than via provider.generate() which blocks import().

    scad_path = output_path.replace(".stl", ".scad")
    Path(scad_path).write_text(scad_code, encoding="utf-8")

    try:
        result = subprocess.run(
            [binary, "-o", output_path, scad_path],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            raise GenerationError(
                f"OpenSCAD compilation failed: {result.stderr[:500]}",
                code="OPENSCAD_COMPILE_ERROR",
            )
    except subprocess.TimeoutExpired as exc:
        raise GenerationError(
            "OpenSCAD compilation timed out after 120s",
            code="OPENSCAD_TIMEOUT",
        ) from exc
    finally:
        # Clean up SCAD file
        with contextlib.suppress(OSError):
            os.unlink(scad_path)

    if not os.path.isfile(output_path):
        raise GenerationError(
            "OpenSCAD produced no output file",
            code="OPENSCAD_NO_OUTPUT",
        )

    # Count triangles
    tri_count = 0
    try:
        import struct as _struct

        with open(output_path, "rb") as fh:
            fh.seek(80)
            data = fh.read(4)
            if len(data) == 4:
                tri_count = _struct.unpack("<I", data)[0]
    except Exception:
        pass

    return {
        "path": output_path,
        "scad_code": scad_code,
        "triangle_count": tri_count,
        "operation_count": len(operations),
    }


def _op_to_scad(op: dict[str, Any], *, indent: int = 0) -> str:
    """Convert a single operation dict to OpenSCAD code."""
    op_type = op.get("type", "")

    if op_type == "primitive":
        return _primitive_to_scad(op, indent=indent)
    elif op_type == "boolean":
        return _boolean_to_scad(op, indent=indent)
    else:
        raise ValueError(f"Unknown operation type: {op_type!r}. Must be 'primitive' or 'boolean'.")


def _primitive_to_scad(op: dict[str, Any], *, indent: int = 0) -> str:
    """Convert a primitive operation to OpenSCAD code."""
    pad = "    " * indent
    shape = op.get("shape", "")
    params = op.get("params", {})
    translate = op.get("translate")
    rotate = op.get("rotate")

    # Build the shape call
    if shape == "cube":
        size = params.get("size", 10)
        if isinstance(size, (list, tuple)):
            shape_code = f"cube([{size[0]}, {size[1]}, {size[2]}]);"
        else:
            shape_code = f"cube({size});"
    elif shape == "cylinder":
        h = params.get("h", 10)
        r = params.get("r")
        r1 = params.get("r1")
        r2 = params.get("r2")
        d = params.get("d")
        if r is not None:
            shape_code = f"cylinder(h={h}, r={r});"
        elif r1 is not None and r2 is not None:
            shape_code = f"cylinder(h={h}, r1={r1}, r2={r2});"
        elif d is not None:
            shape_code = f"cylinder(h={h}, d={d});"
        else:
            shape_code = f"cylinder(h={h}, r=5);"
    elif shape == "sphere":
        r = params.get("r", 5)
        shape_code = f"sphere(r={r});"
    elif shape == "cone":
        h = params.get("h", 10)
        r1 = params.get("r1", 5)
        r2 = params.get("r2", 0)
        shape_code = f"cylinder(h={h}, r1={r1}, r2={r2});"
    elif shape == "torus":
        # Torus via rotate_extrude of a circle
        major_r = params.get("major_r", 10)
        minor_r = params.get("minor_r", 3)
        shape_code = (
            f"rotate_extrude($fn=64) "
            f"translate([{major_r}, 0, 0]) "
            f"circle(r={minor_r}, $fn=32);"
        )
    elif shape == "wedge":
        # Right-triangle prism (wedge/ramp shape)
        w = params.get("width", 10)
        d = params.get("depth", 10)
        h = params.get("height", 10)
        shape_code = (
            f"linear_extrude(height={d}) "
            f"polygon(points=[[0,0],[{w},0],[0,{h}]]);"
        )
    elif shape == "hex_prism":
        # Regular hexagonal prism (for nuts, bolts, hex stock)
        r = params.get("r", 5)
        h = params.get("h", 5)
        shape_code = f"cylinder(h={h}, r={r}, $fn=6);"
    elif shape == "text":
        # Embossed text via linear_extrude + text()
        content = params.get("text", "Kiln")
        size = params.get("size", 10)
        depth = params.get("depth", 2)
        font = params.get("font", "Liberation Sans")
        shape_code = (
            f"linear_extrude(height={depth}) "
            f'text("{content}", size={size}, font="{font}", halign="center", valign="center");'
        )
    elif shape == "rounded_cube":
        # Cube with rounded edges via minkowski (cube + sphere)
        size = params.get("size", [10, 10, 10])
        r = params.get("radius", 1)
        if isinstance(size, (list, tuple)):
            inner = [max(0.1, s - 2 * r) for s in size]
            shape_code = (
                f"minkowski() {{ cube([{inner[0]}, {inner[1]}, {inner[2]}]); "
                f"sphere(r={r}, $fn=16); }}"
            )
        else:
            inner = max(0.1, size - 2 * r)
            shape_code = (
                f"minkowski() {{ cube({inner}); "
                f"sphere(r={r}, $fn=16); }}"
            )
    elif shape == "pipe":
        # Hollow cylinder (pipe/tube)
        h = params.get("h", 20)
        outer_r = params.get("outer_r", 10)
        inner_r = params.get("inner_r", 8)
        shape_code = (
            f"difference() {{ cylinder(h={h}, r={outer_r}); "
            f"translate([0, 0, -0.1]) cylinder(h={h + 0.2}, r={inner_r}); }}"
        )
    else:
        raise ValueError(
            f"Unknown primitive shape: {shape!r}. Use 'cube', 'cylinder', "
            f"'sphere', 'cone', 'torus', 'wedge', 'hex_prism', 'text', "
            f"'rounded_cube', or 'pipe'."
        )

    # Wrap with transformations
    lines: list[str] = []
    close_parens = 0

    if translate:
        lines.append(f"{pad}translate([{translate[0]}, {translate[1]}, {translate[2]}])")
        close_parens += 1
    if rotate:
        lines.append(f"{pad}    " * (1 if translate else 0) + f"{'    ' * indent}rotate([{rotate[0]}, {rotate[1]}, {rotate[2]}])")
        close_parens += 1

    if lines:
        # Nest the transformations
        result_parts = []
        if translate and rotate:
            result_parts.append(f"{pad}translate([{translate[0]}, {translate[1]}, {translate[2]}])")
            result_parts.append(f"{pad}    rotate([{rotate[0]}, {rotate[1]}, {rotate[2]}])")
            result_parts.append(f"{pad}        {shape_code}")
        elif translate:
            result_parts.append(f"{pad}translate([{translate[0]}, {translate[1]}, {translate[2]}])")
            result_parts.append(f"{pad}    {shape_code}")
        elif rotate:
            result_parts.append(f"{pad}rotate([{rotate[0]}, {rotate[1]}, {rotate[2]}])")
            result_parts.append(f"{pad}    {shape_code}")
        return "\n".join(result_parts)
    else:
        return f"{pad}{shape_code}"


def _boolean_to_scad(op: dict[str, Any], *, indent: int = 0) -> str:
    """Convert a boolean operation to OpenSCAD code."""
    pad = "    " * indent
    operation = op.get("operation", "union")
    children = op.get("children", [])

    if operation not in ("union", "difference", "intersection"):
        raise ValueError(f"Unknown boolean operation: {operation!r}")

    if len(children) < 2:
        raise ValueError(f"Boolean '{operation}' requires at least 2 children, got {len(children)}")

    lines = [f"{pad}{operation}() {{"]
    for child in children:
        lines.append(_op_to_scad(child, indent=indent + 1))
    lines.append(f"{pad}}}")

    return "\n".join(lines)


def _parse_openscad_output(stderr: str, return_code: int) -> dict[str, Any]:
    """Parse OpenSCAD stderr into structured error/warning lists.

    Args:
        stderr: Raw stderr output from OpenSCAD.
        return_code: Process exit code.

    Returns:
        Dict with ``valid``, ``errors``, and ``warnings``.
    """
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    for line in stderr.splitlines():
        line = line.strip()
        if not line:
            continue

        entry: dict[str, Any] = {"message": line}

        # Extract file:line info
        loc_match = re.match(r"(?:ERROR|WARNING|TRACE):\s*(.+?),\s*line\s+(\d+)", line, re.IGNORECASE)
        if loc_match:
            entry["line"] = int(loc_match.group(2))

        # Also handle "Parser error in line N:" format
        parser_match = re.search(r"in line (\d+)", line, re.IGNORECASE)
        if parser_match and "line" not in entry:
            entry["line"] = int(parser_match.group(1))

        lower = line.lower()
        if "error" in lower or "parser error" in lower:
            errors.append(entry)
        elif "warning" in lower or "deprecated" in lower:
            warnings.append(entry)
        elif return_code != 0:
            errors.append(entry)

    return {
        "valid": return_code == 0 and len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
    }
