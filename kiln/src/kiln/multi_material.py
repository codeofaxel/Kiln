"""What a printer has for multi-material, in ONE record, from ONE question."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from kiln.ams_routing import UNREAD_MATERIAL, Tray, loaded_trays, normalize_hex
from kiln.bambu_trays import EXTERNAL_SPOOL_TRAY, NO_TRAY, TRAYS_PER_UNIT, read_tray_id

logger = logging.getLogger(__name__)

# : Kinds a status can carry
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

KILN_DRIVEN_CHANGERS: frozenset[str] = frozenset({"ams", "ams_lite"})

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
    "ace_gen2": "Anycubic ACE GEN 2",
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
    """One printer's multi-material situation, as read just now."""

    kind: str
    driven_by_kiln: bool
    source: str
    slots: tuple[Tray, ...] = ()
    num_slots: int | None = None
    tool_map: tuple[int, ...] | None = None
    unit_name: str | None = None
    version: str | None = None
    warnings: list[str] = field(default_factory=list)
    feeding: tuple[int, int] | None = None
    external_spool: bool = False

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
                {"slot": t.slot, "unit": t.unit, "tray_id": t.tray_id, "name": t.name,
                 "material": t.material, "color": t.hex6}
                for t in self.slots
            ],
            "tool_map": list(self.tool_map) if self.tool_map is not None else None,
            "feeding": list(self.feeding) if self.feeding is not None else None,
            "external_spool": self.external_spool,
            "unit_name": self.unit_name,
            "version": self.version,
            "warnings": list(self.warnings),
            "summary": self.describe(),
        }


def status_from_dict(data: Any) -> MultiMaterialStatus | None:
    """A reading back from :meth:`MultiMaterialStatus.to_dict`."""
    if not isinstance(data, dict) or not isinstance(data.get("kind"), str):
        return None
    slots: list[Tray] = []
    for raw in data.get("loaded_slots") or []:
        if not isinstance(raw, dict) or isinstance(raw.get("slot"), bool) or not isinstance(raw.get("slot"), int):
            return None
        unit = raw.get("unit", 0)
        slots.append(Tray(
            slot=raw["slot"],
            material=str(raw.get("material") or UNREAD_MATERIAL),
            hex6=normalize_hex(raw.get("color")),
            unit=unit if isinstance(unit, int) and not isinstance(unit, bool) else 0,
            name=str(raw["name"]) if isinstance(raw.get("name"), str) else None,
        ))

    def _ints(value: Any, n: int | None = None) -> tuple[int, ...] | None:
        if not isinstance(value, list) or (n is not None and len(value) != n):
            return None
        if not all(isinstance(v, int) and not isinstance(v, bool) for v in value):
            return None
        return tuple(value)

    num = data.get("num_slots")
    return MultiMaterialStatus(
        kind=data["kind"],
        driven_by_kiln=data.get("driven_by_kiln") is True,
        source=str(data.get("source") or "reported"),
        slots=tuple(slots),
        num_slots=num if isinstance(num, int) and not isinstance(num, bool) else None,
        tool_map=_ints(data.get("tool_map")),
        unit_name=str(data["unit_name"]) if isinstance(data.get("unit_name"), str) else None,
        version=str(data["version"]) if isinstance(data.get("version"), str) else None,
        warnings=[str(w) for w in data.get("warnings") or [] if isinstance(w, str)],
        feeding=_ints(data.get("feeding"), 2),  # type: ignore[arg-type]
        external_spool=data.get("external_spool") is True,
    )


class ReadOnce:
    """A printer that answers its multi-material read with one already taken."""

    def __init__(self, adapter: Any, reading: MultiMaterialStatus) -> None:
        self._adapter = adapter
        self._reading = reading

    def get_multi_material_status(self) -> MultiMaterialStatus:
        return self._reading

    def __getattr__(self, name: str) -> Any:
        return getattr(self._adapter, name)


def none_status(source: str = "no_probe") -> MultiMaterialStatus:
    return MultiMaterialStatus(kind=KIND_NONE, driven_by_kiln=False, source=source)


def unknown_status(source: str, why: str) -> MultiMaterialStatus:
    return MultiMaterialStatus(
        kind=KIND_UNKNOWN, driven_by_kiln=False, source=source, warnings=[why],
    )


def multi_material_status(adapter: Any) -> MultiMaterialStatus:
    """Ask *adapter* what it has."""
    probe = getattr(adapter, "get_multi_material_status", None)
    status: MultiMaterialStatus | None = None
    if callable(probe):
        try:
            status = probe()
        except Exception as exc:
            _reraise_refusal(exc)
            logger.debug("multi-material probe failed on %r: %s", adapter, exc)
            status = unknown_status(
                f"{getattr(adapter, 'name', type(adapter).__name__)}:probe_failed",
                f"probe failed: {exc}",
            )
    if not isinstance(status, MultiMaterialStatus):
        status = _legacy_ams_status(adapter)
    _record_seen(status)
    return status


def _reraise_refusal(exc: BaseException) -> None:
    """Let the one-printer refusal through; everything else is a failed read."""
    from kiln.printers.engagement import refusal_in_flight

    if refusal_in_flight(exc) is not None:
        raise exc


def _legacy_ams_status(adapter: Any) -> MultiMaterialStatus:
    """The ``get_ams_status`` duck-type, as a status."""
    read = getattr(adapter, "get_ams_status", None)
    label = getattr(adapter, "name", None) or type(adapter).__name__
    if not callable(read):
        return none_status(f"{label}:no_multi_material_path")
    try:
        info = read()
    except Exception as exc:
        _reraise_refusal(exc)
        logger.debug("AMS not readable on %r: %s", adapter, exc)
        return unknown_status(f"{label}:ams_unreadable", f"AMS not readable: {exc}")
    return from_bambu_ams(info if isinstance(info, dict) else None, printer_model=None)


def _record_seen(status: MultiMaterialStatus) -> None:
    """The instrument: which kinds of unit Kiln meets in the field."""
    if status.kind == KIND_UNKNOWN:
        return
    try:
        from kiln.daily_stats import record_multi_material_seen

        record_multi_material_seen(status.kind)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------


BAMBU_TRAYS_PER_UNIT = TRAYS_PER_UNIT
BAMBU_EXTERNAL_SPOOL_TRAY = EXTERNAL_SPOOL_TRAY
BAMBU_NO_TRAY = NO_TRAY


def _bambu_feeding(info: dict[str, Any]) -> tuple[tuple[int, int] | None, bool]:
    """``(feeding, external_spool)`` from a reading."""
    if "feeding" in info:
        feeding = info.get("feeding")
        if not isinstance(feeding, dict):
            return None, False
        ref = read_tray_id(feeding.get("tray_id"))
    else:
        ref = read_tray_id(info.get("tray_now"))
    if ref is None:
        return None, False
    if ref.external:
        return None, True
    if ref.loaded_tray:
        return (ref.unit, ref.slot), False
    return None, False


def from_bambu_ams(ams_info: dict[str, Any] | None, *, printer_model: str | None) -> MultiMaterialStatus:
    """A :class:`MultiMaterialStatus` from a Bambu ``get_ams_status`` reading."""
    info = ams_info if isinstance(ams_info, dict) else {}
    units = info.get("units") or []
    exist = str(info.get("ams_exist_bits", "0") or "0").strip()
    trays = str(info.get("tray_exist_bits", "0") or "0").strip()
    model = str(printer_model or "").strip().lower()
    heads = {
        str(u.get("module_name") or "").partition("/")[0].lower()
        for u in units if isinstance(u, dict)
    } - {""}
    if "ams_f1" in heads:
        kind = KIND_AMS_LITE
    elif heads:
        kind = KIND_AMS
    else:
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
    feeding, external_spool = _bambu_feeding(info)
    return MultiMaterialStatus(
        kind=kind,
        driven_by_kiln=True,
        source="bambu:get_ams_status",
        slots=slots,
        num_slots=num_slots,
        warnings=warnings,
        feeding=feeding,
        external_spool=external_spool,
    )


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


def from_happy_hare(mmu: dict[str, Any], machine: dict[str, Any] | None = None) -> MultiMaterialStatus:
    """A :class:`MultiMaterialStatus` from Klipper's ``mmu`` (+ ``mmu_machine``) objects."""
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
    """A :class:`MultiMaterialStatus` from Klipper's ``AFC`` object."""
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
    """A :class:`MultiMaterialStatus` from Creality's ``get_cfs_status`` reading."""
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
