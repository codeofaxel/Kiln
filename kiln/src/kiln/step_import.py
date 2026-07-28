"""STEP file import — converts .step/.stp CAD files to STL for Kiln's mesh pipeline.

Prefers whatever external tool is already on the machine (FreeCADCmd, then
gmsh) via subprocess, then falls back to the in-process OCCT kernel — bare
``OCP`` first, full ``cadquery`` after it, since OCP is the kernel cadquery
wraps and going direct skips a ~2 s import for identical geometry.

Preference order is "cheapest thing already installed first," which makes the
kernel LAST at runtime and FIRST for installing: it is the only backend pip
can put on every platform Kiln runs on.  See :data:`PIP_BACKEND`.

When no backend is present the caller gets a message written for the surface
they're on: a one-command fix locally, an honest "this is our gap" on the
hosted server, where they have nothing to install.  ``kiln
install-step-backend`` is the local fix.

This is a **free-tier** feature — no kiln-pro dependency.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_VALID_EXTENSIONS = frozenset({".step", ".stp"})

#: Default tessellation tolerance for mesh conversion (lower = finer mesh).
TESSELLATION_TOLERANCE: float = 0.1

#: Subprocess timeout in seconds for FreeCAD/Gmsh backends.
SUBPROCESS_TIMEOUT_S: int = 300

#: The backend Kiln installs on demand: the bare OCCT kernel, NOT full
#: cadquery.  Two reasons.
#:
#: Coverage — ``cadquery-ocp`` publishes wheels for cp310–cp314 on macOS
#: (arm64 + x86_64), Linux (x86_64 + aarch64) and Windows, i.e. every Python
#: and platform Kiln supports (``requires-python = ">=3.10"``).  That is why
#: it, and not FreeCAD, is the backend we can install FOR somebody: pip works
#: on every machine that runs Kiln, with no package manager, no GUI
#: installer, no admin password.
#:
#: Size — all three numbers measured on disk 2026-07-27, not estimated:
#:
#:   pip install cadquery            1163 MB  (vtk 619, ocp 228, llvmlite 131,
#:                                             casadi 130, numba 27, ezdxf 21…)
#:   pip install cadquery-ocp         848 MB  (kernel + vtk 619 — the plain
#:                                             kernel HARD-REQUIRES vtk==9.6.2
#:                                             and won't even import without
#:                                             it: its .so links
#:                                             libvtkWrappingPythonCore)
#:   pip install cadquery-ocp-novtk   228 MB  ← this one
#:
#: Everything past the kernel serves cadquery's visualization and solver
#: features, which STEP→mesh conversion never calls.  ``-novtk`` is the same
#: OCCT 7.9.3.1.1 build with the VTK linkage dropped; it converts identically
#: (1612 triangles on the same test part) and about twice as fast.  Nobody
#: should download a gigabyte to open one STEP file.
PIP_BACKEND = "cadquery-ocp-novtk"

#: The one command that fixes a local install.
INSTALL_COMMAND = "kiln install-step-backend"

#: The equivalent for someone who'd rather do it by hand.
PIP_INSTALL_COMMAND = 'pip install "kiln3d[step]"'

_LOCAL_INSTALL_HELP = (
    "No STEP import backend found on this machine.\n"
    "\n"
    "  Fix it in one command:\n"
    f"      {INSTALL_COMMAND}\n"
    "\n"
    "  Or install it yourself:\n"
    f"      {PIP_INSTALL_COMMAND}\n"
    "\n"
    "  Already have FreeCAD or Gmsh? Kiln will prefer them — just make sure\n"
    "  'FreeCADCmd' or 'gmsh' is on your PATH."
)

# On the hosted server the caller has no shell, no filesystem, and no
# business being handed an install command — the gap is ours to close, not
# theirs.  Telling them to `pip install` something would be advice they
# cannot act on, which is worse than saying plainly that it's our problem.
_HOSTED_INSTALL_HELP = (
    "STEP conversion isn't available on this server right now.\n"
    "\n"
    "  This is a server-side gap — there is nothing for you to install.\n"
    "  Please report it with `report_issue` so we can fix it.\n"
    "\n"
    "  In the meantime `step_file_info` still works: it reads a STEP file's\n"
    "  metadata (product names, body count, schema) without a converter."
)


def install_help() -> str:
    """The no-backend message, written for whoever is actually reading it.

    A local user gets a command they can run.  A hosted caller gets the
    truth — that it's our server's gap — because an install instruction
    they can't act on is a dead end dressed up as help.
    """
    from kiln.runtime_env import is_hosted_multitenant

    return _HOSTED_INSTALL_HELP if is_hosted_multitenant() else _LOCAL_INSTALL_HELP


def install_remedy() -> dict[str, Any]:
    """The same answer as :func:`install_help`, structured for tool callers.

    An agent shouldn't have to regex a prose blob to find out whether the
    user can do anything about this.  ``actionable_by_caller`` is the field
    that matters: False means don't tell the user to go install something.
    """
    from kiln.runtime_env import is_hosted_multitenant

    hosted = is_hosted_multitenant()
    return {
        "surface": "hosted" if hosted else "local",
        "actionable_by_caller": not hosted,
        "command": None if hosted else INSTALL_COMMAND,
        "pip_command": None if hosted else PIP_INSTALL_COMMAND,
        "message": _HOSTED_INSTALL_HELP if hosted else _LOCAL_INSTALL_HELP,
    }

# FreeCAD Python helper script executed via FreeCADCmd.
_FREECAD_SCRIPT_TEMPLATE = r"""
import sys, json, os
import FreeCAD
import Part
import Mesh

step_path = {step_path!r}
output_dir = {output_dir!r}
merge = {merge!r}
tolerance = {tolerance!r}

doc = FreeCAD.newDocument("import")
Part.insert(step_path, doc.Name)

bodies = [obj for obj in doc.Objects if hasattr(obj, "Shape")]
body_count = len(bodies)

if merge or body_count <= 1:
    shapes = [obj.Shape for obj in bodies]
    out_path = os.path.join(output_dir, "merged.stl")
    mesh = Mesh.Mesh()
    for s in shapes:
        mesh.addMesh(Mesh.Mesh(s.tessellate(tolerance)))
    mesh.write(out_path)
    outputs = [out_path]
else:
    outputs = []
    for i, obj in enumerate(bodies):
        name = getattr(obj, "Label", "body_%d" % i)
        name = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)
        out_path = os.path.join(output_dir, "%s.stl" % name)
        m = Mesh.Mesh()
        m.addMesh(Mesh.Mesh(obj.Shape.tessellate(tolerance)))
        m.write(out_path)
        outputs.append(out_path)

result = {{"body_count": body_count, "outputs": outputs}}
print("KILN_RESULT:" + json.dumps(result))
"""

# Gmsh Python helper script.
_GMSH_SCRIPT_TEMPLATE = r"""
import sys, json, os
import gmsh

step_path = {step_path!r}
output_dir = {output_dir!r}

gmsh.initialize()
gmsh.option.setNumber("General.Terminal", 0)
gmsh.open(step_path)

entities = gmsh.model.getEntities(3)
body_count = max(len(entities), 1)

out_path = os.path.join(output_dir, "merged.stl")
gmsh.model.mesh.generate(2)
gmsh.write(out_path)
gmsh.finalize()

result = {{"body_count": body_count, "outputs": [out_path]}}
print("KILN_RESULT:" + json.dumps(result))
"""


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class StepImportResult:
    """Result of a STEP-to-STL conversion."""

    output_path: str
    """Path to the primary output STL (or directory if multi-body split)."""

    file_size_bytes: int
    """Size of the output STL file(s) in bytes."""

    body_count: int
    """Number of solid bodies found in the STEP file."""

    conversion_time_s: float
    """Wall-clock seconds for the conversion."""

    warnings: list[str] = field(default_factory=list)
    """Non-fatal warnings encountered during conversion."""

    output_paths: list[str] = field(default_factory=list)
    """All output STL paths (relevant for multi-body split)."""

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-friendly dictionary."""
        return asdict(self)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class StepImportError(Exception):
    """Raised when STEP import fails."""


class NoBackendError(StepImportError):
    """Raised when no conversion backend is available.

    Carries :attr:`remedy` so a caller can answer "can the user do anything
    about this?" without parsing the message text.
    """

    def __init__(self) -> None:
        self.remedy = install_remedy()
        super().__init__(self.remedy["message"])


# ---------------------------------------------------------------------------
# Path validation
# ---------------------------------------------------------------------------


def _validate_step_path(step_path: str) -> Path:
    """Validate and resolve a STEP file path.

    Raises:
        FileNotFoundError: If the file does not exist.
        ValueError: If the extension is not .step or .stp.
    """
    p = Path(step_path).resolve()

    # Security: block path traversal patterns in the raw input.
    if ".." in Path(step_path).parts:
        raise ValueError(f"Path traversal not allowed: {step_path}")

    if not p.exists():
        raise FileNotFoundError(f"STEP file not found: {p}")

    if p.suffix.lower() not in _VALID_EXTENSIONS:
        raise ValueError(
            f"Unsupported file extension '{p.suffix}'. "
            f"Expected one of: {', '.join(sorted(_VALID_EXTENSIONS))}"
        )

    return p


def _validate_output_dir(output_dir: str | None, step_path: Path) -> Path:
    """Resolve and create the output directory."""
    if output_dir is not None:
        out = Path(output_dir).resolve()
        if ".." in Path(output_dir).parts:
            raise ValueError(f"Path traversal not allowed in output_dir: {output_dir}")
    else:
        out = step_path.parent

    out.mkdir(parents=True, exist_ok=True)
    return out


# ---------------------------------------------------------------------------
# Backend detection
# ---------------------------------------------------------------------------


def _find_freecad_cmd() -> str | None:
    """Return the FreeCADCmd executable name if found on PATH."""
    for name in ("FreeCADCmd", "freecadcmd", "freecad-cmd"):
        if shutil.which(name):
            return name
    return None


def _find_gmsh_cmd() -> str | None:
    """Return 'gmsh' if the CLI is on PATH."""
    return "gmsh" if shutil.which("gmsh") else None


def _cadquery_available() -> bool:
    """Return True if cadquery can be imported."""
    try:
        import cadquery  # noqa: F401

        return True
    except ImportError:
        return False


def _ocp_available() -> bool:
    """Return True if the bare OCCT bindings (``cadquery-ocp``) can be imported.

    Deliberately probes the ``OCP`` MODULE rather than a distribution name:
    three different packages provide it (``cadquery-ocp-novtk``,
    ``cadquery-ocp``, and full ``cadquery`` transitively), and a user who
    already has any of them should just work.  See :data:`PIP_BACKEND` for
    which one we install and the measured reason why.
    """
    try:
        from OCP.STEPControl import STEPControl_Reader  # noqa: F401

        return True
    except ImportError:
        return False


def check_step_support() -> dict[str, Any]:
    """Check which STEP import backends are available.

    Returns:
        Dictionary with backend names as keys and availability info.
    """
    freecad_cmd = _find_freecad_cmd()
    gmsh_cmd = _find_gmsh_cmd()
    ocp = _ocp_available()
    cq = _cadquery_available()

    backends: dict[str, Any] = {
        "freecad": {
            "available": freecad_cmd is not None,
            "executable": freecad_cmd,
            "priority": 1,
        },
        "gmsh": {
            "available": gmsh_cmd is not None,
            "executable": gmsh_cmd,
            "priority": 2,
        },
        "ocp": {
            "available": ocp,
            "executable": None,
            "priority": 3,
        },
        "cadquery": {
            "available": cq,
            "executable": None,
            "priority": 4,
        },
    }

    any_available = any(b["available"] for b in backends.values())

    return {
        "any_available": any_available,
        "backends": backends,
        "install_help": None if any_available else install_help(),
        "remedy": None if any_available else install_remedy(),
    }


# ---------------------------------------------------------------------------
# Conversion backends
# ---------------------------------------------------------------------------


def _convert_via_freecad(
    step_path: Path,
    output_dir: Path,
    merge_bodies: bool,
    freecad_cmd: str,
) -> tuple[list[str], int]:
    """Run FreeCADCmd with a helper script to convert STEP → STL.

    Returns:
        (list of output paths, body_count)
    """
    script = _FREECAD_SCRIPT_TEMPLATE.format(
        step_path=str(step_path),
        output_dir=str(output_dir),
        merge=merge_bodies,
        tolerance=TESSELLATION_TOLERANCE,
    )

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", delete=False
    ) as f:
        f.write(script)
        script_path = f.name

    try:
        result = subprocess.run(
            [freecad_cmd, script_path],
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT_S,
        )
    finally:
        os.unlink(script_path)

    # Parse output for KILN_RESULT line.
    return _parse_subprocess_result(result, "FreeCAD")


def _convert_via_gmsh(
    step_path: Path,
    output_dir: Path,
) -> tuple[list[str], int]:
    """Run gmsh Python script to convert STEP → STL.

    Returns:
        (list of output paths, body_count)
    """
    script = _GMSH_SCRIPT_TEMPLATE.format(
        step_path=str(step_path),
        output_dir=str(output_dir),
    )

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", delete=False
    ) as f:
        f.write(script)
        script_path = f.name

    try:
        result = subprocess.run(
            ["python3", script_path],
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT_S,
        )
    finally:
        os.unlink(script_path)

    return _parse_subprocess_result(result, "Gmsh")


#: Tessellation parameters for the OCP backend, chosen to match what
#: ``cadquery.exporters.export(..., "STL")`` produces so switching backends
#: doesn't silently change mesh density.  Verified 2026-07-27 on a filleted
#: plate with a through hole: cadquery 1032 triangles, OCP 1612 — OCP is the
#: slightly finer of the two, which is the safe direction to err.
_OCP_LINEAR_DEFLECTION = 1e-3
_OCP_ANGULAR_DEFLECTION = 0.1


def _convert_via_ocp(
    step_path: Path,
    output_dir: Path,
    merge_bodies: bool,
) -> tuple[list[str], int]:
    """Use the OCCT kernel directly (in-process) to convert STEP → STL.

    The same kernel cadquery wraps, called without the wrapper.  Preferred
    over :func:`_convert_via_cadquery` when both are present: it skips
    cadquery's heavy import (~2 s on first call) for an identical result.

    Returns:
        (list of output paths, body_count)
    """
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.IFSelect import IFSelect_ReturnStatus
    from OCP.StlAPI import StlAPI_Writer
    from OCP.STEPControl import STEPControl_Reader
    from OCP.TopAbs import TopAbs_ShapeEnum
    from OCP.TopExp import TopExp_Explorer

    reader = STEPControl_Reader()
    if reader.ReadFile(str(step_path)) != IFSelect_ReturnStatus.IFSelect_RetDone:
        raise StepImportError(f"OCCT could not read the STEP file: {step_path}")
    reader.TransferRoots()
    shape = reader.OneShape()

    solids = []
    explorer = TopExp_Explorer(shape, TopAbs_ShapeEnum.TopAbs_SOLID)
    while explorer.More():
        solids.append(explorer.Current())
        explorer.Next()
    body_count = len(solids) or 1

    def _write(target: Any, path: str) -> None:
        # Tessellate in place, then write.  A shape with no triangulation
        # writes an empty STL rather than failing, so meshing first is not
        # optional.
        BRepMesh_IncrementalMesh(
            target, _OCP_LINEAR_DEFLECTION, False, _OCP_ANGULAR_DEFLECTION, True
        )
        writer = StlAPI_Writer()
        writer.ASCIIMode = False  # binary: ~4x smaller for identical geometry
        writer.Write(target, path)

    if merge_bodies or body_count <= 1:
        out_path = str(output_dir / "merged.stl")
        _write(shape, out_path)
        return [out_path], body_count

    outputs: list[str] = []
    for i, solid in enumerate(solids):
        out_path = str(output_dir / f"body_{i}.stl")
        _write(solid, out_path)
        outputs.append(out_path)
    return outputs, body_count


def _convert_via_cadquery(
    step_path: Path,
    output_dir: Path,
    merge_bodies: bool,
) -> tuple[list[str], int]:
    """Use cadquery (in-process) to convert STEP → STL.

    Returns:
        (list of output paths, body_count)
    """
    import cadquery as cq  # type: ignore[import-untyped]

    result = cq.importers.importStep(str(step_path))
    solids = result.solids().vals()
    body_count = len(solids) if solids else 1

    if merge_bodies or body_count <= 1:
        out_path = str(output_dir / "merged.stl")
        cq.exporters.export(result, out_path, exportType="STL")
        return [out_path], body_count
    else:
        outputs: list[str] = []
        for i, solid in enumerate(solids):
            out_path = str(output_dir / f"body_{i}.stl")
            ws = cq.Workplane().add(solid)
            cq.exporters.export(ws, out_path, exportType="STL")
            outputs.append(out_path)
        return outputs, body_count


def _parse_subprocess_result(
    result: subprocess.CompletedProcess[str],
    backend_name: str,
) -> tuple[list[str], int]:
    """Extract KILN_RESULT JSON from subprocess stdout.

    Returns:
        (list of output paths, body_count)

    Raises:
        StepImportError: If the subprocess failed or result not found.
    """
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()[:500]
        raise StepImportError(
            f"{backend_name} conversion failed (exit {result.returncode}): {stderr}"
        )

    for line in (result.stdout or "").splitlines():
        if line.startswith("KILN_RESULT:"):
            try:
                data = json.loads(line[len("KILN_RESULT:"):])
            except json.JSONDecodeError as exc:
                raise StepImportError(
                    f"{backend_name} produced malformed result JSON: {exc}"
                ) from exc
            if "outputs" not in data or "body_count" not in data:
                raise StepImportError(
                    f"{backend_name} result missing required keys "
                    f"('outputs', 'body_count'): {data!r}"
                )
            return data["outputs"], data["body_count"]

    raise StepImportError(
        f"{backend_name} conversion produced no result. "
        f"stdout: {(result.stdout or '')[:300]}"
    )


# ---------------------------------------------------------------------------
# Main conversion function
# ---------------------------------------------------------------------------


def is_step_file(path: str) -> bool:
    """True if this path names a STEP file, by extension."""
    return Path(path).suffix.lower() in _VALID_EXTENSIONS


def ensure_mesh_path(
    path: str,
    *,
    output_dir: str | None = None,
) -> tuple[str, str | None]:
    """Hand this any model path; get back something the mesh pipeline can read.

    The single door for "a STEP file turned up somewhere that wants a mesh."
    Anything that is already a mesh passes straight through, so a caller can
    apply this unconditionally without special-casing STEP itself.

    Exists because the alternative is every pipeline growing its own STEP
    branch, and the ones that didn't have one accepted ``.step`` at their
    front door and then failed several layers down with a contradictory
    "unsupported format" from code that never got the memo.

    Returns:
        ``(mesh_path, note)`` — ``note`` is a human-readable line for a
        report when a conversion happened, else ``None``.

    Raises:
        NoBackendError: It IS a STEP file but nothing can convert it.  The
            error carries :attr:`~NoBackendError.remedy`, so a caller can
            tell the user what to do instead of guessing.
    """
    if not is_step_file(path):
        return path, None

    result = convert_step_to_stl(path, output_dir=output_dir, merge_bodies=True)
    note = (
        f"Converted from STEP ({Path(path).name}) to mesh — "
        f"{result.body_count} "
        f"{'body' if result.body_count == 1 else 'bodies'}, "
        f"{result.conversion_time_s:.1f}s."
    )
    return result.output_path, note


def convert_step_to_stl(
    step_path: str,
    output_dir: str | None = None,
    *,
    merge_bodies: bool = True,
) -> StepImportResult:
    """Convert a STEP (.step/.stp) file to STL.

    Tries backends in order: FreeCADCmd → gmsh → cadquery.

    Args:
        step_path: Path to the STEP file.
        output_dir: Directory for output STL(s).  Defaults to the STEP
            file's parent directory.
        merge_bodies: If ``True`` (default), merge all bodies into one STL.
            If ``False``, export each body as a separate STL.

    Returns:
        A :class:`StepImportResult` with output path, body count, timing,
        and any warnings.

    Raises:
        FileNotFoundError: STEP file does not exist.
        ValueError: Invalid file extension or path traversal.
        NoBackendError: No conversion backend available.
        StepImportError: Conversion failed.
    """
    validated_path = _validate_step_path(step_path)
    out_dir = _validate_output_dir(output_dir, validated_path)

    warnings: list[str] = []
    t0 = time.monotonic()

    # Try backends in priority order.
    freecad_cmd = _find_freecad_cmd()
    if freecad_cmd is not None:
        logger.info("Converting STEP via FreeCAD (%s)", freecad_cmd)
        try:
            outputs, body_count = _convert_via_freecad(
                validated_path, out_dir, merge_bodies, freecad_cmd
            )
        except StepImportError:
            raise
        except Exception as exc:
            warnings.append(f"FreeCAD failed ({exc}), trying next backend")
            logger.warning("FreeCAD backend failed: %s", exc)
            freecad_cmd = None  # fall through

    if freecad_cmd is None:
        gmsh_cmd = _find_gmsh_cmd()
        if gmsh_cmd is not None:
            logger.info("Converting STEP via Gmsh")
            if not merge_bodies:
                warnings.append(
                    "Gmsh backend does not support per-body split; "
                    "producing merged output."
                )
            try:
                outputs, body_count = _convert_via_gmsh(validated_path, out_dir)
            except StepImportError:
                raise
            except Exception as exc:
                warnings.append(f"Gmsh failed ({exc}), trying next backend")
                logger.warning("Gmsh backend failed: %s", exc)
                gmsh_cmd = None

        if gmsh_cmd is None:
            # OCP before cadquery: it IS cadquery's kernel, so anyone with
            # cadquery has it, and going direct skips a ~2 s import for the
            # same geometry.
            if _ocp_available():
                logger.info("Converting STEP via OCCT (OCP)")
                outputs, body_count = _convert_via_ocp(
                    validated_path, out_dir, merge_bodies
                )
            elif _cadquery_available():
                logger.info("Converting STEP via CadQuery")
                outputs, body_count = _convert_via_cadquery(
                    validated_path, out_dir, merge_bodies
                )
            else:
                raise NoBackendError()

    elapsed = time.monotonic() - t0

    # Verify at least one output file was actually created.
    existing = [p for p in outputs if Path(p).exists()]
    if not existing:
        raise StepImportError(
            "Conversion produced no output files. "
            f"Expected: {outputs}"
        )
    missing = set(outputs) - set(existing)
    if missing:
        warnings.append(f"Some output files were not created: {sorted(missing)}")

    # Compute total file size.
    total_size = sum(Path(p).stat().st_size for p in existing)

    primary = outputs[0] if len(outputs) == 1 else str(out_dir)

    return StepImportResult(
        output_path=primary,
        file_size_bytes=total_size,
        body_count=body_count,
        conversion_time_s=round(elapsed, 3),
        warnings=warnings,
        output_paths=outputs,
    )


# ---------------------------------------------------------------------------
# Metadata extraction
# ---------------------------------------------------------------------------


def get_step_metadata(step_path: str) -> dict[str, Any]:
    """Extract metadata from a STEP file without full conversion.

    Parses the STEP ASCII header to extract product names, body count
    estimate, and file information.  This is a lightweight operation that
    does not require any external backend.

    Args:
        step_path: Path to the STEP file.

    Returns:
        Dictionary with keys: ``file_name``, ``file_size_bytes``,
        ``estimated_body_count``, ``products``, ``schema``,
        ``description``, ``author``.

    Raises:
        FileNotFoundError: File does not exist.
        ValueError: Invalid extension or path traversal.
    """
    validated_path = _validate_step_path(step_path)

    metadata: dict[str, Any] = {
        "file_name": validated_path.name,
        "file_size_bytes": validated_path.stat().st_size,
        "estimated_body_count": 0,
        "products": [],
        "schema": None,
        "description": None,
        "author": None,
    }

    try:
        text = validated_path.read_text(errors="replace")
    except Exception as exc:
        metadata["parse_error"] = str(exc)
        return metadata

    # FILE_SCHEMA line.
    schema_match = re.search(r"FILE_SCHEMA\s*\(\s*\('([^']+)'\s*\)\s*\)", text)
    if schema_match:
        metadata["schema"] = schema_match.group(1)

    # FILE_DESCRIPTION.
    desc_match = re.search(r"FILE_DESCRIPTION\s*\(\s*\('([^']+)'\s*\)", text)
    if desc_match:
        metadata["description"] = desc_match.group(1)

    # FILE_NAME — author field is 7th parameter.
    name_match = re.search(r"FILE_NAME\s*\(\s*'([^']*)'", text)
    if name_match:
        metadata["original_file_name"] = name_match.group(1)

    # Count PRODUCT entities (rough body count estimate).
    product_pattern = re.compile(
        r"PRODUCT\s*\(\s*'([^']*)'", re.IGNORECASE
    )
    products = product_pattern.findall(text)
    metadata["products"] = products
    metadata["estimated_body_count"] = max(len(products), 1) if products else 0

    # Count CLOSED_SHELL or MANIFOLD_SOLID_BREP for better body estimate.
    solid_count = len(re.findall(r"MANIFOLD_SOLID_BREP\s*\(", text, re.IGNORECASE))
    shell_count = len(re.findall(r"CLOSED_SHELL\s*\(", text, re.IGNORECASE))
    if solid_count > 0:
        metadata["estimated_body_count"] = max(
            metadata["estimated_body_count"], solid_count
        )
    elif shell_count > 0:
        metadata["estimated_body_count"] = max(
            metadata["estimated_body_count"], shell_count
        )

    return metadata
