"""Safe-motion planning for a nozzle over a bed that is NOT empty.

Every mid-print motion — layer resume, pause/park, abort, mid-print
decoration handoff — moves a hot nozzle over a bed that already carries a
partial print.  A start-of-print sequence may assume the bed is clear; a
mid-print sequence may not.  The two firmware behaviours that turn that
assumption into a crash:

- ``G28`` (home ALL axes) descends Z toward the bed at the machine's
  homing XY.  On a Klipper machine with ``[safe_z_home]`` that XY is
  usually bed centre — directly over the part.  On an endstop-homed
  bedslinger it is a fixed corner.  Either way the nozzle is driven
  down with a part in the workspace.
- Bed probing (``G29`` / Klipper ``BED_MESH_CALIBRATE``) probes wherever
  the mesh says — impossible with a print on the plate.

The proven-safe shape (the Bambu resume preamble in
:mod:`kiln.printers.bambu_3mf`, in production since the mid-print
decoration feature shipped) is::

    heat (non-blocking) -> relative Z lift -> home X/Y ONLY ->
    wait for temps -> travel at safe Z -> optional prime ->
    descend to resume Z

This module is the one place that shape lives.  Callers — the recovery
gcode generator, the firmware-resume adapters, mid-print decoration —
build their sequences from these helpers instead of hand-writing G28s.

Two invariants every sequence built here satisfies:

1. No command ever homes Z (no bare ``G28``, no ``G28 Z``).  The Z the
   machine already trusts is the only Z that is provably above the part.
2. A relative Z lift precedes the first homing or XY travel command.
   Mid-print callers hold the precondition that the nozzle sits at or
   above the highest printed layer (it was just printing there), so a
   relative lift of ``+clearance`` provably clears the part even when
   nothing else about the machine is known.

Occupancy is modelled as the XY bounding box of the job's OWN gcode
moves (:func:`kiln.printers.bed_fit.compute_gcode_bbox` and friends)
plus a margin.  A bbox over-approximates the real footprint — which is
the correct direction for "do not go here".  Never treat the complement
of a *guessed* region as free space: when this module cannot prove a
point is outside the occupied region it refuses (``ok=False``) instead
of answering with a coordinate.
"""
from __future__ import annotations

import contextlib
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# Default clearance for the pre-home relative lift.  Matches the Bambu
# resume preamble's Z+5 safety lift.
DEFAULT_LIFT_MM = 5.0
# Default margin added around a job's bbox when deciding where the
# nozzle may park or travel.
DEFAULT_OCCUPIED_MARGIN_MM = 10.0
# How far inside the bed rectangle a park corner is placed.
DEFAULT_EDGE_INSET_MM = 5.0

# Line cap for occupancy scans.  Far above any realistic sliced file
# (a multi-day 100MB print is ~2.5M lines); a scan that still hits this
# is treated as unparseable, because a PARTIAL footprint under-covers
# the plate — the one direction occupancy must never err in.
_OCCUPANCY_MAX_LINES = 5_000_000

_LIFT_FEEDRATE = 600  # mm/min — deliberate, not a travel move
_TRAVEL_FEEDRATE = 6000

# Any G28 that names Z, or names no axis at all, descends Z.
_G28_RE = re.compile(r"^\s*G28(?!\d)(?P<rest>[^;]*)", re.IGNORECASE)


def homes_z(command: str) -> bool:
    """True if a gcode command would home the Z axis.

    ``G28`` with no axis words homes everything; ``G28 Z`` (in any
    combination) homes Z.  ``G28 X Y`` does not.
    """
    m = _G28_RE.match(command)
    if not m:
        return False
    rest = m.group("rest").upper()
    axes = set(re.findall(r"[XYZ]", rest))
    return not axes or "Z" in axes


def first_homing_index(commands: list[str]) -> int | None:
    """Index of the first G28 in *commands*, or ``None``."""
    for i, cmd in enumerate(commands):
        if _G28_RE.match(cmd):
            return i
    return None


def check_mid_print_sequence(commands: list[str]) -> list[str]:
    """Validate a mid-print gcode sequence against the two invariants.

    Returns a list of violation strings (empty = clean).  Used by tests
    and available to any door that assembles a sequence by hand.

    "Lift" is counted strictly: a POSITIVE relative Z move (inside
    G91).  An absolute Z move before homing proves nothing — with an
    untrusted position it could just as well be a descent.
    """
    violations: list[str] = []
    lift_seen = False
    relative = False
    for i, cmd in enumerate(commands):
        stripped = cmd.split(";", 1)[0].strip().upper()
        if not stripped:
            continue
        if homes_z(stripped):
            violations.append(
                f"line {i}: {cmd.strip()!r} homes Z with a part on the bed"
            )
        if stripped.startswith("G91"):
            relative = True
        elif stripped.startswith("G90"):
            relative = False
        if relative and re.match(r"^G[01]\b", stripped):
            zm = re.search(r"\bZ(-?\d+\.?\d*)", stripped)
            if zm and float(zm.group(1)) > 0:
                lift_seen = True
        if _G28_RE.match(stripped) and not lift_seen:
            violations.append(
                f"line {i}: {cmd.strip()!r} homes before any relative Z lift"
            )
    return violations


# ---------------------------------------------------------------------------
# Occupied region
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class OccupiedRegion:
    """XY rectangle the partial print occupies, margin included.

    The rectangle is a deliberate over-approximation of the part's real
    footprint: correct for "keep out", never valid as "everything
    outside my *unmargined* bbox is free".
    """

    x_min: float
    x_max: float
    y_min: float
    y_max: float
    margin_mm: float
    z_top_mm: float | None = None
    source: str = "unknown"
    # Object label when the region came from a per-object declaration
    # (Klipper exclude-object).  Lets a caller aim AT one object (a
    # mid-print decoration target) while avoiding the others.
    name: str | None = None

    def contains(self, x: float, y: float) -> bool:
        """True if (x, y) falls inside the region INCLUDING margin."""
        return (
            self.x_min - self.margin_mm <= x <= self.x_max + self.margin_mm
            and self.y_min - self.margin_mm <= y <= self.y_max + self.margin_mm
        )


# Klipper/Moonraker exclude-object declaration, emitted by PrusaSlicer,
# OrcaSlicer and SuperSlicer when object labelling is on:
#   EXCLUDE_OBJECT_DEFINE NAME=part_1 CENTER=110,110 POLYGON=[[95,95],[125,95],...]
_EXCLUDE_OBJECT_RE = re.compile(
    r"^EXCLUDE_OBJECT_DEFINE\s+(?P<params>.*)$", re.IGNORECASE
)
_NAME_PARAM_RE = re.compile(r"\bNAME=(?P<name>\S+)", re.IGNORECASE)
_POLYGON_PARAM_RE = re.compile(r"\bPOLYGON=(?P<poly>\[\[.*?\]\])", re.IGNORECASE)

# Per-object print-block markers, one dialect per slicer family.  A line
# matching a start pattern opens an object's block; moves until the
# matching end line belong to that object.  Order matters: Bambu/Orca's
# "; start printing object, id: N" must be tried before PrusaSlicer's
# "; printing object NAME id:0 copy 0" would ever see it.
_OBJECT_START_RES = (
    re.compile(r"^EXCLUDE_OBJECT_START\s+NAME=(?P<name>\S+)", re.IGNORECASE),
    re.compile(r"^;\s*start printing object\s*,?\s*id:\s*(?P<name>\S+)", re.IGNORECASE),
    re.compile(r"^;\s*printing object\s+(?P<name>.+?)\s*$", re.IGNORECASE),
)
_OBJECT_END_RES = (
    re.compile(r"^EXCLUDE_OBJECT_END\b", re.IGNORECASE),
    re.compile(r"^;\s*stop printing object\b", re.IGNORECASE),
)

_MOVE_X_RE = re.compile(r"\bX(-?\d+\.?\d*)")
_MOVE_Y_RE = re.compile(r"\bY(-?\d+\.?\d*)")
_MOVE_Z_RE = re.compile(r"\bZ(-?\d+\.?\d*)")
_MOVE_E_RE = re.compile(r"\bE(-?\d+\.?\d*)")


def _job_gcode_lines(job_path: str):
    """Iterator over the job's gcode lines, or ``None`` for non-gcode files.

    ``.gcode`` streams the file; ``.3mf`` yields the first embedded
    ``Metadata/plate_*.gcode`` (the Bambu layout).
    """
    import io
    import zipfile

    path = Path(job_path)
    ext = path.suffix.lower()
    if ext == ".gcode":
        return open(path, encoding="utf-8", errors="replace")
    if ext == ".3mf":
        try:
            with zipfile.ZipFile(path) as zf:
                names = [
                    n for n in zf.namelist()
                    if n.startswith("Metadata/plate_") and n.endswith(".gcode")
                ]
                if not names:
                    return None
                data = zf.read(names[0]).decode("utf-8", errors="replace")
            return io.StringIO(data)
        except (zipfile.BadZipFile, KeyError, OSError):
            return None
    return None


def occupied_regions_for_job(
    job_path: str,
    *,
    margin_mm: float = DEFAULT_OCCUPIED_MARGIN_MM,
    stop_after_line: int | None = None,
) -> list[OccupiedRegion] | None:
    """Per-object occupied regions, with per-object heights where the
    gcode reveals them.

    ``stop_after_line`` bounds the scan to the first N lines, so each
    region describes the job only up to that point rather than the whole
    file.  A bounded scan is deliberately not treated as truncation:
    stopping where the caller asked is the answer, not a partial one.

    Two sources, merged per object name:

    - ``EXCLUDE_OBJECT_DEFINE`` polygons (footprint declarations) —
      bbox of the declared polygon, a safe over-approximation.
    - Per-object print blocks (``EXCLUDE_OBJECT_START/END``, Bambu/Orca
      ``; start/stop printing object``, PrusaSlicer ``; printing
      object``) — the extruding moves inside a block give that object's
      observed footprint AND its top Z in the file.  The file's top is
      at or above the part's current top mid-print, so a fly-over
      planned against it can only over-clear.

    Falls back to a single whole-job region when the file declares no
    objects.  Returns ``None`` when occupancy cannot be derived — which
    callers must treat as "unknown", never "clear".  A scan that hits
    the line cap also returns ``None``: a partial footprint under-covers
    the plate.
    """
    path = Path(job_path)
    if not path.is_file():
        return None

    lines = _job_gcode_lines(job_path)
    define_boxes: dict[str, tuple[float, float, float, float]] = {}
    observed: dict[str, dict[str, float]] = {}
    truncated = False
    if lines is not None:
        try:
            current: str | None = None
            relative = False
            last_z: float | None = None
            anon = 0
            for i, line in enumerate(lines):
                if stop_after_line is not None and i >= stop_after_line:
                    break  # asked-for stop, NOT truncation
                if i > _OCCUPANCY_MAX_LINES:
                    truncated = True
                    break
                stripped = line.strip()
                if not stripped:
                    continue
                first = stripped[0]
                if first in ";Ee":  # comment or EXCLUDE_OBJECT_*
                    matched = False
                    for pat in _OBJECT_START_RES:
                        m = pat.match(stripped)
                        if m:
                            name = m.group("name").strip()
                            if not name:
                                anon += 1
                                name = f"object_{anon}"
                            current = name
                            matched = True
                            break
                    if matched:
                        continue
                    for pat in _OBJECT_END_RES:
                        if pat.match(stripped):
                            current = None
                            break
                    m = _EXCLUDE_OBJECT_RE.match(stripped)
                    if m:
                        params = m.group("params")
                        poly_m = _POLYGON_PARAM_RE.search(params)
                        if poly_m:
                            try:
                                import json

                                pts = json.loads(poly_m.group("poly"))
                                xs = [float(p[0]) for p in pts]
                                ys = [float(p[1]) for p in pts]
                            except (ValueError, TypeError, IndexError):
                                xs = ys = []
                            if xs and ys:
                                name_m = _NAME_PARAM_RE.search(params)
                                anon += 0 if name_m else 1
                                key = (
                                    name_m.group("name")
                                    if name_m
                                    else f"object_{anon}"
                                )
                                define_boxes[key] = (
                                    min(xs), max(xs), min(ys), max(ys),
                                )
                    continue
                if not (stripped.startswith("G0") or stripped.startswith("G1")):
                    if stripped.startswith("G91"):
                        relative = True
                    elif stripped.startswith("G90"):
                        relative = False
                    continue
                code = stripped.split(";", 1)[0]
                zm = _MOVE_Z_RE.search(code)
                if zm and not relative:
                    last_z = float(zm.group(1))
                if current is None:
                    continue
                if _MOVE_E_RE.search(code) is None:
                    continue
                xm = _MOVE_X_RE.search(code)
                ym = _MOVE_Y_RE.search(code)
                if xm is None and ym is None:
                    continue
                box = observed.setdefault(
                    current,
                    {
                        "x_min": float("inf"), "x_max": float("-inf"),
                        "y_min": float("inf"), "y_max": float("-inf"),
                        "z_max": float("-inf"),
                    },
                )
                if xm:
                    v = float(xm.group(1))
                    box["x_min"] = min(box["x_min"], v)
                    box["x_max"] = max(box["x_max"], v)
                if ym:
                    v = float(ym.group(1))
                    box["y_min"] = min(box["y_min"], v)
                    box["y_max"] = max(box["y_max"], v)
                if last_z is not None:
                    box["z_max"] = max(box["z_max"], last_z)
        except OSError as exc:
            logger.warning(
                "occupied_regions_for_job failed for %s: %s", job_path, exc
            )
            return None
        finally:
            with contextlib.suppress(Exception):
                lines.close()

    if truncated:
        logger.warning(
            "occupied_regions_for_job: scan truncated for %s — "
            "treating occupancy as unknown", job_path,
        )
        return None

    regions: list[OccupiedRegion] = []
    for name in sorted(set(define_boxes) | set(observed)):
        seen = observed.get(name)
        declared = define_boxes.get(name)
        boxes = []
        if seen is not None and seen["x_min"] <= seen["x_max"]:
            boxes.append((seen["x_min"], seen["x_max"], seen["y_min"], seen["y_max"]))
        if declared is not None:
            boxes.append(declared)
        if not boxes:
            continue
        z_top = None
        if seen is not None and seen["z_max"] > float("-inf"):
            z_top = seen["z_max"]
        regions.append(
            OccupiedRegion(
                x_min=min(b[0] for b in boxes),
                x_max=max(b[1] for b in boxes),
                y_min=min(b[2] for b in boxes),
                y_max=max(b[3] for b in boxes),
                margin_mm=margin_mm,
                z_top_mm=z_top,
                source="object_markers" if seen is not None else "exclude_object",
                name=name,
            )
        )
    if regions:
        return regions
    single = occupied_region_for_job(job_path, margin_mm=margin_mm)
    return [single] if single is not None else None


def occupied_region_for_job(
    job_path: str,
    *,
    margin_mm: float = DEFAULT_OCCUPIED_MARGIN_MM,
    z_top_mm: float | None = None,
) -> OccupiedRegion | None:
    """Occupied XY region derived from the job artifact's own moves.

    Accepts sliced gcode (``.gcode``), a Bambu gcode-3MF (``.3mf`` with
    embedded plate gcode), or a mesh file.  Returns ``None`` when the
    file cannot be parsed — callers must treat ``None`` as "occupancy
    unknown", i.e. refuse to plan XY targets, not as "bed clear".
    """
    from kiln.printers import bed_fit

    path = Path(job_path)
    if not path.is_file():
        return None
    ext = path.suffix.lower()
    bbox = None
    source = ""
    if ext == ".gcode":
        bbox = bed_fit.compute_gcode_bbox(job_path, max_lines=_OCCUPANCY_MAX_LINES)
        source = "gcode_bbox"
    elif ext == ".3mf":
        bbox = bed_fit.compute_3mf_bbox(job_path, max_lines=_OCCUPANCY_MAX_LINES)
        source = "3mf_gcode_bbox"
        if bbox is None:
            bbox = bed_fit.compute_mesh_bbox(job_path)
            source = "3mf_geometry_bbox"
    else:
        bbox = bed_fit.compute_mesh_bbox(job_path)
        source = "mesh_bbox"
    if bbox is None:
        return None
    if bbox.get("truncated"):
        # The scan stopped before the end of the file: later moves are
        # unseen, so this bbox may UNDER-cover the plate.  For occupancy
        # an incomplete footprint is worse than none — refuse.
        logger.warning(
            "occupied_region_for_job: bbox scan truncated for %s — "
            "treating occupancy as unknown", job_path,
        )
        return None
    return OccupiedRegion(
        x_min=bbox["x_min"],
        x_max=bbox["x_max"],
        y_min=bbox["y_min"],
        y_max=bbox["y_max"],
        margin_mm=margin_mm,
        z_top_mm=z_top_mm if z_top_mm is not None else (bbox.get("z_max") or None),
        source=source,
    )


def regions_not_cleared_at(
    regions: list[OccupiedRegion] | None,
    travel_z_mm: float,
    *,
    clearance_mm: float = 0.0,
    ignore_names: set[str] | None = None,
) -> list[OccupiedRegion] | None:
    """Regions a nozzle at ``travel_z_mm`` would NOT provably fly over.

    A region is cleared only when its ``z_top_mm`` is KNOWN and sits
    below ``travel_z_mm - clearance_mm``.  An unknown height is never
    cleared — proof, not optimism.  Returns ``None`` when *regions* is
    ``None`` (occupancy unknown), which callers must distinguish from an
    empty list (occupancy known, everything cleared).
    """
    if regions is None:
        return None
    blocked: list[OccupiedRegion] = []
    for region in regions:
        if ignore_names and region.name in ignore_names:
            continue
        top = region.z_top_mm
        if top is None or top >= travel_z_mm - clearance_mm:
            blocked.append(region)
    return blocked


# ---------------------------------------------------------------------------
# Homing behaviour (per-machine, not per-family)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class HomingBehavior:
    """Where and how this machine homes — read from ITS config, not its brand.

    ``style``:

    - ``"safe_z_home"``: Klipper ``[safe_z_home]`` — Z homes at a fixed
      configured XY (``home_xy``), often bed centre.
    - ``"probe_in_place"``: Z homes via probe wherever the head currently
      is (Klipper ``[probe]``/``[bltouch]`` as virtual endstop, no
      safe_z_home).
    - ``"endstop"``: physical Z endstop; Z homes at the endstop corner.
    - ``"unknown"``: no config available — assume nothing.
    """

    style: str = "unknown"
    home_xy: tuple[float, float] | None = None
    notes: list[str] = field(default_factory=list)


def _config_section(config: dict | None, name: str) -> dict | None:
    """Case-insensitive section lookup in a parsed Klipper config."""
    if not isinstance(config, dict):
        return None
    for key, value in config.items():
        if key.strip().lower() == name and isinstance(value, dict):
            return value
    return None


def bed_rect_from_config(
    config: dict | None,
) -> tuple[float, float, float, float] | None:
    """Usable XY rectangle read from the machine's OWN Klipper config.

    Returns ``(x_min, x_max, y_min, y_max)`` from ``stepper_x`` /
    ``stepper_y`` ``position_min``/``position_max`` (min defaults to 0,
    as Klipper does), or ``None`` when the config doesn't state both
    maxima.  This is how a printer that isn't in the catalogue — or one
    with a negative-X wipe area — still gets provable planning: its own
    firmware config is the primary source.
    """
    sx = _config_section(config, "stepper_x")
    sy = _config_section(config, "stepper_y")
    if sx is None or sy is None:
        return None
    try:
        x_max = float(str(sx["position_max"]).strip())
        y_max = float(str(sy["position_max"]).strip())
        x_min = float(str(sx.get("position_min", 0.0)).strip() or 0.0)
        y_min = float(str(sy.get("position_min", 0.0)).strip() or 0.0)
    except (KeyError, ValueError, TypeError):
        return None
    if x_max <= x_min or y_max <= y_min:
        return None
    return (x_min, x_max, y_min, y_max)


def home_xy_from_config(
    config: dict | None,
) -> tuple[float, float] | None:
    """Where ``G28 X Y`` leaves the toolhead, read from the machine's OWN
    config (``stepper_x``/``stepper_y`` ``position_endstop``).

    Machines home at whichever end their endstops sit — origin corner on
    most bedslingers, back-right on many CoreXY builds — so this is a
    per-machine fact, never a brand assumption.  ``None`` when the config
    doesn't state both endstops; callers must then treat the post-home
    position as unknown and skip any move that needs it proven.
    """
    sx = _config_section(config, "stepper_x")
    sy = _config_section(config, "stepper_y")
    if sx is None or sy is None:
        return None
    try:
        hx = float(str(sx["position_endstop"]).strip())
        hy = float(str(sy["position_endstop"]).strip())
    except (KeyError, ValueError, TypeError):
        return None
    return (hx, hy)


def _resolve_bed_rect(
    printer_id: str | None,
    bed_rect: tuple[float, float, float, float] | None,
) -> tuple[float, float, float, float] | None:
    """The planning rectangle: explicit override first, then catalogue."""
    if bed_rect is not None:
        return bed_rect
    from kiln.printers import bed_fit

    volume = bed_fit.get_build_volume(printer_id)
    if volume is None:
        return None
    return (0.0, volume[0], 0.0, volume[1])


def analyze_homing_config(config: dict | None) -> HomingBehavior:
    """Derive homing behaviour from a parsed Klipper config.

    *config* is the mapping returned by
    :meth:`MoonrakerAdapter.get_printer_config` — one entry per
    ``printer.cfg`` section with string values.  ``None`` (config not
    available) yields ``style="unknown"``.
    """
    if not isinstance(config, dict):
        return HomingBehavior()

    def _section(name: str) -> dict | None:
        return _config_section(config, name)

    notes: list[str] = []
    safe_z = _section("safe_z_home")
    if safe_z is not None:
        home_xy: tuple[float, float] | None = None
        raw = str(safe_z.get("home_xy_position", "")).strip()
        if raw:
            parts = [p.strip() for p in raw.split(",")]
            if len(parts) >= 2:
                try:
                    home_xy = (float(parts[0]), float(parts[1]))
                except ValueError:
                    notes.append(
                        f"unparseable safe_z_home home_xy_position: {raw!r}"
                    )
        notes.append(
            "safe_z_home: a bare G28 travels to the configured XY and "
            "descends Z there"
        )
        return HomingBehavior(style="safe_z_home", home_xy=home_xy, notes=notes)

    stepper_z = _section("stepper_z")
    if stepper_z is not None:
        endstop = str(stepper_z.get("endstop_pin", "")).lower()
        if "probe" in endstop or "bltouch" in endstop:
            notes.append(
                "probe-as-Z-endstop without safe_z_home: G28 probes "
                "wherever the head currently is"
            )
            return HomingBehavior(style="probe_in_place", notes=notes)
        if endstop:
            notes.append("physical Z endstop: G28 descends at the endstop corner")
            return HomingBehavior(style="endstop", notes=notes)

    return HomingBehavior(notes=["no stepper_z/safe_z_home sections found"])


# ---------------------------------------------------------------------------
# Park-point planning
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ParkPlan:
    """Outcome of park-point planning.  ``ok=False`` means REFUSED —
    no coordinate is offered, and ``reason`` says why."""

    ok: bool
    reason: str
    xy: tuple[float, float] | None = None


def plan_park_point(
    printer_id: str | None,
    occupied: OccupiedRegion | list[OccupiedRegion] | None,
    *,
    edge_inset_mm: float = DEFAULT_EDGE_INSET_MM,
    bed_rect: tuple[float, float, float, float] | None = None,
) -> ParkPlan:
    """Choose a park point PROVABLY outside every occupied region.

    Candidates are the four bed corners inset by *edge_inset_mm*.  The
    bed rectangle comes from *bed_rect* when given — pass
    :func:`bed_rect_from_config` output so the machine's own firmware
    config wins over the catalogue (and printers the catalogue has
    never heard of still get planning) — else from the catalogue's
    corner-origin volume.  Among corners that clear ALL regions
    (margins included), the one farthest from its nearest part wins.

    Refuses — rather than guessing — when the bed geometry is unknown,
    when occupancy is unknown, or when no candidate clears every region.
    """
    rect = _resolve_bed_rect(printer_id, bed_rect)
    if rect is None:
        return ParkPlan(
            ok=False,
            reason=(
                f"bed geometry unknown for printer {printer_id!r} — cannot "
                "prove any XY point is on the bed"
            ),
        )
    if occupied is None:
        return ParkPlan(
            ok=False,
            reason=(
                "occupied region unknown (job artifact missing or "
                "unparseable) — cannot prove any XY point is clear"
            ),
        )
    regions = [occupied] if isinstance(occupied, OccupiedRegion) else list(occupied)
    if not regions:
        return ParkPlan(
            ok=False,
            reason="empty occupied-region list — occupancy was not derived",
        )
    bx0, bx1, by0, by1 = rect
    inset = edge_inset_mm
    corners = [
        (bx0 + inset, by0 + inset),
        (bx1 - inset, by0 + inset),
        (bx0 + inset, by1 - inset),
        (bx1 - inset, by1 - inset),
    ]
    clear = [
        c for c in corners if not any(r.contains(*c) for r in regions)
    ]
    if not clear:
        return ParkPlan(
            ok=False,
            reason=(
                "occupied regions (plus margin) cover every candidate park "
                "corner — no on-bed point is provably clear"
            ),
        )

    def _distance_to_nearest_part(corner: tuple[float, float]) -> float:
        px, py = corner
        best = float("inf")
        for r in regions:
            cx = (r.x_min + r.x_max) / 2.0
            cy = (r.y_min + r.y_max) / 2.0
            best = min(best, (px - cx) ** 2 + (py - cy) ** 2)
        return best

    best = max(clear, key=_distance_to_nearest_part)
    return ParkPlan(ok=True, reason="corner farthest from the nearest part", xy=best)


# ---------------------------------------------------------------------------
# Travel planning — a provably clear route between two on-bed points
# ---------------------------------------------------------------------------

# Corners of an inflated obstacle are pushed out by this much so a route
# hugging a corner never rides the exact boundary of the keep-out box.
_CORNER_EPS_MM = 0.01


@dataclass(frozen=True)
class TravelPlan:
    """Outcome of travel planning.  ``ok=False`` is a REFUSAL: no
    waypoints are offered and ``reason`` says what could not be proven."""

    ok: bool
    reason: str
    waypoints: list[tuple[float, float]] = field(default_factory=list)
    # "direct" (segment clear), "fly_over" (Z proven above every part),
    # or "route_around" (visibility-graph detour in XY).
    strategy: str = ""


def _rect(region: OccupiedRegion) -> tuple[float, float, float, float]:
    """Inflated keep-out box (margin included) as (x0, x1, y0, y1)."""
    return (
        region.x_min - region.margin_mm,
        region.x_max + region.margin_mm,
        region.y_min - region.margin_mm,
        region.y_max + region.margin_mm,
    )


def _segment_enters_rect(
    p: tuple[float, float],
    q: tuple[float, float],
    rect: tuple[float, float, float, float],
) -> bool:
    """True if segment p->q passes through the OPEN interior of *rect*.

    Liang-Barsky clip against the open box: touching the boundary (a
    route hugging the keep-out edge) does not count as entering.
    """
    x0, x1, y0, y1 = rect
    px, py = p
    qx, qy = q
    dx = qx - px
    dy = qy - py
    t_enter = 0.0
    t_exit = 1.0
    for delta, lo, hi, start in ((dx, x0, x1, px), (dy, y0, y1, py)):
        if delta == 0.0:
            if start <= lo or start >= hi:
                return False  # parallel and on/outside this slab boundary
            continue
        t_lo = (lo - start) / delta
        t_hi = (hi - start) / delta
        if t_lo > t_hi:
            t_lo, t_hi = t_hi, t_lo
        t_enter = max(t_enter, t_lo)
        t_exit = min(t_exit, t_hi)
        if t_enter >= t_exit:
            return False
    # A positive-length overlap of the parameter intervals means the
    # segment spends real length inside the open box.
    return t_enter < t_exit


def _segment_clear(
    p: tuple[float, float],
    q: tuple[float, float],
    rects: list[tuple[float, float, float, float]],
) -> bool:
    return not any(_segment_enters_rect(p, q, r) for r in rects)


def plan_travel(
    printer_id: str | None,
    start_xy: tuple[float, float],
    goal_xy: tuple[float, float],
    obstacles: list[OccupiedRegion] | OccupiedRegion | None,
    *,
    travel_z_mm: float | None = None,
    ignore_names: set[str] | None = None,
    bed_rect: tuple[float, float, float, float] | None = None,
) -> TravelPlan:
    """Plan an XY route from *start_xy* to *goal_xy* that provably avoids
    every obstacle region (margin included), or refuse.

    Decision ladder:

    1. **Fly-over** — when *travel_z_mm* is given and every obstacle has a
       known ``z_top_mm`` strictly below it, the direct segment is safe at
       that height regardless of XY.
    2. **Direct** — the straight segment misses every inflated keep-out box.
    3. **Route-around** — shortest path on the visibility graph whose nodes
       are the start, the goal, and the (slightly pushed-out) corners of
       every inflated box, with edges only between mutually visible nodes.
       Every returned segment is verified clear — the plan is a proof,
       not a heuristic.
    4. **Refusal** — bed geometry unknown, occupancy unknown, an endpoint
       is off-bed or inside a keep-out box, or no clear route exists.

    ``ignore_names`` exempts named regions (e.g. the object a mid-print
    decoration is deliberately approaching).  ``bed_rect`` overrides the
    catalogue bed rectangle — pass :func:`bed_rect_from_config` output
    so the machine's own firmware config wins, including printers the
    catalogue has never heard of.
    """
    rect_bounds = _resolve_bed_rect(printer_id, bed_rect)
    if rect_bounds is None:
        return TravelPlan(
            ok=False,
            reason=(
                f"bed geometry unknown for printer {printer_id!r} — cannot "
                "prove the route stays on the bed"
            ),
        )
    if obstacles is None:
        return TravelPlan(
            ok=False,
            reason=(
                "occupied regions unknown (job artifact missing or "
                "unparseable) — cannot prove any route is clear"
            ),
        )
    if isinstance(obstacles, OccupiedRegion):
        obstacles = [obstacles]
    if ignore_names:
        obstacles = [o for o in obstacles if o.name not in ignore_names]

    bx0, bx1, by0, by1 = rect_bounds
    for label, (x, y) in (("start", start_xy), ("goal", goal_xy)):
        if not (bx0 <= x <= bx1 and by0 <= y <= by1):
            return TravelPlan(
                ok=False,
                reason=(
                    f"{label} ({x:g}, {y:g}) is outside the usable bed "
                    f"[{bx0:g}..{bx1:g}] x [{by0:g}..{by1:g}]"
                ),
            )

    # 1. Fly-over: proven only when EVERY obstacle's height is known and
    #    strictly below the travel height.  One unknown height sinks it.
    if travel_z_mm is not None and obstacles:
        tops = [o.z_top_mm for o in obstacles]
        if all(t is not None and t < travel_z_mm for t in tops):
            return TravelPlan(
                ok=True,
                reason=(
                    f"travel at Z={travel_z_mm:g} clears every part "
                    f"(tallest {max(tops):g})"
                ),
                waypoints=[start_xy, goal_xy],
                strategy="fly_over",
            )

    rects = [_rect(o) for o in obstacles]

    for label, point in (("start", start_xy), ("goal", goal_xy)):
        inside = [
            o.name or o.source
            for o, r in zip(obstacles, rects, strict=True)
            if r[0] < point[0] < r[1] and r[2] < point[1] < r[3]
        ]
        if inside:
            return TravelPlan(
                ok=False,
                reason=(
                    f"{label} lies inside keep-out region "
                    f"{inside[0]!r} (margin included) — cannot prove a "
                    "clear approach; pass ignore_names to aim at it "
                    "deliberately"
                ),
            )

    # 2. Direct.
    if _segment_clear(start_xy, goal_xy, rects):
        return TravelPlan(
            ok=True,
            reason="direct segment misses every keep-out region",
            waypoints=[start_xy, goal_xy],
            strategy="direct",
        )

    # 3. Visibility graph over pushed-out box corners.
    nodes: list[tuple[float, float]] = [start_xy, goal_xy]
    for x0, x1, y0, y1 in rects:
        for cx, cy in (
            (x0 - _CORNER_EPS_MM, y0 - _CORNER_EPS_MM),
            (x1 + _CORNER_EPS_MM, y0 - _CORNER_EPS_MM),
            (x0 - _CORNER_EPS_MM, y1 + _CORNER_EPS_MM),
            (x1 + _CORNER_EPS_MM, y1 + _CORNER_EPS_MM),
        ):
            if bx0 <= cx <= bx1 and by0 <= cy <= by1:
                nodes.append((cx, cy))

    import heapq

    n = len(nodes)
    adjacency: list[list[tuple[int, float]]] = [[] for _ in range(n)]
    for i in range(n):
        for j in range(i + 1, n):
            if _segment_clear(nodes[i], nodes[j], rects):
                dist = (
                    (nodes[i][0] - nodes[j][0]) ** 2
                    + (nodes[i][1] - nodes[j][1]) ** 2
                ) ** 0.5
                adjacency[i].append((j, dist))
                adjacency[j].append((i, dist))

    best = {0: 0.0}
    prev: dict[int, int] = {}
    heap: list[tuple[float, int]] = [(0.0, 0)]
    while heap:
        d, u = heapq.heappop(heap)
        if u == 1:
            break
        if d > best.get(u, float("inf")):
            continue
        for v, w in adjacency[u]:
            nd = d + w
            if nd < best.get(v, float("inf")):
                best[v] = nd
                prev[v] = u
                heapq.heappush(heap, (nd, v))

    if 1 not in best:
        return TravelPlan(
            ok=False,
            reason=(
                "no clear route exists: the keep-out regions (margin "
                "included) separate start from goal on this bed"
            ),
        )
    order = [1]
    while order[-1] != 0:
        order.append(prev[order[-1]])
    waypoints = [nodes[i] for i in reversed(order)]
    return TravelPlan(
        ok=True,
        reason=f"routed around {len(rects)} keep-out region(s)",
        waypoints=waypoints,
        strategy="route_around",
    )


def plan_live_park(
    printer_id: str | None,
    start_xy: tuple[float, float] | None,
    occupied: list[OccupiedRegion] | OccupiedRegion | None,
    *,
    bed_rect: tuple[float, float, float, float] | None = None,
    edge_inset_mm: float = DEFAULT_EDGE_INSET_MM,
    ignore_names: set[str] | None = None,
) -> TravelPlan:
    """A PROVEN route from *start_xy* to the best clear park corner.

    Tries the corners that clear every occupied region, farthest from the
    nearest part first, and returns the first one reachable by a route
    :func:`plan_travel` can prove.  Refuses when the start position is
    unknown, occupancy is unknown, no corner clears, or no clear corner
    is provably reachable — a park move is a convenience, and an unproven
    convenience move over an occupied bed is how conveniences melt parts.
    """
    if start_xy is None:
        return TravelPlan(
            ok=False,
            reason=(
                "post-home toolhead position unknown (machine config does "
                "not state its endstop positions) — cannot prove any park "
                "route"
            ),
        )
    park = plan_park_point(
        printer_id, occupied, edge_inset_mm=edge_inset_mm, bed_rect=bed_rect
    )
    if not park.ok:
        return TravelPlan(ok=False, reason=park.reason)

    # plan_park_point already validated geometry; rank every clear corner
    # (not just its winner) so a blocked best corner falls through to the
    # next-best instead of a refusal.
    regions = (
        [occupied] if isinstance(occupied, OccupiedRegion) else list(occupied or [])
    )
    rect = _resolve_bed_rect(printer_id, bed_rect)
    bx0, bx1, by0, by1 = rect  # non-None: plan_park_point succeeded above
    inset = edge_inset_mm
    corners = [
        (bx0 + inset, by0 + inset),
        (bx1 - inset, by0 + inset),
        (bx0 + inset, by1 - inset),
        (bx1 - inset, by1 - inset),
    ]

    def _distance_to_nearest_part(corner: tuple[float, float]) -> float:
        px, py = corner
        return min(
            (px - (r.x_min + r.x_max) / 2.0) ** 2
            + (py - (r.y_min + r.y_max) / 2.0) ** 2
            for r in regions
        )

    clear = sorted(
        (c for c in corners if not any(r.contains(*c) for r in regions)),
        key=_distance_to_nearest_part,
        reverse=True,
    )
    for corner in clear:
        route = plan_travel(
            printer_id,
            start_xy,
            corner,
            regions,
            ignore_names=ignore_names,
            bed_rect=bed_rect,
        )
        if route.ok:
            return TravelPlan(
                ok=True,
                reason=(
                    f"park at ({corner[0]:g}, {corner[1]:g}) — {route.reason}"
                ),
                waypoints=route.waypoints,
                strategy=route.strategy,
            )
    return TravelPlan(
        ok=False,
        reason=(
            "no clear park corner is reachable by a provable route from "
            f"({start_xy[0]:g}, {start_xy[1]:g})"
        ),
    )


# ---------------------------------------------------------------------------
# Sequence builders — the one copy of the proven shape
# ---------------------------------------------------------------------------

def build_lift_and_home_xy(
    *,
    lift_mm: float = DEFAULT_LIFT_MM,
    feedrate: int = _LIFT_FEEDRATE,
) -> list[str]:
    """Relative Z lift, then home X/Y only.  Never Z.

    Safe under the mid-print precondition (nozzle at or above the top
    printed layer): the lift clears the part before any XY motion, and Z
    is left exactly as trusted.
    """
    return [
        "G91",  # Relative positioning
        f"G1 Z{lift_mm:g} F{feedrate}",  # Lift clear of the part FIRST
        "G90",  # Absolute positioning
        "G28 X Y",  # Home X/Y only — NEVER Z with a part on the bed
    ]


def build_resume_preamble(
    *,
    hotend_temp: int,
    bed_temp: int,
    resume_z_mm: float,
    lift_mm: float = DEFAULT_LIFT_MM,
    prime_mm: float = 5.0,
    retract_mm: float = 2.0,
    header_comment: str | None = None,
) -> list[str]:
    """The generalised resume preamble: heat -> lift -> home X/Y -> wait
    for temps -> travel to safe Z above the resume layer -> prime.

    Descent from ``resume_z + lift`` to the exact resume Z is the resumed
    gcode body's first move; the preamble deliberately stops one lift
    above it.

    When ``resume_z_mm`` is not a positive height the resume Z is
    UNKNOWN, and an absolute descent could dive below the part's top at
    the homed corner — so the preamble stays at the lifted height and
    says so, leaving the descent to a caller that actually knows Z.
    """
    commands: list[str] = []
    if header_comment:
        commands.append(f"; {header_comment}")
    commands += [
        f"M104 S{hotend_temp}",  # Start heating hotend (non-blocking)
        f"M140 S{bed_temp}",  # Start heating bed (non-blocking)
        *build_lift_and_home_xy(lift_mm=lift_mm),
        f"M109 S{hotend_temp}",  # Wait for hotend
        f"M190 S{bed_temp}",  # Wait for bed
    ]
    if resume_z_mm > 0:
        commands.append(
            f"G1 Z{resume_z_mm + lift_mm:.1f} F{_LIFT_FEEDRATE}"  # Safe Z above resume layer
        )
    else:
        commands.append(
            "; resume Z unknown — staying at the lifted height; descend "
            "only once the resume Z is known"
        )
    if prime_mm > 0:
        commands.append(f"G1 E{prime_mm:g} F300")  # Prime nozzle
    if retract_mm > 0:
        commands.append(f"G1 E-{retract_mm:g} F2400")  # Retract against ooze
    return commands


def build_safe_abort_sequence(
    *,
    lift_mm: float = 10.0,
    park_route: TravelPlan | None = None,
) -> list[str]:
    """Abort with the part still on the bed: heaters off, lift, home X/Y,
    optionally park clear of the part, release steppers.

    *park_route* is a PROVEN route (:func:`plan_live_park`) from the
    post-home position.  A refused or absent route is recorded as a
    comment and the nozzle simply stays where homing put it — the
    pre-park behaviour.  The park moves are emitted BEFORE ``M84``:
    after the steppers are disabled no move happens at all.
    """
    commands = [
        "M104 S0",  # Hotend heater off
        "M140 S0",  # Bed heater off
        *build_lift_and_home_xy(lift_mm=lift_mm, feedrate=1000),
    ]
    if park_route is not None:
        commands.extend(build_park_moves(park_route))
    commands.append("M84")  # Disable steppers — nothing moves after this
    return commands


def build_park_moves(
    route: TravelPlan, *, feedrate: int = _TRAVEL_FEEDRATE
) -> list[str]:
    """Absolute XY moves along a proven park route (first waypoint is the
    current position and is skipped).

    A refused route yields a comment stating why, never a coordinate:
    parking the hot nozzle somewhere unproven is worse than leaving it
    where homing put it.
    """
    if not route.ok or len(route.waypoints) < 2:
        return [f"; park skipped — {route.reason}"]
    moves = [
        f"G1 X{x:.1f} Y{y:.1f} F{feedrate}"
        for x, y in route.waypoints[1:]
    ]
    moves[-1] += "  ; park clear of the part"
    return moves


def build_firmware_resume_positioning(
    *,
    z_height_mm: float,
    hotend_temp_c: float,
    bed_temp_c: float,
    fan_pwm: int,
    flow_rate_pct: float,
    prime_length_mm: float,
    z_clearance_mm: float,
) -> list[str]:
    """Positioning sequence for firmware-level resume (Marlin-style
    printers driven over serial/OctoPrint).

    The nozzle is physically at the interrupted print's top layer
    (``z_height_mm``) but the firmware, freshly restarted, does not know
    it.  The lift is therefore RELATIVE and comes before the X/Y home,
    so homing travel happens above the part, not through its top layer.
    ``G92`` then teaches the firmware the lifted position
    (``z_height + clearance``) — same end state as lifting after G92,
    without the at-part-height homing travel.
    """
    return [
        "M413 S0",  # Disable Marlin power-loss recovery
        "G91",  # Relative positioning
        f"G1 Z{z_clearance_mm} F300",  # Lift clear of the part BEFORE homing
        "G90",  # Absolute positioning
        "G28 X Y",  # Home X/Y only (NEVER Z)
        f"M140 S{bed_temp_c}",  # Start heating bed (non-blocking)
        f"M104 S{hotend_temp_c}",  # Start heating hotend (non-blocking)
        f"M190 S{bed_temp_c}",  # Wait for bed temp
        f"M109 S{hotend_temp_c}",  # Wait for hotend temp
        "G92 E0",  # Reset extruder position
        f"G92 Z{z_height_mm + z_clearance_mm}",  # Teach firmware the lifted Z
        f"G1 E{prime_length_mm} F200",  # Prime nozzle
        "G92 E0",  # Reset extruder again
        f"M106 S{fan_pwm}",  # Part-cooling fan
        f"M221 S{int(flow_rate_pct)}",  # Flow rate multiplier
    ]
