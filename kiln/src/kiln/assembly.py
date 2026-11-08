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
    """True when the CALLER's plan is Pro or above.

    Asks :mod:`kiln.licensing`, the one name every gate imports: on Kiln's
    hosted server it reads the per-request tier (the process itself holds a
    licence, so a process check would wave every caller past the cap), and
    on a plain install it reads the plan of the Kiln account signed in on
    this machine.  An unreadable plan is Free.
    """
    from kiln.licensing import LicenseTier, check_tier

    try:
        allowed, _ = check_tier(LicenseTier.PRO)
    except Exception:  # noqa: BLE001 -- an unreadable plan is Free
        return False
    return bool(allowed)


# Joint type clearance ranges (mm) — used when design_templates.json
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

# Mapping from joint_type shorthand to design_templates.json keys
_JOINT_TEMPLATE_MAP: dict[str, str] = {
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

    ``driver`` is the driver the screw takes as whoever supplies the kit
    states it (``"2 mm hex key"``, ``"PH2"``, ``"T10"``): the screw in the
    bag, not the thread in general.  ``product`` names the exact bought
    part (maker and part number) when the kit ships one.  Both are optional
    and are the caller's own statement.
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
    driver: str | None = None
    product: str | None = None

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
        for field_name in ("head_type", "drive_type", "surface_type", "driver", "product"):
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
            "driver": self.driver,
            "product": self.product,
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
            driver=data.get("driver"),
            product=data.get("product"),
        )


# ---------------------------------------------------------------------------
# Joint motion: the axis and range a moving joint is declared with
# ---------------------------------------------------------------------------

#: The ways a joint can move, in the user's words.
MOTION_KINDS: tuple[str, ...] = ("hinge", "slider", "ball", "gear")

#: Kinds whose range is an angle; a slider's range is a distance.
_ANGULAR_MOTION_KINDS: frozenset[str] = frozenset({"hinge", "ball", "gear"})

#: The gap class a motion kind's clearance is judged in when the record
#: names none itself (``validate_joint``'s ``mating`` vocabulary).  A ball
#: joint has no class of its own yet, so its check keeps the default gap.
_MOTION_MATING: dict[str, str | None] = {
    "hinge": "pin_in_bore",
    "gear": "gear_flank",
    "slider": "slot",
    "ball": None,
}

#: Joint types that hold their parts fixed once assembled.  A motion
#: declared on one is a contradiction in the record, not a joint.
_FIXED_JOINT_TYPES: frozenset[str] = frozenset({"press_fit", "glued"})


@dataclass
class JointMotion:
    """How a moving joint moves: its axis and its range, declared once on
    the interface so a check can sweep the joint through every pose it
    takes instead of judging only the pose it was drawn in.

    Part B moves relative to part A.  Coordinates are in the assembly
    frame, the frame ``AssemblyPart.position_mm`` is given in.

    ``kind``: ``"hinge"`` turns about the axis; ``"slider"`` moves along
    it; ``"ball"`` swings about ``axis_point_mm``, measured from
    ``axis_direction`` (the stem's direction at rest); ``"gear"`` turns
    about the axis while meshing with part A.

    ``axis_point_mm``: a point on the axis (a ball joint's centre).
    ``axis_direction``: the axis as a direction; stored normalised.
    ``range_min`` / ``range_max``: degrees for a hinge, ball or gear, where
    0 is the pose as modelled and a full turn is 0 to 360; millimetres
    along the axis for a slider.
    ``mating``: the gap class the clearance is judged in.  Left unset it
    follows the kind (:data:`_MOTION_MATING`).
    ``continuous``: a hinge or gear that turns without end (a crankshaft, a
    wrist pin in a mechanism); its range is one turn, 0 to 360, and nothing
    stops it.
    """

    kind: str
    axis_point_mm: tuple[float, float, float]
    axis_direction: tuple[float, float, float]
    range_min: float
    range_max: float
    mating: str | None = None
    continuous: bool = False

    def __post_init__(self) -> None:
        self.kind = str(self.kind or "").strip().lower()
        if self.kind not in MOTION_KINDS:
            raise ValueError(
                f"JointMotion.kind must be one of {', '.join(MOTION_KINDS)}; "
                f"got {self.kind!r}"
            )
        self.axis_point_mm = _three_numbers(self.axis_point_mm, "JointMotion.axis_point_mm")
        direction = _three_numbers(self.axis_direction, "JointMotion.axis_direction")
        length = math.sqrt(sum(c * c for c in direction))
        if length <= 0.0:
            raise ValueError("JointMotion.axis_direction must not be the zero vector")
        self.axis_direction = tuple(c / length for c in direction)  # type: ignore[assignment]
        self.range_min = _finite_number(self.range_min, "JointMotion.range_min")
        self.range_max = _finite_number(self.range_max, "JointMotion.range_max")
        if self.range_min >= self.range_max:
            raise ValueError(
                "JointMotion.range_min must be below range_max; a joint with no "
                "range does not move"
            )
        if self.kind in _ANGULAR_MOTION_KINDS and self.range_max - self.range_min > 360.0:
            raise ValueError("JointMotion range spans more than one full turn (360 degrees)")
        if self.kind == "ball" and (self.range_min < 0.0 or self.range_max > 180.0):
            raise ValueError(
                "JointMotion range for a ball joint is a swing from the rest direction, "
                "0 to at most 180 degrees"
            )
        if self.mating is None:
            self.mating = _MOTION_MATING[self.kind]
        else:
            self.mating = str(self.mating).strip() or _MOTION_MATING[self.kind]
        self.continuous = bool(self.continuous)
        if self.continuous and self.kind not in ("hinge", "gear"):
            raise ValueError("JointMotion.continuous is for a joint that turns: a hinge or a gear")
        if self.continuous and self.range_max - self.range_min != 360.0:
            raise ValueError("a continuous joint's range is one full turn, 0 to 360 degrees")

    @property
    def range_unit(self) -> str:
        """``"deg"`` for a turning or swinging joint, ``"mm"`` for a slider."""
        return "deg" if self.kind in _ANGULAR_MOTION_KINDS else "mm"

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "axis_point_mm": list(self.axis_point_mm),
            "axis_direction": list(self.axis_direction),
            "range_min": self.range_min,
            "range_max": self.range_max,
            "range_unit": self.range_unit,
            "mating": self.mating,
            **({"continuous": True} if self.continuous else {}),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> JointMotion | None:
        if data is None:
            return None
        if not isinstance(data, dict):
            raise ValueError("JointMotion data must be a dict")
        missing = [k for k in ("kind", "axis_point_mm", "axis_direction", "range_min", "range_max")
                   if data.get(k) is None]
        if missing:
            raise ValueError(f"JointMotion is missing {', '.join(missing)}")
        return cls(
            kind=str(data["kind"]),
            axis_point_mm=data["axis_point_mm"],
            axis_direction=data["axis_direction"],
            range_min=data["range_min"],
            range_max=data["range_max"],
            mating=data.get("mating"),
            continuous=bool(data.get("continuous", False)),
        )


#: How two joints of a mechanism turn together.
COUPLING_KINDS = ("gear", "rack", "screw")


@dataclass
class Coupling:
    """Two joints of a mechanism that move together, named by their place in
    the assembly's ``interfaces``.  ``"gear"``: joint B turns ``ratio`` times
    joint A, the sign against their declared axes (an external pair turns
    the other way, so its ratio is negative).  ``"rack"``: a turning joint
    and a sliding one, the slide moving ``pitch_radius_mm`` times the turn
    in radians.  ``"screw"``: a turning joint and a sliding one, the slide
    moving ``lead_mm`` per turn."""

    kind: str
    a: int
    b: int
    ratio: float | None = None
    pitch_radius_mm: float | None = None
    lead_mm: float | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"kind": self.kind, "a": self.a, "b": self.b}
        for key in ("ratio", "pitch_radius_mm", "lead_mm"):
            if getattr(self, key) is not None:
                out[key] = getattr(self, key)
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Coupling:
        if not isinstance(data, dict):
            raise ValueError("a coupling is a dict")
        def number(key: str) -> float | None:
            value = data.get(key)
            return None if value is None else float(value)
        return cls(kind=str(data["kind"]), a=int(data["a"]), b=int(data["b"]),
                   ratio=number("ratio"), pitch_radius_mm=number("pitch_radius_mm"),
                   lead_mm=number("lead_mm"))


@dataclass
class Mechanism:
    """Joints that move together, turned by one driver: the joint named by
    ``driver`` (its place in the assembly's ``interfaces``), through
    ``driver_range`` or, when ``continuous``, round and round (one period is
    how far it turns before every part repeats, which the sweep works out).
    Every other moving joint follows; ``couplings`` say which turn together.
    """

    driver: int
    couplings: list[Coupling] = field(default_factory=list)
    continuous: bool = False
    driver_range: tuple[float, float] | None = None

    def to_dict(self) -> dict[str, Any]:
        driver: dict[str, Any] = {"interface": self.driver}
        if self.continuous:
            driver["continuous"] = True
        else:
            driver["range"] = list(self.driver_range) if self.driver_range is not None else None
        return {"driver": driver, "couplings": [c.to_dict() for c in self.couplings]}

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> Mechanism | None:
        if data is None:
            return None
        if not isinstance(data, dict) or not isinstance(data.get("driver"), dict):
            raise ValueError("a mechanism is a dict whose driver is a dict naming its interface")
        driver = data["driver"]
        span = driver.get("range")
        return cls(
            driver=int(driver["interface"]),
            continuous=bool(driver.get("continuous", False)),
            driver_range=(float(span[0]), float(span[1])) if span is not None else None,
            couplings=[Coupling.from_dict(c) for c in data.get("couplings") or []],
        )

    def problems(self, interfaces: list[Any]) -> list[str]:
        """What in this declaration cannot be: each a sentence, empty when
        it reads.  Only the record is judged here; whether the parts move
        as declared is the sweep's question."""
        out: list[str] = []

        def motion_of(index: Any, what: str) -> Any:
            if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(interfaces):
                out.append(f"the {what} names joint {index!r}, which is not one of the assembly's interfaces")
                return None
            motion = getattr(interfaces[index], "motion", None)
            if motion is None:
                out.append(f"the {what} names joint {index}, which declares no motion")
            return motion

        driver = motion_of(self.driver, "driver")
        if driver is not None:
            if self.continuous and not driver.continuous:
                out.append("a continuous driver must be a joint declared continuous")
            if not self.continuous:
                span = self.driver_range
                if span is None or not all(math.isfinite(v) for v in span) or span[0] >= span[1]:
                    out.append("a driver that is not continuous needs a range, its low end below its high end")
        for coupling in self.couplings:
            if coupling.kind not in COUPLING_KINDS:
                out.append(f"a coupling's kind must be one of {', '.join(COUPLING_KINDS)}")
                continue
            a, b = motion_of(coupling.a, "coupling"), motion_of(coupling.b, "coupling")
            if a is None or b is None:
                continue
            if coupling.a == coupling.b:
                out.append(f"a coupling names joint {coupling.a} on both sides")
            turning = {"hinge", "gear"}
            if coupling.kind == "gear":
                if a.kind not in turning or b.kind not in turning:
                    out.append("a gear coupling joins two joints that turn")
                if coupling.ratio is None or not math.isfinite(coupling.ratio) or coupling.ratio == 0.0:
                    out.append("a gear coupling needs a ratio that is a number other than zero")
            else:
                if {a.kind in turning, b.kind == "slider"} != {True} and {b.kind in turning, a.kind == "slider"} != {True}:
                    out.append(f"a {coupling.kind} coupling joins one joint that turns and one that slides")
                size = coupling.pitch_radius_mm if coupling.kind == "rack" else coupling.lead_mm
                if size is None or not math.isfinite(size) or size == 0.0:
                    out.append(f"a {coupling.kind} coupling needs its "
                               f"{'pitch radius' if coupling.kind == 'rack' else 'lead'}, a number other than zero")
        return out


def _finite_number(value: Any, name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a number") from exc
    if not math.isfinite(number):
        raise ValueError(f"{name} must be a finite number")
    return number


def _three_numbers(value: Any, name: str) -> tuple[float, float, float]:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ValueError(f"{name} must be three numbers (x, y, z)")
    x, y, z = (_finite_number(c, name) for c in value)
    return (x, y, z)


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

    ``motion`` is set on a joint whose parts move after printing (a
    hinge, slider, ball or gear): the axis and range the joint is swept
    through when its clearance is checked (:class:`JointMotion`).  ``None``
    means the interface declares no motion.
    """

    part_a_id: str
    part_b_id: str
    joint_type: str  # snap_fit, press_fit, clearance_fit, threaded, glued, magnetic, loose
    clearance_mm: float = 0.2
    tolerance_mm: float = 0.1
    contact_area_mm2: float = 0.0
    magnet_polarity_aligned: bool | None = None
    fastener_spec: FastenerSpec | None = None
    motion: JointMotion | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["fastener_spec"] = (
            self.fastener_spec.to_dict() if self.fastener_spec is not None else None
        )
        data["motion"] = self.motion.to_dict() if self.motion is not None else None
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
    screw_hole: dict[str, Any] | None = None
    """Compensated self-threading hole detail when this joint drives a
    screw INTO a printed part: from kiln-pro (the engine lives there; see
    https://kiln3d.com), or on an install without it from Kiln's servers
    when the caller's plan includes Kiln Pro.  Carries both hole
    diameters, thread engagement, install-torque ceiling, and lead-in
    chamfer.  ``None`` for through-bolts into metal/wood, non-fastened
    joints, or when neither answers."""
    motion_sweep: dict[str, Any] | None = None
    """For a joint declared with a motion: its parts posed through the
    declared range and read by Kiln's servers
    (:mod:`kiln._pro_assembly_bridge`), the sweep as they gave it.  ``None``
    when the interface declares no motion or there is no sweep (a
    recommendation says why)."""

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
    mechanism: Mechanism | None = None

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
            **({"mechanism": self.mechanism.to_dict()} if self.mechanism is not None else {}),
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
            mechanism=Mechanism.from_dict(data.get("mechanism")),
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
                    motion=JointMotion.from_dict(i.get("motion")),
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
                    motion_sweep=j.get("motion_sweep"),
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
def _load_design_templates() -> dict[str, Any]:
    """Load design_templates.json with ``@lru_cache``."""
    path = Path(__file__).resolve().parent / "data" / "design_knowledge" / "design_templates.json"
    try:
        with open(path, encoding="utf-8") as fh:
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
            # Facet normal from vertex winding (right-hand rule): n = (b-a) x (c-a).
            # A real unit normal, not (0,0,0): PrusaSlicer's binary-STL loader
            # rejects an all-zero-normal mesh ("Loading of a model file failed").
            ax, ay, az = tri[0][0], tri[0][1], tri[0][2]
            bx, by, bz = tri[1][0], tri[1][1], tri[1][2]
            cx, cy, cz = tri[2][0], tri[2][1], tri[2][2]
            nx = (by - ay) * (cz - az) - (bz - az) * (cy - ay)
            ny = (bz - az) * (cx - ax) - (bx - ax) * (cz - az)
            nz = (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)
            length = (nx * nx + ny * ny + nz * nz) ** 0.5
            if length > 0.0:
                nx, ny, nz = nx / length, ny / length, nz / length
            f.write(struct.pack("<fff", nx, ny, nz))
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
    *,
    printer_id: str | None = None,
    mating: str | None = None,
    _clearance_cache: dict[Any, Any] | None = None,
    _sweep: bool = True,
    _interfaces: list[MatingInterface] | None = None,
) -> JointValidation:
    """Validate a single mating interface against design rules.

    A joint declared with a motion (``MatingInterface.motion``) is swept
    through its range with the parts' own files: everything joined to its
    moving part moves with it, every other part in *parts* stays (read from
    ``_interfaces``, the assembly's full list; this interface alone when not
    given), where they meet is an issue, the tightest gap is said, and
    ``motion_sweep`` carries the numbers.  A joint that could not be swept
    says why.

    A joint whose parts have to move is checked against a gap derived
    for the machine and material rather than a fixed minimum; the
    printer is taken from ``printer_id`` or, failing that, from the
    user's active printer.  Free tier and un-configured setups keep the
    static check.

    When kiln-pro is installed AND the interface drives a screw into a
    printed part, the result also carries a ``screw_hole`` block with the
    compensated self-threading hole (diameter, engagement, torque,
    chamfer) — Bambu-calibrated on a Bambu X1/P1/A1 + PLA/PETG named by
    ``printer_id``, otherwise a generic starting point.

    On an install without kiln-pro, a caller whose plan includes Kiln Pro
    gets Kiln Pro's answers for the joint (the glue, the hole a screw
    threads into, the gap for moving parts) from Kiln's servers
    (:mod:`kiln._pro_assembly_bridge`); when they do not arrive, the joint
    is checked against the standard rules and the recommendations say
    why, once per validation.  Below Kiln Pro, ``screw_hole`` is ``None``.
    """
    from kiln._pro_assembly_bridge import asking

    with asking() as servers:
        validation = _validate_joint(
            interface,
            parts,
            printer_id=printer_id,
            mating=mating,
            _clearance_cache=_clearance_cache,
            sweep=_sweep,
            interfaces=_interfaces,
        )
        said = servers.note()
    if said:
        validation.recommendations.append(said)
    return validation


def _validate_joint(
    interface: MatingInterface,
    parts: dict[str, AssemblyPart] | list[AssemblyPart],
    *,
    printer_id: str | None,
    mating: str | None,
    _clearance_cache: dict[Any, Any] | None,
    sweep: bool = True,
    interfaces: list[MatingInterface] | None = None,
) -> JointValidation:
    """:func:`validate_joint`'s rules, inside the validation's questions."""
    jtype = interface.joint_type
    issues: list[str] = []
    rules_checked: list[str] = []
    recommendations: list[str] = []

    # A declared motion says which gap the clearance is judged in, unless
    # the caller named one; and it cannot sit on a joint that holds its
    # parts fixed, or the print comes off the plate as one piece.
    motion = getattr(interface, "motion", None)
    if motion is not None:
        if mating is None:
            mating = motion.mating
        if jtype in _FIXED_JOINT_TYPES:
            rules_checked.append("motion_on_fixed_joint")
            issues.append(
                f"A {jtype} joint holds its parts fixed, but this interface "
                f"declares a {motion.kind} motion. Record the motion on the "
                "interface where the parts move (a clearance_fit or loose "
                "joint), or drop it."
            )

    # Resolve part materials — accept dict or list
    if isinstance(parts, dict):
        parts_by_id = parts
    else:
        parts_by_id = {p.part_id: p for p in parts}
    mat_a = parts_by_id[interface.part_a_id].material.upper() if interface.part_a_id in parts_by_id else "PLA"
    mat_b = parts_by_id[interface.part_b_id].material.upper() if interface.part_b_id in parts_by_id else "PLA"

    # Load design template data (if available for this joint type)
    templates = _load_design_templates()
    template_key = _JOINT_TEMPLATE_MAP.get(jtype)
    template = templates.get(template_key, {}) if template_key else {}

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
        # Validate against the SAME number the recommendation gives, or
        # the two disagree and a design passes a check it will fail on
        # the plate: the static minimum is one number for every machine,
        # so a gap it calls fine can still be one this printer closes.
        running_view = _running_clearance_view(
            jtype,
            material=mat_a,
            printer_id=printer_id,
            mating=mating,
            cache=_clearance_cache,
        )
        if running_view is not None:
            minimum_mm, running_detail = running_view
            if interface.clearance_mm < minimum_mm:
                issues.append(
                    f"Clearance {interface.clearance_mm:.2f} mm is below the "
                    f"{minimum_mm:.2f} mm these parts need to still move on "
                    f"this printer in {mat_a} — they are likely to come off "
                    f"the plate fused together."
                )
        elif interface.clearance_mm < clearance_range[0]:
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

        # Which adhesive? Pro+ gets the cited recommendation inline; a caller
        # whose plan does not include it gets a nudge to the adhesive
        # intelligence.  The recommendation itself is computed and tier-gated
        # in kiln-pro — nothing curated is decided here.  A paying caller
        # whose answer did not arrive is told why once (validate_joint) and
        # never pitched the plan they hold.
        rules_checked.append("adhesive_recommendation")
        hint = _adhesive_hint_for_joint(mat_a, mat_b)
        if hint:
            recommendations.append(hint)
        elif hint is None and not _has_pro_license():
            recommendations.append(
                "Bonding two printed plastics? Kiln Pro's adhesive intelligence "
                "recommends the right cited adhesive, surface prep, and the full "
                "cure schedule for your exact materials (recommend_adhesive). "
                "More: https://kiln3d.com/pricing"
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

    # -- Material compatibility from design_templates.json ----------------
    if template and "material_compatibility" in template:
        compat = template["material_compatibility"]
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

    # kiln-pro screw-hole intelligence: when this joint drives a screw
    # INTO a printed part, attach the compensated self-threading hole
    # (diameter, engagement, torque, chamfer).  When there is none to
    # give, this is None and nothing else changes.
    screw_hole = _screw_hole_detail_for_joint(
        getattr(interface, "fastener_spec", None), mat_a, printer_id,
    )
    if screw_hole is not None:
        rules_checked.append("compensated_screw_hole")
        recommendations.append(_screw_hole_recommendation(screw_hole))

    motion_sweep = None
    if motion is not None and jtype not in _FIXED_JOINT_TYPES:
        rules_checked.append("motion_sweep")
        motion_sweep = _sweep_motion(
            interface, parts_by_id, sweep, issues, recommendations, interfaces,
        )
        if motion_sweep is not None and jtype in _MOVING_JOINT_TYPES:
            rules_checked.append("measured_running_gap")
            _judge_measured_gap(
                motion_sweep, interface.clearance_mm, issues,
                _running_clearance_view(
                    jtype, material=mat_a, printer_id=printer_id, mating=mating,
                    cache=_clearance_cache,
                ),
                mat_a,
            )

    valid = len(issues) == 0

    return JointValidation(
        joint_type=jtype,
        valid=valid,
        issues=issues,
        design_rules_checked=rules_checked,
        recommendations=recommendations,
        screw_hole=screw_hole,
        motion_sweep=motion_sweep,
    )


def _judge_measured_gap(
    motion_sweep: dict[str, Any],
    declared_mm: float,
    issues: list[str],
    running_view: tuple[float, dict[str, Any]] | None,
    material: str,
) -> None:
    """The gap the parts are modelled with, judged by the rule the declared
    clearance meets: Kiln Pro's gap for this printer and material when it
    arrived, else the clearance-fit minimum.  A record can declare one gap
    and the geometry hold another; what prints is the geometry."""
    gap = motion_sweep.get("printed_gap_mm")
    if gap is None or motion_sweep.get("printed_touching"):
        return  # far apart, or already said: modelled as one piece
    if running_view is not None:
        floor_mm = running_view[0]
        needed = f"the {floor_mm:.2f} mm these parts need to still move on this printer in {material}"
    else:
        floor_mm = _DEFAULT_JOINT_CLEARANCES["clearance_fit"][0]
        needed = f"the {floor_mm:.2f} mm minimum for parts that move"
    if gap < floor_mm:
        issues.append(
            f"As modelled its parts are only {gap:.2f} mm apart (the joint declares "
            f"{declared_mm:.2f} mm), under {needed}: they are likely to come off the "
            "plate fused, or too stiff to move."
        )


def _sweep_motion(
    interface: MatingInterface,
    parts_by_id: dict[str, AssemblyPart],
    allowed: bool,
    issues: list[str],
    recommendations: list[str],
    interfaces: list[MatingInterface] | None = None,
) -> dict[str, Any] | None:
    """Ask for the sweep of the interface's declared motion and say what it
    found: where the parts meet is an issue, a clear range a recommendation,
    no sweep a recommendation saying why.  A record that contradicts itself
    (a loop of joints, one part on both sides) is an issue, worked out here."""
    from kiln._pro_assembly_bridge import current
    from kiln.joint_record import JOINT_WORDS, MAX_JOINTS_PER_CHECK, RecordContradiction

    what = JOINT_WORDS.get(interface.motion.kind, "joint")  # type: ignore[union-attr]
    if not allowed:
        recommendations.append(
            f"The {what} was not swept through its range: one check sweeps the "
            f"first {MAX_JOINTS_PER_CHECK} joints that move."
        )
        return None
    swept, why = current().joint_sweep(interface, parts_by_id, interfaces or [interface])
    if swept is None:
        if isinstance(why, RecordContradiction):
            issues.append(f"The {what} cannot move as declared: {why}.")
        elif why:
            recommendations.append(f"The {what} was not swept through its range: {why}.")
        return None
    faulty = bool(swept.get("binds") or swept.get("printed_touching"))
    said = swept.get("summary")
    if isinstance(said, str) and said:
        (issues if faulty else recommendations).append(said)
    hold = swept.get("hold") if isinstance(swept.get("hold"), dict) else {}
    if isinstance(hold.get("summary"), str) and hold["summary"]:
        recommendations.append(hold["summary"])
    return {key: value for key, value in swept.items() if key != "readings"}


def validate_assembly(
    assembly: Assembly, *, printer_id: str | None = None,
) -> Assembly:
    """Run all clearance checks and joint validations.

    Sets ``overall_valid`` to ``False`` if any overlap is detected or
    any joint validation fails.  Returns the mutated assembly.

    ``printer_id`` names the machine a screwed-into-plastic joint's
    compensated ``screw_hole`` block is calibrated for (see
    :func:`validate_joint` for who gets one); omit it and each joint is
    judged generically.
    """
    from kiln._pro_assembly_bridge import asking

    check_all_clearances(assembly)

    joint_validations: list[JointValidation] = []
    # One cache for the whole sweep — every interface asks the same
    # calibration question and it cannot change mid-call.  The same goes
    # for Kiln's servers: each question once, a miss said once.
    clearance_cache: dict[Any, Any] = {}
    from kiln.joint_record import MAX_JOINTS_PER_CHECK

    moving = 0
    with asking():
        for iface in assembly.interfaces:
            if getattr(iface, "motion", None) is not None:
                moving += 1
            jv = validate_joint(
                iface,
                assembly.parts,
                printer_id=printer_id,
                _clearance_cache=clearance_cache,
                _sweep=moving <= MAX_JOINTS_PER_CHECK,
                _interfaces=assembly.interfaces,
            )
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
    if assembly.mechanism is not None:
        # The parts still print; what cannot be is playing them together.
        all_recs.extend(
            f"The mechanism cannot be played as declared: {problem}."
            for problem in assembly.mechanism.problems(assembly.interfaces)
        )
    assembly.recommendations = all_recs

    return assembly


def compose_assembly(
    assembly: Assembly,
    output_path: str,
) -> dict[str, Any]:
    """Merge all assembly parts into a single positioned STL.

    Each part's vertices are translated by its ``position_mm`` offset
    before merging with :func:`compose_stls`.  Joints declared with a
    motion are kept beside the file (:mod:`kiln.joint_stage`): which
    triangles move, the motion, and the sweep of it, so the stage can play
    each joint; the result's ``joints`` says what each one does.
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

    out: dict[str, Any] = {
        "output_path": output_path,
        "total_triangles": result.get("total_triangles", 0),
        "bounding_box": bbox,
    }
    joints = _record_joints(assembly, result, output_path)
    if joints:
        out["joints"] = joints
    return out


def _record_joints(
    assembly: Assembly, merged: dict[str, Any], output_path: str,
) -> list[dict[str, Any]]:
    """Keep each joint that moves beside the composed file, and say what it
    does: the sweep's sentence, or why it has none."""
    from kiln import joint_stage
    from kiln._pro_assembly_bridge import asking

    moving = [i for i in assembly.interfaces if getattr(i, "motion", None) is not None]
    if not moving:
        return []
    spans: dict[str, tuple[int, int]] = {}
    first = 0
    for part, stats in zip(assembly.parts, merged.get("per_file") or [], strict=False):
        count = int(stats.get("triangles", 0))
        spans[part.part_id] = (first, count)
        first += count
    entries = []
    said = []
    parts_by_id = {p.part_id: p for p in assembly.parts}
    with asking() as servers:  # every moving joint asked at once, a miss said once
        swept = {
            id(iface): servers.joint_sweep(iface, parts_by_id, assembly.interfaces)
            for iface in moving if iface.part_b_id in spans
        }
    for iface in moving:
        if iface.part_b_id not in spans:
            continue
        entry = joint_stage.joint_entry(
            iface, spans, assembly.parts, assembly.interfaces, swept=swept[id(iface)],
        )
        entries.append(entry)
        sweep = entry.get("sweep") or {}
        said.append({
            "part_a": iface.part_a_id,
            "part_b": iface.part_b_id,
            "kind": iface.motion.kind,  # type: ignore[union-attr]
            "binds": sweep.get("binds"),
            "says": sweep.get("summary") or f"Not swept: {entry.get('sweep_why_not')}.",
        })
    joint_stage.record(output_path, entries, total_triangles=first)
    return said


# Calibration narrowing — the half-width that the HIGH tier maps to
# (matches the `confidence_to_band_mm` table in
# kiln_pro.engineering.calibration_coach).  The ratio
# `tier_accuracy_mm / _CALIBRATION_REFERENCE_HALF_WIDTH_MM` controls
# how much we shrink each joint's clearance range.  The reference
# ±0.20mm matches the LOW tier (no calibration) so a HIGH tier (±0.10)
# halves the half-width, MEDIUM (±0.18) shaves ~10%, LOW leaves the
# range unchanged.
_CALIBRATION_REFERENCE_HALF_WIDTH_MM = 0.20


def get_clearance_recommendation(
    joint_type: str,
    material_a: str = "PLA",
    material_b: str = "PLA",
    *,
    printer_id: str | None = None,
    mating: str | None = None,
) -> dict[str, Any]:
    """Return recommended clearance, tolerance, and rationale.

    For flexible materials, clearance is increased by 50%.
    For brittle materials with snap_fit, a warning is included.

    When ``printer_id`` is supplied AND kiln-pro is installed, the
    function consults the user's calibration tier (via
    ``calibration_coach.calibration_for``) and narrows the clearance
    range proportionally to the tier's dimensional accuracy.  HIGH tier
    (±0.10mm) halves the range, MEDIUM (±0.18mm) shaves ~10%, LOW /
    UNKNOWN leave it unchanged.  The narrowing is centered on the
    historic midpoint so press-fit interference stays negative and snap-
    fit clearance stays positive.

    The lookup uses ``material_a`` (the more-rigid material is normally
    the body of a snap-fit cantilever or the receiver of a press-fit
    insert; treating it as the calibration anchor keeps the chosen
    range honest about the part where dimensional drift matters most).

    Free users (kiln-pro not installed) get the historic behaviour
    exactly, and the response always carries a ``calibration_used``
    field (empty when no calibration was applied) so downstream
    consumers can rely on the field's presence.

    For a joint whose parts MOVE, omitting ``printer_id`` no longer
    means "answer generically": the user's active printer is resolved
    from their config, because a call site that does not pass a printer
    is almost never asking to ignore the one they own.  The
    ``running_clearance`` block records whether the machine was supplied
    or inferred.  With no configured printer — a bare library call, or
    the hosted server with its empty registry — the static answer
    stands.  On an install without kiln-pro that gap comes from Kiln's
    servers; when it does not arrive, the static answer stands and
    ``warnings`` says why.
    """
    clearance_range = _DEFAULT_JOINT_CLEARANCES.get(joint_type, (0.1, 0.5))
    base_clearance = (clearance_range[0] + clearance_range[1]) / 2.0
    tolerance = abs(clearance_range[1] - clearance_range[0]) / 2.0
    effective_range = (clearance_range[0], clearance_range[1])

    mat_a_upper = material_a.upper()
    mat_b_upper = material_b.upper()
    flexible = mat_a_upper in _FLEXIBLE_MATERIALS or mat_b_upper in _FLEXIBLE_MATERIALS
    brittle = mat_a_upper in _BRITTLE_MATERIALS or mat_b_upper in _BRITTLE_MATERIALS

    rationale_parts: list[str] = [
        f"Base clearance for {joint_type}: {base_clearance:.2f} mm "
        f"(range {clearance_range[0]:.2f} to {clearance_range[1]:.2f} mm)."
    ]
    warnings: list[str] = []
    calibration_used: dict[str, Any] = {}
    running_clearance: dict[str, Any] = {}

    # Calibration narrowing — only consulted when caller supplied a
    # printer_id AND kiln-pro is importable.  Failures degrade silently
    # to the historic flat-range path; the response shape stays the
    # same so callers can rely on the calibration_used key.
    if printer_id is not None:
        cal_view = _calibration_view_for_clearance(
            printer_id=printer_id,
            material=material_a,
        )
        if cal_view is not None:
            verdict_block, narrow_factor, tier_label = cal_view
            calibration_used = verdict_block
            if narrow_factor < 1.0:
                midpoint = (clearance_range[0] + clearance_range[1]) / 2.0
                half_width = (clearance_range[1] - clearance_range[0]) / 2.0
                new_half = half_width * narrow_factor
                effective_range = (
                    midpoint - new_half,
                    midpoint + new_half,
                )
                tolerance = new_half
                rationale_parts.append(
                    f"Calibration tier {tier_label.upper()} "
                    f"(plus-or-minus {verdict_block.get('expected_accuracy_mm', 0):.2f} mm) "
                    f"narrows the range to "
                    f"{effective_range[0]:.2f}-{effective_range[1]:.2f} mm."
                )

    # A joint that has to MOVE gets a derived gap rather than the middle
    # of a static band.  This runs after the narrowing above and replaces
    # its answer for moving joints only: the narrowing shrinks a fixed
    # range around its historic midpoint, which is a percentage tweak on
    # a number that was never about this printer or this material.
    from kiln._pro_assembly_bridge import asking

    with asking() as servers:
        running_view = _running_clearance_view(
            joint_type,
            material=material_a,
            printer_id=printer_id,
            mating=mating,
        )
        said = servers.note()
    if said:
        warnings.append(said)
    if running_view is not None:
        base_clearance, running_detail = running_view
        running_clearance = running_detail
        assumed = (
            " Shape not specified, so the more demanding round-joint case "
            "was assumed."
            if running_detail.get("mating_assumed")
            else ""
        )
        rationale_parts.append(
            f"These parts have to move against each other, so the gap is "
            f"derived for this printer and material rather than taken from "
            f"a fixed table: {base_clearance:.2f} mm, leaving about "
            f"{running_detail['expected_as_printed_mm']:.2f} mm of real "
            f"space once the walls close in.{assumed}"
        )

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

    if running_clearance:
        # Make the three numbers agree, AFTER every adjustment above.
        #
        # A running clearance is a FLOOR, not a target: more room costs a
        # little play, less welds the parts together.  A symmetric
        # tolerance cannot say that, so it is clamped to the distance
        # actually available above the floor — otherwise a caller reading
        # `recommended +/- tolerance` designs a gap the recommendation
        # exists to rule out.  It reached zero-minus-side honestly rather
        # than pretending there is give that is not there.
        #
        # The range has to be recomputed here too: the flexible-material
        # bump above raises the recommendation without knowing about the
        # floor, which on TPU put the recommended value outside its own
        # range.
        floor_mm = float(running_clearance["recommended_mm"])
        effective_range = (
            floor_mm,
            max(base_clearance, effective_range[1], floor_mm),
        )
        tolerance = min(tolerance, max(0.0, base_clearance - floor_mm))
        rationale_parts.append(
            f"{floor_mm:.2f} mm is a minimum rather than a target — more "
            f"room only costs a little play, less can weld the parts "
            f"together."
        )

    # A recommendation has to lie inside the range it ships with, on
    # every path.  The flexible-material bump raises the recommendation
    # without touching the range, which has always reported a threaded
    # TPU joint as 0.30 mm recommended against a 0.15-0.25 mm range.
    # Widening the range to contain its own recommendation changes no
    # advice; it stops the payload contradicting itself.
    effective_range = (
        min(effective_range[0], base_clearance),
        max(effective_range[1], base_clearance),
    )

    return {
        "joint_type": joint_type,
        "material_a": material_a,
        "material_b": material_b,
        "recommended_clearance_mm": round(base_clearance, 3),
        "tolerance_mm": round(tolerance, 3),
        "clearance_range_mm": [
            round(effective_range[0], 3),
            round(effective_range[1], 3),
        ],
        "rationale": " ".join(rationale_parts),
        "warnings": warnings,
        "calibration_used": calibration_used,
        "running_clearance": running_clearance,
    }


def _calibration_view_for_clearance(
    *,
    printer_id: str,
    material: str,
) -> tuple[dict[str, Any], float, str] | None:
    """Resolve the calibration verdict for ``(printer_id, material)``.

    Lazy-imports ``kiln_pro.engineering.calibration_coach`` so public
    Kiln keeps working without the pro package installed.  Returns
    ``None`` when the package is missing OR any error occurs during
    lookup — the caller falls back to the historic flat-range path.

    The narrow-factor maps the verdict's expected accuracy onto a
    half-width multiplier:

    - HIGH (±0.10mm) → 0.5  (halves the range)
    - MEDIUM (±0.18mm) → 0.9 (~10% narrower)
    - LOW (±0.20mm) → 1.0 (unchanged)
    - UNKNOWN (±0.30mm) → 1.0 (unchanged — wider would mislead)

    The ratio uses the LOW tier's ±0.20mm as the reference half-width
    so a no-calibration call returns 1.0 (no change) by construction.
    """
    try:
        from kiln_pro.engineering.calibration_coach import (  # type: ignore[import-not-found]
            calibration_for,
            calibration_used_block,
        )
    except ImportError:
        return None

    try:
        verdict = calibration_for(printer_id, material)
        verdict_block = calibration_used_block(verdict, printer_id=printer_id)
    except Exception:
        return None

    tier = verdict_block.get("tier", "unknown")
    accuracy_mm = verdict_block.get("expected_accuracy_mm")
    if not isinstance(accuracy_mm, (int, float)) or accuracy_mm <= 0:
        return verdict_block, 1.0, str(tier)

    # Only HIGH and MEDIUM produce a narrowing — LOW / UNKNOWN keep
    # the historic flat range so we never over-promise on a
    # poorly-calibrated machine.
    if tier in ("low", "unknown"):
        narrow_factor = 1.0
    else:
        narrow_factor = min(
            1.0,
            float(accuracy_mm) / _CALIBRATION_REFERENCE_HALF_WIDTH_MM,
        )

    return verdict_block, narrow_factor, str(tier)


#: Joint types where the two parts are meant to MOVE against each other
#: once assembled, and so need a running clearance rather than an
#: assembly clearance.  A snap-fit, press-fit, threaded or glued joint is
#: static once together and is deliberately absent.
_MOVING_JOINT_TYPES: frozenset[str] = frozenset({"clearance_fit", "loose"})

#: What to assume when the caller has not said what SHAPE is moving.
#: ``MatingInterface`` records a joint type, not a geometry, so an
#: unqualified moving joint could be a shaft in a bore or two flat faces
#: sliding.  A bore closes on the part from both sides and needs about
#: twice the allowance a single flat gap does, so assuming it is the
#: erring-loose choice: too generous a gap on a flat joint just adds
#: play, while too tight a bore seizes.  Callers who know say so.
_DEFAULT_MOVING_MATING = "pin_in_bore"


def _resolve_printer_for_clearance(explicit: str | None) -> tuple[str | None, bool]:
    """The machine to judge a moving joint against.

    An explicit ``printer_id`` always wins.  Otherwise the user's ACTIVE
    printer is resolved from their config, because "the caller did not
    pass a printer" was never a request for a generic answer — it is
    almost always a call site that predates the parameter.  Without this,
    a machine-aware clearance only reaches callers who already knew to
    ask for it, which leaves every existing assembly path on the static
    table forever.

    Returns ``(printer_id, was_resolved)`` so the response can say the
    machine was inferred rather than supplied.  ``None`` when there is no
    configured printer to infer — a bare library call, or the hosted
    server, which boots with an empty printer registry and therefore
    keeps the static answer.
    """
    if explicit:
        return explicit, False
    try:
        from kiln.printer_model_resolver import resolve_printer_model

        return resolve_printer_model(), True
    except Exception:  # noqa: BLE001 — no config, unreadable config
        return None, False


def _running_clearance_view(
    joint_type: str,
    *,
    material: str,
    printer_id: str | None,
    mating: str | None = None,
    cache: dict[Any, Any] | None = None,
) -> tuple[float, dict[str, Any]] | None:
    """Machine-aware running clearance for a joint that has to move.

    The static table above answers every printer and every material with
    one number.  Kiln Pro derives the gap from the material's own
    behaviour, the printer's calibration and anything measured about how
    that machine actually prints — so PLA on a calibrated machine and ABS
    on an unknown one stop getting the same answer.  See
    https://kiln3d.com/pricing.

    On an install without kiln-pro the gap comes from Kiln's servers
    (:mod:`kiln._pro_assembly_bridge`), which say why once when it does
    not arrive.

    Returns ``None`` — and the caller keeps the historic static path —
    when the joint does not move, when no printer was named, when the
    caller's tier does not include it, when the gap does not arrive, or
    on any error.  Free tier is not a degraded answer here, it is the
    answer Kiln has always given.

    The tier check is :func:`_has_pro_license`, which resolves the
    CALLER rather than the process: on the hosted server the process
    holds a licence for everybody, so asking whether kiln-pro is
    importable would hand the derivation to every free caller.
    """
    if joint_type not in _MOVING_JOINT_TYPES:
        return None
    printer_id, printer_resolved = _resolve_printer_for_clearance(printer_id)
    if not printer_id:
        return None
    # Resolving calibration scans the filesystem for slicer profiles, and
    # validating an assembly asks the same question once per interface —
    # 7 ms a joint that a 20-joint assembly pays twenty times over for one
    # unchanging fact.  The cache is supplied by the CALLER and lives for
    # one call on purpose: a longer-lived one would answer a later call
    # from profiles the user has since edited.
    key = (joint_type, material, printer_id, mating)
    if cache is not None and key in cache:
        return cache[key]
    if not _has_pro_license():
        if cache is not None:
            cache[key] = None
        return None
    try:
        from kiln_pro.engineering.running_clearance import (  # type: ignore[import-not-found]
            recommend_running_clearance,
        )
    except ImportError:
        from kiln._pro_assembly_bridge import current

        verdict = current().running_clearance(
            mating or _DEFAULT_MOVING_MATING, material, printer_id,
        )
    else:
        try:
            verdict = recommend_running_clearance(
                mating or _DEFAULT_MOVING_MATING,
                material,
                printer_id=printer_id,
            )
        except Exception:
            verdict = None
    if verdict is None:
        if cache is not None:
            cache[key] = None
        return None

    detail: dict[str, Any] = {
        "recommended_mm": verdict.recommended_mm,
        "modeled_quantity": verdict.modeled_quantity,
        "confidence": verdict.confidence,
        "binding_term": verdict.binding_term,
        "expected_as_printed_mm": verdict.expected_as_printed_mm,
        "mating": verdict.mating,
        "mating_assumed": mating is None,
        "printer_id": printer_id,
        "printer_resolved": printer_resolved,
    }
    result = (verdict.recommended_mm, detail)
    if cache is not None:
        cache[key] = result
    return result


def _adhesive_hint_for_joint(material_a: str, material_b: str) -> str | None:
    """Tier-gated adhesive recommendation line for a glued joint, via kiln-pro.

    Pro+ → a one-line cited recommendation (which adhesive + a pointer to
    ``recommend_adhesive``).  ``None`` when there is none to give: below
    Kiln Pro the validator shows an upgrade nudge instead.  On an install
    without kiln-pro the line comes from Kiln's servers
    (:mod:`kiln._pro_assembly_bridge`), and ``""`` there means nothing to
    add (no glue named, or a miss the validation says once, or a sign-in
    to renew).  The recommendation is computed and tier-gated server-side,
    so nothing curated is decided here.
    """
    try:
        from kiln_pro.adhesives import (  # type: ignore[import-not-found]
            adhesive_assembly_hint,
        )
    except ImportError:
        from kiln._pro_assembly_bridge import current

        return current().adhesive_line(material_a, material_b)
    try:
        return adhesive_assembly_hint(material_a, material_b)
    except Exception:
        return None


def _screw_hole_detail_for_joint(
    fastener_spec: Any,
    material: str,
    printer_id: str | None,
) -> dict[str, Any] | None:
    """Compensated self-threading hole for a screw driven into a printed
    part, via kiln-pro's resolver.

    Returns ``None`` when there's no fastener, or when the screw passes
    through into a non-printed substrate (a clearance hole, not a
    self-threading one).  The hole is Kiln Pro's answer at every door:
    where kiln-pro runs (Kiln's servers, where one process answers every
    caller), the caller's own plan is read first and below Kiln Pro the
    joint carries no hole; on an install without kiln-pro the screw's hole
    comes from Kiln's servers when the signed-in plan includes Kiln Pro
    (:mod:`kiln._pro_assembly_bridge`), else ``None``.
    """
    if fastener_spec is None:
        return None
    try:
        from kiln_pro.fasteners_intelligence import (  # type: ignore[import-not-found]
            resolve_threaded_joint,
        )
    except ImportError:
        from kiln._pro_assembly_bridge import current

        return current().screw_hole(fastener_spec, material, printer_id)
    if not _has_pro_license():
        return None
    try:
        joint = resolve_threaded_joint(
            fastener_size=str(getattr(fastener_spec, "size", "") or ""),
            into_substrate=getattr(fastener_spec, "surface_type", None),
            family=getattr(fastener_spec, "family", None),
            material=material,
            printer_id=printer_id,
        )
    except Exception:
        return None
    return joint.to_dict() if joint is not None else None


def _screw_hole_recommendation(detail: dict[str, Any]) -> str:
    """Factual one-line screw-hole callout from a resolver detail dict."""
    size = detail.get("fastener_size", "screw")
    h = detail.get("hole_diameter_horizontal_mm")
    v = detail.get("hole_diameter_vertical_mm")
    eng = detail.get("engagement_length_mm")
    torque = detail.get("install_torque_nm")
    if detail.get("tier") == "bambu_calibrated":
        bits: list[str] = []
        if h is not None and v is not None:
            bits.append(f"{h} mm horizontal / {v} mm vertical hole")
        if eng is not None:
            bits.append(f"{eng} mm thread engagement")
        if torque is not None:
            bits.append(f"{torque} N·m install-torque ceiling")
        bits.append("45° lead-in chamfer")
        return f"{size} self-threading hole (calibrated): " + ", ".join(bits) + "."
    start = f"~{h} mm" if h is not None else "near the screw pitch diameter"
    return (
        f"{size} self-threading hole: start {start}, verify on a test print, "
        f"45° lead-in chamfer (generic starting point — the calibrated "
        f"table covers Bambu X1/P1/A1 + PLA/PETG)."
    )
