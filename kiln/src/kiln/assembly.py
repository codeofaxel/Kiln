"""Assembly management for multi-part 3D prints.

Defines assemblies of multiple STL parts with mating interfaces,
clearance checking, joint validation, and composed STL export.
Uses only stdlib (struct, math, json) -- no external mesh libraries.
"""

from __future__ import annotations

import json
import math
import shutil
import struct
import uuid as _uuid_mod
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from kiln.generation.validation import _parse_stl, compose_stls

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MAX_FREE_TIER_PARTS = 10


def _has_pro_license() -> bool:
    """Check if kiln-pro is installed and has a valid license."""
    try:
        from kiln_pro.bridge import pro_features
        return pro_features.has_valid_license
    except ImportError:
        return False

# Joint type clearance ranges (mm) — used when design_patterns.json
# doesn't have specific values
_DEFAULT_JOINT_CLEARANCES: dict[str, tuple[float, float]] = {
    "snap_fit": (0.1, 0.3),
    "press_fit": (-0.2, -0.05),  # negative = interference
    "clearance_fit": (0.3, 1.0),
    "threaded": (0.15, 0.25),
    "glued": (0.05, 0.15),
    "loose": (0.5, 5.0),
}

# Materials too flexible for press-fit or snap-fit load-bearing
_FLEXIBLE_MATERIALS: frozenset[str] = frozenset({"TPU", "TPE", "SILICONE"})
# Materials too brittle for snap-fit
_BRITTLE_MATERIALS: frozenset[str] = frozenset({"PLA", "PLA+", "SILK-PLA"})

# Mapping from joint_type shorthand to design_patterns.json keys
_JOINT_PATTERN_MAP: dict[str, str] = {
    "snap_fit": "snap_fit_cantilever",
    "press_fit": "press_fit",
    "threaded": "threaded_connection",
}

# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class AssemblyPart:
    """A single part within an assembly."""

    part_id: str
    file_path: str
    position_mm: tuple[float, float, float] = (0.0, 0.0, 0.0)
    rotation_deg: tuple[float, float, float] = (0.0, 0.0, 0.0)
    material: str = "PLA"
    role: str = "structural"  # "structural", "cosmetic", "fastener", "support"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class FastenerSpec:
    """Explicit per-interface fastener metadata.

    ``family`` is a stable taxonomy slot (for example
    ``"metric_machine_screw"``, ``"imperial_machine_screw"``,
    ``"wood_screw"``, ``"sheet_metal_screw"``, or
    ``"concrete_anchor"``).  ``size`` keeps the user-facing callout
    such as ``"M3"``, ``"#8"``, or ``"wood-8"``.  Length can be a
    single value or a range for kits that include a length pack.
    """

    size: str
    family: str = "metric_machine_screw"
    length_mm: float | None = None
    length_range_mm: tuple[float, float] | None = None
    head_type: str | None = None
    drive_type: str | None = None
    surface_type: str | None = None
    quantity_per_interface: int = 1
    notes: str = ""

    def __post_init__(self) -> None:
        self.size = str(self.size).strip()
        if not self.size:
            raise ValueError("FastenerSpec.size is required")
        self.family = str(self.family or "metric_machine_screw").strip()
        if self.length_mm is not None:
            self.length_mm = float(self.length_mm)
            if self.length_mm <= 0:
                raise ValueError("FastenerSpec.length_mm must be positive")
        if self.length_range_mm is not None:
            if (
                not isinstance(self.length_range_mm, (list, tuple))
                or len(self.length_range_mm) != 2
            ):
                raise ValueError("FastenerSpec.length_range_mm must contain two values")
            lo, hi = (float(self.length_range_mm[0]), float(self.length_range_mm[1]))
            if lo <= 0 or hi <= 0 or lo > hi:
                raise ValueError("FastenerSpec.length_range_mm must be positive and ordered")
            self.length_range_mm = (lo, hi)
        for field_name in ("head_type", "drive_type", "surface_type"):
            value = getattr(self, field_name)
            if value is not None:
                value = str(value).strip()
                setattr(self, field_name, value or None)
        self.quantity_per_interface = int(self.quantity_per_interface)
        if self.quantity_per_interface < 1:
            raise ValueError("FastenerSpec.quantity_per_interface must be >= 1")
        self.notes = str(self.notes or "")

    def to_dict(self) -> dict[str, Any]:
        return {
            "size": self.size,
            "family": self.family,
            "length_mm": self.length_mm,
            "length_range_mm": (
                list(self.length_range_mm) if self.length_range_mm is not None else None
            ),
            "head_type": self.head_type,
            "drive_type": self.drive_type,
            "surface_type": self.surface_type,
            "quantity_per_interface": self.quantity_per_interface,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> FastenerSpec | None:
        if data is None:
            return None
        if not isinstance(data, dict):
            raise ValueError("FastenerSpec data must be a dict")
        if not data.get("size"):
            raise ValueError("FastenerSpec.size is required")
        return cls(
            size=str(data["size"]),
            family=str(data.get("family") or "metric_machine_screw"),
            length_mm=(
                float(data["length_mm"]) if data.get("length_mm") is not None else None
            ),
            length_range_mm=data.get("length_range_mm"),
            head_type=data.get("head_type"),
            drive_type=data.get("drive_type"),
            surface_type=data.get("surface_type"),
            quantity_per_interface=int(data.get("quantity_per_interface", 1)),
            notes=str(data.get("notes") or ""),
        )


@dataclass
class MatingInterface:
    """Describes a joint between two parts.

    ``magnet_polarity_aligned`` is meaningful for ``joint_type ==
    "magnetic"``: ``True`` declares the designer has confirmed which
    poles face each other in each magnet pocket, ``None`` means
    "unknown" (downstream tooling treats this as low-confidence and
    refuses to ship a hand-wavy "make sure they pull together"
    instruction).  Ignored for non-magnetic joints.

    ``fastener_spec`` is optional explicit hardware metadata for
    screw/anchor-based interfaces.  When omitted, downstream tools may
    infer a generic fastener from ``joint_type`` and ``clearance_mm``.
    """

    part_a_id: str
    part_b_id: str
    joint_type: str  # snap_fit, press_fit, clearance_fit, threaded, glued, magnetic, loose
    clearance_mm: float = 0.2
    tolerance_mm: float = 0.1
    contact_area_mm2: float = 0.0
    magnet_polarity_aligned: bool | None = None
    fastener_spec: FastenerSpec | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["fastener_spec"] = (
            self.fastener_spec.to_dict() if self.fastener_spec is not None else None
        )
        return data


@dataclass
class ClearanceCheck:
    """Result of checking clearance between two parts."""

    part_a_id: str
    part_b_id: str
    min_clearance_mm: float
    overlaps: bool
    clearance_adequate: bool
    required_clearance_mm: float
    overlap_volume_mm3: float = 0.0
    recommendations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class JointValidation:
    """Result of validating a single joint / mating interface."""

    joint_type: str
    valid: bool
    issues: list[str] = field(default_factory=list)
    design_rules_checked: list[str] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Assembly:
    """Top-level assembly container.

    Designed for pro-tier extensibility — tolerance stacking, parametric
    mates, and BOM can be added by subclassing or composing.
    """

    assembly_id: str
    name: str
    parts: list[AssemblyPart] = field(default_factory=list)
    interfaces: list[MatingInterface] = field(default_factory=list)
    clearance_checks: list[ClearanceCheck] = field(default_factory=list)
    joint_validations: list[JointValidation] = field(default_factory=list)
    overall_valid: bool = True
    recommendations: list[str] = field(default_factory=list)

    # -- mutators ---------------------------------------------------------

    def add_part(self, part: AssemblyPart) -> None:
        """Add a part, enforcing uniqueness and the free-tier limit.

        Pro/Business/Enterprise users with a valid kiln-pro license
        bypass the part limit entirely.
        """
        if any(p.part_id == part.part_id for p in self.parts):
            raise ValueError(f"Part '{part.part_id}' already exists in assembly")
        if len(self.parts) >= _MAX_FREE_TIER_PARTS and not _has_pro_license():
            raise ValueError(
                f"Free tier limited to {_MAX_FREE_TIER_PARTS} parts per assembly. "
                "Upgrade to Kiln Pro for unlimited parts."
            )
        self.parts.append(part)

    def add_interface(self, interface: MatingInterface) -> None:
        """Add a mating interface, validating that both parts exist."""
        part_ids = {p.part_id for p in self.parts}
        if interface.part_a_id not in part_ids:
            raise ValueError(f"Part '{interface.part_a_id}' not in assembly")
        if interface.part_b_id not in part_ids:
            raise ValueError(f"Part '{interface.part_b_id}' not in assembly")
        self.interfaces.append(interface)

    # -- serialisation ----------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "assembly_id": self.assembly_id,
            "name": self.name,
            "parts": [p.to_dict() for p in self.parts],
            "interfaces": [i.to_dict() for i in self.interfaces],
            "clearance_checks": [c.to_dict() for c in self.clearance_checks],
            "joint_validations": [j.to_dict() for j in self.joint_validations],
            "overall_valid": self.overall_valid,
            "recommendations": list(self.recommendations),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Assembly:
        """Reconstruct an Assembly from a dict (e.g., from JSON).

        Note: bypasses ``add_part`` validation (duplicate IDs, free-tier
        limit) intentionally — deserialized data is assumed pre-validated.
        """
        assembly = cls(
            assembly_id=data["assembly_id"],
            name=data["name"],
            overall_valid=data.get("overall_valid", True),
            recommendations=list(data.get("recommendations", [])),
        )
        for p in data.get("parts", []):
            assembly.parts.append(
                AssemblyPart(
                    part_id=p["part_id"],
                    file_path=p["file_path"],
                    position_mm=tuple(p["position_mm"]),
                    rotation_deg=tuple(p["rotation_deg"]),
                    material=p.get("material", "PLA"),
                    role=p.get("role", "structural"),
                )
            )
        for i in data.get("interfaces", []):
            assembly.interfaces.append(
                MatingInterface(
                    part_a_id=i["part_a_id"],
                    part_b_id=i["part_b_id"],
                    joint_type=i["joint_type"],
                    clearance_mm=i.get("clearance_mm", 0.2),
                    tolerance_mm=i.get("tolerance_mm", 0.1),
                    contact_area_mm2=i.get("contact_area_mm2", 0.0),
                    magnet_polarity_aligned=i.get("magnet_polarity_aligned"),
                    fastener_spec=FastenerSpec.from_dict(
                        i.get("fastener_spec") or i.get("fastener")
                    ),
                )
            )
        for c in data.get("clearance_checks", []):
            assembly.clearance_checks.append(
                ClearanceCheck(
                    part_a_id=c["part_a_id"],
                    part_b_id=c["part_b_id"],
                    min_clearance_mm=c["min_clearance_mm"],
                    overlaps=c["overlaps"],
                    clearance_adequate=c["clearance_adequate"],
                    required_clearance_mm=c["required_clearance_mm"],
                    overlap_volume_mm3=c.get("overlap_volume_mm3", 0.0),
                    recommendations=list(c.get("recommendations", [])),
                )
            )
        for j in data.get("joint_validations", []):
            assembly.joint_validations.append(
                JointValidation(
                    joint_type=j["joint_type"],
                    valid=j["valid"],
                    issues=list(j.get("issues", [])),
                    design_rules_checked=list(j.get("design_rules_checked", [])),
                    recommendations=list(j.get("recommendations", [])),
                )
            )
        return assembly


# ---------------------------------------------------------------------------
# Helpers — bounding-box geometry
# ---------------------------------------------------------------------------


def _compute_bbox(
    vertices: list[tuple[float, ...]],
    offset: tuple[float, float, float] = (0.0, 0.0, 0.0),
) -> dict[str, float]:
    """Axis-aligned bounding box from *vertices* with position offset."""
    if not vertices:
        return {
            "x_min": 0.0, "x_max": 0.0,
            "y_min": 0.0, "y_max": 0.0,
            "z_min": 0.0, "z_max": 0.0,
        }
    dx, dy, dz = offset
    xs = [v[0] + dx for v in vertices]
    ys = [v[1] + dy for v in vertices]
    zs = [v[2] + dz for v in vertices]
    return {
        "x_min": min(xs), "x_max": max(xs),
        "y_min": min(ys), "y_max": max(ys),
        "z_min": min(zs), "z_max": max(zs),
    }


def _aabb_overlap(bbox_a: dict[str, float], bbox_b: dict[str, float]) -> bool:
    """Check if two AABBs overlap on all 3 axes."""
    return (
        bbox_a["x_min"] < bbox_b["x_max"]
        and bbox_a["x_max"] > bbox_b["x_min"]
        and bbox_a["y_min"] < bbox_b["y_max"]
        and bbox_a["y_max"] > bbox_b["y_min"]
        and bbox_a["z_min"] < bbox_b["z_max"]
        and bbox_a["z_max"] > bbox_b["z_min"]
    )


def _aabb_overlap_volume(
    bbox_a: dict[str, float],
    bbox_b: dict[str, float],
) -> float:
    """Overlap volume of two AABBs, or 0 if they don't intersect."""
    dx = max(0.0, min(bbox_a["x_max"], bbox_b["x_max"]) - max(bbox_a["x_min"], bbox_b["x_min"]))
    dy = max(0.0, min(bbox_a["y_max"], bbox_b["y_max"]) - max(bbox_a["y_min"], bbox_b["y_min"]))
    dz = max(0.0, min(bbox_a["z_max"], bbox_b["z_max"]) - max(bbox_a["z_min"], bbox_b["z_min"]))
    return dx * dy * dz


def _aabb_min_distance(
    bbox_a: dict[str, float],
    bbox_b: dict[str, float],
) -> float:
    """Minimum distance between two non-overlapping AABBs.

    Returns 0.0 if they overlap.
    """
    dx = max(0.0, max(bbox_a["x_min"] - bbox_b["x_max"], bbox_b["x_min"] - bbox_a["x_max"]))
    dy = max(0.0, max(bbox_a["y_min"] - bbox_b["y_max"], bbox_b["y_min"] - bbox_a["y_max"]))
    dz = max(0.0, max(bbox_a["z_min"] - bbox_b["z_max"], bbox_b["z_min"] - bbox_a["z_max"]))
    return math.sqrt(dx * dx + dy * dy + dz * dz)


# ---------------------------------------------------------------------------
# Helpers — data loading
# ---------------------------------------------------------------------------

_DATA_DIR = Path(__file__).resolve().parent / "data" / "design_knowledge"


@lru_cache(maxsize=1)
def _load_design_patterns() -> dict[str, Any]:
    """Load design_patterns.json with ``@lru_cache``."""
    path = Path(__file__).resolve().parent / "data" / "design_knowledge" / "design_patterns.json"
    try:
        with open(path) as fh:
            data: dict[str, Any] = json.load(fh)
        return {k: v for k, v in data.items() if not k.startswith("_")}
    except (OSError, json.JSONDecodeError):
        return {}


# ---------------------------------------------------------------------------
# Helpers — STL writing
# ---------------------------------------------------------------------------


def _write_stl(path: str | Path, triangles: list[tuple[tuple[float, ...], ...]]) -> None:
    """Write a binary STL file from a list of triangles."""
    with open(path, "wb") as f:
        f.write(b"\x00" * 80)  # header
        f.write(struct.pack("<I", len(triangles)))
        for tri in triangles:
            f.write(struct.pack("<fff", 0.0, 0.0, 0.0))  # normal placeholder
            for v in tri:
                f.write(struct.pack("<fff", v[0], v[1], v[2]))
            f.write(struct.pack("<H", 0))  # attribute byte count


def _translate_triangles(
    triangles: list[tuple[tuple[float, ...], ...]],
    offset: tuple[float, float, float],
) -> list[tuple[tuple[float, ...], ...]]:
    """Return a copy of *triangles* with each vertex shifted by *offset*."""
    dx, dy, dz = offset
    if dx == 0.0 and dy == 0.0 and dz == 0.0:
        return triangles
    translated: list[tuple[tuple[float, ...], ...]] = []
    for tri in triangles:
        new_tri = tuple((v[0] + dx, v[1] + dy, v[2] + dz) for v in tri)
        translated.append(new_tri)
    return translated


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def create_assembly(
    name: str,
    assembly_id: str | None = None,
) -> Assembly:
    """Create a new empty assembly."""
    return Assembly(
        assembly_id=assembly_id or str(_uuid_mod.uuid4()),
        name=name,
    )


def check_clearance(
    assembly: Assembly,
    part_a_id: str,
    part_b_id: str,
    required_clearance_mm: float = 0.2,
) -> ClearanceCheck:
    """Check clearance between two parts using AABB overlap."""
    parts_by_id = {p.part_id: p for p in assembly.parts}
    part_a = parts_by_id.get(part_a_id)
    part_b = parts_by_id.get(part_b_id)
    if part_a is None or part_b is None:
        missing = part_a_id if part_a is None else part_b_id
        raise ValueError(f"Part '{missing}' not found in assembly '{assembly.name}'")

    for p in (part_a, part_b):
        if p.rotation_deg != (0.0, 0.0, 0.0):
            raise NotImplementedError(
                f"Part '{p.part_id}' has rotation {p.rotation_deg}. "
                "Clearance checking with rotation is not yet supported — "
                "use position_mm offsets only, or pre-rotate the STL file."
            )

    errors_a: list[str] = []
    errors_b: list[str] = []
    tri_a, verts_a = _parse_stl(Path(part_a.file_path), errors_a)
    tri_b, verts_b = _parse_stl(Path(part_b.file_path), errors_b)

    if errors_a or errors_b:
        all_errors = errors_a + errors_b
        raise ValueError(f"STL parse errors: {'; '.join(all_errors)}")

    bbox_a = _compute_bbox(verts_a, part_a.position_mm)
    bbox_b = _compute_bbox(verts_b, part_b.position_mm)

    overlaps = _aabb_overlap(bbox_a, bbox_b)
    overlap_vol = _aabb_overlap_volume(bbox_a, bbox_b) if overlaps else 0.0
    min_dist = 0.0 if overlaps else _aabb_min_distance(bbox_a, bbox_b)

    recommendations: list[str] = []
    if overlaps:
        recommendations.append(
            f"Parts '{part_a_id}' and '{part_b_id}' have overlapping bounding boxes "
            f"(overlap volume ~{overlap_vol:.1f} mm\u00b3). "
            "Adjust positions or verify intentional interference fit."
        )
    elif min_dist < required_clearance_mm:
        recommendations.append(
            f"Clearance between '{part_a_id}' and '{part_b_id}' is {min_dist:.2f} mm, "
            f"below the required {required_clearance_mm:.2f} mm. "
            "Increase spacing to avoid print artifacts or assembly issues."
        )

    clearance_adequate = (not overlaps) and (min_dist >= required_clearance_mm)

    return ClearanceCheck(
        part_a_id=part_a_id,
        part_b_id=part_b_id,
        min_clearance_mm=min_dist,
        overlaps=overlaps,
        clearance_adequate=clearance_adequate,
        required_clearance_mm=required_clearance_mm,
        overlap_volume_mm3=overlap_vol,
        recommendations=recommendations,
    )


def check_all_clearances(
    assembly: Assembly,
    default_clearance_mm: float = 0.2,
) -> list[ClearanceCheck]:
    """Check clearance for every unique part pair.  Stores results on assembly."""
    checks: list[ClearanceCheck] = []
    n = len(assembly.parts)
    for i in range(n):
        for j in range(i + 1, n):
            chk = check_clearance(
                assembly,
                assembly.parts[i].part_id,
                assembly.parts[j].part_id,
                required_clearance_mm=default_clearance_mm,
            )
            checks.append(chk)
    assembly.clearance_checks = checks
    return checks


def validate_joint(
    interface: MatingInterface,
    parts: dict[str, AssemblyPart] | list[AssemblyPart],
) -> JointValidation:
    """Validate a single mating interface against design rules."""
    jtype = interface.joint_type
    issues: list[str] = []
    rules_checked: list[str] = []
    recommendations: list[str] = []

    # Resolve part materials — accept dict or list
    if isinstance(parts, dict):
        parts_by_id = parts
    else:
        parts_by_id = {p.part_id: p for p in parts}
    mat_a = parts_by_id[interface.part_a_id].material.upper() if interface.part_a_id in parts_by_id else "PLA"
    mat_b = parts_by_id[interface.part_b_id].material.upper() if interface.part_b_id in parts_by_id else "PLA"

    # Load design pattern data (if available for this joint type)
    patterns = _load_design_patterns()
    pattern_key = _JOINT_PATTERN_MAP.get(jtype)
    pattern = patterns.get(pattern_key, {}) if pattern_key else {}

    clearance_range = _DEFAULT_JOINT_CLEARANCES.get(jtype, (0.0, 1.0))

    # -- Joint-specific validation ----------------------------------------

    if jtype == "snap_fit":
        rules_checked.append("clearance_range")
        if not (clearance_range[0] <= interface.clearance_mm <= clearance_range[1]):
            issues.append(
                f"Snap-fit clearance {interface.clearance_mm:.2f} mm outside "
                f"recommended range {clearance_range[0]}-{clearance_range[1]} mm."
            )

        rules_checked.append("material_brittleness")
        for mat_label, mat_val in [("Part A", mat_a), ("Part B", mat_b)]:
            if mat_val in _BRITTLE_MATERIALS:
                recommendations.append(
                    f"{mat_label} material ({mat_val}) is brittle — snap fits may "
                    "break after a few cycles. Consider PETG or Nylon instead."
                )

        rules_checked.append("material_flexibility")
        for mat_label, mat_val in [("Part A", mat_a), ("Part B", mat_b)]:
            if mat_val in _FLEXIBLE_MATERIALS:
                issues.append(
                    f"{mat_label} material ({mat_val}) is too flexible for "
                    "a load-bearing snap-fit joint."
                )

    elif jtype == "press_fit":
        rules_checked.append("interference_range")
        # Press-fit clearance should be negative (interference)
        if interface.clearance_mm > 0:
            issues.append(
                f"Press-fit clearance is positive ({interface.clearance_mm:.2f} mm) — "
                "expected negative (interference). Part will be loose."
            )
        elif not (clearance_range[0] <= interface.clearance_mm <= clearance_range[1]):
            issues.append(
                f"Interference {interface.clearance_mm:.2f} mm outside "
                f"recommended range {clearance_range[0]} to {clearance_range[1]} mm."
            )

        rules_checked.append("material_flexibility")
        for mat_label, mat_val in [("Part A", mat_a), ("Part B", mat_b)]:
            if mat_val in _FLEXIBLE_MATERIALS:
                recommendations.append(
                    f"{mat_label} material ({mat_val}) is flexible — press fit "
                    "may not hold. Consider a rigid material."
                )

    elif jtype == "threaded":
        rules_checked.append("clearance_range")
        if not (clearance_range[0] <= interface.clearance_mm <= clearance_range[1]):
            issues.append(
                f"Thread clearance {interface.clearance_mm:.2f} mm outside "
                f"recommended range {clearance_range[0]}-{clearance_range[1]} mm."
            )

        rules_checked.append("fine_thread_warning")
        recommendations.append(
            "FDM printers struggle with fine threads (< 1.5 mm pitch). "
            "Use coarse threads (2 mm+) or heat-set inserts for metal screws."
        )

    elif jtype == "clearance_fit":
        rules_checked.append("clearance_range")
        if interface.clearance_mm < clearance_range[0]:
            issues.append(
                f"Clearance {interface.clearance_mm:.2f} mm is below minimum "
                f"{clearance_range[0]} mm for a clearance fit."
            )

    elif jtype == "glued":
        rules_checked.append("clearance_range")
        if not (clearance_range[0] <= interface.clearance_mm <= clearance_range[1]):
            recommendations.append(
                f"Glue joint clearance {interface.clearance_mm:.2f} mm is outside "
                f"typical range {clearance_range[0]}-{clearance_range[1]} mm. "
                "A thin, even gap produces the strongest bond."
            )

    elif jtype == "loose":
        rules_checked.append("clearance_range")
        if interface.clearance_mm < clearance_range[0]:
            recommendations.append(
                f"Loose-fit clearance {interface.clearance_mm:.2f} mm is below "
                f"typical minimum {clearance_range[0]} mm."
            )

    elif jtype == "magnetic":
        rules_checked.append("magnet_pocket_material")
        for mat_label, mat_val in [("Part A", mat_a), ("Part B", mat_b)]:
            if mat_val in _BRITTLE_MATERIALS:
                recommendations.append(
                    f"Magnetic joint with {mat_val} {mat_label}: heat-pressing "
                    f"magnets into {mat_val} can soften the surrounding material; "
                    "print the magnet pocket undersized and use CA glue, OR "
                    "switch to PETG/ABS. Cooling under load minimizes the risk."
                )

    else:
        issues.append(f"Unknown joint type '{jtype}' — no design rules available for validation.")

    # -- Material compatibility from design_patterns.json -----------------
    if pattern and "material_compatibility" in pattern:
        compat = pattern["material_compatibility"]
        rules_checked.append("material_compatibility")
        for mat_label, mat_val in [("Part A", mat_a), ("Part B", mat_b)]:
            mat_lower = mat_val.lower()
            if mat_lower in [m.lower() for m in compat.get("avoid", [])]:
                issues.append(
                    f"{mat_label} material ({mat_val}) is in the 'avoid' list "
                    f"for {jtype} joints."
                )
            elif mat_lower in [m.lower() for m in compat.get("poor", [])]:
                recommendations.append(
                    f"{mat_label} material ({mat_val}) rated 'poor' for {jtype} joints. "
                    "Consider a better-suited material."
                )

    valid = len(issues) == 0

    return JointValidation(
        joint_type=jtype,
        valid=valid,
        issues=issues,
        design_rules_checked=rules_checked,
        recommendations=recommendations,
    )


def validate_assembly(assembly: Assembly) -> Assembly:
    """Run all clearance checks and joint validations.

    Sets ``overall_valid`` to ``False`` if any overlap is detected or
    any joint validation fails.  Returns the mutated assembly.
    """
    check_all_clearances(assembly)

    joint_validations: list[JointValidation] = []
    for iface in assembly.interfaces:
        jv = validate_joint(iface, assembly.parts)
        joint_validations.append(jv)
    assembly.joint_validations = joint_validations

    # Aggregate validity
    has_overlap = any(c.overlaps for c in assembly.clearance_checks)
    has_invalid_joint = any(not j.valid for j in assembly.joint_validations)
    assembly.overall_valid = not (has_overlap or has_invalid_joint)

    # Aggregate recommendations
    all_recs: list[str] = []
    for c in assembly.clearance_checks:
        all_recs.extend(c.recommendations)
    for j in assembly.joint_validations:
        all_recs.extend(j.recommendations)
    assembly.recommendations = all_recs

    return assembly


def compose_assembly(
    assembly: Assembly,
    output_path: str,
) -> dict[str, Any]:
    """Merge all assembly parts into a single positioned STL.

    Each part's vertices are translated by its ``position_mm`` offset
    before merging with :func:`compose_stls`.
    """
    import tempfile

    if not assembly.parts:
        raise ValueError("Assembly has no parts to compose.")

    temp_dir = Path(tempfile.mkdtemp(prefix="kiln_assembly_"))
    temp_files: list[str] = []

    try:
        for part in assembly.parts:
            if part.rotation_deg != (0.0, 0.0, 0.0):
                raise NotImplementedError(
                    f"Part '{part.part_id}' has rotation {part.rotation_deg}. "
                    "Composing with rotation is not yet supported — "
                    "use position_mm offsets only, or pre-rotate the STL file."
                )
            errors: list[str] = []
            triangles, _verts = _parse_stl(Path(part.file_path), errors)
            if errors:
                raise ValueError(
                    f"Failed to parse '{part.file_path}': {'; '.join(errors)}"
                )

            translated = _translate_triangles(triangles, part.position_mm)
            tmp_path = temp_dir / f"{part.part_id}.stl"
            _write_stl(tmp_path, translated)
            temp_files.append(str(tmp_path))

        result = compose_stls(temp_files, output_path)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

    # Compute overall bounding box from the composed output
    errors_out: list[str] = []
    tri_out, verts_out = _parse_stl(Path(output_path), errors_out)
    bbox = _compute_bbox(verts_out) if not errors_out else {}

    return {
        "output_path": output_path,
        "total_triangles": result.get("total_triangles", 0),
        "bounding_box": bbox,
    }


def get_clearance_recommendation(
    joint_type: str,
    material_a: str = "PLA",
    material_b: str = "PLA",
) -> dict[str, Any]:
    """Return recommended clearance, tolerance, and rationale.

    For flexible materials, clearance is increased by 50%.
    For brittle materials with snap_fit, a warning is included.
    """
    clearance_range = _DEFAULT_JOINT_CLEARANCES.get(joint_type, (0.1, 0.5))
    base_clearance = (clearance_range[0] + clearance_range[1]) / 2.0
    tolerance = abs(clearance_range[1] - clearance_range[0]) / 2.0

    mat_a_upper = material_a.upper()
    mat_b_upper = material_b.upper()
    flexible = mat_a_upper in _FLEXIBLE_MATERIALS or mat_b_upper in _FLEXIBLE_MATERIALS
    brittle = mat_a_upper in _BRITTLE_MATERIALS or mat_b_upper in _BRITTLE_MATERIALS

    rationale_parts: list[str] = [
        f"Base clearance for {joint_type}: {base_clearance:.2f} mm "
        f"(range {clearance_range[0]:.2f} to {clearance_range[1]:.2f} mm)."
    ]
    warnings: list[str] = []

    if flexible:
        base_clearance *= 1.5
        tolerance *= 1.5
        rationale_parts.append(
            "Clearance increased by 50% due to flexible material — "
            "flexible parts deform during assembly."
        )

    if brittle and joint_type == "snap_fit":
        warnings.append(
            "Brittle material detected. Snap fits in PLA/PLA+ typically "
            "survive only 2-3 cycles. Consider PETG or Nylon for repeated use."
        )

    return {
        "joint_type": joint_type,
        "material_a": material_a,
        "material_b": material_b,
        "recommended_clearance_mm": round(base_clearance, 3),
        "tolerance_mm": round(tolerance, 3),
        "clearance_range_mm": [clearance_range[0], clearance_range[1]],
        "rationale": " ".join(rationale_parts),
        "warnings": warnings,
    }
