"""Post-generation validation pipeline plugin — comprehensive pass/fail report.

Validates any 3D model (AI-generated, downloaded, or user-created)
before printing.  Chains format check, mesh analysis,
watertight check, auto-repair, printability analysis, structural
assessment, and bed-fit check into a single orchestration tool.

Every AI model passes Kiln's engineering review before it touches your
printer.

Auto-discovered by :func:`~kiln.plugin_loader.register_all_plugins` --
no manual imports needed.
"""

from __future__ import annotations

import logging
import os
import re
import struct
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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
    """Parse binary STL to extract triangle count and bounding box.

    Uses only the standard library.  Falls back gracefully on ASCII STL
    or malformed files.
    """
    path = Path(file_path)
    size = path.stat().st_size
    result: dict[str, Any] = {}

    with open(path, "rb") as fh:
        header = fh.read(_STL_HEADER_SIZE)
        if size < _STL_HEADER_SIZE + 4:
            return {"error": "File too small for valid STL"}

        # Detect ASCII STL early: header starts with "solid" AND the file size
        # doesn't match what a binary STL with that many triangles would be.
        # This must happen before any binary float parsing to avoid garbage.
        if header[:5] == b"solid":
            count_bytes = fh.read(4)
            tri_count_candidate = struct.unpack("<I", count_bytes)[0]
            expected_candidate = _STL_HEADER_SIZE + 4 + tri_count_candidate * _STL_TRIANGLE_SIZE
            if expected_candidate != size:
                # ASCII STL — count facets by line scan, skip binary bbox parse
                fh.seek(0)
                content = fh.read().decode("ascii", errors="ignore")
                result["triangle_count"] = content.lower().count("facet normal")
                return result
            # Sizes match: treat as binary, count bytes already consumed above
            tri_count = tri_count_candidate
        else:
            count_bytes = fh.read(4)
            tri_count = struct.unpack("<I", count_bytes)[0]

        result["triangle_count"] = tri_count

        expected = _STL_HEADER_SIZE + 4 + tri_count * _STL_TRIANGLE_SIZE
        if size < expected:
            # Unexpected size mismatch — skip bbox rather than parse garbage
            tri_count = 0

        # Bounding box from binary STL vertices
        if tri_count > 0:
            x_min = y_min = z_min = float("inf")
            x_max = y_max = z_max = float("-inf")
            for _ in range(min(tri_count, 500_000)):
                data = fh.read(_STL_TRIANGLE_SIZE)
                if len(data) < _STL_TRIANGLE_SIZE:
                    break
                # Skip normal (3 floats), read 3 vertices (9 floats)
                verts = struct.unpack_from("<9f", data, 12)
                for i in range(3):
                    x, y, z = verts[i * 3], verts[i * 3 + 1], verts[i * 3 + 2]
                    x_min, x_max = min(x_min, x), max(x_max, x)
                    y_min, y_max = min(y_min, y), max(y_max, y)
                    z_min, z_max = min(z_min, z), max(z_max, z)

            if x_min != float("inf"):
                result["bounding_box"] = {
                    "x_min": round(x_min, 3),
                    "x_max": round(x_max, 3),
                    "y_min": round(y_min, 3),
                    "y_max": round(y_max, 3),
                    "z_min": round(z_min, 3),
                    "z_max": round(z_max, 3),
                }
                result["dimensions_mm"] = {
                    "x": round(x_max - x_min, 2),
                    "y": round(y_max - y_min, 2),
                    "z": round(z_max - z_min, 2),
                }
                # Approximate volume from bounding box (upper bound)
                vol = (x_max - x_min) * (y_max - y_min) * (z_max - z_min)
                result["bounding_box_volume_cm3"] = round(vol / 1000.0, 2)

    return result


# ---------------------------------------------------------------------------
# Build volume lookup
# ---------------------------------------------------------------------------


def _get_build_volume_for_printer(printer_id: str) -> tuple[float, float, float] | None:
    """Resolve build volume from printer_id via safety profiles."""
    try:
        from kiln.safety_profiles import get_profile

        profile = get_profile(printer_id)
        if profile.build_volume and len(profile.build_volume) >= 3:
            return (
                float(profile.build_volume[0]),
                float(profile.build_volume[1]),
                float(profile.build_volume[2]),
            )
    except Exception:
        _logger.debug("Could not resolve build volume for %s", printer_id, exc_info=True)
    return None


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

_AUTO_SCALE_SMALL_THRESHOLD_MM = 10.0  # max dim below this → suspiciously small
_AUTO_SCALE_LARGE_THRESHOLD_MM = 500.0  # max dim above this → likely microns
_AUTO_SCALE_MIN_TRIANGLES = 1000  # complex model threshold
_AUTO_SCALE_TARGET_HEIGHT_MM = 80.0  # reasonable figurine size
_AUTO_SCALE_MICRON_FACTOR = 0.001  # microns → mm conversion
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


def _auto_scale_if_needed(
    stl_path: str,
    model_info: dict[str, Any],
) -> tuple[str | None, float]:
    """Detect and fix AI-generated models exported in wrong units.

    Heuristics:
        1. max_dim < 10mm AND triangles > 1000 → model was exported in meters.
           Scale to 80mm target height.
        2. max_dim > 500mm AND triangles > 1000 → model was exported in microns.
           Scale by 0.001 (microns → mm).
        3. Otherwise → no scaling needed.

    The triangle count threshold prevents scaling genuinely small/simple models
    (like washers, pins, spacers) that have few triangles.

    :returns: (scaled_path, scale_factor) or (None, 0.0) if no scaling needed.
    """
    dims = model_info.get("dimensions_mm") or model_info.get("bounding_box", {})
    x = float(dims.get("x", dims.get("width_mm", 0)) or 0)
    y = float(dims.get("y", dims.get("depth_mm", 0)) or 0)
    z = float(dims.get("z", dims.get("height_mm", 0)) or 0)
    max_dim = max(x, y, z)
    tri_count = int(model_info.get("triangles", 0))

    if max_dim <= 0 or tri_count <= 0:
        return None, 0.0

    scale_factor = 0.0

    if max_dim < _AUTO_SCALE_SMALL_THRESHOLD_MM and tri_count > _AUTO_SCALE_MIN_TRIANGLES:
        # Likely exported in meters — scale up to target height
        scale_factor = _AUTO_SCALE_TARGET_HEIGHT_MM / max_dim
    elif max_dim > _AUTO_SCALE_LARGE_THRESHOLD_MM and tri_count > _AUTO_SCALE_MIN_TRIANGLES:
        # Likely exported in microns — scale down
        scale_factor = _AUTO_SCALE_MICRON_FACTOR
    else:
        return None, 0.0

    # Try kiln.mesh_tools.rescale_model first (more robust), fall back to inline
    try:
        from kiln.server import rescale_model

        result = rescale_model(stl_path, scale_factor=scale_factor)
        scaled_path = result.get("path", "")
        if scaled_path and Path(scaled_path).exists():
            return scaled_path, scale_factor
    except Exception:
        _logger.debug("rescale_model unavailable, using inline STL scaler", exc_info=True)

    # Inline fallback
    try:
        scaled_path = _inline_stl_scale(stl_path, scale_factor)
        return scaled_path, scale_factor
    except Exception:
        _logger.debug("Inline STL scaling failed", exc_info=True)
        return None, 0.0


# ---------------------------------------------------------------------------
# Material-specific checks
# ---------------------------------------------------------------------------

_MATERIAL_ALIASES: dict[str, str] = {
    "pla+": "pla",
    "petg": "petg",
    "abs": "abs",
    "asa": "abs",  # ABS/ASA share warping behaviour
    "tpu": "tpu",
    "tpe": "tpu",
}


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
    if ext == ".stl" and report.model_info.get("triangles", 0) > 0:
        scaled_path, scale_factor = _auto_scale_if_needed(
            input_path, report.model_info,
        )
        if scaled_path is not None and scale_factor > 0:
            _auto_scaled = True
            # Determine what happened
            old_dims = report.model_info.get("dimensions_mm") or report.model_info.get("bounding_box", {})
            old_max = max(
                float(old_dims.get("x", old_dims.get("width_mm", 0)) or 0),
                float(old_dims.get("y", old_dims.get("depth_mm", 0)) or 0),
                float(old_dims.get("z", old_dims.get("height_mm", 0)) or 0),
            )
            new_max = round(old_max * scale_factor, 1)

            if scale_factor > 1:
                reason = "model was likely exported in meters"
            else:
                reason = "model was likely exported in microns"

            report.checks.append(_CheckResult(
                name="auto_scale",
                passed=True,
                details=(
                    f"Scaled {scale_factor:.1f}x "
                    f"({old_max:.1f}mm \u2192 {new_max:.1f}mm) "
                    f"\u2014 {reason}"
                ),
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
    """Step 7: bed size check + scale_check."""
    if printer_id:
        build_vol = _get_build_volume_for_printer(printer_id)
        if build_vol is not None:
            dims_mm = report.model_info.get("dimensions_mm") or report.model_info.get("bounding_box", {})
            mx = dims_mm.get("x", dims_mm.get("width_mm", 0))
            my = dims_mm.get("y", dims_mm.get("depth_mm", 0))
            mz = dims_mm.get("z", dims_mm.get("height_mm", 0))

            if mx > 0 and my > 0 and mz > 0:
                fits = mx <= build_vol[0] and my <= build_vol[1] and mz <= build_vol[2]
                vol_str = f"{build_vol[0]:.0f}x{build_vol[1]:.0f}x{build_vol[2]:.0f}mm"

                if fits:
                    report.checks.append(_CheckResult(
                        name="bed_fit",
                        passed=True,
                        details=f"Fits build volume ({vol_str})",
                    ))
                else:
                    report.checks.append(_CheckResult(
                        name="bed_fit",
                        passed=False,
                        details=(
                            f"Model {mx:.1f}x{my:.1f}x{mz:.1f}mm exceeds "
                            f"build volume ({vol_str})"
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

    # Check for suspiciously small models (likely wrong units)
    # Skip if auto-scale already fixed this in Step 2b
    if not auto_scaled:
        dims_mm = report.model_info.get("dimensions_mm") or report.model_info.get("bounding_box", {})
        x = dims_mm.get("x", 0) or dims_mm.get("width_mm", 0)
        y = dims_mm.get("y", 0) or dims_mm.get("depth_mm", 0)
        z = dims_mm.get("z", 0) or dims_mm.get("height_mm", 0)
        max_dim = max(x, y, z)
        if 0 < max_dim < 10:
            report.checks.append(_CheckResult(
                name="scale_check",
                passed=False,
                details=(
                    f"Model is only {max_dim:.1f}mm in its largest dimension — "
                    f"likely exported in meters or another unit. "
                    f"Use rescale_model or scale_mesh_to_fit to scale up."
                ),
                severity="warning",
            ))
            report.recommendations.insert(0,
                f"Model appears to be in the wrong units ({max_dim:.1f}mm max). "
                f"Scale up with rescale_model() — a 50-100x scale factor is typical "
                f"for models exported in meters."
            )


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


class _ValidationPipelinePlugin:
    """Post-generation validation pipeline — comprehensive pass/fail report.

    Tools:
        - validate_and_prepare
        - prepare_ai_model_for_print
    """

    @property
    def name(self) -> str:
        return "validation_pipeline_tools"

    @property
    def description(self) -> str:
        return "Post-generation validation pipeline for any 3D model"

    def register(self, mcp: Any) -> None:
        """Register validation pipeline tools with the MCP server."""

        @mcp.tool()
        def validate_and_prepare(
            input_path: str,
            printer_id: str = "",
            material: str = "",
        ) -> dict[str, Any]:
            """Comprehensive validation pipeline for any 3D model before printing.

            Runs format check, mesh analysis, watertight check, auto-repair,
            printability analysis, structural assessment, bed-fit check, and
            (optionally) material-specific checks.
            Returns a detailed pass/fail report with actionable recommendations
            plus a numeric printability score (0-100).

            Works with any model — AI-generated, downloaded from marketplaces,
            or created in CAD.  Every model passes Kiln's engineering review
            before it touches your printer.

            Each step is resilient — if a step's underlying module is unavailable
            the step is skipped and the pipeline continues.

            :param input_path: Path to a 3D model file (.stl, .3mf, .obj, .step, .glb).
            :param printer_id: Optional printer model ID (e.g. "bambu_a1") for
                bed-fit checking.  If empty, bed-fit check is skipped.
            :param material: Optional material name (e.g. "pla", "petg", "abs",
                "asa", "tpu").  When provided, adds a material-specific check
                for known print-quality risks.  If empty, material check is skipped.
            :returns: Dict with pass/fail status, per-check details, recommendations,
                ``printability_score`` (0-100), and ``score_breakdown``.
            """
            report = _PipelineReport(input_path=input_path)

            # Step 1: Format check
            ext = _step_format_check(report, input_path)
            if ext is None:
                return report.to_dict()

            # Step 2: Mesh analysis
            _step_mesh_analysis(report, input_path, ext)

            # Step 2b: Auto-scale
            input_path, _auto_scaled = _step_auto_scale(report, input_path, ext)

            # Step 3: Watertight check
            is_manifold = _step_watertight_check(report, input_path)

            # Step 4: Auto-repair
            working_path = _step_repair(
                report, input_path, Path(input_path), is_manifold,
            )

            # Step 5: Printability
            _step_printability(report, working_path)

            # Step 5b: Support assessment
            _step_support_assessment(
                report, working_path, material,
            )

            # Step 6: Structural
            _step_structural(report)

            # Step 7: Bed fit + scale check
            _step_bed_fit(report, printer_id, _auto_scaled)

            # Step 8: Material check
            _step_material_check(report, material)

            # Step 9: Estimate
            _step_estimate(report, working_path)

            # ----------------------------------------------------------
            # Step 10: Aggregate verdict
            # ----------------------------------------------------------
            has_failure = any(
                not c.passed and c.severity == "error" for c in report.checks
            )
            has_warning = any(
                (not c.passed and c.severity == "warning")
                or (c.passed and c.severity == "warning")
                for c in report.checks
            )

            if has_failure:
                report.status = "fail"
                report.ready_to_print = False
            elif has_warning:
                report.status = "pass_with_warnings"
                report.ready_to_print = True
            else:
                report.status = "pass"
                report.ready_to_print = True

            # ----------------------------------------------------------
            # Step 11: Printability score
            # ----------------------------------------------------------
            report.printability_score, report.score_breakdown = _compute_printability_score(
                report.checks,
                repaired=report.repaired,
            )

            # ----------------------------------------------------------
            # Step 12: validated_path
            # ----------------------------------------------------------
            report.validated_path = report.repaired_path or input_path

            # ----------------------------------------------------------
            # Step 13: summary
            # ----------------------------------------------------------
            issues: list[str] = []
            warnings: list[str] = []
            for c in report.checks:
                if not c.passed and c.severity == "error":
                    issues.append(_sanitize_summary_detail(c.details))
                elif not c.passed and c.severity == "warning":
                    warnings.append(_sanitize_summary_detail(c.details))

            score_str = f"{report.printability_score}/100"

            # Build cost/time snippet for summary from model_info
            _est_snippet = ""
            _t = report.model_info.get("estimated_print_time_min")
            _c = report.model_info.get("estimated_cost_usd")
            if _t:
                _est_snippet = f" ~{_t} min, ~${_c:.2f}." if _c else f" ~{_t} min."

            if report.ready_to_print and not issues and not warnings:
                report.summary = f"Print-ready ({score_str}).{_est_snippet}"
            elif report.ready_to_print:
                count = len(warnings)
                label = "warning" if count == 1 else "warnings"
                first = warnings[0] if warnings else ""
                base = f"Print-ready ({score_str}).{_est_snippet} {count} {label}: {first}"
                report.summary = base[:200]
            else:
                count = len(issues)
                label = "issue" if count == 1 else "issues"
                brief = "; ".join(issues[:2])
                base = f"Not ready ({score_str}). {count} {label}: {brief}"
                report.summary = base[:200]

            # ----------------------------------------------------------
            # Step 14: next_action
            # ----------------------------------------------------------
            if report.ready_to_print:
                slice_args: dict[str, Any] = {"input_path": report.validated_path}
                if printer_id:
                    slice_args["printer_id"] = printer_id
                report.next_action = {
                    "tool": "slice_model",
                    "args": slice_args,
                }
            else:
                # Determine most actionable next step
                bed_fail = any(
                    not c.passed and c.name == "bed_fit" for c in report.checks
                )
                mesh_fail = any(
                    not c.passed and c.name in ("watertight", "repair")
                    for c in report.checks
                )
                printability_fail = any(
                    not c.passed and c.name == "printability"
                    for c in report.checks
                )
                scale_fail = any(
                    not c.passed and c.name == "scale_check"
                    for c in report.checks
                )
                if bed_fail:
                    action: dict[str, Any] = {
                        "tool": "scale_mesh_to_fit",
                        "reason": "Model exceeds printer build volume",
                    }
                    if printer_id:
                        action["printer_id"] = printer_id
                    report.next_action = action
                elif scale_fail:
                    report.next_action = {
                        "tool": "rescale_model",
                        "reason": "Model is suspiciously small — likely exported in wrong units",
                    }
                elif mesh_fail:
                    report.next_action = {
                        "tool": "repair_mesh_advanced",
                        "reason": "Mesh is non-manifold and auto-repair failed",
                    }
                elif printability_fail:
                    report.next_action = {
                        "tool": "auto_orient_model",
                        "reason": "Low printability score — reorienting may improve it",
                    }
                else:
                    report.next_action = None

            return report.to_dict()

        @mcp.tool()
        def prepare_ai_model_for_print(
            input_path: str,
            target_height_mm: float = 0,
            printer_id: str = "",
            material: str = "PLA",
        ) -> dict[str, Any]:
            """Prepare any AI-generated model for printing — auto-fixes the unit problem.

            AI model generators (Meshy, Tripo, Stability, Gemini) routinely
            export models in meters instead of millimeters, producing figurines
            that are 1.9mm tall.  This tool detects and fixes that, plus runs
            the full validation pipeline and provides smart recommendations
            for simplification and hollowing.

            Pipeline:
                1. Run validate_and_prepare for baseline analysis
                2. Auto-scale detection — if max dim < 10mm, scale to a
                   reasonable size (or to target_height_mm if provided)
                3. Mesh simplification recommendation (if > 100K triangles)
                4. Smart hollow recommendation (only when appropriate)
                5. Re-validate the scaled model
                6. Return combined before/after comparison

            Works with STL, OBJ, and 3MF files.

            :param input_path: Path to the AI-generated model file.
            :param target_height_mm: Desired height in mm.  If 0, auto-detects
                a reasonable size based on model aspect ratio.
            :param printer_id: Optional printer model ID for bed-fit checking.
            :param material: Material name (default "PLA") for material checks.
            :returns: Dict with original/prepared comparison, actions taken,
                recommendations, and next_action for slicing.
            """
            actions_taken: list[str] = []
            recommendations: list[str] = []

            # -------------------------------------------------------
            # Step 1: Baseline validation
            # -------------------------------------------------------
            baseline = validate_and_prepare(
                input_path, printer_id=printer_id, material=material,
            )

            original_info: dict[str, Any] = {
                "dimensions_mm": baseline.get("model_info", {}).get("dimensions_mm", {}),
                "triangles": baseline.get("model_info", {}).get("triangles", 0),
                "printability_score": baseline.get("printability_score", 0),
            }

            # Extract dimensions for scaling logic
            dims = baseline.get("model_info", {}).get("dimensions_mm", {})
            x = float(dims.get("x", dims.get("width_mm", 0)) or 0)
            y = float(dims.get("y", dims.get("depth_mm", 0)) or 0)
            z = float(dims.get("z", dims.get("height_mm", 0)) or 0)
            max_dim = max(x, y, z)
            tri_count = int(baseline.get("model_info", {}).get("triangles", 0))

            # Track the working path — may change after scaling
            working_path = baseline.get("validated_path", input_path)
            scale_factor = 0.0

            # -------------------------------------------------------
            # Step 2: Auto-scale detection
            # -------------------------------------------------------
            needs_scaling = (
                max_dim > 0
                and max_dim < _AUTO_SCALE_SMALL_THRESHOLD_MM
                and tri_count > _AUTO_SCALE_MIN_TRIANGLES
            )

            if needs_scaling:
                if target_height_mm > 0:
                    # User-specified target height — scale to that
                    scale_factor = target_height_mm / z if z > 0 else target_height_mm / max_dim
                    reason = f"scaled to target height {target_height_mm}mm"
                else:
                    # Auto-detect reasonable size based on aspect ratio
                    if z > 0 and x > 0 and y > 0:
                        if z > max(x, y) * 1.5:
                            # Figurine: tall and narrow → 80mm tall
                            target = 80.0
                            scale_factor = target / z
                            reason = "figurine detected (tall) — scaled to 80mm height"
                        elif z < max(x, y) * 0.5:
                            # Flat object → 100mm in widest dimension
                            target = 100.0
                            widest = max(x, y)
                            scale_factor = target / widest
                            reason = "flat object detected — scaled to 100mm wide"
                        else:
                            # Cubic → 60mm in largest dimension
                            target = 60.0
                            scale_factor = target / max_dim
                            reason = "cubic object detected — scaled to 60mm"
                    else:
                        # Fallback: scale to 60mm
                        target = 60.0
                        scale_factor = target / max_dim if max_dim > 0 else 1.0
                        reason = "auto-scaled to 60mm (default)"

                if scale_factor > 0 and scale_factor != 1.0:
                    scaled_path: str | None = None

                    # Try rescale_model from server
                    try:
                        from kiln.server import rescale_model as _rescale

                        result = _rescale(input_path, scale_factor=scale_factor)
                        sp = result.get("path", "")
                        if sp and Path(sp).exists():
                            scaled_path = sp
                    except Exception:
                        _logger.debug(
                            "rescale_model unavailable for prepare_ai_model",
                            exc_info=True,
                        )

                    # Inline fallback for STL
                    if scaled_path is None and Path(input_path).suffix.lower() == ".stl":
                        try:
                            scaled_path = _inline_stl_scale(input_path, scale_factor)
                        except Exception:
                            _logger.debug(
                                "Inline STL scaling failed in prepare_ai_model",
                                exc_info=True,
                            )

                    if scaled_path is not None:
                        working_path = scaled_path
                        actions_taken.append(
                            f"Scaled {scale_factor:.1f}x ({reason})"
                        )
                    else:
                        # Could not scale — report factor for manual use
                        recommendations.append(
                            f"Could not auto-scale. Apply scale factor "
                            f"{scale_factor:.1f}x manually with rescale_model."
                        )
            elif target_height_mm > 0 and z > 0 and max_dim >= _AUTO_SCALE_SMALL_THRESHOLD_MM:
                # Model is normal size but user wants a specific height
                scale_factor = target_height_mm / z
                if abs(scale_factor - 1.0) > 0.01:
                    scaled_path_t: str | None = None
                    try:
                        from kiln.server import rescale_model as _rescale2

                        result2 = _rescale2(input_path, scale_factor=scale_factor)
                        sp2 = result2.get("path", "")
                        if sp2 and Path(sp2).exists():
                            scaled_path_t = sp2
                    except Exception:
                        _logger.debug("rescale_model unavailable for target_height", exc_info=True)

                    if scaled_path_t is None and Path(input_path).suffix.lower() == ".stl":
                        try:
                            scaled_path_t = _inline_stl_scale(input_path, scale_factor)
                        except Exception:
                            _logger.debug("Inline STL scaling failed for target_height", exc_info=True)

                    if scaled_path_t is not None:
                        working_path = scaled_path_t
                        actions_taken.append(
                            f"Scaled {scale_factor:.2f}x to target height {target_height_mm}mm"
                        )

            # -------------------------------------------------------
            # Step 3: Mesh simplification recommendation
            # -------------------------------------------------------
            if tri_count > _SIMPLIFY_THRESHOLD:
                recommendations.append(
                    f"Model has {tri_count:,} triangles — consider "
                    f"simplify_mesh_model for faster slicing "
                    f"(FDM can't resolve detail finer than 0.1mm)"
                )

            # -------------------------------------------------------
            # Step 4: Smart hollow recommendation
            # -------------------------------------------------------
            # Use scaled dimensions if scaling occurred, otherwise baseline.
            _h_dims = baseline.get("model_info", {}).get("dimensions_mm", {})
            if scale_factor > 0 and _h_dims:
                # Apply scale factor to baseline dims for hollow analysis
                _h_dims = {
                    k: float(v or 0) * scale_factor
                    for k, v in _h_dims.items()
                    if isinstance(v, (int, float)) or (isinstance(v, str) and v.replace(".", "", 1).isdigit())
                }
            _h_x = float(_h_dims.get("x", _h_dims.get("width_mm", 0)) or 0)
            _h_y = float(_h_dims.get("y", _h_dims.get("depth_mm", 0)) or 0)
            _h_z = float(_h_dims.get("z", _h_dims.get("height_mm", 0)) or 0)
            _h_max_dim = max(_h_x, _h_y, _h_z)

            # Compute surface-area and volume from bounding box
            _h_sa = 2.0 * (_h_x * _h_y + _h_x * _h_z + _h_y * _h_z) if (_h_x > 0 and _h_y > 0 and _h_z > 0) else 0.0
            _h_vol = _h_x * _h_y * _h_z  # mm3
            _h_bbox_vol_cm3 = _h_vol / 1000.0  # cm3
            _h_sa_vol_ratio = _h_sa / _h_vol if _h_vol > 0 else 0.0

            if _h_max_dim < 30:
                pass  # Too small — walls would be paper-thin, don't suggest hollow
            elif _h_sa_vol_ratio > 1.0:
                # Already thin-shelled — don't suggest hollow
                recommendations.append(
                    "Model is thin-shelled (surface-to-volume ratio "
                    f"{_h_sa_vol_ratio:.2f}/mm) — consider "
                    "thicken_mesh_walls for structural integrity"
                )
            elif _h_bbox_vol_cm3 > 50 and _h_sa_vol_ratio < 0.3:
                # Large, solid model — hollowing saves material
                est_save = min(60, max(20, int(_h_bbox_vol_cm3 / 5)))
                recommendations.append(
                    f"Large solid model ({_h_bbox_vol_cm3:.0f} cm\u00b3, "
                    f"surface-to-volume ratio {_h_sa_vol_ratio:.2f}/mm) "
                    f"— hollow_mesh_model would save ~{est_save}% material "
                    f"and reduce print time"
                )
            # Otherwise: no hollow recommendation (case by case)

            # -------------------------------------------------------
            # Step 5: Re-validate on the prepared model
            # -------------------------------------------------------
            if working_path != input_path:
                prepared_report = validate_and_prepare(
                    working_path, printer_id=printer_id, material=material,
                )
            else:
                prepared_report = baseline

            prepared_dims = prepared_report.get("model_info", {}).get("dimensions_mm", {})
            prepared_info: dict[str, Any] = {
                "dimensions_mm": prepared_dims,
                "triangles": prepared_report.get("model_info", {}).get("triangles", 0),
                "printability_score": prepared_report.get("printability_score", 0),
                "stl_path": working_path,
            }

            # -------------------------------------------------------
            # Step 6: Build return envelope
            # -------------------------------------------------------
            ready = prepared_report.get("ready_to_print", False)

            next_action: dict[str, Any] | None = None
            if ready:
                next_action = {
                    "tool": "slice_model",
                    "args": {"input_path": working_path},
                }
            else:
                next_action = prepared_report.get("next_action")

            return {
                "status": "success" if ready else "needs_attention",
                "original": original_info,
                "prepared": prepared_info,
                "actions_taken": actions_taken,
                "recommendations": recommendations,
                "scale_factor": round(scale_factor, 2) if scale_factor else 0,
                "ready_to_print": ready,
                "next_action": next_action,
            }

        _logger.debug("Registered validation pipeline tools")


plugin = _ValidationPipelinePlugin()
