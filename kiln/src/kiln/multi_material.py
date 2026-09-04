"""What a printer has for multi-material, in ONE record, from ONE question.

Kiln's multi-material surface grew up Bambu-AMS-shaped.  Every door that
needed to know "can this printer change filament, and what is loaded?"
asked in its own words — ``hasattr(adapter, "get_ams_status")`` at a dozen
sites, ``== "bambu"`` at twenty more, ``isinstance(adapter, BambuAdapter)``
in the CLI — and every one of those spellings answers the same thing for
a Klipper machine carrying a Happy Hare or AFC MMU: *nothing here*.  So a
four-colour file at a Voron ERCF slipped past the colour-mismatch refusal
the AMS path has, ``multi_material_print`` printed every object in one
filament and mentioned it afterwards, and ``kiln print --ams-mapping``
exited 0 having dropped the flag on the floor.

This module is the one place that question is asked and the one place
its answer is written down:

* :func:`multi_material_status` asks the adapter through
  :meth:`~kiln.printers.base.PrinterAdapter.get_multi_material_status`
  and always returns a :class:`MultiMaterialStatus`, even when the
  adapter has no such probe (``kind="none"``) or the probe failed
  (``kind="unknown"``).  A failed read is reported as a failed read —
  never quietly as "no multi-material", which is the silence this
  module exists to end.
* :attr:`MultiMaterialStatus.driven_by_kiln` is the only place the fact
  "Kiln has a hardware-validated slot-control path for this changer" is
  recorded.  Today that is Bambu AMS / AMS Lite and nothing else: Kiln
  can READ a Happy Hare gate map, and says so, but does not route tool
  changes to it.  :data:`KILN_DRIVEN_CHANGERS` is the same fact in the
  ``slicer_profiles.json`` ``tool_changer`` vocabulary, for the estimator
  that has no adapter in hand.

Loaded slots reuse :class:`kiln.ams_routing.Tray` so the colour matcher
the AMS print gate already uses (:func:`kiln.ams_routing.plan_ams_mapping`
/ :func:`kiln.ams_routing.advise_colours`) works unchanged over an MMU
gate map — a read, not a route: the verdict says what is loaded, the
MMU's own tool map still decides where a ``Tn`` goes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from kiln.ams_routing import UNREAD_MATERIAL, Tray, loaded_trays, normalize_hex

logger = logging.getLogger(__name__)

#: Kinds a status can carry.  ``none`` = asked, nothing there; ``unknown``
#: = could not ask.  The two are different facts and never collapse.
KIND_AMS = "ams"
KIND_AMS_LITE = "ams_lite"
KIND_HAPPY_HARE = "happy_hare"
KIND_AFC = "afc"
KIND_CFS = "cfs"
KIND_NONE = "none"
KIND_UNKNOWN = "unknown"
KINDS: frozenset[str] = frozenset({
    KIND_AMS, KIND_AMS_LITE, KIND_HAPPY_HARE, KIND_AFC, KIND_CFS,
    KIND_NONE, KIND_UNKNOWN,
})

#: The changers Kiln DRIVES — reads the loaded slots AND routes each
#: extruder to one, through an adapter path exercised on real hardware.
#: Everything else Kiln can at most read.  ``slicer_profiles.json``'s
#: ``tool_changer`` vocabulary; :mod:`kiln.pre_estimate` derives its
#: ``hardware_unverified`` from this set instead of a per-row flag that
#: has to be remembered (four rows had already forgotten it).
KILN_DRIVEN_CHANGERS: frozenset[str] = frozenset({"ams", "ams_lite"})

#: Human labels for the ``tool_changer`` vocabulary, for sentences that
#: name the changer instead of saying "CFS" to a Voron owner.
CHANGER_LABELS: dict[str, str] = {
    "ams": "Bambu AMS",
    "ams_lite": "Bambu AMS Lite",
    "mmu3": "Prusa MMU3",
    "mmu2s": "Prusa MMU2S",
    "ercf": "ERCF (Happy Hare)",
    "cfs": "Creality CFS",
    "palette": "Mosaic Palette",
    "kcm": "Co Print KCM",
    "chameleon": "3D Chameleon",
    "canvas": "Elegoo Canvas",
    "tool_changer": "tool changer",
    "dual_extruder": "dual extruder",
    "idex": "IDEX",
    KIND_HAPPY_HARE: "Happy Hare MMU",
    KIND_AFC: "AFC MMU",
    KIND_CFS: "Creality CFS",
}


def changer_label(changer: str | None) -> str:
    """The name a user knows the changer by, for ``tool_changer`` ids."""
    key = str(changer or "").strip().lower()
    return CHANGER_LABELS.get(key, key.replace("_", " ") or "multi-material unit")


@dataclass(frozen=True)
class MultiMaterialStatus:
    """One printer's multi-material situation, as read just now.

    ``slots`` holds the LOADED positions only (a gate/tray reporting
    filament), in slot order, as :class:`~kiln.ams_routing.Tray` so the
    existing colour matcher reads them.  ``num_slots`` is the physical
    count when the unit reports one.  ``tool_map`` is Happy Hare's
    tool-to-gate indirection when it exists — the reason a ``T2`` at an
    MMU need not mean gate 2.
    """

    kind: str
    driven_by_kiln: bool
    source: str
    slots: tuple[Tray, ...] = ()
    num_slots: int | None = None
    tool_map: tuple[int, ...] | None = None
    unit_name: str | None = None
    version: str | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def detected(self) -> bool:
        """A multi-material unit was actually seen (not none, not unknown)."""
        return self.kind not in (KIND_NONE, KIND_UNKNOWN)

    @property
    def label(self) -> str:
        return changer_label(self.kind)

    def describe(self) -> str:
        """One sentence a door can put in front of a user."""
        if self.kind == KIND_UNKNOWN:
            why = f" ({self.warnings[0]})" if self.warnings else ""
            return f"Kiln could not read the printer's multi-material state{why}."
        if self.kind == KIND_NONE:
            return "No multi-material unit is reported on this printer."
        where = f" '{self.unit_name}'" if self.unit_name else ""
        count = f"{self.num_slots}-slot " if self.num_slots else ""
        loaded = (
            f"{len(self.slots)} loaded"
            if self.slots
            else "no slot reporting filament"
        )
        if self.driven_by_kiln:
            return f"Kiln sees a {count}{self.label}{where} ({loaded}) and routes filament changes to it."
        return (
            f"Kiln sees a {count}{self.label}{where} ({loaded}) but does not drive "
            f"it yet: the unit's own tool map decides which slot each tool change "
            f"pulls from."
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "detected": self.detected,
            "driven_by_kiln": self.driven_by_kiln,
            "source": self.source,
            "num_slots": self.num_slots,
            "loaded_slots": [
                {"slot": t.slot, "material": t.material, "color": t.hex6}
                for t in self.slots
            ],
            "tool_map": list(self.tool_map) if self.tool_map is not None else None,
            "unit_name": self.unit_name,
            "version": self.version,
            "warnings": list(self.warnings),
            "summary": self.describe(),
        }


def none_status(source: str = "no_probe") -> MultiMaterialStatus:
    return MultiMaterialStatus(kind=KIND_NONE, driven_by_kiln=False, source=source)


def unknown_status(source: str, why: str) -> MultiMaterialStatus:
    return MultiMaterialStatus(
        kind=KIND_UNKNOWN, driven_by_kiln=False, source=source, warnings=[why],
    )


def multi_material_status(adapter: Any) -> MultiMaterialStatus:
    """Ask *adapter* what it has.  Never raises; never answers "none" for a failed read.

    An adapter without :meth:`get_multi_material_status` (or one whose
    default returns ``None``) is a printer Kiln knows no multi-material
    path for — ``kind="none"``.  A probe that raises is ``kind="unknown"``
    carrying the reason, so a door can say "could not read" instead of
    treating a network blip as an empty printer.
    """
    probe = getattr(adapter, "get_multi_material_status", None)
    status: MultiMaterialStatus | None = None
    if callable(probe):
        try:
            status = probe()
        except Exception as exc:  # a read must never take the door down
            logger.debug("multi-material probe failed on %r: %s", adapter, exc)
            status = unknown_status(
                f"{getattr(adapter, 'name', type(adapter).__name__)}:probe_failed",
                f"probe failed: {exc}",
            )
    if not isinstance(status, MultiMaterialStatus):
        # No probe, a probe that knows nothing (the base default), or one
        # that answered in a shape this module does not own: fall back to
        # the duck-typed Bambu contract the rest of the stack has always
        # used, ``get_ams_status``.  Anything Bambu-shaped keeps working
        # unchanged; anything else is an honest ``none``.
        status = _legacy_ams_status(adapter)
    _record_seen(status)
    return status


def _legacy_ams_status(adapter: Any) -> MultiMaterialStatus:
    """The ``get_ams_status`` duck-type, as a status.  Never raises."""
    read = getattr(adapter, "get_ams_status", None)
    label = getattr(adapter, "name", None) or type(adapter).__name__
    if not callable(read):
        return none_status(f"{label}:no_multi_material_path")
    try:
        info = read()
    except Exception as exc:
        logger.debug("AMS not readable on %r: %s", adapter, exc)
        return unknown_status(f"{label}:ams_unreadable", f"AMS not readable: {exc}")
    # Deliberately no printer_model: the only way to learn it here is
    # BambuAdapter's private ``_printer_model``, and a shared module has no
    # business reading another class's privates.  Every in-tree Bambu
    # answers get_multi_material_status directly and never reaches this
    # path, so the AMS/AMS-Lite distinction is lost only for a duck-typed
    # third-party adapter, which is a label, not a behaviour.
    return from_bambu_ams(info if isinstance(info, dict) else None, printer_model=None)


def _record_seen(status: MultiMaterialStatus) -> None:
    """The instrument: which kinds of unit Kiln meets in the field.

    Counted here, at the one chokepoint every door reads through, so the
    heartbeat can answer "how many Klipper installs carry an MMU" — a
    number nobody has today and the one that decides whether lane
    routing is ever worth building.  ``unknown`` is not counted: a
    failed read is evidence of nothing.  Silent by contract.
    """
    if status.kind == KIND_UNKNOWN:
        return
    try:
        from kiln.daily_stats import record_multi_material_seen

        record_multi_material_seen(status.kind)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Builders the adapters use, kept here so the two vendors' shapes are
# normalised in one file and the record they produce is the same record.
# ---------------------------------------------------------------------------


def from_bambu_ams(ams_info: dict[str, Any] | None, *, printer_model: str | None) -> MultiMaterialStatus:
    """A :class:`MultiMaterialStatus` from a Bambu ``get_ams_status`` reading."""
    info = ams_info if isinstance(ams_info, dict) else {}
    units = info.get("units") or []
    exist = str(info.get("ams_exist_bits", "0") or "0").strip()
    trays = str(info.get("tray_exist_bits", "0") or "0").strip()
    model = str(printer_model or "").strip().lower()
    kind = KIND_AMS_LITE if model in ("bambu_a1", "bambu_a1_mini", "bambu_a2l") else KIND_AMS
    if not units and exist == "0" and trays == "0":
        return none_status("bambu:no_ams_hardware")
    slots = tuple(loaded_trays(info))
    num_slots = sum(
        len(u.get("trays") or []) for u in units if isinstance(u, dict)
    ) or None
    warnings: list[str] = []
    if not units:
        warnings.append(
            "AMS hardware bits are set but no tray state was reported — "
            "the MQTT cache may still be repopulating."
        )
    return MultiMaterialStatus(
        kind=kind,
        driven_by_kiln=True,
        source="bambu:get_ams_status",
        slots=slots,
        num_slots=num_slots,
        warnings=warnings,
    )


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


def from_happy_hare(mmu: dict[str, Any], machine: dict[str, Any] | None = None) -> MultiMaterialStatus:
    """A :class:`MultiMaterialStatus` from Klipper's ``mmu`` (+ ``mmu_machine``) objects.

    Happy Hare exposes the gate map as parallel per-gate arrays on the
    ``mmu`` printer object: ``gate_status`` (-1 unknown, 0 empty, 1
    available, 2 available from buffer), ``gate_material``, ``gate_color``
    (hex6, no ``#``), ``gate_filament_name``, ``gate_spool_id`` (-1 =
    none), and ``ttg_map`` (tool → gate).  Verified against Happy Hare
    4.0.0 running in the virtual-klipper-printer simulator, 2026-09-03.
    A gate is LOADED when its status is 1 or 2; unknown (-1) is not a
    load, and a map that is all -1 says so in ``warnings`` rather than
    reading as an empty unit.
    """
    machine = machine if isinstance(machine, dict) else {}
    statuses = _as_list(mmu.get("gate_status"))
    materials = _as_list(mmu.get("gate_material"))
    colours = _as_list(mmu.get("gate_color"))
    try:
        num_gates = int(mmu.get("num_gates") or machine.get("num_gates") or len(statuses)) or None
    except (TypeError, ValueError):
        num_gates = len(statuses) or None

    slots: list[Tray] = []
    for gate, raw_status in enumerate(statuses):
        try:
            status = int(raw_status)
        except (TypeError, ValueError):
            continue
        if status < 1:
            continue
        material = str((materials[gate] if gate < len(materials) else None) or "").strip().upper()
        hex6 = normalize_hex(colours[gate] if gate < len(colours) else None)
        # ``gate_filament_name`` is the SPOOL's name — "Polymaker Galaxy
        # Black", "RedPLA" — and is deliberately NOT used as a fallback
        # material.  It reads like one ("REDPLA"), which is how a brand
        # string reached the colour matcher and produced "colour agrees,
        # material differs" against a real PLA: a mismatch Kiln invented
        # out of a label.  An uncurated gate is UNKNOWN, which the matcher
        # already treats as "cannot judge the material" rather than as a
        # material that disagrees.
        slots.append(Tray(slot=gate, material=material or UNREAD_MATERIAL, hex6=hex6))

    warnings: list[str] = []
    if statuses and all(_int_or(s, -1) < 0 for s in statuses):
        warnings.append(
            "The MMU has not reported which gates hold filament (every gate "
            "status is unknown) — run MMU_CHECK_GATES or set the gate map."
        )
    if mmu.get("enabled") is False:
        warnings.append("The MMU reports itself disabled.")

    ttg = _as_list(mmu.get("ttg_map"))
    tool_map = tuple(_int_or(g, -1) for g in ttg) if ttg else None
    unit_name = None
    unit0 = machine.get("unit_0")
    if isinstance(unit0, dict):
        unit_name = str(unit0.get("display_name") or unit0.get("name") or "") or None
    version = machine.get("happy_hare_version")
    return MultiMaterialStatus(
        kind=KIND_HAPPY_HARE,
        driven_by_kiln=False,
        source="moonraker:mmu",
        slots=tuple(slots),
        num_slots=num_gates,
        tool_map=tool_map,
        unit_name=unit_name,
        version=str(version) if version is not None else None,
        warnings=warnings,
    )


def from_afc(afc: dict[str, Any]) -> MultiMaterialStatus:
    """A :class:`MultiMaterialStatus` from Klipper's ``AFC`` object.

    Read from the AFC-Klipper-Add-On source, NOT from hardware or a
    simulator run — the per-lane shape (``lanes`` keyed by name, each with
    ``lane``/``map``/``load``/``prep``/``material``/``color``) is parsed
    defensively and the status says so in ``warnings``.  Kiln has never
    seen a live AFC.
    """
    lanes_raw = afc.get("lanes")
    lanes: list[dict[str, Any]] = []
    if isinstance(lanes_raw, dict):
        lanes = [dict(v, name=v.get("name", k)) for k, v in lanes_raw.items() if isinstance(v, dict)]
    elif isinstance(lanes_raw, list):
        lanes = [v for v in lanes_raw if isinstance(v, dict)]

    slots: list[Tray] = []
    for index, lane in enumerate(lanes):
        tool = str(lane.get("map") or "").strip().upper()
        if tool.startswith("T") and tool[1:].isdigit():
            slot = int(tool[1:])
        else:
            slot = _int_or(lane.get("lane"), index)
        if not (lane.get("load") or lane.get("prep") or lane.get("tool_loaded")):
            continue
        material = str(lane.get("material") or "").strip().upper()
        hex6 = normalize_hex(lane.get("color"))
        slots.append(Tray(slot=slot, material=material or UNREAD_MATERIAL, hex6=hex6))
    slots.sort(key=lambda t: t.slot)
    return MultiMaterialStatus(
        kind=KIND_AFC,
        driven_by_kiln=False,
        source="moonraker:AFC",
        slots=tuple(slots),
        num_slots=len(lanes) or None,
        warnings=[
            "AFC lane reading is parsed from the add-on's source and has not "
            "been verified against a live AFC unit."
        ],
    )


def from_creality_cfs(cfs: dict[str, Any]) -> MultiMaterialStatus:
    """A :class:`MultiMaterialStatus` from Creality's ``get_cfs_status`` reading.

    The CFS is a real multi-material unit and Kiln has always been able to
    SEE it — the adapter's discovery walks Moonraker for CFS objects and
    normalises whatever slot-shaped data is visible.  It was left out of
    the first cut of this module, so a K2 with a CFS answered ``none``:
    "no multi-material unit", to an owner looking straight at one.  That is
    the same lie this module exists to end, so it is wired here rather than
    left to the door that happens to ask.

    ``driven_by_kiln`` is False and stays False: the adapter's own
    docstring records that Creality publishes no stable slot-control API,
    and the reading itself carries ``hardware_unverified``.  Kiln reads the
    slots and says so; it does not claim to route them.
    """
    detected = bool(cfs.get("detected"))
    raw_slots = cfs.get("slots")
    slots: list[Tray] = []
    if isinstance(raw_slots, list):
        for index, slot in enumerate(raw_slots):
            if not isinstance(slot, dict):
                continue
            material = str(slot.get("material") or "").strip().upper()
            hex6 = normalize_hex(slot.get("color"))
            if not material and hex6 is None:
                continue  # an empty bay is not a loaded slot
            slots.append(Tray(
                slot=_int_or(slot.get("slot"), index),
                material=material or UNREAD_MATERIAL,
                hex6=hex6,
            ))
    slots.sort(key=lambda t: t.slot)
    if not detected and not slots:
        return none_status("creality:no_cfs_discovered")
    warnings = [str(w) for w in (cfs.get("warnings") or []) if w]
    return MultiMaterialStatus(
        kind=KIND_CFS,
        driven_by_kiln=False,
        source="creality:get_cfs_status",
        slots=tuple(slots),
        num_slots=len(raw_slots) if isinstance(raw_slots, list) and raw_slots else None,
        warnings=warnings,
    )


def _int_or(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
