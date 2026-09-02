"""Bed-fit safety checks for FDM printers.

Prevents the class of crash where a mesh with negative X/Y coordinates
(e.g. an OpenSCAD cylinder centered on model origin) gets sliced for a
printer whose bed origin is the corner, causing the nozzle to drive
into the purge/wipe tool at layer 1.

Incident #0 (2026-04-15, Bambu A1): `compose_part_from_primitives`
produced a Ø25mm disc with bbox x/y in [-12.5, +12.5].  The bundled
PrusaSlicer CLI profile does not auto-center.  Result: layer-1 moves
targeted (-12.5, -12.5) and the nozzle slammed into the post-purge
cleaning tool.

This module provides a LAST-LINE-OF-DEFENSE validator used by:
    - slice_model / slice_and_print / reslice_with_overrides  (pre-slice)
    - start_print                                             (pre-send)
    - resume_interrupted_print                                (gcode output)
    - mid-print decoration generators                         (gcode output)

Any caller can reject OR (preferably) auto-center with a well-defined
translation so the printer always receives coordinates that fit.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import zipfile
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Printer intelligence lookup
# ---------------------------------------------------------------------------

_PRINTER_INTELLIGENCE_PATH = (
    Path(__file__).resolve().parent.parent / "data" / "printer_intelligence.json"
)
_printer_intelligence_cache: dict[str, Any] | None = None


# Explicit variants and common shorthand names that are not one-to-one entries
# in printer_intelligence.json.  Keep this narrow: the JSON catalog remains the
# source of truth for first-class printer models.
_BUILD_VOLUME_OVERRIDES: dict[str, tuple[float, float, float]] = {
    "bambu_x1": (256.0, 256.0, 256.0),
    "voron_2_4_350": (350.0, 350.0, 350.0),
    "voron_350": (350.0, 350.0, 350.0),
    "voron_2_4_300": (300.0, 300.0, 300.0),
    "voron_2_4_250": (250.0, 250.0, 250.0),
}


def _load_printer_intelligence() -> dict[str, Any]:
    """Load printer_intelligence.json (cached)."""
    global _printer_intelligence_cache  # noqa: PLW0603
    if _printer_intelligence_cache is None:
        with open(_PRINTER_INTELLIGENCE_PATH) as f:
            _printer_intelligence_cache = json.load(f)
    return _printer_intelligence_cache


def _normalise_printer_id(printer_id: str) -> str:
    value = printer_id.lower().strip()
    value = re.sub(r"[^a-z0-9]+", "_", value)
    value = re.sub(r"_+", "_", value).strip("_")
    return value


def _printer_id_candidates(printer_id: str | None) -> list[str]:
    if not printer_id:
        return []
    normalised = _normalise_printer_id(printer_id)
    if not normalised:
        return []

    candidates = [normalised]
    if normalised.startswith("creality_"):
        candidates.insert(0, normalised.removeprefix("creality_"))

    vendor_stripped = normalised
    for token in (
        "bambu_lab_",
        "original_prusa_",
        "prusa_research_",
        "creality_",
    ):
        if vendor_stripped.startswith(token):
            vendor_stripped = vendor_stripped.removeprefix(token)
            if token == "bambu_lab_":
                vendor_stripped = "bambu_" + vendor_stripped
            elif token in ("original_prusa_", "prusa_research_"):
                vendor_stripped = "prusa_" + vendor_stripped
            candidates.append(vendor_stripped)

    for candidate in list(candidates):
        candidates.append(re.sub(r"([a-z])_(\d)", r"\1\2", candidate))

    aliases = {
        "x1": "bambu_x1",
        "x1c": "bambu_x1c",
        "a1": "bambu_a1",
        "a1_mini": "bambu_a1_mini",
        "p1s": "bambu_p1s",
        "p1p": "bambu_p1p",
        "mk3s": "prusa_mk3s",
        "mk4": "prusa_mk4",
        "mini": "prusa_mini",
        "xl": "prusa_xl",
    }
    for candidate in list(candidates):
        if candidate in aliases:
            candidates.append(aliases[candidate])

    seen: set[str] = set()
    ordered: list[str] = []
    for candidate in candidates:
        if candidate and candidate not in seen:
            seen.add(candidate)
            ordered.append(candidate)
    return ordered


def _lookup_build_volume_exact(
    candidate: str,
) -> tuple[float, float, float] | None:
    if candidate == "default":
        return None
    if candidate in _BUILD_VOLUME_OVERRIDES:
        return _BUILD_VOLUME_OVERRIDES[candidate]
    entry = _load_printer_intelligence().get(candidate)
    if not entry:
        return None
    vol = entry.get("build_volume_mm")
    if not vol or len(vol) < 3:
        return None
    try:
        return (float(vol[0]), float(vol[1]), float(vol[2]))
    except (TypeError, ValueError):
        return None


def get_build_volume(printer_id: str | None) -> tuple[float, float, float] | None:
    """Return (x, y, z) build volume in mm for a known printer_id.

    Returns ``None`` when the printer_id is unknown or lacks volume data.
    Callers should treat ``None`` as "unknown — don't block" rather than
    as "no volume" (we'd rather allow a print than block on missing data
    for an obscure printer model).

    ``printer_id`` may be a canonical id (``bambu_a1``), a vendor-prefixed
    id (``creality_k1_max``), or a common human label (``Bambu Lab A1``).
    """
    for candidate in _printer_id_candidates(printer_id):
        looked_up = _lookup_build_volume_exact(candidate)
        if looked_up is not None:
            return looked_up
    return None


def resolve_build_volume_printer_id(printer_id: str | None) -> str | None:
    """Return the canonical id that provided a known build volume."""
    for candidate in _printer_id_candidates(printer_id):
        if _lookup_build_volume_exact(candidate) is not None:
            return candidate
    return None


def resolve_build_volume(
    printer_id: str | None,
) -> tuple[str, tuple[float, float, float]] | None:
    """Return ``(canonical_printer_id, build_volume_mm)`` if known."""
    for candidate in _printer_id_candidates(printer_id):
        looked_up = _lookup_build_volume_exact(candidate)
        if looked_up is not None:
            return candidate, looked_up
    return None


def get_printer_display_name(printer_id: str | None) -> str | None:
    """Catalogue display name for a CANONICAL printer id, or ``None``.

    Exact lookup, no alias walking and no ``default`` fallback: callers that
    put this in front of a user (the plate the 3D stage etches a machine's
    name on) would rather show nothing than the wrong printer.  Pass an id
    that :func:`resolve_build_volume` already canonicalised.
    """
    if not printer_id:
        return None
    entry = _load_printer_intelligence().get(printer_id)
    name = (entry or {}).get("display_name")
    return str(name) if name else None


# ---------------------------------------------------------------------------
# Bounding-box extraction
# ---------------------------------------------------------------------------

def compute_mesh_bbox(mesh_path: str) -> dict[str, float] | None:
    """Compute bounding box of a mesh file (STL/OBJ/3MF-geometry).

    Returns a dict with x_min/x_max/y_min/y_max/z_min/z_max in mm, or
    ``None`` if the file cannot be parsed.  Uses the existing STL parser
    for .stl files (consistent with other kiln tools), the
    transform-aware 3MF parser for .3mf files, and falls back to
    trimesh for other formats.

    For a .3mf the bbox is the geometry AS THE SLICER WILL PLACE IT
    (build-item transforms applied), because that is the question every
    caller of this function is asking — slicers honour a 3MF's placement
    literally, so raw vertex bounds would pass a file that slices to
    nothing.
    """
    path = Path(mesh_path)
    if not path.is_file():
        return None
    ext = path.suffix.lower()
    try:
        if ext == ".stl":
            from kiln.generation.validation import _bounding_box, _parse_stl
            errors: list[str] = []
            _, vertices = _parse_stl(path, errors)
            if errors or not vertices:
                return None
            return _bounding_box(vertices)
        if ext == ".3mf":
            bbox = compute_3mf_geometry_bbox(str(path))
            if bbox is not None:
                return bbox
            # No parseable <mesh> geometry (e.g. a gcode-carrying 3MF)
            # — fall through to trimesh as a last resort.
        # Fallback for .obj / .glb (and unparseable .3mf) via trimesh
        import trimesh  # type: ignore[import-not-found]
        mesh = trimesh.load(str(path), force="mesh")
        if mesh is None or not hasattr(mesh, "bounds"):
            return None
        bounds = mesh.bounds
        return {
            "x_min": float(bounds[0][0]), "x_max": float(bounds[1][0]),
            "y_min": float(bounds[0][1]), "y_max": float(bounds[1][1]),
            "z_min": float(bounds[0][2]), "z_max": float(bounds[1][2]),
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("compute_mesh_bbox failed for %s: %s", mesh_path, exc)
        return None


_GCODE_MOVE_RE = re.compile(
    r"^G[01]\s+(?:.*\bX(?P<x>-?\d+\.?\d*))?(?:.*\bY(?P<y>-?\d+\.?\d*))?",
    re.MULTILINE,
)


def compute_gcode_bbox(
    gcode_path: str, *, skip_initial_lines: int = 0, max_lines: int = 200_000
) -> dict[str, Any] | None:
    """Scan a gcode file for G0/G1 X/Y moves in the PRINT region and
    return their bbox.

    IMPORTANT: many printers' start-gcode legitimately goes OUTSIDE the
    build plate to reach mechanical features: Bambu A1 purges at
    X=[-28, -48] (wiper), homes Y to 262 (silicone wipe strip), etc.
    These moves are SAFE because they happen after G28 homing.  We
    ignore them by scanning only moves AFTER the first ``;LAYER_CHANGE``
    marker (PrusaSlicer/OrcaSlicer/BambuStudio convention).  If no
    layer-change marker is found, falls back to scanning from
    ``skip_initial_lines`` (caller-controlled).

    Returns None if no print moves found.

    The result carries a ``truncated`` key: ``True`` when the scan hit
    ``max_lines`` before the end of the file, i.e. the bbox may MISS
    later moves.  Fit checks can ignore it (layer 1 decides fit);
    occupancy callers must treat a truncated bbox as unknown, never as
    a complete keep-out footprint.
    """
    path = Path(gcode_path)
    if not path.is_file():
        return None
    x_min = y_min = float("inf")
    x_max = y_max = float("-inf")
    found = False
    truncated = False
    in_print_region = False
    # If the file has no LAYER_CHANGE marker, scan everything starting
    # at skip_initial_lines (legacy behaviour).  We detect that up front.
    has_layer_marker = False
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            head = f.read(65536)
            if ";LAYER_CHANGE" in head or ";LAYER:" in head or ";TYPE:" in head:
                has_layer_marker = True
    except Exception:
        pass
    if not has_layer_marker:
        # Conservative fallback — scan from skip_initial_lines
        in_print_region = True
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                if i > max_lines:
                    truncated = True
                    break
                if not in_print_region:
                    stripped = line.strip()
                    if stripped.startswith((";LAYER_CHANGE", ";LAYER:", ";TYPE:")):
                        in_print_region = True
                    continue
                if i < skip_initial_lines:
                    continue
                if not (line.startswith("G0") or line.startswith("G1")):
                    continue
                code = line.split(";", 1)[0]
                # Only count moves with extrusion (E) — those are ACTUAL
                # print moves.  Travel/park/wipe moves (G1 without E) can
                # legitimately exit the print area (Bambu A1 parks at
                # X=-48 for wipe after print; X=267 for silicone wipe
                # strip; etc.).  Those are firmware-safe post-G28 moves.
                if " E" not in code and "E" not in code.replace("F", ""):
                    continue
                # Robust extrusion check — exclude retraction-only moves
                # (G1 E-0.8 F1800 has no X/Y).  The bbox we want is just
                # the print area, so we need X and/or Y present AND E.
                em = re.search(r"\bE(-?\d+\.?\d*)", code)
                if em is None:
                    continue
                xm = re.search(r"\bX(-?\d+\.?\d*)", code)
                ym = re.search(r"\bY(-?\d+\.?\d*)", code)
                if xm:
                    found = True
                    v = float(xm.group(1))
                    if v < x_min:
                        x_min = v
                    if v > x_max:
                        x_max = v
                if ym:
                    found = True
                    v = float(ym.group(1))
                    if v < y_min:
                        y_min = v
                    if v > y_max:
                        y_max = v
    except Exception as exc:  # noqa: BLE001
        logger.warning("compute_gcode_bbox failed for %s: %s", gcode_path, exc)
        return None
    if not found:
        return None
    return {
        "x_min": x_min, "x_max": x_max,
        "y_min": y_min, "y_max": y_max,
        "z_min": 0.0, "z_max": 0.0,
        "truncated": truncated,
    }


def compute_3mf_bbox(
    threemf_path: str, *, max_lines: int = 200_000
) -> dict[str, Any] | None:
    """Extract embedded gcode from a Bambu .gcode.3mf and compute its
    XY bounding box from the G0/G1 moves.

    Bambu .3mf files store the gcode at ``Metadata/plate_1.gcode`` inside
    the zip.  This unpacks that, writes to a temp file, and runs
    ``compute_gcode_bbox``.
    """
    path = Path(threemf_path)
    if not path.is_file():
        return None
    try:
        with zipfile.ZipFile(path) as zf:
            # Find the plate gcode (plate_1.gcode for single-plate, but
            # could be plate_N.gcode for multi-plate — we check all).
            gcode_names = [
                n for n in zf.namelist()
                if n.startswith("Metadata/plate_") and n.endswith(".gcode")
            ]
            if not gcode_names:
                return None
            # Use the first plate (most common case is single-plate)
            gcode_bytes = zf.read(gcode_names[0])
    except (zipfile.BadZipFile, KeyError) as exc:
        logger.warning("compute_3mf_bbox failed for %s: %s", threemf_path, exc)
        return None
    # Write to a temp file and reuse compute_gcode_bbox
    import tempfile
    with tempfile.NamedTemporaryFile(
        suffix=".gcode", delete=False, mode="wb"
    ) as tf:
        tf.write(gcode_bytes)
        tmp_path = tf.name
    try:
        return compute_gcode_bbox(tmp_path, max_lines=max_lines)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)


def compute_3mf_geometry_bbox(threemf_path: str) -> dict[str, float] | None:
    """Bbox of a model 3MF's geometry AS THE SLICER WILL PLACE IT.

    This applies each ``<build><item>`` transform — the coordinates that
    decide whether the object is on the bed.  (:func:`compute_mesh_bbox`
    routes .3mf here, so both report placed geometry.)  Slicers honour a
    3MF's transforms literally (no STL-style auto-centre), so a file
    whose vertices span negative coordinates under an identity transform
    is off a corner-origin bed even though its raw mesh "fits".

    Handles ``<components>`` recursion with composed transforms, follows
    the production extension's ``p:path`` into the other model parts a
    BambuStudio / OrcaSlicer project keeps its meshes in, and scales by
    the model ``unit`` — the walk is :class:`kiln.threemf_parser._ModelArchive`,
    shared with every other 3MF reader.  Returns ``None`` when the archive
    has no parseable geometry — callers treat that as "check skipped",
    never as a failure.
    """
    from kiln.threemf_parser import (
        _CORE_NS,
        _apply_3mf_transform,
        _ModelArchive,
        _parse_vertices,
    )

    points: list[tuple[float, float, float]] = []
    errors: list[str] = []
    try:
        with zipfile.ZipFile(threemf_path) as zf:
            archive = _ModelArchive(zf, errors)
            to_mm = archive.root_transform_mm()
            if to_mm is None:
                logger.warning(
                    "compute_3mf_geometry_bbox skipped %s: %s",
                    threemf_path, "; ".join(errors),
                )
                return None
            for placed in archive.placements(root_transform=to_mm):
                mesh_el = placed.element.find(f"{{{_CORE_NS}}}mesh")
                if mesh_el is None:
                    continue
                points.extend(
                    _apply_3mf_transform(placed.transform, v)
                    for v in _parse_vertices(mesh_el)
                )
    except (zipfile.BadZipFile, ValueError, KeyError, OSError) as exc:
        logger.warning("compute_3mf_geometry_bbox failed for %s: %s",
                       threemf_path, exc)
        return None
    if errors:
        logger.warning(
            "compute_3mf_geometry_bbox read %s with trouble: %s",
            threemf_path, "; ".join(errors),
        )

    if not points:
        return None
    return {
        "x_min": min(p[0] for p in points), "x_max": max(p[0] for p in points),
        "y_min": min(p[1] for p in points), "y_max": max(p[1] for p in points),
        "z_min": min(p[2] for p in points), "z_max": max(p[2] for p in points),
    }


# ---------------------------------------------------------------------------
# The fit check
# ---------------------------------------------------------------------------

# Margin in mm — coords this far INTO the bed from the edge are treated
# as fitting.  Prevents floating-point noise from rejecting a mesh whose
# x_max is, say, 256.0000001.
_FIT_EPSILON_MM = 0.5


def check_bed_fit(
    bbox: dict[str, float] | None,
    build_volume: tuple[float, float, float] | None,
    *,
    source: str = "mesh",
) -> dict[str, Any]:
    """Evaluate whether a bbox fits inside a build volume.

    Args:
        bbox: Bounding box dict (x_min/x_max/y_min/y_max/z_min/z_max).
            May be None if extraction failed — we return ok=True with
            a warning in that case (don't block on missing data).
        build_volume: (x, y, z) mm.  May be None for unknown printers —
            we return ok=True in that case.
        source: "mesh" | "gcode" | "3mf" — affects the error message.

    Returns:
        Dict with:
          - ``ok``: True when the geometry fits.
          - ``error_code``: One of BBOX_UNKNOWN, VOLUME_UNKNOWN,
            EXCEEDS_BED, OFF_BED_GEOMETRY, None (when ok).
          - ``error_message``: Human-readable description.
          - ``bbox``: The bbox that was checked (or None).
          - ``build_volume``: The volume that was checked (or None).
          - ``suggested_translate``: [dx, dy, dz] that would center
            the bbox on the bed — None when not applicable.
    """
    result: dict[str, Any] = {
        "ok": True,
        "error_code": None,
        "error_message": None,
        "bbox": bbox,
        "build_volume": build_volume,
        "suggested_translate": None,
    }
    if bbox is None:
        result["ok"] = True  # don't block on parse failure
        result["error_code"] = "BBOX_UNKNOWN"
        result["error_message"] = f"Could not extract bbox from {source}."
        return result
    if build_volume is None:
        result["ok"] = True  # unknown printer — allow
        result["error_code"] = "VOLUME_UNKNOWN"
        result["error_message"] = (
            "Printer build volume unknown — bed-fit check skipped."
        )
        return result

    bed_x, bed_y, bed_z = build_volume
    dx = bbox["x_max"] - bbox["x_min"]
    dy = bbox["y_max"] - bbox["y_min"]
    dz = bbox["z_max"] - bbox["z_min"]

    # Check 1: fundamentally too big (cannot be fixed by translation)
    if dx > bed_x + _FIT_EPSILON_MM or dy > bed_y + _FIT_EPSILON_MM \
            or dz > bed_z + _FIT_EPSILON_MM:
        result["ok"] = False
        result["error_code"] = "EXCEEDS_BED"
        result["error_message"] = (
            f"Geometry ({dx:.1f}×{dy:.1f}×{dz:.1f}mm) exceeds the printer's "
            f"build volume ({bed_x:g}×{bed_y:g}×{bed_z:g}mm). "
            f"Rescale with rescale_model() or split the model."
        )
        return result

    # Check 2: mis-positioned (negative X/Y or past the far edge).
    # Z under 0 is OK if it's epsilon noise but rejected if significant.
    on_bed = (
        bbox["x_min"] >= -_FIT_EPSILON_MM
        and bbox["x_max"] <= bed_x + _FIT_EPSILON_MM
        and bbox["y_min"] >= -_FIT_EPSILON_MM
        and bbox["y_max"] <= bed_y + _FIT_EPSILON_MM
        and bbox["z_min"] >= -_FIT_EPSILON_MM
        and bbox["z_max"] <= bed_z + _FIT_EPSILON_MM
    )
    if on_bed:
        return result  # ok=True

    # Off-bed.  Compute the suggested translation.
    cx = (bbox["x_min"] + bbox["x_max"]) / 2.0
    cy = (bbox["y_min"] + bbox["y_max"]) / 2.0
    tx = (bed_x / 2.0) - cx
    ty = (bed_y / 2.0) - cy
    tz = -bbox["z_min"]  # lift so z_min becomes 0
    result["ok"] = False
    result["error_code"] = "OFF_BED_GEOMETRY"
    result["error_message"] = (
        f"{source.capitalize()} bbox "
        f"X[{bbox['x_min']:.1f}..{bbox['x_max']:.1f}] "
        f"Y[{bbox['y_min']:.1f}..{bbox['y_max']:.1f}] "
        f"Z[{bbox['z_min']:.1f}..{bbox['z_max']:.1f}] "
        f"falls outside the printer's bed "
        f"({bed_x:g}×{bed_y:g}×{bed_z:g}mm, origin at corner). "
        f"Call center_model_on_bed(bed_x_mm={bed_x:g}, bed_y_mm={bed_y:g}) "
        f"first, or pass auto_center=True to the slicer. "
        f"Without this, the nozzle will drive to negative coordinates "
        f"and may crash into the printer frame."
    )
    result["suggested_translate"] = [tx, ty, tz]
    return result


# ---------------------------------------------------------------------------
# High-level validators (use these from MCP tools)
# ---------------------------------------------------------------------------

def validate_mesh_for_printer(
    mesh_path: str, printer_id: str | None,
) -> dict[str, Any]:
    """Validate a mesh (STL/OBJ/3MF geometry) against a printer's bed.

    Used by slice_model / slice_and_print / reslice_with_overrides as
    a pre-slice gate.
    """
    bbox = compute_mesh_bbox(mesh_path)
    volume = get_build_volume(printer_id) if printer_id else None
    return check_bed_fit(bbox, volume, source="mesh")


def validate_gcode_for_printer(
    gcode_path: str, printer_id: str | None,
) -> dict[str, Any]:
    """Validate a gcode file's X/Y move range against a printer's bed.

    Used as a secondary / last-line check when the mesh is unavailable
    (e.g. custom gcode uploaded by the user).
    """
    bbox = compute_gcode_bbox(gcode_path)
    volume = get_build_volume(printer_id) if printer_id else None
    fit = check_bed_fit(bbox, volume, source="gcode")
    if fit["ok"]:
        homing = check_gcode_has_homing(gcode_path, source="gcode")
        if not homing["ok"]:
            return homing
    return fit


def validate_3mf_for_printer(
    threemf_path: str, printer_id: str | None,
) -> dict[str, Any]:
    """Validate a .gcode.3mf file's embedded gcode against a printer's bed.

    Used by start_print as the last-line gate before the 3MF is sent
    to the printer over FTPS.
    """
    bbox = compute_3mf_bbox(threemf_path)
    volume = get_build_volume(printer_id) if printer_id else None
    fit = check_bed_fit(bbox, volume, source="3mf")
    # Also run the homing-sequence check — separate bug class from bbox.
    if fit["ok"]:
        homing = check_gcode_has_homing(threemf_path, source="3mf")
        if not homing["ok"]:
            return homing  # promote homing failure to the top-level error
    return fit


def verify_3mf_is_safe_to_print(
    threemf_path: str, printer_id: str | None,
) -> dict[str, Any]:
    """Comprehensive safety verification for a 3MF about to be sent to
    a printer.  Runs BOTH checks — bed-fit AND homing — and returns
    a structured result even when everything passes, so callers can
    surface "verified safe" in their response dicts.

    This is the authoritative "is this 3MF safe" check — use it from
    any tool that emits a final 3MF for printer consumption.
    """
    bbox = compute_3mf_bbox(threemf_path)
    volume = get_build_volume(printer_id) if printer_id else None
    fit = check_bed_fit(bbox, volume, source="3mf")
    homing = check_gcode_has_homing(threemf_path, source="3mf")
    checks: list[dict[str, Any]] = []
    checks.append({
        "name": "bed_fit",
        "ok": fit["ok"] or fit["error_code"] in ("BBOX_UNKNOWN", "VOLUME_UNKNOWN"),
        "detail": fit,
    })
    checks.append({
        "name": "homing_sequence",
        "ok": homing["ok"] or homing["error_code"] == "UNKNOWN_FILE",
        "detail": homing,
    })
    failed = [c for c in checks if not c["ok"]]
    return {
        "ok": len(failed) == 0,
        "checks": checks,
        "failed": [c["name"] for c in failed],
        "error_code": failed[0]["detail"]["error_code"] if failed else None,
        "error_message": failed[0]["detail"]["error_message"] if failed else None,
    }


# ---------------------------------------------------------------------------
# Homing-sequence safety check (NEW — root cause of incident #0)
# ---------------------------------------------------------------------------

def check_gcode_has_homing(
    path: str, *, source: str = "gcode",
) -> dict[str, Any]:
    """Verify that a gcode / .3mf file contains a homing sequence (G28)
    BEFORE its first print move.

    Incident #0 (2026-04-15) — the real root cause (identified after
    initial off-bed-geometry hypothesis was ruled out): the 3MF sent to
    the Bambu A1 had NO ``G28`` (homing) and NO Bambu start-gcode
    (``M620`` AMS load, purge line, bed-leveling).  After heat-soak, the
    gcode executed ``G1 Z0.4`` with no homing reference — the printer
    assumed whatever stale internal position the previous job left it
    in, and plunged the nozzle downward into the purge tool.

    This check catches any file that tries to issue print moves without
    a prior homing command.  Complementary to the bed-fit check — a
    properly-centered gcode WITHOUT homing is just as dangerous as
    off-bed geometry.
    """
    from pathlib import Path
    p = Path(path)
    if not p.is_file():
        return {
            "ok": True, "error_code": "UNKNOWN_FILE", "error_message": None,
        }
    # Extract gcode text (from .gcode directly, or from .3mf zip)
    gcode_text: str | None = None
    if p.suffix.lower() == ".3mf" or str(p).lower().endswith(".gcode.3mf"):
        try:
            with zipfile.ZipFile(p) as zf:
                gcode_names = [
                    n for n in zf.namelist()
                    if n.startswith("Metadata/plate_") and n.endswith(".gcode")
                ]
                if gcode_names:
                    gcode_text = zf.read(gcode_names[0]).decode(
                        "utf-8", errors="replace",
                    )
        except (zipfile.BadZipFile, KeyError):
            pass
    else:
        with contextlib.suppress(OSError):
            gcode_text = p.read_text(encoding="utf-8", errors="replace")
    if gcode_text is None:
        return {
            "ok": True, "error_code": "UNKNOWN_FILE", "error_message": None,
        }

    # Find first PRINT move (after LAYER_CHANGE marker if present) and
    # first G28 homing.  If the gcode has no LAYER_CHANGE markers
    # (PrusaSlicer/Orca/BambuStudio convention) we fall back to
    # "first G1 with X/Y and E" — but the LAYER_CHANGE path is more
    # accurate because purge/wipe moves in start-gcode use G1 with
    # X/Y too, and those are legitimate post-home motion.
    first_home = -1
    first_print_move = -1
    in_print_region = False
    lines = gcode_text.split("\n")
    has_layer_marker = any(
        line.strip().startswith((";LAYER_CHANGE", ";LAYER:", ";TYPE:"))
        for line in lines[:min(len(lines), 5000)]
    )
    if not has_layer_marker:
        in_print_region = True  # scan everything
    for i, line in enumerate(lines):
        stripped_raw = line.strip()
        stripped = line.split(";", 1)[0].strip()
        if not in_print_region:
            if stripped_raw.startswith((";LAYER_CHANGE", ";LAYER:", ";TYPE:")):
                in_print_region = True
            # Still track homing that happens in the start-gcode region
            if stripped.startswith("G28") and first_home < 0:
                first_home = i
            continue
        if stripped.startswith("G28") and first_home < 0:
            first_home = i
            continue
        if (
            stripped.startswith("G1 ")
            and " E" in stripped
            and (" X" in stripped or " Y" in stripped)
            and first_print_move < 0
        ):
            first_print_move = i
            break
    if first_print_move < 0:
        return {"ok": True, "error_code": None, "error_message": None}
    if first_home < 0 or first_home > first_print_move:
        return {
            "ok": False,
            "error_code": "NO_HOMING_SEQUENCE",
            "error_message": (
                f"{source.capitalize()} file has no G28 (homing) before the "
                f"first print move at line {first_print_move + 1}. "
                f"The printer would execute G1 moves without a known position "
                f"reference, likely crashing the nozzle into the printer frame "
                f"or bed (incident #0 class).  Re-slice through slice_and_print "
                f"(which uses the adapter's wrap_gcode_as_3mf that adds Bambu "
                f"start-gcode), or manually wrap the gcode via "
                f"wrap_gcode_as_3mf() with the correct printer profile."
            ),
        }
    return {"ok": True, "error_code": None, "error_message": None}


def apply_translation_to_stl(
    stl_path: str, translate: list[float], output_path: str | None = None,
) -> str:
    """Apply a translation to an STL file in-place (or to output_path).

    Used by slicer tools when auto_center=True and the bbox is off-bed.
    Returns the output path.
    """
    from kiln.generation.validation import _parse_stl, _write_binary_stl

    path = Path(stl_path)
    errors: list[str] = []
    triangles, _vertices = _parse_stl(path, errors)
    if errors:
        raise ValueError(f"Failed to parse STL: {'; '.join(errors)}")
    tx, ty, tz = translate
    translated = [
        tuple((v[0] + tx, v[1] + ty, v[2] + tz) for v in tri)
        for tri in triangles
    ]
    out = output_path or str(path)
    _write_binary_stl(translated, out)
    return out
