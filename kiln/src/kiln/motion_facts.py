"""The physical facts that decide whether Kiln may move a printer's head.

Every row of ``printer_intelligence.json`` carries a ``motion`` block: which
part of the machine moves in Z, how Z finds its datum and where, whether
the firmware refuses an unhomed move, how high Z may be commanded, and
what park verb the vendor built.  Each field has a provenance entry in
``_sources`` whose ``class`` is the gate's real input: a fact that rests on
a community post or on nothing is stored as ``null`` -- the note keeps the
hearsay for a person, the field never carries it.  Kiln does not move a
head on a forum post.  The public row carries the class and a link-free
note; the references themselves are research provenance and live in Kiln
Pro's motion provenance overlay, like every other research provenance.

The block is deliberately small (thirteen stored fields) and one thing is
derived rather than stored: :attr:`MotionFacts.z_home_descends_onto_plate`
is false only for the three homing methods that provably cannot land on a
part (a top switch, a switch off the print surface, a dedicated strip);
every other method, and an unknown one, descends.  Two sources of truth
for the same fact was the defect this block replaces.

Read by the home and park templates in :mod:`kiln.printers.base`; the
per-model Bambu emitter keeps its own ``purge_station`` record for the
one vendor sequence it drives.  What the connected machine adds for the
session -- its own config settling a cell the vendor left null -- lives in
:mod:`kiln.machine_motion`, and never in this file's data.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

#: The facts the block stores, in the order the catalogue writes them.
MOTION_FIELDS: tuple[str, ...] = (
    "z_carrier",
    "xy_layout",
    "z_home_method",
    "z_home_xy_mm",
    "home_routine_travels_blind",
    "firmware_family",
    "unhomed_move_policy",
    "z_travel_limit_mm",
    "z_travel_limit_kind",
    "park_verb",
    "park_position_mm",
    "park_refuses_unhomed",
    "purge_wipe_station",
)

Z_CARRIERS: frozenset[str] = frozenset({"head", "bed", "delta"})
XY_LAYOUTS: frozenset[str] = frozenset(
    {"i3", "corexy", "corexz", "cross_gantry", "idex", "cantilever", "delta"}
)
Z_HOME_METHODS: frozenset[str] = frozenset(
    {
        "endstop_switch",            # a Z-min switch; the nozzle reaches plate height at the X/Y home corner
        "endstop_switch_off_plate",  # nothing passes over the print surface at plate height (a pin BEHIND the plate: Voron 2.4 / Trident), or the bed moves AWAY from the nozzle (Voron 0.2 bottom switch) -- never a bed that rises to a switch under a toolhead of unknown XY
        "endstop_switch_top",        # Z homes UPWARD (CoreXZ): the one design that cannot touch a part
        "probe_touch",               # CR Touch / BLTouch / a docked switch probe, pressed onto the plate
        "probe_inductive",           # PINDA / inductive, held above the plate at a stated XY
        "probe_eddy",
        "probe_optical",
        "nozzle_contact_plate",      # the nozzle (or a loadcell behind it) pressed onto the plate
        "nozzle_contact_strip",      # the nozzle pressed onto a dedicated strip behind the plate
    }
)
#: Homing methods that provably cannot land on a part left on the plate.
Z_HOME_METHODS_OFF_PLATE: frozenset[str] = frozenset(
    {"endstop_switch_off_plate", "endstop_switch_top", "nozzle_contact_strip"}
)
FIRMWARE_FAMILIES: frozenset[str] = frozenset(
    {
        "marlin_2", "marlin_1", "prusa_firmware", "prusa_buddy", "klipper",
        "klipper_vendor_fork", "reprapfirmware", "bambu", "proprietary",
    }
)
UNHOMED_MOVE_POLICIES: frozenset[str] = frozenset({"refused", "clamped", "unclamped"})
Z_TRAVEL_LIMIT_KINDS: frozenset[str] = frozenset(
    {"firmware_config", "vendor_prose_reach", "firmware_default_user_adjustable"}
)
PARK_VERBS_FIXED: frozenset[str] = frozenset({"G27", "G27_buddy", "none"})
PARK_VERB_MACRO_PREFIX = "klipper_macro:"
PURGE_STATION_KINDS: frozenset[str] = frozenset({"chute", "pad", "bin", "on_plate_strip"})
#: The provenance classes a non-null field may carry.  ``community`` and
#: ``not_found`` are the two that force the field to ``null``.
#: The one class the catalogue never carries: a fact read off the connected
#: machine itself, for this session -- a Klipper unit's own ``printer.cfg``
#: or a Marlin unit's own firmware reports.  It settles a cell the vendor
#: left per-unit (a home spot that is a placeholder in the reference
#: config, a Z ceiling that differs by build), and it is never written
#: back -- the next session reads it again.
SOURCE_CLASS_MACHINE = "machine_config"
SOURCE_CLASSES_SETTLED: frozenset[str] = frozenset(
    {"vendor_config", "vendor_sentence", "vendor_layout", "vendor_slicer", "firmware_docs",
     SOURCE_CLASS_MACHINE}
)
SOURCE_CLASSES_UNSETTLED: frozenset[str] = frozenset({"community", "not_found"})
SOURCE_CLASSES: frozenset[str] = SOURCE_CLASSES_SETTLED | SOURCE_CLASSES_UNSETTLED
#: The cells a Klipper machine's own ``printer.cfg`` can settle when the
#: catalogue left them null.  Nothing else: which part carries Z is not in
#: a config, and a park macro's body is not parsed.
MACHINE_FILLABLE_FIELDS: tuple[str, ...] = (
    "z_home_method",
    "z_home_xy_mm",
    "home_routine_travels_blind",
    "unhomed_move_policy",
    "z_travel_limit_mm",
    "z_travel_limit_kind",
)

#: What a vendor layout name fixes about the Z carrier.  The only inference
#: the block permits, and only with ``class: vendor_layout`` so the plan
#: text can say "the vendor calls this an i3; Kiln infers the head carries Z".
Z_CARRIER_FROM_LAYOUT: dict[str, str] = {
    "i3": "head",
    "corexz": "head",
    "corexy": "bed",
    "delta": "delta",
}


@dataclass(frozen=True)
class MotionSource:
    """Where one motion fact came from, and how much that is worth.

    The public catalogue ships the ``class`` and a note with no links in
    it; the reference itself (URL or file:line) is research provenance and
    lives in Kiln Pro's motion provenance overlay, as every other research
    provenance does.  ``ref`` is therefore empty on the public row.
    """

    source_class: str
    note: str = ""
    ref: str = ""

    @property
    def settled(self) -> bool:
        return self.source_class in SOURCE_CLASSES_SETTLED


@dataclass(frozen=True)
class MotionFacts:
    """One catalogue row's ``motion`` block, validated.

    Every field is ``None`` where the vendor has not said, or has said it
    only where Kiln will not act on it (a community source).  Read the
    provenance for a field through :meth:`source`.
    """

    printer_id: str
    z_carrier: str | None = None
    xy_layout: str | None = None
    z_home_method: str | None = None
    z_home_xy_mm: tuple[float, float] | None = None
    home_routine_travels_blind: bool | None = None
    firmware_family: str | None = None
    unhomed_move_policy: str | None = None
    z_travel_limit_mm: float | None = None
    z_travel_limit_kind: str | None = None
    park_verb: str | None = None
    park_position_mm: dict[str, float] | None = None
    park_refuses_unhomed: bool | None = None
    purge_wipe_station: str | dict[str, Any] | None = None
    sources: dict[str, MotionSource] = field(default_factory=dict)

    # -- derived -----------------------------------------------------------

    @property
    def z_home_descends_onto_plate(self) -> bool:
        """Whether an idle Z home can land on a part left on the plate.

        False only for the three methods that provably cannot; true for
        every other method AND for an unknown one -- the refusing default.
        """
        return self.z_home_method not in Z_HOME_METHODS_OFF_PLATE

    @property
    def z_home_known(self) -> bool:
        return self.z_home_method is not None

    @property
    def z_carrier_inferred(self) -> bool:
        """True when ``z_carrier`` rests on a layout name, not a vendor sentence."""
        src = self.sources.get("z_carrier")
        return bool(src and src.source_class == "vendor_layout")

    @property
    def machine_read_fields(self) -> tuple[str, ...]:
        """The cells this session read off the connected machine's own config."""
        return tuple(
            name for name in MOTION_FIELDS
            if (src := self.sources.get(name)) is not None and src.source_class == SOURCE_CLASS_MACHINE
        )

    def needs_machine_fill(self) -> bool:
        """True when a fillable cell is null, or the unit could report a less safe
        unhomed-move policy than the vendor's -- the only times the machine is read."""
        if any(getattr(self, name) is None for name in MACHINE_FILLABLE_FIELDS):
            return True
        return self.unhomed_move_policy == "clamped"

    def source(self, field_name: str) -> MotionSource | None:
        return self.sources.get(field_name)

    # -- text for the plan and the refusal ----------------------------------

    def describe_z_home(self) -> str:
        """The Z home as a noun phrase a person can act on, for "homes Z by ...".

        Says what descends and where -- "the nozzle pressed onto the PLATE
        at X130 Y130" -- or that nothing does.
        """
        where = ""
        if self.z_home_xy_mm is not None:
            x, y = self.z_home_xy_mm
            where = f" at X{x:g} Y{y:g}"
        read = self.machine_read_fields
        if "z_home_method" in read or "z_home_xy_mm" in read:
            where += " (read from this machine itself)"
        m = self.z_home_method
        if m is None:
            return ("a method Kiln does not know -- it treats the home as one that presses onto the "
                    "plate until the vendor says otherwise")
        if m == "endstop_switch_top":
            return "a switch at the TOP of its travel -- Z homes upward and nothing descends toward the plate"
        if m == "endstop_switch_off_plate":
            return "a switch off the print surface -- nothing descends onto the plate"
        if m == "nozzle_contact_strip":
            return f"the nozzle pressed onto a dedicated strip behind the plate{where}, not the plate itself"
        if m == "nozzle_contact_plate":
            return f"the nozzle pressed onto the PLATE{where}"
        if m == "endstop_switch":
            return "a switch: the nozzle descends to plate height at the X/Y home corner"
        kind = {"probe_touch": "a touch probe", "probe_inductive": "a probe",
                "probe_eddy": "an eddy-current probe", "probe_optical": "an optical probe"}.get(m, m)
        return f"{kind} pressed onto the PLATE{where}"

    def blind_travel_caveat(self) -> str | None:
        """The sentence before step 1 when the routine may travel at an unknown height."""
        if self.home_routine_travels_blind is False:
            return None
        if self.home_routine_travels_blind:
            head = "this printer's own homing routine moves the head sideways before Z is known"
        else:
            head = "the homing routine may move the head sideways before Z is known -- the vendor has not said"
        policy = " and its soft limits will not catch an unhomed move" if self.unhomed_move_policy == "unclamped" else ""
        return f"{head}{policy}; if a tall print is on the plate, stop here and clear it first"

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {name: getattr(self, name) for name in MOTION_FIELDS}
        if out["z_home_xy_mm"] is not None:
            out["z_home_xy_mm"] = list(out["z_home_xy_mm"])
        out["z_home_descends_onto_plate"] = self.z_home_descends_onto_plate
        out["z_carrier_inferred"] = self.z_carrier_inferred
        out["machine_read_fields"] = list(self.machine_read_fields)
        return out


def _enum(value: Any, allowed: frozenset[str], *, printer_id: str, name: str) -> str | None:
    if value is None:
        return None
    if isinstance(value, str) and value in allowed:
        return value
    logger.warning("motion.%s for %r is %r, not one of %s; treated as unknown", name, printer_id, value, sorted(allowed))
    return None


def _bool(value: Any, *, printer_id: str, name: str) -> bool | None:
    if value is None or isinstance(value, bool):
        return value
    logger.warning("motion.%s for %r is %r, not a bool; treated as unknown", name, printer_id, value)
    return None


def _number(value: Any, *, printer_id: str, name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    logger.warning("motion.%s for %r is %r, not a number; treated as unknown", name, printer_id, value)
    return None


def _park_verb(value: Any, *, printer_id: str) -> str | None:
    if value is None:
        return None
    if isinstance(value, str) and (value in PARK_VERBS_FIXED or (
            value.startswith(PARK_VERB_MACRO_PREFIX) and len(value) > len(PARK_VERB_MACRO_PREFIX))):
        return value
    logger.warning("motion.park_verb for %r is %r; treated as unknown", printer_id, value)
    return None


def _xy(value: Any, *, printer_id: str) -> tuple[float, float] | None:
    if value is None:
        return None
    if (isinstance(value, (list, tuple)) and len(value) == 2
            and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in value)):
        return (float(value[0]), float(value[1]))
    logger.warning("motion.z_home_xy_mm for %r is %r, not [x, y]; treated as unknown", printer_id, value)
    return None


def _position(value: Any, *, printer_id: str) -> dict[str, float] | None:
    if value is None:
        return None
    if isinstance(value, dict) and value and all(
            k in ("x", "y", "z") and isinstance(v, (int, float)) and not isinstance(v, bool)
            for k, v in value.items()):
        return {k: float(v) for k, v in value.items()}
    logger.warning("motion.park_position_mm for %r is %r; treated as unknown", printer_id, value)
    return None


def _station(value: Any, *, printer_id: str) -> str | dict[str, Any] | None:
    if value is None or value == "served_plan":
        return value
    if isinstance(value, dict) and value.get("kind") in PURGE_STATION_KINDS:
        return dict(value)
    logger.warning("motion.purge_wipe_station for %r is %r; treated as unknown", printer_id, value)
    return None


def _sources(raw: Any, *, printer_id: str) -> dict[str, MotionSource]:
    out: dict[str, MotionSource] = {}
    if not isinstance(raw, dict):
        return out
    for name, entry in raw.items():
        if not isinstance(entry, dict):
            continue
        cls = entry.get("class")
        if cls not in SOURCE_CLASSES:
            logger.warning("motion._sources.%s for %r has class %r; treated as not_found", name, printer_id, cls)
            cls = "not_found"
        out[name] = MotionSource(
            source_class=str(cls),
            note=str(entry.get("note") or ""),
            ref=str(entry.get("ref") or ""),
        )
    return out


def load_motion_facts(printer_id: str, raw: Any) -> MotionFacts | None:
    """Validate one row's ``motion`` block.  ``None`` when the row has none.

    A field whose provenance class is ``community`` or ``not_found`` is
    loaded as ``None`` whatever the JSON says -- the catalogue test pins
    that the shipped data never puts a value there, and the loader makes
    the same promise for data it did not ship.
    """
    if not isinstance(raw, dict):
        return None
    sources = _sources(raw.get("_sources"), printer_id=printer_id)

    def gated(name: str, value: Any) -> Any:
        if value is None:
            return None
        src = sources.get(name)
        if src is None or not src.settled:
            logger.warning("motion.%s for %r carries a value on an unsettled source; loaded as unknown", name, printer_id)
            return None
        return value

    return MotionFacts(
        printer_id=printer_id,
        z_carrier=gated("z_carrier", _enum(raw.get("z_carrier"), Z_CARRIERS, printer_id=printer_id, name="z_carrier")),
        xy_layout=gated("xy_layout", _enum(raw.get("xy_layout"), XY_LAYOUTS, printer_id=printer_id, name="xy_layout")),
        z_home_method=gated("z_home_method", _enum(raw.get("z_home_method"), Z_HOME_METHODS, printer_id=printer_id, name="z_home_method")),
        z_home_xy_mm=gated("z_home_xy_mm", _xy(raw.get("z_home_xy_mm"), printer_id=printer_id)),
        home_routine_travels_blind=gated("home_routine_travels_blind", _bool(raw.get("home_routine_travels_blind"), printer_id=printer_id, name="home_routine_travels_blind")),
        firmware_family=gated("firmware_family", _enum(raw.get("firmware_family"), FIRMWARE_FAMILIES, printer_id=printer_id, name="firmware_family")),
        unhomed_move_policy=gated("unhomed_move_policy", _enum(raw.get("unhomed_move_policy"), UNHOMED_MOVE_POLICIES, printer_id=printer_id, name="unhomed_move_policy")),
        z_travel_limit_mm=gated("z_travel_limit_mm", _number(raw.get("z_travel_limit_mm"), printer_id=printer_id, name="z_travel_limit_mm")),
        z_travel_limit_kind=gated("z_travel_limit_kind", _enum(raw.get("z_travel_limit_kind"), Z_TRAVEL_LIMIT_KINDS, printer_id=printer_id, name="z_travel_limit_kind")),
        park_verb=gated("park_verb", _park_verb(raw.get("park_verb"), printer_id=printer_id)),
        park_position_mm=gated("park_position_mm", _position(raw.get("park_position_mm"), printer_id=printer_id)),
        park_refuses_unhomed=gated("park_refuses_unhomed", _bool(raw.get("park_refuses_unhomed"), printer_id=printer_id, name="park_refuses_unhomed")),
        purge_wipe_station=gated("purge_wipe_station", _station(raw.get("purge_wipe_station"), printer_id=printer_id)),
        sources=sources,
    )


def catalogue_keys() -> list[str]:
    """Every printer id the catalogue has a row for, the two generic rows excluded."""
    from kiln.printers.bed_fit import _load_printer_intelligence

    data = _load_printer_intelligence() or {}
    return sorted(k for k in data if not k.startswith("_") and k not in ("default", "klipper_generic"))


def motion_facts_for(printer_id: str | None) -> MotionFacts | None:
    """The validated block for a catalogue key, or ``None`` when unknown.

    Resolves the key the way the rest of the adapter layer does
    (``kiln.printers.bed_fit._printer_id_candidates``): a vendor prefix is
    tolerated, a guess is not.
    """
    if not printer_id:
        return None
    try:
        from kiln.printers.bed_fit import _load_printer_intelligence, _printer_id_candidates

        data = _load_printer_intelligence() or {}
        for candidate in _printer_id_candidates(str(printer_id).strip().lower()):
            record = data.get(candidate)
            if isinstance(record, dict) and isinstance(record.get("motion"), dict):
                return load_motion_facts(candidate, record["motion"])
    except Exception:  # noqa: BLE001 -- a missing fact is a refusal downstream, never a crash here
        logger.debug("motion facts unavailable for %r", printer_id, exc_info=True)
    return None
