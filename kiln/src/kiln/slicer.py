"""Slicer integration for Kiln — headless slicing via PrusaSlicer / OrcaSlicer CLI.

Wraps the command-line interface of PrusaSlicer and OrcaSlicer so that STL
(or 3MF/STEP) files can be sliced to G-code without opening the GUI.  The
slicer binary is auto-detected on PATH or can be specified explicitly.

Supported slicers:
    * PrusaSlicer (``prusa-slicer`` / ``PrusaSlicer``)
    * OrcaSlicer  (``orca-slicer`` / ``OrcaSlicer``)

Both expose the same ``--export-gcode`` flag for headless slicing.

Example::

    from kiln.slicer import slice_file, find_slicer

    slicer = find_slicer()                # auto-detect
    result = slice_file("model.stl")      # -> SliceResult
    print(result.output_path)             # <tempdir>/kiln_sliced/model.gcode
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Names to probe on PATH, in preference order.
_SLICER_NAMES: list[str] = [
    "prusa-slicer",
    "PrusaSlicer",
    "prusaslicer",
    "orca-slicer",
    "OrcaSlicer",
    "orcaslicer",
]

# Common install locations on macOS (app bundles).
# Only populated on macOS to avoid useless stat() calls on Linux/WSL.
_MACOS_PATHS: list[str] = (
    [
        "/Applications/PrusaSlicer.app/Contents/MacOS/PrusaSlicer",
        "/Applications/Original Prusa Drivers/PrusaSlicer.app/Contents/MacOS/PrusaSlicer",
        "/Applications/OrcaSlicer.app/Contents/MacOS/OrcaSlicer",
    ]
    if sys.platform == "darwin"
    else []
)

# Extensions the slicer can accept as input.
_INPUT_EXTENSIONS = {".stl", ".3mf", ".step", ".stp", ".obj", ".amf"}

# Default output directory.
_DEFAULT_OUTPUT_DIR = os.path.join(tempfile.gettempdir(), "kiln_sliced")


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class SlicerError(Exception):
    """Raised when slicing fails."""


class SlicerNotFoundError(SlicerError):
    """Raised when no slicer binary is found on the system."""


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class SlicerInfo:
    """Information about a discovered slicer binary."""

    path: str
    name: str
    version: str | None = None

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "name": self.name,
            "version": self.version,
        }


@dataclass
class SliceResult:
    """Outcome of a slicing operation."""

    success: bool
    output_path: str | None = None
    slicer: str | None = None
    message: str = ""
    stdout: str = ""
    stderr: str = ""

    def to_dict(self) -> dict:
        d = {
            "success": self.success,
            "output_path": self.output_path,
            "slicer": self.slicer,
            "message": self.message,
        }
        if self.stderr:
            d["stderr"] = self.stderr[:500]
        return d


# ---------------------------------------------------------------------------
# Slicer discovery
# ---------------------------------------------------------------------------


def find_slicer(slicer_path: str | None = None) -> SlicerInfo:
    """Locate a slicer binary on the system.

    Args:
        slicer_path: Explicit path to a slicer binary.  If provided,
            this is used directly (validated for existence).  If ``None``,
            auto-detection is performed.

    Returns:
        A :class:`SlicerInfo` with the resolved path and name.

    Raises:
        SlicerNotFoundError: If no slicer binary can be found.
    """
    if slicer_path:
        if os.path.isfile(slicer_path) and os.access(slicer_path, os.X_OK):
            name = Path(slicer_path).stem.lower()
            version = _get_version(slicer_path)
            return SlicerInfo(path=slicer_path, name=name, version=version)
        raise SlicerNotFoundError(f"Slicer binary not found or not executable: {slicer_path}")

    # Check PATH
    for name in _SLICER_NAMES:
        found = shutil.which(name)
        if found:
            version = _get_version(found)
            return SlicerInfo(path=found, name=name.lower(), version=version)

    # Check macOS app bundles
    for path in _MACOS_PATHS:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            name = Path(path).stem.lower()
            version = _get_version(path)
            return SlicerInfo(path=path, name=name, version=version)

    # Check env var
    env_path = os.environ.get("KILN_SLICER_PATH")
    if env_path and os.path.isfile(env_path) and os.access(env_path, os.X_OK):
        name = Path(env_path).stem.lower()
        version = _get_version(env_path)
        return SlicerInfo(path=env_path, name=name, version=version)

    raise SlicerNotFoundError(
        "No slicer found. Install PrusaSlicer or OrcaSlicer:\n"
        "  Linux/WSL: apt install prusa-slicer  (or download from prusaslicer.org)\n"
        "  macOS: brew install --cask prusaslicer\n"
        "Or set KILN_SLICER_PATH to the binary location."
    )


def _get_version(slicer_path: str) -> str | None:
    """Try to get the slicer version string."""
    try:
        result = subprocess.run(
            [slicer_path, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        output = (result.stdout or result.stderr or "").strip()
        # PrusaSlicer outputs "PrusaSlicer-2.7.1+linux-..."
        # OrcaSlicer outputs "OrcaSlicer 2.0.0"
        if output:
            return output.split("\n")[0].rstrip("\r")[:100]
    except Exception as exc:
        logger.debug("Failed to get slicer version from %s: %s", slicer_path, exc)
    return None


# ---------------------------------------------------------------------------
# Slicing
# ---------------------------------------------------------------------------


def slice_file(
    input_path: str,
    *,
    output_dir: str | None = None,
    output_name: str | None = None,
    profile: str | None = None,
    slicer_path: str | None = None,
    extra_args: list[str] | None = None,
    timeout: int = 300,
) -> SliceResult:
    """Slice a 3D model file to G-code.

    Args:
        input_path: Path to the input file (STL, 3MF, STEP, OBJ, AMF).
        output_dir: Directory for the output G-code file.  Defaults to
            the system temp directory.
        output_name: Override the output file name.  Defaults to the
            input file's stem with ``.gcode`` extension.
        profile: Path to a slicer profile/config file (.ini or .json).
        slicer_path: Explicit slicer binary path.  Auto-detected if omitted.
        extra_args: Additional CLI arguments to pass to the slicer.
        timeout: Maximum slicing time in seconds (default 300).

    Returns:
        A :class:`SliceResult` with the path to the generated G-code.

    Raises:
        SlicerError: If slicing fails.
        SlicerNotFoundError: If no slicer binary is found.
        FileNotFoundError: If the input file does not exist.
    """
    # Validate input
    input_abs = os.path.abspath(input_path)
    if not os.path.isfile(input_abs):
        raise FileNotFoundError(f"Input file not found: {os.path.basename(input_abs)}")

    ext = Path(input_abs).suffix.lower()
    if ext not in _INPUT_EXTENSIONS:
        raise SlicerError(f"Unsupported input format '{ext}'. Supported: {', '.join(sorted(_INPUT_EXTENSIONS))}")

    # Find slicer
    slicer = find_slicer(slicer_path)

    # Prepare output
    out_dir = output_dir or _DEFAULT_OUTPUT_DIR
    os.makedirs(out_dir, mode=0o700, exist_ok=True)

    if output_name:
        # Strict filename sanitisation: basename, null bytes, length, reserveds
        safe_name = output_name.replace("\x00", "")
        safe_name = os.path.basename(safe_name)
        if not safe_name or safe_name != output_name.replace("\x00", "") or safe_name in {".", ".."}:
            raise ValueError(
                f"output_name must be a plain filename without path separators "
                f"or traversal sequences, got: {output_name!r}"
            )
        if len(safe_name) > 255:
            raise ValueError(f"output_name too long ({len(safe_name)} chars, max 255)")
        out_file = os.path.join(out_dir, safe_name)
    else:
        stem = Path(input_abs).stem
        out_file = os.path.join(out_dir, f"{stem}.gcode")

    # Build command
    cmd: list[str] = [
        slicer.path,
        "--export-gcode",
        input_abs,
        "--output",
        out_file,
    ]

    if profile:
        if not os.path.isfile(profile):
            raise SlicerError(f"Profile file not found: {os.path.basename(profile)}")
        cmd.extend(["--load", profile])

    if extra_args:
        cmd.extend(extra_args)

    logger.info("Slicing: %s", " ".join(cmd))

    # Run
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        # Clean up partial output on timeout
        with contextlib.suppress(OSError):
            os.unlink(out_file)
        raise SlicerError(
            f"Slicing timed out after {timeout}s. The model may be too complex or the slicer is hanging."
        ) from None
    except OSError as exc:
        raise SlicerError(f"Failed to run slicer: {exc}") from exc

    if result.returncode != 0:
        stderr_snippet = (result.stderr or "").strip()[:500]
        raise SlicerError(f"Slicer exited with code {result.returncode}. stderr: {stderr_snippet}")

    # Verify output exists
    if not os.path.isfile(out_file):
        raise SlicerError(
            f"Slicer completed but output file was not created. "
            f"stdout: {(result.stdout or '').strip()[:200]}"
        )

    return SliceResult(
        success=True,
        output_path=out_file,
        slicer=slicer.name,
        message=f"Sliced {Path(input_abs).name} -> {Path(out_file).name}",
        stdout=(result.stdout or "").strip(),
        stderr=(result.stderr or "").strip(),
    )
