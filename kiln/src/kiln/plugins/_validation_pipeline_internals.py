"""Internal helpers for ``validation_pipeline_tools``.

Constants, dataclasses, helper functions, and the eleven ``_step_*``
functions that implement the validation pipeline.  Split out from
``validation_pipeline_tools.py`` so that the public plugin file stays
focused on the MCP tool surface (the ``register`` method and its
``@mcp.tool()``-decorated inner functions).

This module has no ``plugin`` attribute, so ``plugin_loader`` imports
it but does not register anything — keeps tool discovery clean.
"""

from __future__ import annotations

import logging
import os
import re
import struct
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NamedTuple

from kiln.support_assessment import MATERIAL_ALIASES as _MATERIAL_ALIASES

_logger = logging.getLogger(__name__)

_SUPPORTED_FORMATS = {".stl", ".3mf", ".obj", ".step", ".stp", ".glb"}


# ---------------------------------------------------------------------------
# Internal dataclasses
# ---------------------------------------------------------------------------


@dataclass
class _CheckResult:
    """Result of a single validation check."""

    name: str
    passed: bool
    details: str
    severity: str = "info"  # "info", "warning", "error"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "details": self.details,
            "severity": self.severity,
        }


@dataclass
class _PipelineReport:
    """Aggregate report from the validation pipeline."""

    status: str = "pass"  # "pass", "fail", "pass_with_warnings"
    input_path: str = ""
    repaired: bool = False
    repaired_path: str | None = None
    cleanup_hint: str | None = None
    checks: list[_CheckResult] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)
    ready_to_print: bool = True
    model_info: dict[str, Any] = field(default_factory=dict)
    printability_score: int = 100
    score_breakdown: list[str] = field(default_factory=list)

    summary: str = ""
    validated_path: str = ""
    next_action: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "status": self.status,
            "input_path": self.input_path,
            "repaired": self.repaired,
            "repaired_path": self.repaired_path,
            "checks": [c.to_dict() for c in self.checks],
            "recommendations": self.recommendations,
            "ready_to_print": self.ready_to_print,
            "model_info": self.model_info,
            "printability_score": self.printability_score,
            "score_breakdown": self.score_breakdown,
            "summary": self.summary,
            "validated_path": self.validated_path,
            "next_action": self.next_action,
        }
        if self.cleanup_hint is not None:
            d["cleanup_hint"] = self.cleanup_hint
        return d


# ---------------------------------------------------------------------------
# Inline STL analysis fallback (no external deps)
# ---------------------------------------------------------------------------

_STL_HEADER_SIZE = 80
_STL_TRIANGLE_SIZE = 50

# ---------------------------------------------------------------------------
# Scoring constants
# ---------------------------------------------------------------------------

_MIN_PASS_SCORE = 60  # minimum printability score to pass
_SCORE_PENALTY_ERROR = 25  # deduction for error-severity check failure
_SCORE_PENALTY_WARNING = 10  # deduction for warning-severity check failure
_SCORE_PENALTY_SKIP = 5  # deduction for skipped check (passed=True, severity="warning")
_SCORE_PENALTY_REPAIR = 15  # deduction when mesh required repair

# ---------------------------------------------------------------------------
# Material / cost estimation constants
# ---------------------------------------------------------------------------

_PLA_DENSITY_G_PER_CM3 = 1.24  # PLA material density in g/cm³
_DEFAULT_INFILL_FACTOR = 0.3  # approximate infill ratio for volume estimation
_MATERIAL_COST_PER_GRAM = 0.02  # $20/kg PLA → $0.02/g
_ABS_WARP_THRESHOLD_MM = 100.0  # bed footprint threshold for ABS/ASA warping warning


_REPR_PATTERN = re.compile(r"\b\w+Analysis\(|\bdict\(")


def _sanitize_summary_detail(detail: str) -> str:
    """Return a clean, human-readable version of a check detail string.

    Strips Python repr garbage (``SomethingAnalysis(``, ``dict(``), truncates
    to the first sentence if the result is over 80 chars, and hard-caps at
    80 chars with an ellipsis so the overall summary stays under 200 chars.
    """
    # Drop anything that looks like a Python repr constructor
    if _REPR_PATTERN.search(detail):
        # Keep only the part before the first repr token
        detail = _REPR_PATTERN.split(detail)[0].rstrip(" ,(")

    detail = detail.strip()

    if len(detail) <= 80:
        return detail

    # Try to truncate at the first sentence boundary
    for sep in (".", "!", "?"):
        idx = detail.find(sep)
        if 0 < idx <= 80:
            return detail[: idx + 1]

    return detail[:80] + "..."


def _inline_stl_analysis(file_path: str) -> dict[str, Any]:
    """Extract triangle count, bounding box, and dimensions from an STL.

    Delegates to the canonical STL parser in
    :mod:`kiln.generation.validation` when available; falls back to a
    minimal inline binary parser otherwise.
    """
    path = Path(file_path)
    if not path.is_file():
        return {"error": f"File not found: {file_path}"}

    try:
        from kiln.generation.validation import (
            _compute_bounding_box,
            _parse_stl,
        )

        errors: list[str] = []
        triangles, vertices = _parse_stl(path, errors)
        if errors:
            return {"error": "; ".join(errors)}

        result: dict[str, Any] = {"triangle_count": len(triangles)}

        bbox = _compute_bounding_box(vertices)
        if bbox:
            dims = bbox.pop("dimensions_mm")
            result["bounding_box"] = bbox
            result["dimensions_mm"] = dims
            vol = dims["x"] * dims["y"] * dims["z"]
            result["bounding_box_volume_cm3"] = round(vol / 1000.0, 2)

        return result
    except (ImportError, ModuleNotFoundError):
        return _inline_stl_binary_fallback(path)


def _inline_stl_binary_fallback(path: Path) -> dict[str, Any]:
    """Minimal binary STL parser — no external dependencies."""
    import struct as _struct

    data = path.read_bytes()

    # ASCII STL detection — only return triangle count (no bounding box)
    if data[:5] == b"solid" and b"facet" in data[:1000]:
        count = data.count(b"endfacet")
        if count > 0:
            return {"triangle_count": count}
        return {"error": "Could not parse ASCII STL"}

    # Binary STL: 80-byte header + 4-byte count + 50 bytes per triangle
    if len(data) < 84:
        return {"error": f"File too small for binary STL: {len(data)} bytes"}

    tri_count = _struct.unpack_from("<I", data, 80)[0]
    expected_size = 84 + tri_count * 50
    if len(data) < expected_size:
        return {"error": f"Truncated STL: expected {expected_size} bytes, got {len(data)}"}

    result: dict[str, Any] = {"triangle_count": tri_count}

    if tri_count > 0:
        x_min = y_min = z_min = float("inf")
        x_max = y_max = z_max = float("-inf")

        for i in range(tri_count):
            offset = 84 + i * 50 + 12  # skip normal vector
            for _ in range(3):
                x, y, z = _struct.unpack_from("<fff", data, offset)
                x_min, x_max = min(x_min, x), max(x_max, x)
                y_min, y_max = min(y_min, y), max(y_max, y)
                z_min, z_max = min(z_min, z), max(z_max, z)
                offset += 12

        dims = {
            "x": round(x_max - x_min, 2),
            "y": round(y_max - y_min, 2),
            "z": round(z_max - z_min, 2),
        }
        result["bounding_box"] = {
            "x_min": round(x_min, 2), "x_max": round(x_max, 2),
            "y_min": round(y_min, 2), "y_max": round(y_max, 2),
            "z_min": round(z_min, 2), "z_max": round(z_max, 2),
        }
        result["dimensions_mm"] = dims
        vol = dims["x"] * dims["y"] * dims["z"]
        result["bounding_box_volume_cm3"] = round(vol / 1000.0, 2)

    return result


# ---------------------------------------------------------------------------
# Build volume lookup
# ---------------------------------------------------------------------------


class _BuildVolume(NamedTuple):
    """A resolved build volume and where its numbers came from.

    The provenance has to be resolved HERE because only this function
    knows which of the two sources answered.  A caller that guessed
    would be attributing a curated catalogue number to the machine's
    owner, or the reverse — a false claim is worse than no claim, which
    is why the bed-fit verdict said nothing at all until now.
    """

    dims: tuple[float, float, float]
    #: Ready-to-append parenthetical, or ``""`` when the number is Kiln's.
    provenance: str


def _resolve_build_volume(printer_id: str) -> _BuildVolume | None:
    """Resolve build volume via printer intelligence, then safety profiles.

    Order is unchanged; what is new is that the answer says which source
    produced it.

    The intelligence catalogue is entirely Kiln's own — a narrow dict of
    variant shorthands plus ``printer_intelligence.json`` — so a hit
    there carries no provenance note.  A safety profile may hold numbers
    the machine's owner typed, so a hit there is attributed with the
    same helper the G-code refusals use.

    Worth knowing for ``build_volume`` specifically: unlike the
    temperature and flow ceilings, it is NOT clamped against curated
    data, because a bed is not a safety limit that only tightens.  So an
    owner-declared volume can be LARGER than the real machine, and the
    dangerous direction is a model that "fits" a bed only on paper.
    That is why the caller attributes the passing verdict too, not just
    the refusal.
    """
    try:
        from kiln.printers.bed_fit import get_build_volume

        volume = get_build_volume(printer_id)
        if volume is not None:
            return _BuildVolume(volume, "")
    except Exception:
        _logger.debug(
            "Could not resolve printer-intelligence build volume for %s",
            printer_id,
            exc_info=True,
        )
    try:
        from kiln.safety_profiles import get_profile, limit_provenance_suffix

        profile = get_profile(printer_id)
        if profile and profile.build_volume and len(profile.build_volume) >= 3:
            return _BuildVolume(
                (
                    float(profile.build_volume[0]),
                    float(profile.build_volume[1]),
                    float(profile.build_volume[2]),
                ),
                limit_provenance_suffix(profile, "build_volume"),
            )
    except Exception:
        _logger.debug("Could not resolve build volume for %s", printer_id, exc_info=True)
    return None


def _get_build_volume_for_printer(printer_id: str) -> tuple[float, float, float] | None:
    """Resolve build volume from printer_id via intelligence, then profiles.

    Dimensions only, for callers that do not report a verdict about
    them.  One resolution path, so this can never disagree with
    :func:`_resolve_build_volume` about which source wins.
    """
    resolved = _resolve_build_volume(printer_id)
    return resolved.dims if resolved is not None else None


# ---------------------------------------------------------------------------
# Printability score computation
# ---------------------------------------------------------------------------


def _compute_printability_score(
    checks: list[_CheckResult],
    *,
    repaired: bool,
) -> tuple[int, list[str]]:
    """Compute a 0-100 printability score from the pipeline check results.

    Scoring formula:
        - Start at 100
        - Each failed check with severity "error":  -25
        - Each failed check with severity "warning": -10
        - Each skipped/degraded check (passed=True, severity="warning"): -5
        - Repair needed (mesh was non-manifold):    -15
        - Clamp result to [0, 100]

    Checks with ``passed=True`` and ``severity="warning"`` represent steps
    that were skipped (e.g. analysis module unavailable) or degraded (e.g.
    fell back to a less accurate method).  The 5-point deduction reflects
    reduced confidence in the overall assessment when a check could not
    run at full fidelity.

    :returns: (score, breakdown) where breakdown is a list of human-readable
        deduction strings.
    """
    score = 100
    breakdown: list[str] = []

    for c in checks:
        if not c.passed and c.severity == "error":
            score -= _SCORE_PENALTY_ERROR
            breakdown.append(f"-{_SCORE_PENALTY_ERROR}: failed check '{c.name}' (error)")
        elif not c.passed and c.severity == "warning":
            score -= _SCORE_PENALTY_WARNING
            breakdown.append(f"-{_SCORE_PENALTY_WARNING}: failed check '{c.name}' (warning)")
        elif c.passed and c.severity == "warning":
            score -= _SCORE_PENALTY_SKIP
            breakdown.append(f"-{_SCORE_PENALTY_SKIP}: warning on check '{c.name}'")

    if repaired:
        score -= _SCORE_PENALTY_REPAIR
        breakdown.append(f"-{_SCORE_PENALTY_REPAIR}: mesh required repair")

    score = max(0, min(100, score))
    return score, breakdown


# ---------------------------------------------------------------------------
# Material-specific checks
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Auto-scale constants and helpers
# ---------------------------------------------------------------------------

#: A unit mix-up multiplies every coordinate by a FIXED conversion, so the
#: repair is one of a short list of real factors — never a free choice of
#: size.  Scaling a model to a "reasonable" target height instead is what
#: this replaced: it lands near the right answer only for the one input
#: whose true size happened to be that height, and is wrong by construction
#: for every other, including the ones it correctly detects as mis-exported.
_UNIT_CONVERSIONS: tuple[tuple[str, float], ...] = (
    ("meters", 1000.0),
    ("centimeters", 10.0),
    ("inches", 25.4),
    ("microns", 0.001),
)

#: The band a real printable object's largest dimension falls in.  The floor
#: is one FDM feature — under a millimetre a 0.4mm nozzle has no object to
#: lay down.  The ceiling is the largest build volume in Kiln's own catalog
#: (Elegoo OrangeStorm Giga, 1000mm), so a part that fits SOME machine Kiln
#: knows about is never mistaken for a unit error.
_PRINTABLE_MIN_MM = 1.0
_PRINTABLE_MAX_MM = 1000.0

#: Below this, a model is still printable and is never touched, but the user
#: is told what other units would have made it.  A COURTESY threshold, not a
#: correction one: nothing is ever rescaled because of it.
_UNIT_NOTICE_BELOW_MM = 10.0

_SIMPLIFY_THRESHOLD = 100_000  # triangle count above which simplification is recommended


def _inline_stl_scale(stl_path: str, scale_factor: float) -> str:
    """Scale a binary STL by multiplying all vertex coordinates by *scale_factor*.

    Reads the binary STL, multiplies each vertex coordinate, and writes a new
    temp file.  Returns the path to the scaled file.

    Only handles binary STL format — ASCII STL is not supported for scaling.
    """
    src = Path(stl_path)
    with open(src, "rb") as fh:
        header = fh.read(_STL_HEADER_SIZE)
        if header[:5] == b"solid" and b"\n" in header:
            raise ValueError("ASCII STL not supported for inline scaling")
        count_bytes = fh.read(4)
        tri_count = struct.unpack("<I", count_bytes)[0]

        triangles_data = bytearray()
        for _ in range(tri_count):
            chunk = fh.read(_STL_TRIANGLE_SIZE)
            if len(chunk) < _STL_TRIANGLE_SIZE:
                break
            # Normal: 3 floats (unchanged), Vertices: 9 floats (scaled), attr: 2 bytes
            normal = chunk[:12]
            verts = struct.unpack_from("<9f", chunk, 12)
            attr = chunk[48:50]

            scaled_verts = struct.pack(
                "<9f", *(v * scale_factor for v in verts)
            )
            triangles_data += normal + scaled_verts + attr

    fd, out_path = tempfile.mkstemp(suffix=".stl", prefix="kiln_autoscale_")
    os.close(fd)
    with open(out_path, "wb") as fh:
        fh.write(header)
        fh.write(count_bytes)
        fh.write(bytes(triangles_data))

    return out_path


def _scaled_copy_path(stl_path: str) -> str:
    """A fresh temp path for a rescaled copy — same convention as the
    inline scaler, so both writers keep the never-mutate-the-original
    contract with one spelling of the output location."""
    fd, out_path = tempfile.mkstemp(suffix=Path(stl_path).suffix or ".stl", prefix="kiln_autoscale_")
    os.close(fd)
    return out_path


@dataclass(frozen=True)
class _UnitVerdict:
    """What a model's measured size says about the units it was written in.

    Six outcomes, and only ONE of them changes the user's geometry:

    ``plausible``
        The size already reads as a printable object.  Nothing to do — this
        is the answer for the overwhelming majority of files, including the
        small-but-real parts (a 6mm pin, an 8mm gear) that the previous
        target-height rule silently inflated.
    ``small``
        Printable, left alone, and near the bottom of the range, so the
        other readings are offered in case the user expected one of them.
        It replaces a warning that told a 2mm part it was "likely exported
        in meters" — which meters cannot explain, since that would make it
        2000mm, larger than any printer in the catalog.  Naming the one
        unit arithmetically ruled out is worse than saying nothing.
    ``corrected``
        Exactly one real unit conversion turns this into a printable size,
        so it is the only explanation on offer and we apply it.
    ``ambiguous``
        Several conversions would work and nothing distinguishes them.  A
        0.5 reading is 500mm from meters, 5mm from centimeters and 12.7mm
        from inches; picking one is a guess wearing a measurement's clothes.
    ``oversize``
        Bigger than any machine in the catalog, and a microns reading would
        land it printable.  Unlike the sub-millimetre side, there are TWO
        real explanations up here: a microns export, or a model genuinely
        this big that the user means to cut up with split_mesh_to_fit —
        a workflow Kiln ships tools for.  Shrinking would act on a guess
        between them, so both readings are offered instead.
    ``unexplained``
        No conversion lands it anywhere printable, so "wrong units" is not
        the story and inventing a scale would only hide the real problem.

    The last three are reported, never acted on: a part that silently comes
    out the wrong size is worse than one the user is asked about, because
    the wrong size reaches the printer looking exactly like a right one.
    """

    status: str
    max_dim_mm: float
    unit: str = ""
    factor: float = 0.0
    candidates: tuple[tuple[str, float], ...] = ()

    @property
    def corrected(self) -> bool:
        return self.status == "corrected"

    def _readings(self) -> str:
        return ", ".join(
            f"{unit} → {self.max_dim_mm * factor:g}mm"
            for unit, factor in self.candidates
        )

    def describe(self) -> str:
        """One sentence a user can act on, naming real sizes, never a guess."""
        if self.status == "corrected":
            return (
                f"Rescaled x{self.factor:g} "
                f"({self.max_dim_mm:g}mm → {self.max_dim_mm * self.factor:g}mm) "
                f"— the file was written in {self.unit}, the only unit that "
                f"makes it a printable size."
            )
        if self.status == "small":
            return (
                f"This model is {self.max_dim_mm:g}mm at its largest — small, but a "
                f"printable size, so nothing was changed.  If you expected it "
                f"bigger, it may have been exported in {self._readings()}."
            )
        if self.status == "ambiguous":
            return (
                f"This model measures {self.max_dim_mm:g}mm at its largest, which "
                f"is not a printable size, and more than one unit would explain "
                f"it ({self._readings()}).  Nothing was rescaled — say which unit "
                f"it was exported in, or use rescale_model with the factor you want."
            )
        if self.status == "oversize":
            return (
                f"This model measures {self.max_dim_mm:g}mm at its largest — bigger "
                f"than any printer in Kiln's catalog ({_PRINTABLE_MAX_MM:g}mm).  If it "
                f"was exported in {self.unit} it is really "
                f"{self.max_dim_mm * self.factor:g}mm, and rescale_model "
                f"x{self.factor:g} fixes that in one step; if it really is this "
                f"big, split_mesh_to_fit can cut it into printable sections.  "
                f"Nothing was rescaled — both readings are real, so this one "
                f"is your call."
            )
        return (
            f"This model measures {self.max_dim_mm:g}mm at its largest, which is "
            f"not a printable size, and no unit conversion lands it in a "
            f"printable range either.  Nothing was rescaled — check the export "
            f"itself before scaling it."
        )

    def describe_unapplied(self) -> str:
        """The ``corrected`` diagnosis, worded for when no rescale was written.

        ``describe()`` says "Rescaled", which is a lie the moment the writer
        cannot run — a non-STL container, or a write failure.  The diagnosis
        still holds and the size is still unprintable, so it is restated as
        an instruction rather than a receipt.
        """
        return (
            f"This model measures {self.max_dim_mm:g}mm at its largest, which is "
            f"not a printable size — the file looks like a {self.unit} export "
            f"({self.max_dim_mm:g} → {self.max_dim_mm * self.factor:g}mm), the "
            f"only unit that explains it.  It was not rescaled here; "
            f"rescale_model x{self.factor:g} fixes it in one step."
        )


def _max_dim_mm(model_info: dict[str, Any]) -> float:
    """Largest bounding-box dimension in mm, or 0.0 when it is not known.

    ``or`` rather than ``dict.get``'s default throughout: a present-but-zero
    ``x`` must fall through to ``width_mm``, which a default never does.
    """
    dims = model_info.get("dimensions_mm") or model_info.get("bounding_box") or {}
    if not isinstance(dims, dict):
        return 0.0
    try:
        return max(
            float(dims.get("x") or dims.get("width_mm") or 0),
            float(dims.get("y") or dims.get("depth_mm") or 0),
            float(dims.get("z") or dims.get("height_mm") or 0),
        )
    except (TypeError, ValueError):
        return 0.0


def _unit_verdict(max_dim_mm: float) -> _UnitVerdict:
    """Decide what *max_dim_mm* says about the file's units.

    Pure: no file is read and no geometry is touched, so the judgement can
    be tested on numbers alone and the side effect lives at one call site.

    Deliberately no triangle-count guard.  The old rule required >1000
    triangles before it would correct anything, on the reasoning that simple
    parts are legitimately small — which was true, and was the wrong lever:
    it left a 50mm cube exported in meters (12 triangles, reads as 0.05mm)
    permanently broken while still inflating detailed small parts.  Asking
    whether a real conversion explains the size answers both, and answers
    them from the physics rather than from the mesh's complexity.
    """
    if max_dim_mm <= 0:
        return _UnitVerdict("plausible", max_dim_mm)

    candidates = tuple(
        (unit, factor)
        for unit, factor in _UNIT_CONVERSIONS
        if _PRINTABLE_MIN_MM <= max_dim_mm * factor <= _PRINTABLE_MAX_MM
    )

    if _PRINTABLE_MIN_MM <= max_dim_mm <= _PRINTABLE_MAX_MM:
        if max_dim_mm < _UNIT_NOTICE_BELOW_MM and candidates:
            return _UnitVerdict("small", max_dim_mm, candidates=candidates)
        return _UnitVerdict("plausible", max_dim_mm)

    if max_dim_mm > _PRINTABLE_MAX_MM and candidates:
        # Necessarily microns — it is the only shrinking conversion, and any
        # enlarging one pushes an oversize reading further out of the band.
        # Never auto-applied, unlike the single-candidate case below: under
        # the 1mm floor no real object exists, so an enlargement acts on the
        # only possible reading, but a model bigger than every machine can
        # genuinely be one the user means to split (split_mesh_to_fit is a
        # shipped workflow).  The old rule shrank everything over 500mm by
        # 0.001; auto-shrinking everything over 1000mm would be the same
        # mistake with a better threshold.
        unit, factor = candidates[0]
        return _UnitVerdict("oversize", max_dim_mm, unit, factor, candidates)

    if len(candidates) == 1:
        unit, factor = candidates[0]
        return _UnitVerdict("corrected", max_dim_mm, unit, factor, candidates)
    if candidates:
        return _UnitVerdict("ambiguous", max_dim_mm, candidates=candidates)
    return _UnitVerdict("unexplained", max_dim_mm)


def _auto_scale_if_needed(
    stl_path: str,
    model_info: dict[str, Any],
) -> tuple[str | None, float]:
    """Apply a unit correction to *stl_path* when exactly one explains its size.

    Thin wrapper over :func:`_unit_verdict` — the judgement is there, the
    file writing is here.

    :returns: (scaled_path, scale_factor), or (None, 0.0) when nothing was
        scaled, which includes both "already fine" and "we will not guess".
    """
    verdict = _unit_verdict(_max_dim_mm(model_info))
    if not verdict.corrected:
        return None, 0.0
    return _apply_scale(stl_path, verdict.factor)


def _apply_scale(stl_path: str, scale_factor: float) -> tuple[str | None, float]:
    """Write a rescaled copy of *stl_path* via the shared rescale engine."""
    # ``rescale_stl`` is the engine the ``rescale_model`` tool wraps —
    # imported directly because a registered tool is not an importable
    # function.  (This used to try ``from kiln.server import rescale_model``,
    # which stopped resolving when the tool moved into a plugin, so every
    # call silently landed on the binary-only inline scaler below.)  The
    # engine handles ASCII and binary STL; an explicit ``output_path`` keeps
    # the write-a-copy contract — the caller's original is never mutated.
    try:
        from kiln.generation.validation import rescale_stl

        out = _scaled_copy_path(stl_path)
        result = rescale_stl(stl_path, scale_factor=scale_factor, output_path=out)
        scaled_path = result.get("path", "")
        if scaled_path and Path(scaled_path).exists():
            return scaled_path, scale_factor
    except Exception:
        _logger.debug("rescale_stl failed, using inline STL scaler", exc_info=True)

    try:
        return _inline_stl_scale(stl_path, scale_factor), scale_factor
    except Exception:
        _logger.debug("Inline STL scaling failed", exc_info=True)
        return None, 0.0


# ---------------------------------------------------------------------------
# Material-specific checks
# ---------------------------------------------------------------------------


def _run_material_check(
    material: str,
    model_info: dict[str, Any],
) -> _CheckResult | None:
    """Return a material-specific _CheckResult (always a warning) or None if
    no concern applies.

    Checks are bounding-box heuristics — no mesh traversal required.

    :param material: Normalised lowercase material name.
    :param model_info: ``report.model_info`` dict, may contain
        ``dimensions_mm`` with keys x/y/z or width_mm/depth_mm/height_mm.
    """
    mat = material.lower().strip()
    mat = _MATERIAL_ALIASES.get(mat, mat)

    dims = model_info.get("dimensions_mm") or model_info.get("bounding_box", {})
    x = float(dims.get("x", dims.get("width_mm", 0)) or 0)
    y = float(dims.get("y", dims.get("depth_mm", 0)) or 0)
    z = float(dims.get("z", dims.get("height_mm", 0)) or 0)
    min_dim = min(d for d in (x, y, z) if d > 0) if any(d > 0 for d in (x, y, z)) else 0.0

    if mat == "pla":
        # PLA droops on steep overhangs; use z-height as a proxy for overhang
        # depth — tall, narrow models are risky
        if z > 0 and (x > 0 or y > 0):
            aspect = z / max(x, y, 1.0)
            if aspect > 2.0:
                return _CheckResult(
                    name="material_check",
                    passed=False,
                    details=(
                        f"PLA warning: tall model (aspect ratio {aspect:.1f}) may have "
                        "steep overhangs >60° — consider supports or reorientation"
                    ),
                    severity="warning",
                )
        return _CheckResult(
            name="material_check",
            passed=True,
            details="PLA: no high-risk overhang geometry detected from bounding box",
        )

    if mat == "petg":
        if min_dim > 0 and min_dim < 1.0:
            return _CheckResult(
                name="material_check",
                passed=False,
                details=(
                    f"PETG warning: thin feature detected ({min_dim:.2f} mm minimum "
                    "dimension) — PETG strings on thin features <1 mm"
                ),
                severity="warning",
            )
        return _CheckResult(
            name="material_check",
            passed=True,
            details="PETG: no thin features <1 mm detected from bounding box",
        )

    if mat == "abs":
        bed_footprint = max(x, y)  # warping is a bed-adhesion problem — height is irrelevant
        if bed_footprint > _ABS_WARP_THRESHOLD_MM:
            return _CheckResult(
                name="material_check",
                passed=False,
                details=(
                    f"ABS/ASA warning: large bed footprint detected "
                    f"({bed_footprint:.0f} mm longest bed axis) — high warping risk; "
                    "use enclosure + brim"
                ),
                severity="warning",
            )
        return _CheckResult(
            name="material_check",
            passed=True,
            details=f"ABS/ASA: bed footprint within low-warp range (<{_ABS_WARP_THRESHOLD_MM:.0f} mm)",
        )

    if mat == "tpu":
        if min_dim > 0 and min_dim < 2.0:
            return _CheckResult(
                name="material_check",
                passed=False,
                details=(
                    f"TPU warning: fine detail detected ({min_dim:.2f} mm minimum "
                    "dimension) — TPU flexes and smears features <2 mm"
                ),
                severity="warning",
            )
        return _CheckResult(
            name="material_check",
            passed=True,
            details="TPU: no fine details <2 mm detected from bounding box",
        )

    # Unknown material — skip silently
    return None


# ---------------------------------------------------------------------------
# Pipeline step helpers (extracted from validate_and_prepare)
# ---------------------------------------------------------------------------


def _step_format_check(report: _PipelineReport, input_path: str) -> str | None:
    """Step 1: format check. Returns file extension, or None on early-exit."""
    path = Path(input_path)
    if not path.exists():
        report.checks.append(_CheckResult(
            name="format",
            passed=False,
            details=f"File not found: {input_path}",
            severity="error",
        ))
        report.status = "fail"
        report.ready_to_print = False
        report.validated_path = input_path
        report.summary = f"Not ready (0/100). 1 issue: File not found: {input_path}"
        report.printability_score = 0
        report.next_action = None
        return None

    ext = path.suffix.lower()
    if ext not in _SUPPORTED_FORMATS:
        report.checks.append(_CheckResult(
            name="format",
            passed=False,
            details=f"Unsupported format: {ext}. Supported: {', '.join(sorted(_SUPPORTED_FORMATS))}",
            severity="error",
        ))
        report.status = "fail"
        report.ready_to_print = False
        report.validated_path = input_path
        report.summary = f"Not ready (0/100). 1 issue: Unsupported format: {ext}"
        report.printability_score = 0
        report.next_action = None
        return None

    file_size = path.stat().st_size
    report.checks.append(_CheckResult(
        name="format",
        passed=True,
        details=f"{ext.upper().lstrip('.')} file, {file_size:,} bytes",
    ))
    return ext


def _step_mesh_analysis(
    report: _PipelineReport, input_path: str, ext: str,
) -> dict[str, Any]:
    """Step 2: mesh analysis. Returns mesh_info dict."""
    mesh_info: dict[str, Any] = {}
    try:
        from kiln.generation.validation import analyze_mesh

        analysis = analyze_mesh(input_path)
        mesh_info = analysis.to_dict()
        tri_count = mesh_info.get("triangle_count", 0)
        dims = mesh_info.get("dimensions_mm") or {}
        vol_mm3 = mesh_info.get("volume_mm3", 0)
        vol_cm3 = round(vol_mm3 / 1000.0, 2) if vol_mm3 else 0

        report.model_info["triangles"] = tri_count
        if dims:
            report.model_info["bounding_box"] = dims
            report.model_info["dimensions_mm"] = dims
        if vol_cm3:
            report.model_info["bounding_box_volume_cm3"] = vol_cm3

        details = f"{tri_count:,} triangles"
        if vol_cm3:
            details += f", {vol_cm3:.1f} cm\u00b3"
        if dims:
            w = dims.get("width_mm", 0)
            d = dims.get("depth_mm", 0)
            h = dims.get("height_mm", 0)
            if w and d and h:
                details += f", {w:.1f} x {d:.1f} x {h:.1f} mm"

        report.checks.append(_CheckResult(
            name="mesh_geometry",
            passed=tri_count > 0,
            details=details,
        ))
    except ImportError:
        _logger.debug("kiln.generation.validation not available, using inline STL parser")
        if ext == ".stl":
            fallback = _inline_stl_analysis(input_path)
            if "error" not in fallback:
                tri_count = fallback.get("triangle_count", 0)
                report.model_info["triangles"] = tri_count
                if "bounding_box" in fallback:
                    report.model_info["bounding_box"] = fallback["bounding_box"]
                if "dimensions_mm" in fallback:
                    report.model_info["dimensions_mm"] = fallback["dimensions_mm"]
                if "bounding_box_volume_cm3" in fallback:
                    report.model_info["bounding_box_volume_cm3"] = fallback["bounding_box_volume_cm3"]

                details = f"{tri_count:,} triangles (inline parser)"
                d_mm = fallback.get("dimensions_mm")
                if d_mm:
                    details += f", {d_mm['x']:.1f} x {d_mm['y']:.1f} x {d_mm['z']:.1f} mm"
                report.checks.append(_CheckResult(
                    name="mesh_geometry",
                    passed=tri_count > 0,
                    details=details,
                ))
            else:
                report.checks.append(_CheckResult(
                    name="mesh_geometry",
                    passed=False,
                    details=f"Inline parse failed: {fallback['error']}",
                    severity="error",
                ))
        else:
            report.checks.append(_CheckResult(
                name="mesh_geometry",
                passed=True,
                details="Skipped — analysis module unavailable for non-STL format",
                severity="warning",
            ))
    except Exception as exc:
        _logger.debug("Mesh analysis failed: %s", exc, exc_info=True)
        report.checks.append(_CheckResult(
            name="mesh_geometry",
            passed=True,
            details=f"Skipped — analysis error: {exc}",
            severity="warning",
        ))
    return mesh_info


def _step_auto_scale(
    report: _PipelineReport, input_path: str, ext: str,
) -> tuple[str, bool]:
    """Step 2b: auto-scale. Returns (possibly-updated input_path, auto_scaled flag)."""
    _auto_scaled = False

    verdict = _unit_verdict(_max_dim_mm(report.model_info))

    # A size that no unit explains is reported for EVERY format, not just the
    # one Kiln can rewrite.  The correction below is binary-STL only because
    # that is what the inline scaler writes; saying nothing about a 3MF whose
    # size is nonsense would be the format deciding whether the user is told.
    if verdict.status in ("ambiguous", "oversize", "unexplained"):
        report.checks.append(_CheckResult(
            name="unit_check",
            passed=False,
            details=verdict.describe(),
            severity="warning",
        ))
        report.recommendations.insert(0, verdict.describe())
        return input_path, False

    if verdict.status == "small":
        # Passing, info-level: the model is fine and untouched.  Said here
        # rather than at the bed-fit step so every answer about units comes
        # from one judgement instead of two rules with different thresholds.
        report.checks.append(_CheckResult(
            name="unit_check",
            passed=True,
            details=verdict.describe(),
            severity="info",
        ))
        return input_path, False

    if ext == ".stl" and verdict.corrected:
        scaled_path, scale_factor = _apply_scale(input_path, verdict.factor)
        if scaled_path is not None and scale_factor > 0:
            _auto_scaled = True
            report.checks.append(_CheckResult(
                name="auto_scale",
                passed=True,
                details=verdict.describe(),
            ))

            report.repaired = True
            report.repaired_path = scaled_path
            report.cleanup_hint = (
                f"Delete auto-scaled temp file when done: {scaled_path}"
            )

            # Re-run mesh geometry on the scaled model to update dimensions
            try:
                from kiln.generation.validation import analyze_mesh as _re_analyze

                re_analysis = _re_analyze(scaled_path)
                new_info = re_analysis.to_dict()
                new_dims = new_info.get("dimensions_mm") or {}
                if new_dims:
                    report.model_info["dimensions_mm"] = new_dims
                    report.model_info["bounding_box"] = new_dims
                new_vol = new_info.get("volume_mm3", 0)
                if new_vol:
                    report.model_info["bounding_box_volume_cm3"] = round(new_vol / 1000.0, 2)
            except Exception:
                # Fallback: compute new dims from scale factor
                for key in ("dimensions_mm", "bounding_box"):
                    d = report.model_info.get(key)
                    if d and isinstance(d, dict):
                        scaled_d = {}
                        for k, v in d.items():
                            try:
                                scaled_d[k] = round(float(v) * scale_factor, 2)
                            except (TypeError, ValueError):
                                scaled_d[k] = v
                        report.model_info[key] = scaled_d
                old_vol = report.model_info.get("bounding_box_volume_cm3", 0)
                if old_vol:
                    report.model_info["bounding_box_volume_cm3"] = round(
                        old_vol * (scale_factor ** 3), 2
                    )

            # Update working path for downstream steps
            input_path = scaled_path

    if verdict.corrected and not _auto_scaled:
        # The diagnosis held but no correction was written — a container the
        # inline scaler cannot rewrite (3MF, OBJ), or the write itself
        # failed.  The size is still unprintable, so silence here would be
        # the file format deciding whether the user finds out; instead the
        # same diagnosis goes out as an instruction.
        report.checks.append(_CheckResult(
            name="unit_check",
            passed=False,
            details=verdict.describe_unapplied(),
            severity="warning",
        ))
        report.recommendations.insert(0, verdict.describe_unapplied())

    return input_path, _auto_scaled


def _step_watertight_check(
    report: _PipelineReport, input_path: str,
) -> bool | None:
    """Step 3: watertight check. Returns is_manifold."""
    is_manifold: bool | None = None
    try:
        from kiln.generation.validation import validate_mesh

        validation = validate_mesh(input_path)
        is_manifold = validation.is_manifold
        if is_manifold:
            report.checks.append(_CheckResult(
                name="watertight",
                passed=True,
                details="Manifold mesh — watertight",
            ))
        else:
            issues = "; ".join(validation.errors) if validation.errors else "Non-manifold geometry"
            report.checks.append(_CheckResult(
                name="watertight",
                passed=False,
                details=issues,
                severity="warning",
            ))
    except ImportError:
        report.checks.append(_CheckResult(
            name="watertight",
            passed=True,
            details="Skipped — validation module unavailable",
            severity="warning",
        ))
    except Exception as exc:
        _logger.debug("Watertight check failed: %s", exc, exc_info=True)
        report.checks.append(_CheckResult(
            name="watertight",
            passed=True,
            details=f"Skipped — check error: {exc}",
            severity="warning",
        ))
    return is_manifold


def _step_repair(
    report: _PipelineReport,
    input_path: str,
    path: Path,
    is_manifold: bool | None,
) -> str:
    """Step 4: auto-repair. Returns working_path."""
    if is_manifold is False:
        try:
            import shutil

            from kiln.generation.validation import repair_stl

            repair_dir = Path(tempfile.mkdtemp(prefix="kiln_repairs_"))
            suffix = path.suffix or ".stl"
            fd, repair_tmp_path = tempfile.mkstemp(
                suffix=suffix, prefix="kiln_vp_repair_", dir=str(repair_dir)
            )
            os.close(fd)
            shutil.copy2(input_path, repair_tmp_path)

            repair_stl(repair_tmp_path)

            # Re-check manifold status
            from kiln.generation.validation import validate_mesh as _re_validate

            post_repair = _re_validate(repair_tmp_path)
            if post_repair.is_manifold:
                report.repaired = True
                report.repaired_path = repair_tmp_path
                report.model_info["repair_dir"] = str(repair_dir)
                report.cleanup_hint = (
                    f"Delete repaired temp file when done: {repair_tmp_path}"
                )
                report.checks.append(_CheckResult(
                    name="repair",
                    passed=True,
                    details="Mesh repaired — now watertight",
                ))
            else:
                # Repair dir still exists — track for caller cleanup
                report.model_info["repair_dir"] = str(repair_dir)
                report.checks.append(_CheckResult(
                    name="repair",
                    passed=False,
                    details="Repair attempted but mesh remains non-manifold",
                    severity="warning",
                ))
                report.recommendations.append(
                    "Mesh is non-manifold after repair. "
                    "Try repair_mesh_advanced or fix in Blender/MeshLab."
                )
        except ImportError:
            report.checks.append(_CheckResult(
                name="repair",
                passed=True,
                details="Skipped — repair module unavailable",
                severity="warning",
            ))
        except Exception as exc:
            _logger.debug("Repair failed: %s", exc, exc_info=True)
            report.checks.append(_CheckResult(
                name="repair",
                passed=False,
                details=f"Repair failed: {exc}",
                severity="warning",
            ))
            report.recommendations.append(
                "Auto-repair failed. Try repair_mesh_advanced or fix manually."
            )

    # Use repaired path for remaining analysis if available
    return report.repaired_path or input_path


def _step_printability(report: _PipelineReport, working_path: str) -> None:
    """Step 5: printability analysis."""
    try:
        from kiln.printability import analyze_printability

        pa_report = analyze_printability(working_path)
        score = pa_report.score
        grade = pa_report.grade

        passed = score >= _MIN_PASS_SCORE
        details = f"Score {score}/100 (grade {grade})"
        severity = "info" if passed else "warning"

        if hasattr(pa_report, "thin_walls") and pa_report.thin_walls:
            tw = pa_report.thin_walls
            if isinstance(tw, list):
                count = len(tw)
            elif isinstance(tw, (int, float)):
                count = int(tw)
            elif hasattr(tw, "thin_wall_count"):
                count = tw.thin_wall_count
            else:
                count = 0
            if count > 0:
                min_w = getattr(tw, "min_wall_thickness_mm", None)
                if min_w:
                    details += f", {count} thin wall(s) (min {min_w:.2f}mm)"
                else:
                    details += f", {count} thin wall(s)"
        if hasattr(pa_report, "overhang_percentage") and pa_report.overhang_percentage:
            details += f", {pa_report.overhang_percentage:.0f}% overhang"

        report.checks.append(_CheckResult(
            name="printability",
            passed=passed,
            details=details,
            severity=severity,
        ))
        if hasattr(pa_report, "recommendations"):
            report.recommendations.extend(pa_report.recommendations)
    except ImportError:
        report.checks.append(_CheckResult(
            name="printability",
            passed=True,
            details="Skipped — printability module unavailable",
            severity="warning",
        ))
    except Exception as exc:
        _logger.debug("Printability analysis failed: %s", exc, exc_info=True)
        report.checks.append(_CheckResult(
            name="printability",
            passed=True,
            details=f"Skipped — analysis error: {exc}",
            severity="warning",
        ))


def _step_structural(report: _PipelineReport) -> None:
    """Step 6: structural assessment."""
    _struct_dims = report.model_info.get("dimensions_mm") or report.model_info.get("bounding_box", {})
    s_w = float(_struct_dims.get("x", _struct_dims.get("width_mm", 0)) or 0)
    s_d = float(_struct_dims.get("y", _struct_dims.get("depth_mm", 0)) or 0)
    s_h = float(_struct_dims.get("z", _struct_dims.get("height_mm", 0)) or 0)

    try:
        from kiln.design_intelligence import estimate_load_capacity

        est = estimate_load_capacity(
            width_mm=s_w, depth_mm=s_d, height_mm=s_h,
        )
        est_dict = est.to_dict() if hasattr(est, "to_dict") else {}
        safe_load = est_dict.get("safe_load_n", 0)
        report.model_info["structural_estimate"] = est_dict
        report.checks.append(_CheckResult(
            name="structural",
            passed=True,
            details=f"Estimated safe load {safe_load:.1f} N (via design_intelligence)",
        ))
    except (ImportError, TypeError):
        # Fallback: inline geometric risk factors from bounding box

        if s_w > 0 and s_d > 0 and s_h > 0:
            # Aspect ratio: height / min horizontal dim — tall = tippy
            min_horiz = min(s_w, s_d)
            aspect_ratio = s_h / min_horiz

            # Minimum cross-section from two smallest dims — proxy for thin-wall risk
            sorted_dims = sorted([s_w, s_d, s_h])
            min_cross_section = sorted_dims[0] * sorted_dims[1]

            # Surface-area-to-volume ratio from bbox — high = shell-like, fragile
            sa = 2.0 * (s_w * s_d + s_w * s_h + s_d * s_h)
            vol = s_w * s_d * s_h
            sa_vol_ratio = sa / vol if vol > 0 else 0.0

            is_risky = aspect_ratio >= 3.0 or min(s_w, s_d, s_h) <= 5.0
            severity = "warning" if is_risky else "info"

            report.checks.append(_CheckResult(
                name="structural",
                passed=not is_risky,
                details=(
                    f"Aspect ratio {aspect_ratio:.1f}:1"
                    f"{' (tall/narrow — consider adding a base)' if aspect_ratio >= 3.0 else ''}. "
                    f"Min cross-section {min_cross_section:.0f} mm\u00b2. "
                    f"Surface-to-volume ratio {sa_vol_ratio:.2f}/mm."
                ),
                severity=severity,
            ))
        else:
            report.checks.append(_CheckResult(
                name="structural",
                passed=True,
                details=(
                    "Skipped — dimensions not available for geometric assessment. "
                    "Use estimate_structural_load() for detailed analysis."
                ),
                severity="warning",
            ))
    except Exception as exc:
        _logger.debug("Structural assessment failed: %s", exc, exc_info=True)
        report.checks.append(_CheckResult(
            name="structural",
            passed=True,
            details=f"Skipped — assessment error: {exc}",
            severity="warning",
        ))


def _step_support_assessment(
    report: _PipelineReport,
    working_path: str,
    material: str,
    *,
    printer_ctx: dict[str, Any] | None = None,
    layer_height_mm: float = 0.2,
) -> None:
    """Step 5b: support feasibility assessment."""
    if not material:
        return  # Can't assess without material

    ext = Path(working_path).suffix.lower()
    if ext != ".stl":
        return  # Only STL supported for now

    try:
        from kiln.support_assessment import assess_support_feasibility

        ctx = printer_ctx or {}
        assessment = assess_support_feasibility(
            stl_path=working_path,
            material=material,
            nozzle_diameter_mm=float(ctx.get("nozzle_diameter_mm", 0.4)),
            layer_height_mm=layer_height_mm if layer_height_mm > 0 else 0.2,
        )

        report.model_info["support_assessment"] = assessment.to_dict()

        # Determine severity
        if assessment.trapped_regions:
            report.checks.append(_CheckResult(
                name="support_assessment",
                passed=False,
                details=(
                    f"Enclosed cavity detected — {len(assessment.trapped_regions)} "
                    f"support region(s) would be trapped and irremovable"
                ),
                severity="error",
            ))
        elif assessment.needs_supports and assessment.removal_difficulty == "hard":
            report.checks.append(_CheckResult(
                name="support_assessment",
                passed=False,
                details=(
                    f"{assessment.overhang_percentage:.0f}% overhangs require supports. "
                    f"{material.upper()} support removal is difficult — "
                    f"{assessment.removal_notes}"
                ),
                severity="warning",
            ))
        elif assessment.needs_supports:
            report.checks.append(_CheckResult(
                name="support_assessment",
                passed=True,
                details=(
                    f"{assessment.overhang_percentage:.0f}% overhangs. "
                    f"Supports recommended ({assessment.recommended_support_type}). "
                    f"Removal: {assessment.removal_difficulty}."
                ),
                severity="info",
            ))
        else:
            report.checks.append(_CheckResult(
                name="support_assessment",
                passed=True,
                details="No supports needed — all overhangs within material tolerance",
                severity="info",
            ))

        report.recommendations.extend(assessment.recommendations)

    except ImportError:
        report.checks.append(_CheckResult(
            name="support_assessment",
            passed=True,
            details="Skipped — support assessment module unavailable",
            severity="warning",
        ))
    except Exception as exc:
        _logger.debug("Support assessment failed: %s", exc, exc_info=True)
        report.checks.append(_CheckResult(
            name="support_assessment",
            passed=True,
            details=f"Skipped — assessment error: {exc}",
            severity="warning",
        ))


def _step_bed_fit(
    report: _PipelineReport, printer_id: str, auto_scaled: bool,
) -> None:
    """Step 7: bed size check.

    ``auto_scaled`` is no longer consulted — the second-opinion scale_check
    it used to suppress is gone (see the comment at the bottom) — but the
    parameter stays: these steps are consumed positionally outside this
    repo (kiln-pro's recovery validation gate), and a units question has no
    business changing the bed-fit signature.
    """
    if printer_id:
        resolved_vol = _resolve_build_volume(printer_id)
        build_vol = resolved_vol.dims if resolved_vol is not None else None
        vol_note = resolved_vol.provenance if resolved_vol is not None else ""
        if build_vol is not None:
            dims_mm = report.model_info.get("dimensions_mm") or report.model_info.get("bounding_box", {})
            mx = dims_mm.get("x", dims_mm.get("width_mm", 0))
            my = dims_mm.get("y", dims_mm.get("depth_mm", 0))
            mz = dims_mm.get("z", dims_mm.get("height_mm", 0))

            if mx > 0 and my > 0 and mz > 0:
                fits = mx <= build_vol[0] and my <= build_vol[1] and mz <= build_vol[2]
                vol_str = f"{build_vol[0]:.0f}x{build_vol[1]:.0f}x{build_vol[2]:.0f}mm"

                if fits:
                    # The PASSING verdict is attributed too, and for this
                    # field that matters more than the refusal does: a bed
                    # is not a safety limit that only tightens, so an
                    # owner-declared volume is never clamped down to the
                    # curated one.  A model can therefore "fit" a bed that
                    # is larger on paper than in the room, and this is the
                    # only place a reader would ever be told.
                    report.checks.append(_CheckResult(
                        name="bed_fit",
                        passed=True,
                        details=f"Fits build volume ({vol_str}){vol_note}",
                    ))
                else:
                    report.checks.append(_CheckResult(
                        name="bed_fit",
                        passed=False,
                        details=(
                            f"Model {mx:.1f}x{my:.1f}x{mz:.1f}mm exceeds "
                            f"build volume ({vol_str}){vol_note}"
                        ),
                        severity="error",
                    ))
                    report.recommendations.append(
                        "Model exceeds printer build volume. "
                        "Use scale_mesh_to_fit to auto-shrink, or split the model."
                    )
            else:
                report.checks.append(_CheckResult(
                    name="bed_fit",
                    passed=True,
                    details="Skipped — model dimensions not available",
                    severity="warning",
                ))
        else:
            report.checks.append(_CheckResult(
                name="bed_fit",
                passed=True,
                details=f"Skipped — no build volume found for printer '{printer_id}'",
                severity="warning",
            ))

    # The units question is answered once, by ``_unit_verdict`` in step 2b.
    # A second opinion used to live here: it warned below a different
    # threshold (10mm), named meters as the likely cause for sizes meters
    # cannot produce, and recommended "a 50-100x scale factor" — which is
    # not a unit conversion at all, but the old scale-to-80mm rule leaking
    # into user-facing advice dressed as a fact about meters (they are
    # exactly 1000x).  Two rules answering one question with different
    # numbers is how the wrong one survives; there is now one.


def _step_material_check(report: _PipelineReport, material: str) -> None:
    """Step 8: material-specific check."""
    if material:
        mat_result = _run_material_check(material, report.model_info)
        if mat_result is not None:
            report.checks.append(mat_result)
        else:
            report.checks.append(_CheckResult(
                name="material_check",
                passed=True,
                details=f"Material '{material}' not recognised — check skipped",
                severity="warning",
            ))


def _step_estimate(report: _PipelineReport, working_path: str) -> None:
    """Step 9: print time/cost estimate."""
    _estimate_available = False
    try:
        from kiln.generation.validation import estimate_print_time_from_mesh as _est_fn

        est_result = _est_fn(working_path)
        time_min = int(est_result.get("time_min", 0))
        filament_g = round(float(est_result.get("filament_g", 0)), 1)
        cost_usd = round(filament_g * _MATERIAL_COST_PER_GRAM, 2)
        report.model_info["estimated_print_time_min"] = time_min
        report.model_info["estimated_filament_g"] = filament_g
        report.model_info["estimated_cost_usd"] = cost_usd
        _estimate_available = True
    except (ImportError, AttributeError):
        pass

    if not _estimate_available:
        # Fallback: rough estimate from bounding box volume
        bbox_vol_cm3 = report.model_info.get("bounding_box_volume_cm3", 0.0) or 0.0
        if bbox_vol_cm3 > 0:
            time_min = max(1, int(round(bbox_vol_cm3 * 8)))
            filament_g = round(bbox_vol_cm3 * _PLA_DENSITY_G_PER_CM3 * _DEFAULT_INFILL_FACTOR, 1)
            cost_usd = round(filament_g * _MATERIAL_COST_PER_GRAM, 2)
            report.model_info["estimated_print_time_min"] = time_min
            report.model_info["estimated_filament_g"] = filament_g
            report.model_info["estimated_cost_usd"] = cost_usd
            report.model_info["estimate_source"] = "bounding_box"
            est_detail = f"~{time_min} min, ~{filament_g}g PLA, ~${cost_usd:.2f} (rough, from bounding box)"
        else:
            time_min = 0
            filament_g = 0.0
            cost_usd = 0.0
            est_detail = "Could not estimate — bounding box dimensions unavailable"
    else:
        est_detail = f"~{time_min} min, ~{filament_g}g PLA, ~${cost_usd:.2f}"

    report.checks.append(_CheckResult(
        name="estimate",
        passed=True,
        details=est_detail,
        severity="info",
    ))


# ---------------------------------------------------------------------------
# Plugin class
# ---------------------------------------------------------------------------
