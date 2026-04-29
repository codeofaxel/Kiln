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
from typing import Any

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


def _get_build_volume_for_printer(printer_id: str) -> tuple[float, float, float] | None:
    """Resolve build volume from printer_id via intelligence, then profiles."""
    try:
        from kiln.printers.bed_fit import get_build_volume

        volume = get_build_volume(printer_id)
        if volume is not None:
            return volume
    except Exception:
        _logger.debug(
            "Could not resolve printer-intelligence build volume for %s",
            printer_id,
            exc_info=True,
        )
    try:
        from kiln.safety_profiles import get_profile

        profile = get_profile(printer_id)
        if profile and profile.build_volume and len(profile.build_volume) >= 3:
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
