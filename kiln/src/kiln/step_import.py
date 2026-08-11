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

This is a **free-tier** feature — it needs nothing beyond this package.
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import logging
import math
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_VALID_EXTENSIONS = frozenset({".step", ".stp"})

#: The first line of every STEP file, mandated by the exchange standard
#: itself (ISO 10303-21 clause 5).  Every CAD system that writes STEP writes
#: this, so it identifies the format from the BYTES rather than from a name a
#: user is free to change.
#:
#: Kept here, beside the extension set, because this module owns the question
#: "is this a STEP file?" and the answer should not be spelled out again in
#: whichever mesh parser happens to be holding the file.
_STEP_MAGIC = b"ISO-10303-21"

#: How far in to look for it.  A conforming file starts with the magic at byte
#: zero; a few exporters emit a comment or a byte-order mark first, so allow a
#: short run-up rather than insisting on offset 0 and calling a real CAD file
#: unrecognised.
_STEP_MAGIC_WINDOW = 512

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
curvature_elements = {curvature_elements!r}

gmsh.initialize()
gmsh.option.setNumber("General.Terminal", 0)
try:
    # Set BEFORE open(), so a gmsh that cannot be bounded costs nothing.
    # See _GMSH_CURVATURE_ELEMENTS for what this buys and why it is that
    # number rather than one somebody picked.
    gmsh.option.setNumber("Mesh.MeshSizeFromCurvature", curvature_elements)
except Exception:
    # Renamed from Mesh.CharacteristicLengthFromCurvature in gmsh 4.7
    # (2020-11); 4.15.2 still accepts BOTH, so this only fires on a build
    # older than that rename.  The honest answer there is to hand the file
    # to a backend that CAN be told a density, not to emit a mesh whose
    # fineness nobody chose.
    sys.stderr.write("this gmsh does not accept Mesh.MeshSizeFromCurvature\n")
    raise SystemExit({unboundable_exit})
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


@dataclass(frozen=True)
class TessellationBound:
    """What a backend was actually TOLD about mesh density, in its own terms.

    Four shapes, because the backends genuinely differ and any record that
    flattened them to one would have to lie about the rest:

    - ``"linear_angular"`` — both bounds given (the OCCT paths, cadquery).
    - ``"linear"`` — a chord tolerance only, no angular bound (FreeCAD).
    - ``"elements_per_circle"`` — a curvature target in gmsh's own unit,
      which is the only density gmsh can be given.
    - ``"unbounded"`` — Kiln named no density at all and the backend used
      whatever it derives on its own.

    ``"unbounded"`` is a state, not a missing number.  The honest answer to
    "how fine is this mesh?" there is "we did not say" — which a reader must
    be able to show, because rendering it as blank would let a mesh nobody
    chose the density of pass for one somebody did.  No backend reports it
    today; it stays because the condition it names is a real one, and a
    reader that never learned to show it would go silent the moment a new
    backend arrives unbounded.

    A backend that gains a bound fills its numbers in and changes
    :attr:`kind` — which is what gmsh just did, and nothing downstream had
    to learn a new shape to follow it.
    """

    kind: str
    """``"linear_angular"``, ``"linear"``, ``"elements_per_circle"``, or
    ``"unbounded"``."""

    linear: float | None = None
    """Linear (chordal) deflection, in the STEP's OWN units.

    Not necessarily mm: a STEP carries its units, and this is the number
    handed to the backend, not a conversion of it.
    """

    angular: float | None = None
    """Angular deflection in radians, when the backend takes one."""

    elements_per_circle: int | None = None
    """Elements gmsh is asked for per full circle of curvature.

    Its own unit, kept as its own unit.  Gmsh cannot be handed a chordal
    deflection at all, and restating this as one would claim a guarantee it
    does not make: the segment count implies a sag finer than the mesher
    actually delivers, because ``MeshSizeFromCurvature`` is a target for a
    surface mesh rather than an exact per-circle count.
    """

    reason: str | None = None
    """Why there is no number, when :attr:`kind` is ``"unbounded"``."""


@dataclass(frozen=True)
class SourceTopology:
    """What the CAD kernel saw, counted before tessellation erased it.

    STL has no topology at all — it is a bag of triangles — so the question
    "did this file declare a SOLID, or only the surfaces around one?" is
    answerable exactly once, in the kernel, and never again downstream.  A
    reader that welds coincident vertices reconstructs a closed mesh from
    either one, which is why the mesh cannot be asked afterwards.

    Recorded because the two are genuinely different files with the same
    triangles, and the difference is the user's to know: a surface export is
    usually an accident of the export dialog, and the fix lives in their CAD
    tool rather than in mesh repair.

    **This is not a printability verdict, and must not be read as one.**
    Measured on this module's own fixtures: a compound of the six loose faces
    of a box reports zero solids here and still tessellates to a watertight
    mesh of the correct volume, byte-identical to the solid's.  Surfaces that
    meet still mesh into a printable part; the file simply never said they
    should.  Whether the RESULT is printable is the mesh layer's question,
    asked of the mesh.
    """

    solids: int
    """Closed solid bodies the kernel found.  Zero means a surface model."""

    shells: int
    """Connected face groups.  A solid has at least one; a surface model has
    one per disjoint patch, which is how six loose faces read as six."""

    faces: int
    """Trimmed surfaces, whatever they are attached to."""

    @property
    def is_surface_model(self) -> bool:
        """True when the file carried surfaces but declared no solid.

        The ``faces`` guard keeps an empty or unreadable shape — zero of
        everything — from reading as a surface model.  Nothing at all is a
        different fact from surfaces-without-a-solid.
        """
        return self.solids == 0 and self.faces > 0


@dataclass(frozen=True)
class ExactGeometry:
    """What the CAD kernel measures on the user's OWN file, before triangles.

    Every size number Kiln otherwise reports about a CAD part is measured off
    a mesh Kiln generated from it, which makes the answer a property of the
    converter as much as of the part — the same file can measure differently
    on two machines.  These numbers do not move.
    They come from the analytic B-rep — the surfaces the engineer actually
    drew — so they match what their CAD package says, and a user can check.

    Measured on this module's own fixtures: a 72x46x11 plate with 9 mm and
    14 mm through-holes has an analytic volume of 34038.8918 mm3.  The kernel
    reads it back to within 4e-13 %; the mesh Kiln converts it to reads
    34039.8800 mm3, out by 0.0029 %.  Small, and not zero — and it is the
    kind of number a machinist checks against their own model.

    **Always returned, never ``None``** — :attr:`available` carries the
    answer instead, with :attr:`reason` next to it.  A report that has to
    explain why a band is missing cannot do it from an absent object, and
    the two states a caller must tell apart (no kernel installed vs. a file
    the kernel refused) are both reasons rather than gaps.

    **Units are millimetres, whatever the file declares.**  Verified against
    an inch-declared fixture (``CONVERSION_BASED_UNIT('INCH')``, coordinates
    written in inches): OCCT's reader normalises on transfer, so a 1 inch
    cube reads back as 25.4 mm and 16387.064 mm3.  No unit guess is involved
    here, which is the point — Kiln's mesh pipeline elsewhere INFERS units
    from overall size, and that inference cannot be right for every part.

    A caution for anyone extending this: the file's DECLARED unit is not
    readable from ``SI_UNIT`` in the header.  The same inch fixture carries
    ``SI_UNIT(.MILLI.,.METRE.)`` as its base unit with the inch layered on
    top as a ``CONVERSION_BASED_UNIT``, so a text scan for ``SI_UNIT``
    reports an inch part as millimetres.  Nothing here reads it, and nothing
    should read it that way.
    """

    available: bool
    """Whether the kernel measured this file.  False leaves every number
    below ``None`` and fills in :attr:`reason`."""

    reason: str | None = None
    """Why there are no numbers, in a sentence a reader can act on."""

    volume_mm3: float | None = None
    """Enclosed volume of the solid, in mm3, from ``BRepGProp``.

    Signed by the kernel's convention and returned as measured.  A surface
    model with no closed volume reports whatever its faces enclose, which is
    why :attr:`topology` travels with it — the number needs its subject.
    """

    surface_area_mm2: float | None = None
    """Total area of every face, in mm2."""

    bbox_min_mm: tuple[float, float, float] | None = None
    """Tight analytic bounding-box minimum, in mm."""

    bbox_max_mm: tuple[float, float, float] | None = None
    """Tight analytic bounding-box maximum, in mm."""

    size_mm: tuple[float, float, float] | None = None
    """Bounding-box extents, in mm — the part's real envelope.

    Computed with ``AddOptimal`` over the exact geometry, triangulation
    excluded.  Not ``Add``: it pads by shape tolerance and is not tight on
    spline surfaces.  Do not swap them.
    """

    is_valid: bool | None = None
    """``BRepCheck_Analyzer``'s verdict on the shape the file describes.

    A property of the CAD, not of anything Kiln did to it.  False means the
    file itself carries a topological fault — self-intersecting faces, a
    shell that does not close where it claims to — which is the user's to
    fix in their CAD tool, and not something mesh repair addresses.
    """

    topology: SourceTopology | None = None
    """Solid / shell / face counts, from the same read.

    Duplicated from :attr:`MeshConversion.source` on purpose: that one is
    recorded only when a kernel backend happened to do the CONVERSION, and
    this read stands on its own.  Both come from the same kernel counting
    the same file, so they agree; neither depends on the other existing.
    """


@dataclass(frozen=True)
class MeshConversion:
    """How a mesh was made from CAD — the neutral half of that question.

    Records only what this module is in a position to know for certain:
    which backend did the work, and what it was told.  Both are facts about
    an operation that just happened here, so neither can be re-derived
    honestly from outside: the backend is picked by fall-through inside
    :func:`convert_step_to_stl` (a machine with FreeCAD installed but broken
    uses a different one than the priority order alone predicts), and the
    bound is whichever constant that path quotes.

    What the accuracy MEANS — whether it is fine enough for a part, how it
    compares across backends, whether a tolerance survives it — is judgment,
    and deliberately not here.
    """

    backend: str
    """Which backend ran: ``freecad``, ``gmsh``, ``occt``, ``occt-xcaf``,
    or ``cadquery``.  Stable tokens, not display strings."""

    bound: TessellationBound
    """The density it was told to use."""

    source: SourceTopology | None = None
    """What the kernel counted in the file, when the backend that ran can say.

    ``None`` is a state, not a gap: only the two kernel backends (``occt``,
    ``cadquery``) read a topology Kiln can count.  FreeCAD and gmsh are
    driven as converters — they are handed a path and hand back a mesh — and
    the colour-aware ``occt-xcaf`` path walks document labels rather than
    the shape tree.  Reporting zero solids for those would turn "we did not
    look" into "there is no solid", which is the one wrong answer here.
    """


@dataclass
class StepImportResult:
    """Result of a STEP-to-STL conversion."""

    output_path: str
    """Path to the primary output STL (or directory if multi-body split)."""

    file_size_bytes: int
    """Size of the output STL file(s) in bytes."""

    body_count: int
    """How many meshes the conversion planned to write — at least 1.

    Not a count of solids, though it equals one for every file that has any:
    a surface model has no solid and still writes its single merged mesh, so
    this floors at 1 where :attr:`MeshConversion.source` reports 0.  The
    honest count of what the file DECLARED lives there.
    """

    conversion_time_s: float
    """Wall-clock seconds for the conversion."""

    warnings: list[str] = field(default_factory=list)
    """Non-fatal warnings encountered during conversion."""

    output_paths: list[str] = field(default_factory=list)
    """All output STL paths (relevant for multi-body split)."""

    output_format: str = "stl"
    """``"stl"`` or ``"3mf"`` — what :attr:`output_path` actually is."""

    part_names: list[str] = field(default_factory=list)
    """Per-part names, when the colour-aware path ran — as a user will see them.

    These are the names as written to the output, whatever its format, so a
    caller can address a part by the name it will read there.  A STEP may name
    two bodies the same and may name none of them at all; the output may do
    neither (see :func:`~kiln.threemf_parser.unique_object_names`), so a repeat
    carries a ``" (2)"`` suffix and a body nobody named reads as ``Part 1``.
    """

    part_colors: list[str | None] = field(default_factory=list)
    """Per-part ``#RRGGBB`` colours (or ``None``), same order as names."""

    conversion: MeshConversion | None = None
    """Which backend drew this mesh, and at what density.

    ``None`` only where nothing converted anything — a caller holding a
    result it did not obtain from a conversion.  Every path in this module
    that produces a mesh from a STEP fills this in.
    """

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


# The purpose-built console binaries.  These take the script directly and
# cannot open a window, so they are tried first on every platform.
_FREECAD_CONSOLE_NAMES = ("FreeCADCmd", "freecadcmd", "freecad-cmd")

# The ordinary launcher, which runs a script headless when given ``-c``.
# FreeCAD 1.x ships NO FreeCADCmd at all — measured 2026-08-11, a stock
# /Applications/FreeCAD.app contains exactly one binary, ``FreeCAD`` — so on a
# current install this is not a fallback, it is the only door there is.
_FREECAD_LAUNCHER_NAMES = ("FreeCAD", "freecad")

# Where the launcher lives when it is NOT on PATH.  A macOS .app is a
# directory, so installing FreeCAD the normal way puts nothing on PATH and
# ``shutil.which`` can never see it.  Both entries are the standard system and
# per-user Applications folders; deliberately no Linux or Windows paths, since
# a wrong guess here is worse than none — see :func:`_find_freecad_cmd`.
_FREECAD_BUNDLE_PATHS = (
    "/Applications/FreeCAD.app/Contents/MacOS/FreeCAD",
    "~/Applications/FreeCAD.app/Contents/MacOS/FreeCAD",
)


def _find_freecad_cmd() -> list[str] | None:
    """Return argv for a FreeCAD that can run a script headless, or None.

    Returns the ARGV rather than a bare name so the console flag travels with
    the binary that needs it: ``FreeCADCmd script.py`` and
    ``FreeCAD -c script.py`` are the same job through two different front
    doors, and only the caller splicing in the script path can keep them
    straight.

    This searched PATH for ``FreeCADCmd``/``freecadcmd``/``freecad-cmd`` and
    nothing else until 2026-08-11, which found a normally-installed FreeCAD in
    neither respect: on macOS it installs as an .app bundle (nothing on PATH),
    and FreeCAD 1.x dropped the ``FreeCADCmd`` binary entirely (nothing by
    that name to find).  Both misses point the same way — Kiln reported no
    STEP backend and sent the user to a 228 MB download while a working
    converter sat in /Applications.

    A path is added here only when it has been run and measured.  The reason
    is asymmetric cost: :func:`convert_step_to_stl` re-raises
    :class:`StepImportError` from a detected backend instead of falling
    through to the next one, so a hopeful guess does not merely fail to help
    — it takes a machine that converts fine via the OCCT kernel today and
    breaks it.  An honest gap costs a user one install; a wrong path costs
    them a working feature.
    """
    for name in _FREECAD_CONSOLE_NAMES:
        if found := shutil.which(name):
            return [found]
    for name in _FREECAD_LAUNCHER_NAMES:
        if found := shutil.which(name):
            return [found, "-c"]
    for candidate in _FREECAD_BUNDLE_PATHS:
        bundle = Path(candidate).expanduser()
        if bundle.is_file() and os.access(bundle, os.X_OK):
            return [str(bundle), "-c"]
    return None


def _find_gmsh_cmd() -> str | None:
    """Return 'gmsh' if the CLI is on PATH.

    Deliberately NOT given the app-bundle treatment above, and the reason is
    not that gmsh lacks a macOS bundle — it is that this probe and the
    conversion it gates do not ask the same question.
    :func:`_convert_via_gmsh` never runs this command: it shells
    ``python3`` and the script does ``import gmsh``, so what the conversion
    actually needs is the gmsh PYTHON MODULE, while what this checks for is
    the CLI binary.  Teaching it to find a Gmsh.app would therefore report a
    backend that cannot convert — and per the hard-fail note in
    :func:`_find_freecad_cmd`, that is exactly the shape that breaks a
    working machine.  Closing that mismatch means deciding which of the two
    the probe is for, which is a real change and wants a machine with gmsh
    installed to verify against; none was available (measured 2026-08-11: no
    binary, no app bundle, no importable module).
    """
    return "gmsh" if shutil.which("gmsh") else None


def _module_installed(name: str) -> bool:
    """True when *name* is importable from disk, WITHOUT importing it.

    Asking by import is what a probe naturally wants to do and is the
    wrong trade here.  Importing the OCCT bindings to answer "is the
    kernel present?" costs 323 modules and ~247 MB resident (measured
    2026-08-03), and :func:`check_step_support` is a registered tool —
    so on the hosted server, a caller merely ASKING whether STEP is
    supported permanently grew the process by a quarter of a gigabyte,
    even when the answer was no and nothing was ever converted.

    Nothing needs the import: every conversion path runs the kernel in a
    CHILD interpreter (see :func:`_convert_via_ocp`), and each caller of
    this probe consumes only its boolean.

    The question this answers is deliberately weaker than "does it
    import cleanly" — a partial or ABI-broken install can carry a valid
    spec and still fail on import.  That gap is covered by the layer
    underneath: the child has to succeed on its own merits, and its
    failure surfaces as a normal conversion error naming the backend.
    Paying a quarter gigabyte in the parent to sharpen a check the child
    repeats for free is the wrong trade.

    ``find_spec`` imports a submodule's PARENT packages in order to
    search them, so the names probed here are top-level on purpose:
    probing ``OCP.STEPControl`` would load the very thing this exists to
    avoid.  It raises ``ModuleNotFoundError`` for a missing parent,
    ``ValueError`` for a name whose ``__spec__`` is None, and
    ``AttributeError`` against an exotic meta-path finder — all of which
    mean "not usable here".
    """
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError, AttributeError):
        return False


def _cadquery_available() -> bool:
    """Return True if cadquery is installed (not imported — see above)."""
    return _module_installed("cadquery")


def _ocp_available() -> bool:
    """Return True if the bare OCCT bindings (``cadquery-ocp``) are present.

    Deliberately probes the ``OCP`` MODULE rather than a distribution name:
    three different packages provide it (``cadquery-ocp-novtk``,
    ``cadquery-ocp``, and full ``cadquery`` transitively), and a user who
    already has any of them should just work.  See :data:`PIP_BACKEND` for
    which one we install and the measured reason why.
    """
    return _module_installed("OCP")


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
            # Reported as one readable command, not the argv list: this goes
            # out through `kiln step check` and a registered tool, where the
            # useful answer is the line a human could paste.
            "available": freecad_cmd is not None,
            "executable": shlex.join(freecad_cmd) if freecad_cmd else None,
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
    freecad_cmd: Sequence[str],
) -> tuple[list[str], int]:
    """Run FreeCAD with a helper script to convert STEP → STL.

    *freecad_cmd* is the argv from :func:`_find_freecad_cmd`, not a bare
    name — the console flag has to stay attached to the binary that needs it.

    Returns:
        (list of output paths, body_count)
    """
    if isinstance(freecad_cmd, str):
        # A str is a Sequence[str], so splatting one would spell the command
        # out a character per argument and exec 'F'.  Cheaper to refuse.

        raise TypeError("freecad_cmd must be an argv sequence, not a string")
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
            [*freecad_cmd, script_path],
            capture_output=True,
            text=True,
            # Console mode reads stdin when it is left open: it finishes the
            # script, drops to an interactive '>>>' and waits there forever.
            # Measured 2026-08-11 — the mesh was written and the process still
            # had to be killed at 90 s, so without this the conversion burns
            # the full timeout and then reports failure over a finished file.
            stdin=subprocess.DEVNULL,
            timeout=SUBPROCESS_TIMEOUT_S,
        )
    finally:
        os.unlink(script_path)

    # Parse output for KILN_RESULT line.  The launcher prints the whole
    # environment ahead of the script's own output, so this has to scan for
    # the KILN_RESULT prefix rather than read a clean stdout — which
    # _parse_kiln_result already does, line by line.
    return _parse_subprocess_result(result, "FreeCAD")


def _convert_via_gmsh(
    step_path: Path,
    output_dir: Path,
) -> tuple[list[str], int]:
    """Run gmsh Python script to convert STEP → STL.

    Density is bounded by :data:`_GMSH_CURVATURE_ELEMENTS`: gmsh is asked for
    126 elements per full circle, which measures out at roughly R/700 of
    chordal sag — 0.067 mm on a 150 mm sphere, against 0.0068 mm from the
    OCCT kernel and 0.162 mm from the FreeCAD path, all three measured on
    the same sphere.  Before that bound existed this backend meshed at
    whatever the installed gmsh derived from the model's bounding box: 330
    triangles and over 3 mm of sag on that sphere, differing per machine
    with nothing in the result to say so.

    Returns:
        (list of output paths, body_count)

    Raises:
        RuntimeError: the installed gmsh will not accept a density bound.
            Deliberately not :class:`StepImportError` — see below.
    """
    script = _GMSH_SCRIPT_TEMPLATE.format(
        step_path=str(step_path),
        output_dir=str(output_dir),
        curvature_elements=_GMSH_CURVATURE_ELEMENTS,
        unboundable_exit=_GMSH_UNBOUNDABLE_EXIT,
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

    if result.returncode == _GMSH_UNBOUNDABLE_EXIT:
        # A plain RuntimeError on purpose.  convert_step_to_stl re-raises
        # StepImportError without trying anything else, and an un-boundable
        # gmsh is precisely the case where the NEXT backend is the better
        # answer — so raising the ordinary kind lets the existing
        # fall-through carry it there.  Without this, bounding gmsh would
        # turn a working pre-4.7 install into a hard failure even on a
        # machine with the OCCT kernel sitting right behind it.
        raise RuntimeError(
            "gmsh is too old to be told a mesh density "
            "(needs Mesh.MeshSizeFromCurvature, gmsh 4.7+)"
        )

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

#: What the GMSH backend gets told, so its density is chosen here too.
#:
#: Until this existed the gmsh script set no size option at all, so per
#: gmsh's own manual (4.15.2 §1.2.2) the element size fell through to the
#: first item in its minimum — "the size of the model bounding box" — which
#: is blind to curvature and moves with the installed gmsh's defaults.  Same
#: file, same user, two machines, no way for the caller to tell.
#:
#: gmsh cannot be given :data:`_OCP_LINEAR_DEFLECTION`.  That manual lists
#: every term it minimises over (bounding box, size at points, curvature,
#: fields, per-entity) and none of them means chordal sag; the one absolute
#: knob, ``Mesh.MeshSizeMax``, applies to FLAT faces too because gmsh is a
#: finite-element mesher and subdivides planes.  The ~1.7 mm element that
#: 0.005 mm of sag implies at r=75 would put ~27,000 triangles on a flat
#: 200 mm square that a tessellator covers with 2.
#:
#: ``Mesh.MeshSizeFromCurvature`` IS expressible, and it is denominated in
#: exactly the unit the kernel's angular bound turns out to use: elements
#: per 2*pi radians.  Measured 2026-08-10 against this OCP build, OCCT lands
#: on ceil(4*pi/angle) segments per full circle, radius-independent — four
#: angles x three radii (r = 0.25, 0.5, 1.0 mm), no exceptions:
#:
#:   _OCP_ANGULAR_DEFLECTION   4*pi/angle   measured segments/circle
#:   0.05                          251.3      252
#:   0.10  ← shipping              125.7      126
#:   0.15                           83.8       84
#:   0.20                           62.8       63
#:
#: So this is not a new number: it is the kernel's own angular guarantee
#: restated in gmsh's units, and it tracks automatically if that constant
#: ever moves.
#:
#: What it ACTUALLY buys — measured against gmsh 4.15.2, not derived.  The
#: derivation above would predict sag <= R*(1-cos(pi/126)) = R/3217; gmsh
#: does not deliver that, because ``MeshSizeFromCurvature`` is a target for
#: a 2-D surface mesh rather than the exact per-circle segment count OCCT's
#: angular deflection resolves to.  Asked for 126 it lands nearer 60-90.
#: Run on spheres of r = 5, 25 and 75 mm:
#:
#:   bound   r      triangles   sag        worst sag/R
#:   none    75           330   3.164 mm   R/24        ← what shipped before
#:   126      5        12,118   0.007 mm   R/710
#:   126     25        12,140   0.017 mm   R/1505
#:   126     75        12,140   0.067 mm   R/1116
#:
#: So the honest claim is sag <= ~R/700 across that range, i.e. 0.067 mm on
#: a 150 mm sphere.  On the same sphere the kernel gives 0.0068 mm and the
#: FreeCAD path gives 0.162 mm (TESSELLATION_TOLERANCE, 0.1 mm linear, no
#: angular bound at all): gmsh lands roughly 10x coarser than one and 2.4x
#: finer than the other.  That is what "comparable" can mean while the two
#: bounded backends sit 24x apart from each other; closing THAT gap is a
#: product decision, not a constant.
#:
#: The number that justifies the change is the first row.  Unbounded, gmsh
#: put 330 triangles on a 150 mm ball and missed the true surface by over
#: 3 mm — visibly faceted, and 20x worse than the coarsest bounded backend.
#: Raising the bound to 220 reaches ~0.019 mm for 3x the triangles; 126 was
#: kept because it already beats the FreeCAD path at 12k triangles, and a
#: viewer gains nothing from the rest.
#:
#: Because gmsh takes the minimum, adding this can only refine: no existing
#: gmsh user gets a coarser mesh than they get today.
_GMSH_CURVATURE_ELEMENTS = math.ceil(4 * math.pi / _OCP_ANGULAR_DEFLECTION)

#: Exit code the gmsh child uses for "this gmsh will not accept a density
#: bound".  It gets its own code because the parent must treat it unlike any
#: other failure — see :func:`_convert_via_gmsh`.
_GMSH_UNBOUNDABLE_EXIT = 9


# What each backend is told about density, kept beside the constants it
# quotes rather than restated at the call sites.  One home means the record
# cannot drift from the value actually passed: change a constant and what
# gets recorded changes with it, because it IS the constant.
_FREECAD_BOUND = TessellationBound(kind="linear", linear=TESSELLATION_TOLERANCE)
_KERNEL_BOUND = TessellationBound(
    kind="linear_angular",
    linear=_OCP_LINEAR_DEFLECTION,
    angular=_OCP_ANGULAR_DEFLECTION,
)
#: Gmsh, in the only unit gmsh accepts.  Reads :data:`_GMSH_CURVATURE_ELEMENTS`
#: rather than restating it, so the recorded bound and the bound actually set
#: on the mesher are the same number by construction and cannot drift apart.
#:
#: Deliberately NOT restated as a chordal deflection, even though the constant
#: is derived from the kernel's angular bound.  The derivation predicts sag of
#: R/3217; measurement puts it near R/700 — 2.9x optimistic — because
#: ``MeshSizeFromCurvature`` is a target for a surface mesh, not the exact
#: per-circle segment count the kernel's angular deflection resolves to.
#: Recording a chord figure here would publish the prediction as the promise.
_GMSH_BOUND = TessellationBound(
    kind="elements_per_circle",
    elements_per_circle=_GMSH_CURVATURE_ELEMENTS,
)


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

# body_count is a WRITE PLAN — how many STLs to emit — so a file with no
# solid still writes its one merged mesh.  The true count goes out
# separately: `or 1` is where a surface model used to become "1 body" and
# the only record of what the file actually declared was lost.
body_count = len(solids) or 1


def count(kind):
    exp = TopExp_Explorer(shape, kind)
    n = 0
    while exp.More():
        n += 1
        exp.Next()
    return n


topology = {{
    "solids": len(solids),
    "shells": count(TopAbs_ShapeEnum.TopAbs_SHELL),
    "faces": count(TopAbs_ShapeEnum.TopAbs_FACE),
}}


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

print("KILN_RESULT:" + json.dumps(
    {{"outputs": outputs, "body_count": body_count, "topology": topology}}
))
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
from OCP.TopAbs import TopAbs_ShapeEnum
from OCP.TopExp import TopExp_Explorer
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


def count(target, kind):
    exp = TopExp_Explorer(target, kind)
    n = 0
    while exp.More():
        n += 1
        exp.Next()
    return n


# Summed over the free shapes, which together ARE the file: this path walks
# document labels rather than one root shape, so the census is accumulated
# instead of read off a single tree.  Same three numbers as the plain
# reader's, so a caller cannot tell which backend counted them.
topology = {{"solids": 0, "shells": 0, "faces": 0}}

outputs, names, colors = [], [], []
for i in range(1, labels.Length() + 1):
    label = labels.Value(i)
    shape = shape_tool.GetShape_s(label)

    topology["solids"] += count(shape, TopAbs_ShapeEnum.TopAbs_SOLID)
    topology["shells"] += count(shape, TopAbs_ShapeEnum.TopAbs_SHELL)
    topology["faces"] += count(shape, TopAbs_ShapeEnum.TopAbs_FACE)

    # Two things a STEP calls a "name" were stamped by software, not chosen by
    # a person, and both reach the user as gibberish in a slicer's object list:
    #
    #   PRODUCT('SOLID','SOLID')                       - shape nobody named
    #   PRODUCT('Open CASCADE STEP translator 7.9 1')  - writer's own identity
    #
    # Both are reported as unnamed so the caller can label them "Part 1".  The
    # first test is against THIS body's own type rather than a list of words,
    # so a FACE someone deliberately called "SOLID" keeps the name they chose;
    # the second is a prefix because the trailing counter varies per file.
    name_attr = TDataStd_Name()
    name = ""
    if label.FindAttribute(TDataStd_Name.GetID_s(), name_attr):
        got = name_attr.Get().ToExtString().strip()
        stamped_by_software = (
            got == shape.ShapeType().name.removeprefix("TopAbs_")
            or got.startswith("Open CASCADE STEP translator")
        )
        if not stamped_by_software:
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
    "names": names, "colors": colors, "topology": topology,
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
        (list of output paths, body_count, source topology)
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

    data = _parse_kiln_result(result, "OCCT")
    return data["outputs"], data["body_count"], _topology_from_result(data)


def _convert_via_cadquery(
    step_path: Path,
    output_dir: Path,
    merge_bodies: bool,
) -> tuple[list[str], int]:
    """Use cadquery (in-process) to convert STEP → STL.

    Returns:
        (list of output paths, body_count, source topology)
    """
    import cadquery as cq  # type: ignore[import-untyped]

    result = cq.importers.importStep(str(step_path))
    solids = result.solids().vals()
    # See the OCP script: body_count is the write plan, topology is the fact.
    body_count = len(solids) if solids else 1
    topology = SourceTopology(
        solids=len(solids),
        shells=len(result.shells().vals()),
        faces=len(result.faces().vals()),
    )

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
        return [out_path], body_count, topology
    else:
        outputs: list[str] = []
        for i, solid in enumerate(solids):
            out_path = str(output_dir / f"body_{i}.stl")
            ws = cq.Workplane().add(solid)
            cq.exporters.export(ws, out_path, exportType="STL", **_tess)
            outputs.append(out_path)
        return outputs, body_count, topology


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


def _topology_from_result(data: dict[str, Any]) -> SourceTopology | None:
    """Read the kernel's census out of a child's KILN_RESULT, or admit none.

    Shared by both OCCT scripts so the plain and colour-aware paths cannot
    grow different ideas of what the counts mean.  Absent or malformed reads
    as ``None`` — "we did not look" — rather than raising or, worse,
    defaulting to zero solids, which is the one wrong answer here: a child
    from a half-upgraded install would report every file as surface soup.
    """
    raw = data.get("topology")
    if not isinstance(raw, dict):
        return None
    try:
        return SourceTopology(
            solids=int(raw["solids"]),
            shells=int(raw["shells"]),
            faces=int(raw["faces"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


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
    # Blank rather than invented: naming an unnamed part happens in ONE place
    # (:func:`~kiln.threemf_parser.unique_object_names`), so every door reports
    # the same "Part 1" instead of each growing its own fallback.
    data.setdefault("names", [""] * n)
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
) -> list[str]:
    """Write parts as a core-spec 3MF with per-object colour and name.

    ``parts``: dicts with ``stl_path`` (binary STL), ``name``, and ``color``
    (``#RRGGBB`` or ``None``).  Colour rides the core spec's
    ``<basematerials>`` + ``displaycolor``, referenced object-level via
    ``pid``/``pindex`` — the one encoding Kiln's own
    :mod:`kiln.threemf_parser`, BambuStudio, and PrusaSlicer all read.  A
    part without a colour gets no ``pid`` and renders in each viewer's
    default, which is honest: the STEP didn't say.

    Names are made unique before they are written
    (:func:`~kiln.threemf_parser.unique_object_names`) — colour is addressed
    per object by name downstream, and a STEP whose bodies share one (two
    instances of the same part, or unnamed bodies that both degrade to their
    shape type) would otherwise cost the file every colour it carries.
    Returns the names as written, in part order, so the caller can report
    what is actually in the file.

    Pure stdlib (zipfile + string XML) by design — this module must import
    with no third-party dependencies installed; the name helper is stdlib
    too, from a module already sitting beside this one.
    """
    import zipfile
    from xml.sax.saxutils import quoteattr

    from kiln.threemf_parser import unique_object_names

    names = unique_object_names([p.get("name") for p in parts])
    colored = [i for i, p in enumerate(parts) if p.get("color")]
    color_index = {part_index: i for i, part_index in enumerate(colored)}

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
        for part_index in colored:
            xml.append(
                f"   <base name={quoteattr(names[part_index])} "
                f"displaycolor=\"{parts[part_index]['color']}\"/>\n"
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
            pid_attr = f' pid="1" pindex="{color_index[obj_index]}"'
        xml.append(
            f"  <object id=\"{obj_id}\" type=\"model\" "
            f"name={quoteattr(names[obj_index])}{pid_attr}>\n   <mesh>\n"
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

    return names


# ---------------------------------------------------------------------------
# Exact geometry — the file's own numbers, never the mesh's
# ---------------------------------------------------------------------------

# One reader, one answer.  Deliberately NOT folded into the two conversion
# templates above, even though each already holds a live shape and could
# compute these for free: a file reaches Kiln through the plain path, the
# colour-aware path, a FreeCAD or gmsh conversion that never loads a kernel
# shape at all, and a cache hit that converts nothing.  Four places computing
# one measurement is four places for it to drift; this is the one that runs
# for every one of them, and its answer is cached beside the mesh cache so no
# door pays twice for the same bytes.
_OCP_EXACT_SCRIPT_TEMPLATE = r'''
import json, sys

from OCP.Bnd import Bnd_Box
from OCP.BRepBndLib import BRepBndLib
from OCP.BRepCheck import BRepCheck_Analyzer
from OCP.BRepGProp import BRepGProp
from OCP.GProp import GProp_GProps
from OCP.IFSelect import IFSelect_ReturnStatus
from OCP.STEPControl import STEPControl_Reader
from OCP.TopAbs import TopAbs_ShapeEnum
from OCP.TopExp import TopExp_Explorer

step_path = {step_path!r}

reader = STEPControl_Reader()
if reader.ReadFile(step_path) != IFSelect_ReturnStatus.IFSelect_RetDone:
    sys.stderr.write("OCCT could not read the STEP file\n")
    raise SystemExit(3)
reader.TransferRoots()
shape = reader.OneShape()

if shape.IsNull():
    sys.stderr.write("the file transferred to an empty shape\n")
    raise SystemExit(4)


def count(kind):
    exp = TopExp_Explorer(shape, kind)
    n = 0
    while exp.More():
        n += 1
        exp.Next()
    return n


vol = GProp_GProps()
BRepGProp.VolumeProperties_s(shape, vol)
area = GProp_GProps()
BRepGProp.SurfaceProperties_s(shape, area)

# useTriangulation=False: with triangulation allowed this measures whatever
# mesh happens to be attached to the shape, which is the exact thing these
# numbers exist to avoid.  AddOptimal, not Add — see ExactGeometry.size_mm.
box = Bnd_Box()
BRepBndLib.AddOptimal_s(shape, box, True, False)
xmin, ymin, zmin, xmax, ymax, zmax = box.Get()

print("KILN_RESULT:" + json.dumps({{
    "volume_mm3": vol.Mass(),
    "surface_area_mm2": area.Mass(),
    "bbox_min_mm": [xmin, ymin, zmin],
    "bbox_max_mm": [xmax, ymax, zmax],
    "is_valid": bool(BRepCheck_Analyzer(shape).IsValid()),
    "topology": {{
        "solids": count(TopAbs_ShapeEnum.TopAbs_SOLID),
        "shells": count(TopAbs_ShapeEnum.TopAbs_SHELL),
        "faces": count(TopAbs_ShapeEnum.TopAbs_FACE),
    }},
}}))
'''


#: Seconds a kernel read gets before it is killed.  Far below the conversion
#: timeout on purpose: reading and measuring a shape is cheap where
#: tessellating it is not, so a read that runs this long is wedged rather
#: than busy, and an intake report that hangs for five minutes has already
#: failed the person waiting for it.
EXACT_READ_TIMEOUT_S: int = 60

#: Shape of the cached exact-read payload.  Bump when a field is added,
#: removed, or changes meaning: it is part of the cache key, so a bump
#: retires every stale entry instead of serving one that no longer answers
#: the question the reader is now asking.
_EXACT_PAYLOAD_VERSION = "exact-v1"


def _exact_unavailable(reason: str) -> ExactGeometry:
    return ExactGeometry(available=False, reason=reason)


def read_exact_geometry(path: str) -> ExactGeometry:
    """Measure a CAD file with the kernel — no tessellation anywhere in it.

    The answer to "how big is this part, really?"  Everything else Kiln
    reports about a CAD part's size is measured off triangles Kiln made,
    which makes it a fact about the converter as much as about the part.
    This is the file's own geometry, and it is what the user's CAD package
    will agree with.

    Runs OUT OF PROCESS for the reasons :func:`_convert_via_ocp` documents
    at length — a compiled kernel call cannot be interrupted from Python and
    a pathological file must not take a server worker or its memory with it.
    The result is cached beside the mesh cache, keyed on file content alone
    (these numbers do not depend on any tessellation setting), so the second
    door to ask about a file pays nothing.

    Never raises for a bad input: a file the kernel refuses, a machine with
    no kernel, and a path that is not CAD at all each come back as
    ``available=False`` with a reason.  A caller composing a report needs the
    reason more than it needs an exception.

    :param path: A ``.step`` / ``.stp`` file.  Anything else is answered
        honestly rather than attempted — mesh formats carry no analytic
        geometry to read, so there is nothing exact to be had from one.
    :returns: An :class:`ExactGeometry`, always.
    """
    if not looks_like_step(path):
        return _exact_unavailable(
            "Exact geometry comes from a CAD file's own surfaces. This is a "
            "mesh — it is already triangles, so there is nothing analytic "
            "left in it to measure."
        )
    if not _ocp_available():
        # Named separately from a read failure because the two have opposite
        # fixes: this one is a missing backend, the other is the file.
        #
        # Worded for whoever is reading it, via the same surface check the
        # conversion refusal uses.  A hosted caller has no shell to run an
        # install in, so handing them one is a dead end dressed up as help —
        # the mistake this module already fixed once for `install_help`, and
        # easy to re-introduce because the local wording reads fine from a
        # laptop.
        from kiln.runtime_env import is_hosted_multitenant

        if is_hosted_multitenant():
            return _exact_unavailable(
                "This server has no CAD kernel, so it could not read the "
                "file's own geometry. That is a gap on our side, not "
                "something for you to install — the measurements taken from "
                "the converted mesh are still below."
            )
        return _exact_unavailable(
            "No CAD kernel on this machine, so nothing could read the file's "
            f"own geometry. {INSTALL_COMMAND} installs one."
        )

    import hashlib

    try:
        # Content plus a payload version, and deliberately NOT the
        # tessellation constants the mesh cache folds in: these numbers come
        # from the analytic surfaces, so no meshing setting can change them.
        #
        # The version is the lesson from the entry next door, which was keyed
        # on content alone and could not tell an old-format record from a
        # current one — so every mesh cached before the conversion record
        # existed answered "not from CAD" forever.  Today a missing field
        # would fall through to a fresh read anyway (every field below is
        # required, so a short payload reads as no measurement); the version
        # is what keeps that true the day one becomes optional.
        key = hashlib.sha256(
            Path(path).read_bytes() + _EXACT_PAYLOAD_VERSION.encode()
        ).hexdigest()
    except OSError as exc:
        return _exact_unavailable(f"Could not read that file: {exc}")

    cache_dir = Path(tempfile.gettempdir()) / "kiln_step_cache"
    cached = cache_dir / f"{key}.exact.json"
    if cached.is_file():
        hit = _exact_from_payload(_read_json_or_none(cached))
        if hit is not None:
            return hit

    script = _OCP_EXACT_SCRIPT_TEMPLATE.format(step_path=str(Path(path).resolve()))
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write(script)
        script_path = f.name
    try:
        result = subprocess.run(
            [sys.executable, script_path],
            capture_output=True,
            text=True,
            timeout=EXACT_READ_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return _exact_unavailable(
            f"Reading this file's geometry took longer than "
            f"{EXACT_READ_TIMEOUT_S}s and was stopped. Very large assemblies "
            "can do this — try a single part."
        )
    except OSError as exc:  # noqa: BLE001 — a spawn failure is not fatal here
        return _exact_unavailable(f"Could not start the CAD kernel: {exc}")
    finally:
        with contextlib.suppress(OSError):
            os.unlink(script_path)

    try:
        data = _parse_kiln_exact_result(result)
    except StepImportError as exc:
        return _exact_unavailable(str(exc))

    exact = _exact_from_payload(data)
    if exact is None:
        return _exact_unavailable(
            "The CAD kernel returned a measurement Kiln could not read."
        )

    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        tmp = cached.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, cached)
    except OSError:
        pass  # a full or read-only temp dir must never fail the read

    return exact


def _read_json_or_none(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _parse_kiln_exact_result(
    result: subprocess.CompletedProcess[str],
) -> dict[str, Any]:
    """The KILN_RESULT line from an exact read, or a reason it is absent.

    Separate from :func:`_parse_kiln_result`, which requires the conversion
    keys (``outputs``, ``body_count``) this payload does not have.  Raises
    :class:`StepImportError` with wording aimed at the person who handed us
    the file, since the likely cause is the file.
    """
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()[:300]
        raise StepImportError(
            f"The CAD kernel could not read this file: {stderr or 'no detail given'}"
        )
    for line in (result.stdout or "").splitlines():
        if line.startswith("KILN_RESULT:"):
            try:
                data = json.loads(line[len("KILN_RESULT:"):])
            except json.JSONDecodeError as exc:
                raise StepImportError(
                    f"The CAD kernel produced an unreadable measurement: {exc}"
                ) from exc
            if not isinstance(data, dict):
                raise StepImportError(
                    "The CAD kernel produced an unreadable measurement."
                )
            return data
    raise StepImportError("The CAD kernel produced no measurement.")


def _exact_from_payload(data: dict[str, Any] | None) -> ExactGeometry | None:
    """Rebuild an :class:`ExactGeometry` from a child's (or cache's) payload.

    ``None`` for anything malformed — a partial record must never be dressed
    up as a measurement, because the whole claim these numbers make is that
    they are exact.  One reader for both the live child and the cache, so a
    stale cache format degrades the same way a broken child does.
    """
    if not isinstance(data, dict):
        return None
    try:
        raw_topology = data.get("topology")
        topology = (
            SourceTopology(
                solids=int(raw_topology["solids"]),
                shells=int(raw_topology["shells"]),
                faces=int(raw_topology["faces"]),
            )
            if isinstance(raw_topology, dict)
            else None
        )
        lo = tuple(float(v) for v in data["bbox_min_mm"])
        hi = tuple(float(v) for v in data["bbox_max_mm"])
        if len(lo) != 3 or len(hi) != 3:
            return None
        return ExactGeometry(
            available=True,
            volume_mm3=float(data["volume_mm3"]),
            surface_area_mm2=float(data["surface_area_mm2"]),
            bbox_min_mm=lo,  # type: ignore[arg-type]
            bbox_max_mm=hi,  # type: ignore[arg-type]
            # strict=True: hi and lo are both 3-axis bounds, so a length
            # mismatch is a corrupt record, not a shorter box to be quietly
            # truncated into a 2-axis size.
            size_mm=tuple(h - low for h, low in zip(hi, lo, strict=True)),  # type: ignore[arg-type]
            is_valid=bool(data["is_valid"]),
            topology=topology,
        )
    except (KeyError, TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Main conversion function
# ---------------------------------------------------------------------------


def surface_model_note(topology: SourceTopology | None) -> str | None:
    """The one sentence Kiln says about a CAD file that declared no solid.

    ``None`` when there is nothing to say — a real solid, or a backend that
    could not look.  Lives here, alone, because the note is the part that
    would otherwise be retyped per door and drift into the stronger claim
    each time.

    It deliberately does NOT say "not printable".  That claim is false on
    measured fixtures (see :class:`SourceTopology`), and a false refusal
    told to an engineer whose file works is worse than saying nothing: it
    costs them a re-export they did not need and the trust to believe the
    next warning.  What is always true is what it reports — the file
    declared surfaces and no solid — and where the fix lives if that was an
    accident.
    """
    if topology is None or not topology.is_surface_model:
        return None
    return (
        f"This CAD file declares no solid body — {topology.faces} "
        f"surface{'s' if topology.faces != 1 else ''}, no enclosed volume. "
        "Kiln converted it as given, and the result can still be a printable "
        "mesh, so this is a note rather than a problem. If it was meant to be "
        "a solid part, re-export it as a solid from your CAD tool; if you "
        "meant to send a surface to thicken or profile, carry on."
    )


def is_step_file(path: str) -> bool:
    """True if this path names a STEP file, by extension."""
    return Path(path).suffix.lower() in _VALID_EXTENSIONS


def looks_like_step(path: str) -> bool:
    """True if this file's CONTENT is STEP, whatever it happens to be named.

    The companion to :func:`is_step_file`, and the one a mesh parser wants.
    A parser is holding an open file, not a user's intent: by the time it is
    deciding how to read the bytes, the extension has already been believed
    once, and if it was wrong the parser is about to describe the file to
    somebody in terms of a format it does not have.

    That is not hypothetical.  Handed a real 19 KB STEP file, Kiln's
    binary-STL readers took bytes 80-84 — the ASCII ``'Ope`` of "Open CASCADE
    Model" — as a triangle count of 1,701,859,111, and reported the file as a
    truncated STL missing 85 GB.  That tells an engineer their CAD export is
    CORRUPT.  It is not; it is simply not an STL.  The point of this function
    is that a parser can say what it is actually looking at instead of
    guessing in its own vocabulary.

    Never raises: a file that cannot be opened or read is not a STEP file as
    far as the caller is concerned, and a parser mid-diagnosis is the worst
    possible place to introduce a new exception.
    """
    try:
        with open(path, "rb") as fh:
            return _STEP_MAGIC in fh.read(_STEP_MAGIC_WINDOW)
    except OSError:
        return False


def ensure_mesh_path(
    path: str,
    *,
    output_dir: str | None = None,
    with_record: bool = False,
) -> tuple[str, str | None] | tuple[str, str | None, MeshConversion | None]:
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

    Args:
        with_record: return the structured :class:`MeshConversion` as a third
            element.  Opt-in, and defaulting to off, because this returns a
            2-tuple in every released version — widening it unconditionally
            would break the unpacking in every caller that already exists,
            inside Kiln and outside it, to hand them something none of them
            asked for.

    Returns:
        ``(mesh_path, note)``, or ``(mesh_path, note, conversion)`` when
        ``with_record``.  ``note`` is a human-readable line for a report when
        a conversion happened, else ``None``.

        ``conversion`` is how the mesh was made — the fact a prose note
        cannot carry, which is why half the callers of this function threw
        the note away.  ``None`` when nothing was converted (an ordinary mesh
        passed straight through), and also for a cache entry written before
        the record existed, which is honest: that mesh really is one whose
        origin was never written down.

    Raises:
        NoBackendError: It IS a STEP file but nothing can convert it.  The
            error carries :attr:`~NoBackendError.remedy`, so a caller can
            tell the user what to do instead of guessing.
    """
    if not is_step_file(path):
        return (path, None, None) if with_record else (path, None)

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

    _freecad_argv = _find_freecad_cmd()
    backend_fingerprint = (
        # Joined to a string so the slot keeps the shape it has always had:
        # a machine with no FreeCAD still fingerprints to "" and keeps every
        # entry it holds, exactly as before.  A machine where FreeCAD is now
        # FOUND does change key — which is the correct outcome, not a cost.
        # Its cached meshes were cut by a different backend (measured on one
        # sphere: 150,970 triangles from the kernel against 8,000 from
        # FreeCAD), so serving them under the new backend would answer with
        # the old converter's geometry forever.
        shlex.join(_freecad_argv) if _freecad_argv else "",
        # The gmsh slot carries its density bound, not merely "is it here".
        # An entry written before gmsh was bounded holds a mesh cut at
        # whatever that machine's gmsh derived from the bounding box, and a
        # key that cannot tell the two apart would keep serving it forever —
        # the fix would land and no repeat caller would ever see it.
        # Folding the bound in HERE rather than into the tuple below is what
        # keeps the invalidation aimed: a machine with no gmsh on PATH
        # fingerprints to "" exactly as before and keeps every entry it has.
        f"gmsh@{_GMSH_CURVATURE_ELEMENTS}" if _find_gmsh_cmd() else "",
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
    # The record rides WITH the cached mesh, in a sidecar beside it.  Without
    # one, a cache hit would return no record at all — so the same file would
    # report how it was made the first time and shrug every time after, which
    # is worse than never reporting it: a caller cannot tell "not from CAD"
    # from "converted, but this run happened to be a hit."
    cached_record = cached.with_suffix(".json")

    if cached.is_file():  # noqa: SIM102 — see below; each `if` has its own reason
        # The miss path creates output_dir on the way through
        # (_validate_output_dir); the hit path never did, so a caller naming
        # a directory that does not exist yet got a mesh on the first call
        # and a FileNotFoundError on every one after — the same call
        # succeeding or failing purely on whether something else had already
        # converted those bytes.
        # A hit is only a hit for what the CALLER asked for.  Entries cached
        # before this sidecar existed carry a mesh and no record, and the key
        # is content + tessellation settings with no format version — so they
        # are indistinguishable from current entries and would answer "not
        # from CAD" for those files forever.  Measured on one developer
        # machine the morning after the record shipped: 135 cached meshes, 5
        # sidecars.  The hosted box hides this (its temp dir goes at every
        # redeploy); a laptop and a CI runner keep the cache for months, so
        # the people most likely to see it are the ones converting the same
        # parts over and over.
        #
        # Falling through re-converts that file ONCE and writes the sidecar,
        # after which it is a hit again.  Callers that never wanted a record
        # keep the fast path untouched.
        if not with_record or cached_record.is_file():
            Path(output_dir).mkdir(parents=True, exist_ok=True)
            out = str(Path(output_dir) / "merged.stl")
            _shutil.copyfile(cached, out)
            note = f"Converted from STEP ({Path(path).name}) to mesh — cached, 0.0s."
            if not with_record:
                return out, note
            return out, note, _read_cached_conversion(cached_record)

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
        if result.conversion is not None:
            # Written AFTER the mesh, and atomically.  The mesh is the thing
            # callers need; a sidecar that failed to write costs a later hit
            # its record and nothing else, whereas a mesh published without
            # its sidecar being durable would be the same situation anyway.
            rtmp = cached_record.with_suffix(f".{os.getpid()}.rtmp")
            rtmp.write_text(
                json.dumps(asdict(result.conversion)), encoding="utf-8"
            )
            os.replace(rtmp, cached_record)
    except OSError:
        pass  # a full or read-only temp dir must never fail the conversion

    if not with_record:
        return result.output_path, note
    return result.output_path, note, result.conversion


def _read_cached_conversion(sidecar: Path) -> MeshConversion | None:
    """Rebuild a conversion record from a cache sidecar, or admit there is none.

    Absent for entries written before the record existed, and unreadable if
    the temp dir was cleaned mid-flight.  Both answer ``None`` rather than a
    partial record: a mesh whose origin was never written down is exactly
    what ``None`` means here, and inventing a plausible one from the cache
    key would be reporting the backends that are INSTALLED as the backend
    that ran.
    """
    try:
        data = json.loads(sidecar.read_text(encoding="utf-8"))
        bound = data["bound"]
        # Sidecars written before the topology existed carry no "source", and
        # the cache key is content + tessellation settings with no format
        # version — so they are indistinguishable from current ones and must
        # read as "we did not look" rather than as zero solids.
        src = data.get("source")
        return MeshConversion(
            backend=data["backend"],
            bound=TessellationBound(
                kind=bound["kind"],
                linear=bound.get("linear"),
                angular=bound.get("angular"),
                elements_per_circle=bound.get("elements_per_circle"),
                reason=bound.get("reason"),
            ),
            source=(
                SourceTopology(
                    solids=src["solids"],
                    shells=src["shells"],
                    faces=src["faces"],
                )
                if isinstance(src, dict)
                else None
            ),
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None


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
    # Set where a backend SUCCEEDS, never where one is merely attempted: the
    # fall-through means the backend that ran is not the one the priority
    # order names on a machine where an earlier one is installed but broken.
    conversion: MeshConversion | None = None
    t0 = time.monotonic()

    # Try backends in priority order.
    freecad_cmd = _find_freecad_cmd()
    if freecad_cmd is not None:
        logger.info("Converting STEP via FreeCAD (%s)", freecad_cmd)
        try:
            outputs, body_count = _convert_via_freecad(
                validated_path, out_dir, merge_bodies, freecad_cmd
            )
            conversion = MeshConversion(backend="freecad", bound=_FREECAD_BOUND)
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
                conversion = MeshConversion(backend="gmsh", bound=_GMSH_BOUND)
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
                outputs, body_count, topology = _convert_via_ocp(
                    validated_path, out_dir, merge_bodies
                )
                conversion = MeshConversion(
                    backend="occt", bound=_KERNEL_BOUND, source=topology
                )
            elif _cadquery_available():
                logger.info("Converting STEP via CadQuery")
                outputs, body_count, topology = _convert_via_cadquery(
                    validated_path, out_dir, merge_bodies
                )
                conversion = MeshConversion(
                    backend="cadquery", bound=_KERNEL_BOUND, source=topology
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

    # Non-blocking by design: a surface model still converted, and the mesh
    # it produced may be perfectly printable.  This is a note attached to a
    # success, never a refusal — see surface_model_note.
    note = surface_model_note(conversion.source if conversion else None)
    if note is not None:
        warnings.append(note)

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
        conversion=conversion,
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

    from kiln.threemf_parser import unique_object_names

    t0 = time.monotonic()
    data = _convert_via_ocp_xcaf(validated_path, out_dir)
    # Named once, above the branch, so both exits report the same thing: a
    # caller reading :attr:`StepImportResult.part_names` should never have to
    # know which output format produced them.
    names = unique_object_names(data["names"])
    # Same, for the census and the note it earns: this is the path
    # import_step_file actually takes, so a surface model has to be visible
    # here or the feature is invisible at the door users knock on.
    conversion = MeshConversion(
        backend="occt-xcaf",
        bound=_KERNEL_BOUND,
        source=_topology_from_result(data),
    )
    note = surface_model_note(conversion.source)
    surface_warnings = [note] if note is not None else []
    parts = [
        {"stl_path": p, "name": n, "color": c}
        for p, n, c in zip(data["outputs"], names, data["colors"], strict=True)
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
            part_names=names,
            part_colors=data["colors"],
            warnings=surface_warnings,
            conversion=conversion,
        )

    out_3mf = str(out_dir / f"{validated_path.stem}.3mf")
    # These names are already unique; the writer re-establishes that for
    # callers who did not, and returns what it wrote.
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
        part_names=names,
        part_colors=data["colors"],
        warnings=surface_warnings,
        conversion=conversion,
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
        ``description``, ``author``, ``originating_system``.

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
        "originating_system": None,
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

    # FILE_NAME's originating_system (6th field) — the stamp a viewer reads
    # to answer "who made this file?".  Fields are strings or paren lists;
    # a plain split can't see that, so walk the top-level commas.
    fn_match = re.search(r"FILE_NAME\s*\((.*?)\)\s*;", text, re.DOTALL)
    if fn_match:
        fields: list[str] = []
        depth = 0
        in_string = False
        current = ""
        for ch in fn_match.group(1):
            if in_string:
                current += ch
                if ch == "'":
                    in_string = False
                continue
            if ch == "'":
                in_string = True
                current += ch
            elif ch == "(":
                depth += 1
                current += ch
            elif ch == ")":
                depth -= 1
                current += ch
            elif ch == "," and depth == 0:
                fields.append(current.strip())
                current = ""
            else:
                current += ch
        fields.append(current.strip())
        if len(fields) >= 6 and fields[5].startswith("'"):
            metadata["originating_system"] = fields[5].strip("'")

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


def resolve_mesh_input(
    path: str,
    *,
    output_dir: str | None = None,
) -> tuple[str, MeshConversion | None, dict[str, Any] | None]:
    """Hand this any model path; get back something a mesh tool can read.

    :func:`ensure_mesh_path` is the door.  This is the door plus the three
    ways it can fail, worded once — because the wording is the part that was
    being retyped.  Every tool that had grown CAD support had also grown its
    own copy of the same try/except: a ``NoBackendError`` branch that
    remembers to attach ``exc.remedy``, a catch-all for a corrupt file, and
    (in the tools that thought of it) an ``ImportError`` branch for a kiln3d
    too old to convert at all.  Fifteen lines each, and the tools that got it
    slightly different are the ones that told a user something unhelpful.

    A tool adopts CAD support in three lines::

        mesh_path, conversion, refusal = resolve_mesh_input(file_path)
        if refusal:
            return refusal

    Anything that is already a mesh passes straight through with no
    conversion and no refusal, so this is safe to call unconditionally — a
    caller never needs to ask whether it was handed CAD.

    :returns: ``(mesh_path, conversion, refusal)``.  ``conversion`` records
        how the CAD became triangles, for a caller that wants to say what its
        answer rests on; it is ``None`` for an ordinary mesh.  ``refusal`` is
        a ready-to-return error envelope, and ``None`` when all is well.
    """
    # Lazily imported: kiln.server imports this module, so taking its error
    # builder at module scope would be a cycle.  By the time any tool calls
    # this, the server is loaded.  Reimplementing the envelope here instead
    # would be a hand-copy of `_error_dict`'s retryable-code logic, which is
    # exactly how two refusals from one product start disagreeing.
    from kiln.server import _error_dict

    try:
        mesh_path, _note, conversion = ensure_mesh_path(
            path, output_dir=output_dir, with_record=True
        )
    except NoBackendError as exc:
        refusal = _error_dict(str(exc), code="NO_BACKEND")
        # Structured remedy: tells the agent whether the user can fix this
        # themselves (install a converter) or is on a hosted server.
        refusal["remedy"] = exc.remedy
        return path, None, refusal
    except Exception as exc:  # noqa: BLE001 — a bad CAD file is user input
        return path, None, _error_dict(
            f"Could not read that CAD file: {exc}",
            code="STEP_CONVERSION_FAILED",
        )
    return mesh_path, conversion, None
