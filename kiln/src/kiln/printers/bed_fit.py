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

from kiln import step_import
from kiln.gcode import _MAX_SCAN_BYTES, GCODE_NUMBER, axis_value
from kiln.gcode_metadata import read_member_text, sliced_gcode_member

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
    resolved = resolve_build_volume(printer_id)
    return resolved[1] if resolved else None


def resolve_build_volume_printer_id(printer_id: str | None) -> str | None:
    """Return the canonical id that provided a known build volume."""
    resolved = resolve_build_volume(printer_id)
    return resolved[0] if resolved else None


def resolve_build_volume(
    printer_id: str | None,
) -> tuple[str, tuple[float, float, float]] | None:
    """Return ``(canonical_printer_id, build_volume_mm)`` if known.

    The catalogue's answer, and only the catalogue's: this is the bed the
    motion planner, the print-start gate and the G-code bounds read, and
    none of them takes a number somebody typed.  A bed its owner stated
    for a printer outside the catalogue is
    :func:`owner_stated_build_volume`, asked for by name.
    """
    for candidate in _printer_id_candidates(printer_id):
        looked_up = _lookup_build_volume_exact(candidate)
        if looked_up is not None:
            return candidate, looked_up
    return None


def owner_stated_build_volume(printer_id: str | None) -> tuple[float, float, float] | None:
    """The bed its owner stated on this machine for a printer the catalogue
    has no row for, or ``None``.

    Never an answer for a catalogue printer, under any spelling: that
    printer's bed is the catalogue's.  Never for the generic row, whose
    name is not a printer's.  The number is the owner's and unverified --
    it can be larger than the machine -- so a caller that passes a verdict
    on it says whose it is (the validation pipeline's resolver does), and
    a caller that only lays a part out on it need not.
    """
    if resolve_build_volume(printer_id) is not None:
        return None
    from kiln.safety_profiles import local_printer_build_volume

    for candidate in _printer_id_candidates(printer_id):
        stated = local_printer_build_volume(candidate) if candidate != "default" else None
        if stated is not None:
            return stated
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
    ``None`` if the file cannot be parsed.  Reads a .stl as one numpy array
    (:func:`kiln.generation.validation.read_stl_triangles`), the
    transform-aware 3MF parser for .3mf files, and falls back to
    trimesh for other formats.

    For a .3mf the bbox is the geometry AS THE SLICER WILL PLACE IT
    (build-item transforms applied), because that is the question every
    caller of this function is asking — slicers honour a 3MF's placement
    literally, so raw vertex bounds would pass a file that slices to
    nothing.

    A STEP file is measured by Kiln's own CAD reader (:func:`_step_bbox`),
    and reported by the same rule: resting at the origin, because both
    slicers lay a STEP file onto the bed themselves.
    """
    path = Path(mesh_path)
    if not path.is_file():
        return None
    if step_import.is_step_file(str(path)):
        return _step_bbox(str(path))
    ext = path.suffix.lower()
    try:
        if ext == ".stl":
            from kiln.generation.validation import read_stl_triangles
            try:
                corners = read_stl_triangles(path).reshape(-1, 3)
            except ValueError:
                return None
            if not len(corners):
                return None
            lo, hi = corners.min(axis=0), corners.max(axis=0)
            return {
                "x_min": float(lo[0]), "x_max": float(hi[0]),
                "y_min": float(lo[1]), "y_max": float(hi[1]),
                "z_min": float(lo[2]), "z_max": float(hi[2]),
            }
        if ext == ".3mf":
            bbox = compute_3mf_geometry_bbox(str(path))
            if bbox is not None:
                return bbox
            # No parseable <mesh> geometry (e.g. a gcode-carrying 3MF)
            # — fall through to trimesh as a last resort.
        # Fallback for .obj / .glb (and unparseable .3mf) via trimesh
        from kiln.mesh_frame import load_mesh
        mesh = load_mesh(str(path), force="mesh")
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
    rf"^G[01]\s+(?:.*(?<![A-Za-z])X(?P<x>{GCODE_NUMBER}))?(?:.*(?<![A-Za-z])Y(?P<y>{GCODE_NUMBER}))?",
    re.MULTILINE,
)


def _step_bbox(step_path: str) -> dict[str, float] | None:
    """A STEP file's exact size, resting at the origin -- or ``None`` when
    this machine cannot read it (no CAD kernel, a file the kernel refuses).

    Until 2026-09-30 a STEP fell through to the mesh library below, which
    needs an add-on Kiln does not install, so every fit check on a STEP file
    passed it unmeasured.  The size is the kernel's, read off the file's own
    geometry and cached by content (:func:`kiln.step_import.read_exact_geometry`).
    The position is not reported: the slicer drops a STEP file onto the bed
    and centres it, so where the CAD file puts it says nothing about where
    it prints.
    """
    exact = step_import.read_exact_geometry(step_path)
    if not exact.available or not exact.size_mm:
        logger.info("No size for %s: %s", os.path.basename(step_path), exact.reason)
        return None
    sx, sy, sz = (float(v) for v in exact.size_mm)
    return {"x_min": 0.0, "x_max": sx, "y_min": 0.0, "y_max": sy, "z_min": 0.0, "z_max": sz}


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
                if axis_value(code, "E") is None:
                    continue
                xv = axis_value(code, "X")
                yv = axis_value(code, "Y")
                if xv is not None:
                    found = True
                    v = xv
                    if v < x_min:
                        x_min = v
                    if v > x_max:
                        x_max = v
                if yv is not None:
                    found = True
                    v = yv
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
    with _plate_gcode_file(threemf_path) as gcode_path:
        return compute_gcode_bbox(gcode_path, max_lines=max_lines) if gcode_path else None


@contextlib.contextmanager
def _plate_gcode_file(threemf_path: str) -> Any:
    """The plate a .gcode.3mf prints, unpacked to a temp G-code file for the
    readers that take a path -- ``None`` when the archive has none.  The
    plate is the one the picker every door uses chooses, read within the
    bound a G-code file gets, and the temp file is gone when the block ends.
    """
    gcode_bytes: bytes | None = None
    if Path(threemf_path).is_file():
        try:
            with zipfile.ZipFile(threemf_path) as zf:
                member = sliced_gcode_member(zf)
                if member is not None:
                    gcode_bytes = read_member_text(zf, member, _MAX_SCAN_BYTES).encode("utf-8")
        except (zipfile.BadZipFile, KeyError, ValueError) as exc:
            logger.warning("No plate read from %s: %s", threemf_path, exc)
    if gcode_bytes is None:
        yield None
        return
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".gcode", delete=False, mode="wb") as tf:
        tf.write(gcode_bytes)
        tmp_path = tf.name
    try:
        yield tmp_path
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
# What prints past the bed
# ---------------------------------------------------------------------------

#: The setting that brings each thing a slicer draws around a part back onto
#: the bed, by its class (:data:`kiln.slicer_geometry.EXTRA_CLASSES`).  A
#: class with none here gets the general remedy: the part further from the
#: edge.
_PAST_THE_BED_REMEDIES: dict[str, str] = {
    "skirt": "no skirt (skirts=0)",
    "brim": "a narrower brim (brim_width)",
    "raft": "a raft that spreads less (raft_first_layer_expansion)",
    "prime_tower": "the prime tower placed on the bed (wipe_tower_x, wipe_tower_y)",
}


def print_past_the_bed(gcode_path: str | None, build_volume: Any) -> dict[str, Any] | None:
    """Whether a sliced file prints past the edge of the bed, and whose moves do.

    The verdict is :func:`check_bed_fit` on :func:`compute_gcode_bbox` -- the
    reading the check before a print makes -- so this never disagrees with
    it.  What it adds is WHOSE moves leave the bed, read off the file's own
    feature labels (:func:`kiln.slicer_geometry.parse_slicer_features`): the
    part's toolpaths, or what the slicer drew around them.  A part within a
    few millimetres of the edge fits while its skirt or brim does not, and
    "rescale or split" is the wrong answer for a part that fits.

    Returns ``None`` when it cannot be told (no bed size, no file, no print
    moves), ``{"on_bed": True}`` when every print move lands on the bed, and
    otherwise ``on_bed`` False with ``past_mm`` (the furthest move past an
    edge), ``part_fits`` (``None`` when the file labels no features, so the
    part cannot be told from what surrounds it), ``part`` (the part's own
    toolpath footprint), ``room_mm`` (from it to the nearest edge) and
    ``added`` (each class the slicer drew whose toolpaths leave the bed).
    """
    if not build_volume or not isinstance(gcode_path, (str, os.PathLike)):
        return None
    printed = compute_gcode_bbox(gcode_path)
    if not printed:
        return None
    bed_x, bed_y = float(build_volume[0]), float(build_volume[1])
    if check_bed_fit(printed, tuple(build_volume), source="gcode")["ok"]:
        return {"on_bed": True}
    past = max(
        -float(printed["x_min"]), float(printed["x_max"]) - bed_x,
        -float(printed["y_min"]), float(printed["y_max"]) - bed_y,
    )
    edge: dict[str, Any] = {
        "on_bed": False, "past_mm": round(past, 1), "part_fits": None, "part": None, "room_mm": None, "added": [],
    }
    from kiln.slicer_geometry import EXTRA_CLASSES, parse_slicer_features

    try:
        parsed = parse_slicer_features(gcode_path)
    except Exception:  # noqa: BLE001 -- an unreadable file says only how far
        logger.debug("No features read from %s", gcode_path, exc_info=True)
        return edge
    if not parsed.labelled or parsed.model_footprint is None:
        return edge

    def _on_bed(x0: float, y0: float, x1: float, y1: float) -> bool:
        return (
            x0 >= -_FIT_EPSILON_MM and y0 >= -_FIT_EPSILON_MM
            and x1 <= bed_x + _FIT_EPSILON_MM and y1 <= bed_y + _FIT_EPSILON_MM
        )

    x0, y0, x1, y1 = parsed.model_footprint
    edge["part"] = {"x_min": x0, "y_min": y0, "x_max": x1, "y_max": y1}
    edge["part_fits"] = _on_bed(x0, y0, x1, y1)
    edge["room_mm"] = round(min(x0, y0, bed_x - x1, bed_y - y1), 1)
    for cls in EXTRA_CLASSES:
        seg = parsed.buckets[cls].segments if cls in parsed.buckets else []
        if not seg:
            continue
        xs, ys = seg[0::6] + seg[3::6], seg[1::6] + seg[4::6]
        if not _on_bed(min(xs), min(ys), max(xs), max(ys)):
            edge["added"].append(cls)
    return edge


def _joined(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def past_the_bed_sentence(edge: dict[str, Any]) -> str:
    """What prints past the edge of the bed and what brings it back, for an
    *edge* from :func:`print_past_the_bed` that is not on the bed.  The cause
    decides the remedy: the part itself, what the slicer drew around it, or
    -- in a file that labels no features -- either.
    """
    past = f"{float(edge['past_mm']):.1f} mm"
    if edge.get("part_fits") is None:
        return (
            f"The file prints {past} past the edge of the bed, and it labels none of its features, so the part "
            "cannot be told from what the slicer drew around it. Slice it again with less around the part "
            "(skirts=0, a narrower brim_width) or with the part further from the edge."
        )
    if not edge["part_fits"]:
        return (
            f"The part itself prints {past} past the edge of the bed. Move it onto the bed (center_model_on_bed, "
            "or slice with auto_center=True), or rescale or split it if it is bigger than the bed."
        )
    from kiln.slicer_geometry import CLASS_LABELS

    names: list[str] = []
    remedies: list[str] = []
    for cls in edge.get("added") or []:
        name = f"the {CLASS_LABELS.get(cls, cls.replace('_', ' ')).lower()}"
        if name not in names:
            names.append(name)
        remedy = _PAST_THE_BED_REMEDIES.get(cls)
        if remedy and remedy not in remedies:
            remedies.append(remedy)
    what = _joined(names) if names else "something the slicer drew around it"
    fix = _joined(remedies) if remedies else "the part further from the edge"
    return (
        f"The file prints {past} past the edge of the bed, and the part itself fits; what leaves the bed is "
        f"{what}. Slice it again with {fix}."
    )


def _say_what_prints_past_the_bed(fit: dict[str, Any], gcode_path: str | None, build_volume: Any) -> None:
    """Word a refusal by its cause, in place.

    A file whose print moves leave the bed is refused, and stays refused --
    extrusion off the plate is never sent (incident #0).  When the part's own
    toolpaths fit, the refusal names what the slicer drew around the part
    instead of telling the person to rescale, split or centre a part that
    fits.  Only the words change: ``ok``, the error code and the bbox stay
    as the check decided, and anything uncertain leaves them as they were.
    """
    if fit.get("ok") or fit.get("error_code") not in ("EXCEEDS_BED", "OFF_BED_GEOMETRY"):
        return
    try:
        edge = print_past_the_bed(gcode_path, build_volume)
    except Exception:  # noqa: BLE001 -- the check's own words stand
        logger.debug("What prints past the bed was not read", exc_info=True)
        return
    if not edge or edge.get("on_bed") or edge.get("part_fits") is not True:
        return
    fit["part_fits_the_bed"] = True
    fit["printed_past_the_bed"] = list(edge["added"])
    fit["error_message"] = f"{past_the_bed_sentence(edge)} Kiln won't send extrusion off the plate."


# ---------------------------------------------------------------------------
# High-level validators (use these from MCP tools)
# ---------------------------------------------------------------------------

def validate_mesh_for_printer(
    mesh_path: str, printer_id: str | None,
) -> dict[str, Any]:
    """Validate a mesh (STL/OBJ/3MF geometry) against a printer's bed.

    Used by slice_model / slice_and_print / reslice_with_overrides as
    a pre-slice gate.

    A STEP file too big as modelled but small enough lying on another face
    is not refused here.  This check measures; it does not turn anything.
    The result carries ``fits_on_another_face`` and says so in ``note``, and
    the slice gate (``_apply_bed_fit_gate``) reads that to lay the part down
    as Kiln's mesh of it.  A caller that does not turn it hands the slicer
    the file as modelled, and the slicer makes the call itself.
    """
    bbox = compute_mesh_bbox(mesh_path)
    volume = get_build_volume(printer_id) if printer_id else None
    fit = check_bed_fit(bbox, volume, source="mesh")
    if (
        fit["error_code"] == "EXCEEDS_BED"
        and step_import.is_step_file(mesh_path)
        and _fits_on_another_face(bbox, volume)
    ):
        fit.update(ok=True, error_code=None, error_message=None, fits_on_another_face=True)
        fit["note"] = (
            "Too big for the bed as modelled, small enough lying on another face. "
            "The slicer gets the file as modelled; turn the part in its CAD file, "
            "or import it (import_step_file) so Kiln can lay it down."
        )
    return fit


def _fits_on_another_face(bbox: dict[str, float], build_volume: tuple[float, float, float]) -> bool:
    """Whether some axis-aligned orientation of *bbox* fits *build_volume*.

    The part's extents and the bed's sides, each sorted, compared pairwise:
    a part fits in some quarter-turn orientation exactly when its smallest
    extent fits the smallest side, its middle the middle and its largest
    the largest.
    """
    extents = sorted(bbox[f"{a}_max"] - bbox[f"{a}_min"] for a in "xyz")
    sides = sorted(float(v) for v in build_volume)
    return all(e <= s + _FIT_EPSILON_MM for e, s in zip(extents, sides))


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
    _say_what_prints_past_the_bed(fit, gcode_path, volume)
    if fit["ok"]:
        homing = check_gcode_has_homing(gcode_path, source="gcode")
        if not homing["ok"]:
            return homing
    return fit


def _check_3mf_plate(threemf_path: str, volume: Any) -> dict[str, Any]:
    """:func:`check_bed_fit` on a .gcode.3mf's plate, its refusal worded by
    its cause (:func:`_say_what_prints_past_the_bed`)."""
    with _plate_gcode_file(threemf_path) as gcode_path:
        fit = check_bed_fit(compute_gcode_bbox(gcode_path) if gcode_path else None, volume, source="3mf")
        _say_what_prints_past_the_bed(fit, gcode_path, volume)
    return fit


def validate_3mf_for_printer(
    threemf_path: str, printer_id: str | None,
) -> dict[str, Any]:
    """Validate a .gcode.3mf file's embedded gcode against a printer's bed.

    Used by start_print as the last-line gate before the 3MF is sent
    to the printer over FTPS.
    """
    volume = get_build_volume(printer_id) if printer_id else None
    fit = _check_3mf_plate(threemf_path, volume)
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
    volume = get_build_volume(printer_id) if printer_id else None
    fit = _check_3mf_plate(threemf_path, volume)
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
                member = sliced_gcode_member(zf)
                if member is not None:
                    gcode_text = read_member_text(zf, member, _MAX_SCAN_BYTES)
        except (zipfile.BadZipFile, KeyError, ValueError):
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
