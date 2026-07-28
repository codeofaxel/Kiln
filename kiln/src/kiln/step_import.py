"""STEP file import — converts .step/.stp CAD files to STL for Kiln's mesh pipeline.

Prefers whatever external tool is already on the machine (FreeCADCmd, then
gmsh) via subprocess, then falls back to the OCCT kernel in a child process —
bare ``OCP`` first, full ``cadquery`` after it, since OCP is the kernel
cadquery wraps and going direct skips a ~2 s import for identical geometry.

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

import contextlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
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
#: OCCT 7.9.3.1.1 build with the VTK linkage dropped; measured 2026-07-27 it
#: converts byte-identically to the vtk builds on the same test part, about
#: twice as fast.  Nobody should download a gigabyte to open one STEP file.
PIP_BACKEND = "cadquery-ocp-novtk"

#: The one command that fixes a local install.
INSTALL_COMMAND = "kiln install-step-backend"

#: The equivalent for someone who'd rather do it by hand.  Names the backend
#: package directly rather than the ``kiln3d[step]`` extra: the extra only
#: exists in a release that ships it, so telling a user on any earlier
#: version to install it hands them an instruction that fails.  This one is
#: true on every version, including the one they already have.
PIP_INSTALL_COMMAND = f'pip install "{PIP_BACKEND}"'

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

    output_format: str = "stl"
    """``"stl"`` or ``"3mf"`` — what :attr:`output_path` actually is."""

    part_names: list[str] = field(default_factory=list)
    """Per-part names from the STEP, when the colour-aware path ran."""

    part_colors: list[str | None] = field(default_factory=list)
    """Per-part ``#RRGGBB`` colours (or ``None``), same order as names."""

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


#: Tessellation bounds, set from what the PRINT PIPELINE can express rather
#: than from any library's default.  Linear deflection caps chordal sag in
#: mm; the slicer is the floor that matters: PrusaSlicer's default G-code
#: resolution is 0.0125 mm, so sag below that is discarded before the
#: printer ever sees it (and FDM positional accuracy, ~±0.1 mm, is another
#: 8x above).  0.005 keeps the measured sag under that floor — the mesh is
#: provably never the bottleneck — without paying for fidelity nothing
#: downstream can print.  Measured 2026-07-27, sphere r=75 mm (the scale
#: where this bites):
#:
#:   linear   triangles   max sag    time    STL size
#:   0.001      755,246   0.0016 mm  49.4 s   36 MB
#:   0.005      150,970   0.0068 mm   3.1 s    7 MB   ← this
#:   0.010       75,380   0.0174 mm   1.0 s    3.6 MB (sag above the floor)
#:
#: At 0.001 a single 150 mm sphere costs 16x the time and 5x the mesh for
#: sag 8x below what the slicer already throws away — and a few such
#: surfaces in one file walk a conversion into SUBPROCESS_TIMEOUT_S.
#: Angular deflection (radians) is what guards SMALL features, where the
#: linear bound relaxes first: at 0.1 rad a Ø22 boss still gets ~60
#: segments per circle (measured sag 0.002 mm).  Both bounds are passed to
#: every kernel backend (OCP here, cadquery in
#: :func:`_convert_via_cadquery`) so backend choice never changes density.
_OCP_LINEAR_DEFLECTION = 5e-3
_OCP_ANGULAR_DEFLECTION = 0.1


# Runs in a CHILD interpreter — see _convert_via_ocp for why.
_OCP_SCRIPT_TEMPLATE = r'''
import json, os, sys

from OCP.BRepMesh import BRepMesh_IncrementalMesh
from OCP.IFSelect import IFSelect_ReturnStatus
from OCP.StlAPI import StlAPI_Writer
from OCP.STEPControl import STEPControl_Reader
from OCP.TopAbs import TopAbs_ShapeEnum
from OCP.TopExp import TopExp_Explorer

step_path = {step_path!r}
output_dir = {output_dir!r}
merge = {merge!r}
linear = {linear!r}
angular = {angular!r}

reader = STEPControl_Reader()
if reader.ReadFile(step_path) != IFSelect_ReturnStatus.IFSelect_RetDone:
    sys.stderr.write("OCCT could not read the STEP file\n")
    raise SystemExit(3)
reader.TransferRoots()
shape = reader.OneShape()

solids = []
explorer = TopExp_Explorer(shape, TopAbs_ShapeEnum.TopAbs_SOLID)
while explorer.More():
    solids.append(explorer.Current())
    explorer.Next()
body_count = len(solids) or 1


def write(target, path):
    # Tessellate in place, then write: an untriangulated shape writes an
    # EMPTY stl rather than failing, so meshing first is not optional.
    BRepMesh_IncrementalMesh(target, linear, False, angular, True)
    writer = StlAPI_Writer()
    writer.ASCIIMode = False  # binary: ~4x smaller, identical geometry
    writer.Write(target, path)


if merge or body_count <= 1:
    out = os.path.join(output_dir, "merged.stl")
    write(shape, out)
    outputs = [out]
else:
    outputs = []
    for i, solid in enumerate(solids):
        out = os.path.join(output_dir, "body_%d.stl" % i)
        write(solid, out)
        outputs.append(out)

print("KILN_RESULT:" + json.dumps({{"outputs": outputs, "body_count": body_count}}))
'''


# The colour-aware sibling of _OCP_SCRIPT_TEMPLATE, used when the caller
# wants part colours and names preserved (see convert_step).  A separate
# template rather than a flag fork: the two scripts share only the meshing
# helper, and keeping the plain one byte-stable means its tests and its
# behavior cannot drift while this one evolves.
#
# XCAF is OCCT's document layer — STEPCAFControl_Reader reads the same file
# as STEPControl_Reader but ALSO populates colour and name attributes.  Each
# top-level ("free") shape label is one part.  Colours nested deeper than
# the per-part level (a sub-face painted differently inside one part) are
# out of scope here and fall back to the part's own colour.
#
# Binding gotcha that cost a probe cycle (2026-07-28): in this OCP build,
# ColorTool.GetColor takes the TopoDS_Shape (via ShapeTool.GetShape_s), not
# the label — the label overload expects a label OUT-param.
_OCP_XCAF_SCRIPT_TEMPLATE = r'''
import json, os, sys

from OCP.BRepMesh import BRepMesh_IncrementalMesh
from OCP.IFSelect import IFSelect_ReturnStatus
from OCP.Quantity import Quantity_Color
from OCP.STEPCAFControl import STEPCAFControl_Reader
from OCP.StlAPI import StlAPI_Writer
from OCP.TCollection import TCollection_ExtendedString
from OCP.TDataStd import TDataStd_Name
from OCP.TDF import TDF_LabelSequence
from OCP.TDocStd import TDocStd_Document
from OCP.XCAFApp import XCAFApp_Application
from OCP.XCAFDoc import XCAFDoc_ColorType, XCAFDoc_DocumentTool

step_path = {step_path!r}
output_dir = {output_dir!r}
linear = {linear!r}
angular = {angular!r}

app = XCAFApp_Application.GetApplication_s()
doc = TDocStd_Document(TCollection_ExtendedString("MDTV-XCAF"))
app.NewDocument(TCollection_ExtendedString("MDTV-XCAF"), doc)

reader = STEPCAFControl_Reader()
reader.SetColorMode(True)
reader.SetNameMode(True)
if reader.ReadFile(step_path) != IFSelect_ReturnStatus.IFSelect_RetDone:
    sys.stderr.write("OCCT could not read the STEP file\n")
    raise SystemExit(3)
reader.Transfer(doc)

shape_tool = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
color_tool = XCAFDoc_DocumentTool.ColorTool_s(doc.Main())
labels = TDF_LabelSequence()
shape_tool.GetFreeShapes(labels)
if labels.Length() == 0:
    sys.stderr.write("XCAF transfer produced no shapes\n")
    raise SystemExit(4)


def write(target, path):
    # Tessellate in place, then write: an untriangulated shape writes an
    # EMPTY stl rather than failing, so meshing first is not optional.
    BRepMesh_IncrementalMesh(target, linear, False, angular, True)
    writer = StlAPI_Writer()
    writer.ASCIIMode = False
    writer.Write(target, path)


outputs, names, colors = [], [], []
for i in range(1, labels.Length() + 1):
    label = labels.Value(i)
    shape = shape_tool.GetShape_s(label)

    name_attr = TDataStd_Name()
    name = "part_%d" % (i - 1)
    if label.FindAttribute(TDataStd_Name.GetID_s(), name_attr):
        got = name_attr.Get().ToExtString().strip()
        if got:
            name = got

    col = Quantity_Color()
    color = None
    for kind in (XCAFDoc_ColorType.XCAFDoc_ColorGen,
                 XCAFDoc_ColorType.XCAFDoc_ColorSurf):
        if color_tool.GetColor(shape, kind, col):
            color = "#%02X%02X%02X" % (
                round(col.Red() * 255),
                round(col.Green() * 255),
                round(col.Blue() * 255),
            )
            break

    out = os.path.join(output_dir, "xcaf_part_%d.stl" % (i - 1))
    write(shape, out)
    outputs.append(out)
    names.append(name)
    colors.append(color)

print("KILN_RESULT:" + json.dumps({{
    "outputs": outputs, "body_count": len(outputs),
    "names": names, "colors": colors,
}}))
'''


def _convert_via_ocp(
    step_path: Path,
    output_dir: Path,
    merge_bodies: bool,
) -> tuple[list[str], int]:
    """Use the OCCT kernel to convert STEP → STL, in a child interpreter.

    The same kernel cadquery wraps, called without the wrapper.  Preferred
    over :func:`_convert_via_cadquery` when both are present: it skips
    cadquery's heavy import for an identical mesh.

    Runs OUT OF PROCESS, like the FreeCAD and gmsh backends, and for the
    same two reasons — both of which bite hardest on the hosted API box:

    1. **It can be timed out.**  Tessellation is a C++ call inside a
       compiled extension.  Python cannot interrupt one: no exception is
       raised at a bytecode boundary, and ``signal.alarm`` only fires on the
       main thread while the API serves from worker threads.  In process, a
       pathological STEP wedges that thread until the machine is restarted.
       A child gets ``SUBPROCESS_TIMEOUT_S`` and a clean kill.
    2. **It can't take the server's memory with it.**  A dense assembly can
       tessellate into gigabytes; in the child that is one dead process and
       a clear error, in process it is the OOM killer taking every in-flight
       job with it (which this deploy has already survived once, from an
       OpenSCAD boolean).

    The cost is one interpreter start (~0.3 s) per conversion, which the
    other two backends already pay and a CAD import can afford.

    Returns:
        (list of output paths, body_count)
    """
    script = _OCP_SCRIPT_TEMPLATE.format(
        step_path=str(step_path),
        output_dir=str(output_dir),
        merge=merge_bodies,
        linear=_OCP_LINEAR_DEFLECTION,
        angular=_OCP_ANGULAR_DEFLECTION,
    )

    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write(script)
        script_path = f.name

    try:
        result = subprocess.run(
            # sys.executable, not "python": the kernel lives in the
            # interpreter running Kiln, which may not be first on PATH.
            [sys.executable, script_path],
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as exc:
        raise StepImportError(
            f"STEP conversion timed out after {SUBPROCESS_TIMEOUT_S}s. "
            "The file is probably a large assembly — try converting a single "
            "part, or simplify it in your CAD tool first."
        ) from exc
    finally:
        os.unlink(script_path)

    return _parse_subprocess_result(result, "OCCT")


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

    # Same tessellation bounds as the OCP backend — see the constants above.
    # cadquery's own export defaults (tolerance=0.1, angularTolerance=0.1 —
    # verified against its source 2026-07-27) are coarser on the linear axis
    # than our bound; passing ours keeps density identical across backends.
    _tess = {
        "tolerance": _OCP_LINEAR_DEFLECTION,
        "angularTolerance": _OCP_ANGULAR_DEFLECTION,
    }

    if merge_bodies or body_count <= 1:
        out_path = str(output_dir / "merged.stl")
        cq.exporters.export(result, out_path, exportType="STL", **_tess)
        return [out_path], body_count
    else:
        outputs: list[str] = []
        for i, solid in enumerate(solids):
            out_path = str(output_dir / f"body_{i}.stl")
            ws = cq.Workplane().add(solid)
            cq.exporters.export(ws, out_path, exportType="STL", **_tess)
            outputs.append(out_path)
        return outputs, body_count


def _parse_kiln_result(
    result: subprocess.CompletedProcess[str],
    backend_name: str,
) -> dict[str, Any]:
    """Extract the KILN_RESULT JSON dict from subprocess stdout.

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
            return data

    raise StepImportError(
        f"{backend_name} conversion produced no result. "
        f"stdout: {(result.stdout or '')[:300]}"
    )


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
    data = _parse_kiln_result(result, backend_name)
    return data["outputs"], data["body_count"]


def _convert_via_ocp_xcaf(
    step_path: Path,
    output_dir: Path,
) -> dict[str, Any]:
    """Colour-aware OCCT conversion: one STL per part, plus names + colours.

    Same child-process discipline as :func:`_convert_via_ocp` (timeout, OOM
    isolation), different reader: XCAF sees the STEP's colour and name
    attributes that the plain reader discards.  The caller decides what to
    assemble from the parts — see :func:`convert_step`.

    Returns:
        The child's result dict: ``outputs``, ``body_count``, ``names``,
        ``colors`` (hex string or ``None`` per part).
    """
    script = _OCP_XCAF_SCRIPT_TEMPLATE.format(
        step_path=str(step_path),
        output_dir=str(output_dir),
        linear=_OCP_LINEAR_DEFLECTION,
        angular=_OCP_ANGULAR_DEFLECTION,
    )

    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write(script)
        script_path = f.name

    try:
        result = subprocess.run(
            [sys.executable, script_path],
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as exc:
        raise StepImportError(
            f"STEP conversion timed out after {SUBPROCESS_TIMEOUT_S}s. "
            "The file is probably a large assembly — try converting a single "
            "part, or simplify it in your CAD tool first."
        ) from exc
    finally:
        os.unlink(script_path)

    data = _parse_kiln_result(result, "OCCT")
    n = len(data["outputs"])
    data.setdefault("names", [f"part_{i}" for i in range(n)])
    data.setdefault("colors", [None] * n)
    return data


def _read_binary_stl(path: str) -> list[tuple[tuple[float, float, float], ...]]:
    """Read a binary STL into a list of triangles (3 vertices each)."""
    import struct

    data = Path(path).read_bytes()
    if len(data) < 84:
        raise StepImportError(f"Not a binary STL (too short): {path}")
    (count,) = struct.unpack_from("<I", data, 80)
    expected = 84 + count * 50
    if len(data) < expected:
        raise StepImportError(
            f"Truncated binary STL ({len(data)} bytes, expected {expected}): {path}"
        )
    triangles = []
    for i in range(count):
        off = 84 + i * 50 + 12  # skip the normal; recomputed by consumers
        v = struct.unpack_from("<9f", data, off)
        triangles.append(((v[0], v[1], v[2]), (v[3], v[4], v[5]), (v[6], v[7], v[8])))
    return triangles


def _write_3mf(
    parts: list[dict[str, Any]],
    out_path: str,
) -> None:
    """Write parts as a core-spec 3MF with per-object colour and name.

    ``parts``: dicts with ``stl_path`` (binary STL), ``name``, and ``color``
    (``#RRGGBB`` or ``None``).  Colour rides the core spec's
    ``<basematerials>`` + ``displaycolor``, referenced object-level via
    ``pid``/``pindex`` — the one encoding Kiln's own
    :mod:`kiln.threemf_parser`, BambuStudio, and PrusaSlicer all read.  A
    part without a colour gets no ``pid`` and renders in each viewer's
    default, which is honest: the STEP didn't say.

    Pure stdlib (zipfile + string XML) by design — this module must import
    with no third-party dependencies installed.
    """
    import zipfile
    from xml.sax.saxutils import quoteattr

    colored = [p for p in parts if p.get("color")]
    color_index = {id(p): i for i, p in enumerate(colored)}

    xml: list[str] = [
        '<?xml version="1.0" encoding="UTF-8"?>\n',
        '<model unit="millimeter" xml:lang="en-US" '
        'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">\n',
        # Same provenance stamp the STL header and the Bambu wrap carry.
        " <metadata name=\"CreatedBy\">Kiln — kiln3d.com</metadata>\n",
        " <resources>\n",
    ]
    if colored:
        xml.append('  <basematerials id="1">\n')
        for p in colored:
            xml.append(
                f"   <base name={quoteattr(p['name'])} "
                f"displaycolor=\"{p['color']}\"/>\n"
            )
        xml.append("  </basematerials>\n")

    build_items: list[str] = []
    for obj_index, part in enumerate(parts):
        obj_id = obj_index + 2  # id 1 is the basematerials group
        triangles = _read_binary_stl(part["stl_path"])

        vertex_ids: dict[tuple[float, float, float], int] = {}
        tri_rows: list[tuple[int, int, int]] = []
        for tri in triangles:
            ids = []
            for v in tri:
                if v not in vertex_ids:
                    vertex_ids[v] = len(vertex_ids)
                ids.append(vertex_ids[v])
            tri_rows.append(tuple(ids))

        pid_attr = ""
        if part.get("color"):
            pid_attr = f' pid="1" pindex="{color_index[id(part)]}"'
        xml.append(
            f"  <object id=\"{obj_id}\" type=\"model\" "
            f"name={quoteattr(part['name'])}{pid_attr}>\n   <mesh>\n"
            "    <vertices>\n"
        )
        for v, _ in sorted(vertex_ids.items(), key=lambda kv: kv[1]):
            xml.append(
                f'     <vertex x="{v[0]:.6f}" y="{v[1]:.6f}" z="{v[2]:.6f}"/>\n'
            )
        xml.append("    </vertices>\n    <triangles>\n")
        for a, b, c in tri_rows:
            xml.append(f'     <triangle v1="{a}" v2="{b}" v3="{c}"/>\n')
        xml.append("    </triangles>\n   </mesh>\n  </object>\n")
        build_items.append(f'  <item objectid="{obj_id}"/>\n')

    xml.append(" </resources>\n <build>\n")
    xml.extend(build_items)
    xml.append(" </build>\n</model>\n")

    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">\n'
            '  <Default Extension="rels" ContentType='
            '"application/vnd.openxmlformats-package.relationships+xml"/>\n'
            '  <Default Extension="model" ContentType='
            '"application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>\n'
            "</Types>\n",
        )
        zf.writestr(
            "_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n'
            '  <Relationship Target="/3D/3dmodel.model" Id="rel0" Type='
            '"http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>\n'
            "</Relationships>\n",
        )
        zf.writestr("3D/3dmodel.model", "".join(xml))


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

    Output goes to a fresh temp directory unless the caller names one.
    ``convert_step_to_stl`` defaults to the STEP file's own folder, which is
    right when a user deliberately asked to import a file and wrong here:
    this runs implicitly, deep inside other tools, and dropping an .stl next
    to somebody's CAD file is a side effect they never asked for.

    Repeat conversions of identical bytes are served from a content-hash
    cache in the OS temp dir (each hit is an independent COPY — mutating a
    returned mesh can never poison a later call).

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

    if output_dir is None:
        output_dir = tempfile.mkdtemp(prefix="kiln_step_")

    # One flow often crosses two doors (validate_and_prepare, then
    # import_external_mesh) and each would reconvert the same bytes.  The
    # cache is content-addressed — same file content, same tessellation
    # constants, same backend availability ⇒ same mesh — so a hit is safe by
    # construction, including on the shared hosted box.  It lives in the OS
    # temp dir on purpose: the OS already owns cleanup there, and Fly's disk
    # is ephemeral anyway, so building our own eviction would be inventing a
    # janitor for a self-cleaning room.  Hits are COPIED out, never aliased:
    # callers repair meshes in place, and handing two callers one file would
    # let the first mutation poison every later hit.
    import hashlib
    import shutil as _shutil

    backend_fingerprint = (
        _find_freecad_cmd() or "",
        _find_gmsh_cmd() or "",
        _ocp_available(),
        _cadquery_available(),
    )
    key = hashlib.sha256(
        Path(path).read_bytes()
        + repr((_OCP_LINEAR_DEFLECTION, _OCP_ANGULAR_DEFLECTION,
                backend_fingerprint)).encode()
    ).hexdigest()
    cache_dir = Path(tempfile.gettempdir()) / "kiln_step_cache"
    cached = cache_dir / f"{key}.stl"

    if cached.is_file():
        out = str(Path(output_dir) / "merged.stl")
        _shutil.copyfile(cached, out)
        return out, (
            f"Converted from STEP ({Path(path).name}) to mesh — cached, 0.0s."
        )

    result = convert_step_to_stl(path, output_dir=output_dir, merge_bodies=True)
    note = (
        f"Converted from STEP ({Path(path).name}) to mesh — "
        f"{result.body_count} "
        f"{'body' if result.body_count == 1 else 'bodies'}, "
        f"{result.conversion_time_s:.1f}s."
    )
    try:
        # Atomic publish so a concurrent process never reads a half-written
        # entry; losing the race just means both converted once.
        cache_dir.mkdir(parents=True, exist_ok=True)
        tmp = cached.with_suffix(f".{os.getpid()}.tmp")
        _shutil.copyfile(result.output_path, tmp)
        os.replace(tmp, cached)
    except OSError:
        pass  # a full or read-only temp dir must never fail the conversion

    return result.output_path, note


def convert_step_to_stl(
    step_path: str,
    output_dir: str | None = None,
    *,
    merge_bodies: bool = True,
) -> StepImportResult:
    """Convert a STEP (.step/.stp) file to STL.

    Tries backends in order: FreeCADCmd → gmsh → OCCT kernel (OCP) → cadquery.

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


def convert_step(
    step_path: str,
    output_dir: str | None = None,
    *,
    merge_bodies: bool = True,
    output_format: str = "auto",
) -> StepImportResult:
    """Convert a STEP file, choosing the output format by what it CARRIES.

    STL by format cannot hold colour or part identity.  When the STEP has
    either — a coloured part, or a multi-body assembly whose parts have
    names — flattening to STL silently throws information away that the
    engineer put there.  So:

    - ``"auto"`` (default): colour or multiple bodies present → one 3MF
      with per-part colour and name (readable by Kiln's own preview,
      BambuStudio, and PrusaSlicer); a plain single solid → STL, exactly
      as before.  Colour detection needs the OCCT kernel; on a FreeCAD/
      Gmsh-only install auto falls back to the classic STL path.
    - ``"stl"``: the classic path, byte-for-byte (:func:`convert_step_to_stl`).
    - ``"3mf"``: force 3MF even for a plain part.  Requires the OCCT kernel.

    Note ``ensure_mesh_path`` deliberately does NOT use auto: pipelines that
    only analyze geometry get STL, the format every mesh tool reads.  This
    door is for the artifact a user KEEPS.

    Raises:
        NoBackendError: no converter available at all.
        StepImportError: ``output_format="3mf"`` without the OCCT kernel,
            or the conversion itself failed.
    """
    if output_format not in ("auto", "stl", "3mf"):
        raise ValueError(
            f"output_format must be 'auto', 'stl' or '3mf', got {output_format!r}"
        )

    if output_format == "stl":
        return convert_step_to_stl(
            step_path, output_dir, merge_bodies=merge_bodies
        )

    if not _ocp_available():
        if output_format == "3mf":
            if not check_step_support()["any_available"]:
                raise NoBackendError()
            raise StepImportError(
                "3MF output (colour + part names) needs the OCCT kernel. "
                f"Install it with: {INSTALL_COMMAND}"
            )
        # auto without the kernel: the FreeCAD/Gmsh chain can convert but
        # cannot see colours — classic STL is the honest result.
        return convert_step_to_stl(
            step_path, output_dir, merge_bodies=merge_bodies
        )

    validated_path = _validate_step_path(step_path)
    out_dir = _validate_output_dir(output_dir, validated_path)

    t0 = time.monotonic()
    data = _convert_via_ocp_xcaf(validated_path, out_dir)
    parts = [
        {"stl_path": p, "name": n, "color": c}
        for p, n, c in zip(data["outputs"], data["names"], data["colors"], strict=True)
    ]

    has_color = any(p["color"] for p in parts)
    wants_3mf = output_format == "3mf" or has_color or len(parts) > 1

    if not wants_3mf:
        # A plain single solid: keep the classic contract (merged.stl).
        final = str(out_dir / "merged.stl")
        os.replace(parts[0]["stl_path"], final)
        elapsed = time.monotonic() - t0
        return StepImportResult(
            output_path=final,
            file_size_bytes=Path(final).stat().st_size,
            body_count=1,
            conversion_time_s=round(elapsed, 3),
            output_paths=[final],
            output_format="stl",
            part_names=data["names"],
            part_colors=data["colors"],
        )

    out_3mf = str(out_dir / f"{validated_path.stem}.3mf")
    _write_3mf(parts, out_3mf)
    for p in parts:  # the per-part STLs were scaffolding, not output
        with contextlib.suppress(OSError):
            os.unlink(p["stl_path"])
    elapsed = time.monotonic() - t0

    return StepImportResult(
        output_path=out_3mf,
        file_size_bytes=Path(out_3mf).stat().st_size,
        body_count=len(parts),
        conversion_time_s=round(elapsed, 3),
        output_paths=[out_3mf],
        output_format="3mf",
        part_names=data["names"],
        part_colors=data["colors"],
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
