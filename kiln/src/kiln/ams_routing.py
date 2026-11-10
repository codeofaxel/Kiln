"""Colour-aware AMS routing — the file's filaments meet the printer's spools."""

from __future__ import annotations

import io
import os
import re
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from kiln.bambu_trays import TRAYS_PER_UNIT as _TRAYS_PER_UNIT
from kiln.bambu_trays import tray_id as _bambu_tray_id
from kiln.bambu_trays import tray_name as _bambu_tray_name
from kiln.colour_distance import delta_e_76, hex_to_lab, normalize_hex
from kiln.gcode import slicer_filament_types
from kiln.gcode_metadata import read_member_text, sliced_gcode_member
from kiln.untrusted_zip import UntrustedZipFile

__all__ = [
    "AmsPlan",
    "FileFilaments",
    "Filament",
    "MATCH_DELTA_E",
    "SpoolAdvisory",
    "Tray",
    "advise_colours",
    "loaded_trays",
    "normalize_hex",
    "plan_ams_mapping",
    "read_file_filaments",
]

MATCH_DELTA_E = 28.0

_UNREAD_SENTINEL = "000000"

_RE_COLOUR_LINE = re.compile(r";\s*filament_colou?r\s*=\s*(.+)", re.IGNORECASE)

# : How much of a G-code file to read from each end
_HEAD_BYTES = 64 * 1024
_TAIL_BYTES = 256 * 1024


# ---------------------------------------------------------------------------
# Colour maths
# ---------------------------------------------------------------------------


_lab = hex_to_lab


def _delta_e(a: str, b: str) -> float:
    """CIE76 distance between two normalised codes: the planner's measure."""
    return delta_e_76(hex_to_lab(a), hex_to_lab(b))


def _colour_name(hex6: str | None) -> str:
    """A short name a person recognises, for messages."""
    if hex6 is None:
        return "unknown colour"
    r, g, b = int(hex6[0:2], 16), int(hex6[2:4], 16), int(hex6[4:6], 16)
    hi, lo = max(r, g, b), min(r, g, b)
    if hi - lo < 24:
        if hi > 225:
            return "white"
        if hi < 40:
            return "black"
        return "grey"
    if r >= g and r >= b:
        if g > 140 and b < 100:
            return "yellow" if g > 190 else "orange"
        if b > 140 and g < 100:
            return "magenta"
        return "red"
    if g >= r and g >= b:
        return "cyan" if b > 160 and r < 100 else "green"
    return "purple" if r > 120 else "blue"


# ---------------------------------------------------------------------------
# What the file wants
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Filament:
    """One extruder's filament as the file declares it."""

    hex6: str | None
    material: str | None = None

    @property
    def label(self) -> str:
        return _colour_name(self.hex6) + (f" {self.material}" if self.material else "")


@dataclass
class FileFilaments:
    """The filaments a print file declares, in extruder order."""

    filaments: list[Filament] = field(default_factory=list)
    source: str = "none"

    @property
    def multicolour(self) -> bool:
        return len(self.filaments) >= 2

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "filaments": [
                {"color": f"#{f.hex6}" if f.hex6 else None, "material": f.material}
                for f in self.filaments
            ],
        }


def _split_list(raw: str) -> list[str]:
    return [p.strip() for p in raw.strip().split(";")]


def _filaments_from_gcode_text(text: str) -> list[Filament]:
    colours: list[str] | None = None
    for line in text.splitlines():
        s = line.strip()
        if s.startswith(";"):
            m = _RE_COLOUR_LINE.match(s)
            if m:
                colours = _split_list(m.group(1))
                break
    # The types, by the one reader of what a file was sliced for.
    types = slicer_filament_types(text)
    if not colours:
        return []
    out: list[Filament] = []
    for i, c in enumerate(colours):
        mat = types[i] if types and i < len(types) and types[i] else None
        out.append(Filament(hex6=normalize_hex(c), material=mat.upper() if mat else None))
    return out


def _read_ends(path: str) -> str:
    size = os.path.getsize(path)
    with open(path, "rb") as fh:
        head = fh.read(min(size, _HEAD_BYTES))
        if size > _HEAD_BYTES + _TAIL_BYTES:
            fh.seek(size - _TAIL_BYTES)
            tail = fh.read()
        elif size > _HEAD_BYTES:
            tail = fh.read()
        else:
            tail = b""
    return (head + b"\n" + tail).decode(errors="replace")


def _filaments_from_painted_3mf(path: str) -> list[Filament]:
    try:
        from kiln.threemf_parser import parse_colored_3mf
    except ImportError:  # pragma: no cover - parser is part of this package
        return []
    try:
        mesh = parse_colored_3mf(path)
    except Exception:  # noqa: BLE001 - not a mesh we can read is "unknown"
        return []
    if not getattr(mesh, "colors_found", False):
        return []
    seen: list[str] = []
    for tri in getattr(mesh, "triangles", []) or []:
        rgb = getattr(tri, "color", None)
        if not rgb:
            continue
        hex6 = "{:02X}{:02X}{:02X}".format(*rgb)
        if hex6 not in seen:
            seen.append(hex6)
    return [Filament(hex6=h) for h in seen]


def read_file_filaments(path: str | None) -> FileFilaments:
    """The filaments ``path`` declares, or an empty answer."""
    if not path or not os.path.isfile(path):
        return FileFilaments()
    lower = path.lower()
    try:
        if lower.endswith(".3mf"):
            with UntrustedZipFile(path) as zf:
                member = sliced_gcode_member(zf)
                if member is not None:
                    from kiln.gcode import _MAX_SCAN_BYTES

                    data = read_member_text(zf, member, _MAX_SCAN_BYTES)
                    text = data[:_HEAD_BYTES] + "\n" + data[-_TAIL_BYTES:]
                    fils = _filaments_from_gcode_text(text)
                    if fils:
                        return FileFilaments(fils, source="gcode_3mf")
            fils = _filaments_from_painted_3mf(path)
            return FileFilaments(fils, source="painted_3mf" if fils else "none")
        if lower.endswith((".gcode", ".gco", ".g")):
            fils = _filaments_from_gcode_text(_read_ends(path))
            return FileFilaments(fils, source="gcode" if fils else "none")
    except (OSError, ValueError, zipfile.BadZipFile, io.UnsupportedOperation):
        return FileFilaments()
    return FileFilaments()


# ---------------------------------------------------------------------------
# What the printer has
# ---------------------------------------------------------------------------


# : A loaded slot whose MATERIAL the printer did not report
UNREAD_MATERIAL = "UNKNOWN"


# : Slots on a chained Bambu unit
TRAYS_PER_UNIT = _TRAYS_PER_UNIT


@dataclass(frozen=True)
class Tray:
    """One loaded AMS tray."""

    slot: int
    material: str
    hex6: str | None
    remain: int | None = None
    unit: int = 0
    name: str | None = None

    @property
    def tray_id(self) -> int:
        """The printer's id for this tray (see :func:`kiln.bambu_trays.tray_id`)."""
        return _bambu_tray_id(self.unit, self.slot)

    @property
    def where(self) -> str:
        """``slot B2`` on a Bambu; ``slot 3`` on a one-unit changer."""
        return f"slot {self.name}" if self.name else f"slot {self.slot + 1}"

    @property
    def label(self) -> str:
        # "red UNKNOWN in slot 1" reads like a material called UNKNOWN
        material = "filament" if self.material == UNREAD_MATERIAL else self.material
        return f"{_colour_name(self.hex6)} {material} in {self.where}"


def loaded_trays(ams_info: dict[str, Any] | None) -> list[Tray]:
    """Loaded trays from an ``ams_status`` reading, in slot order."""
    out: list[Tray] = []
    if not isinstance(ams_info, dict):
        return out
    for position, unit in enumerate(ams_info.get("units", []) or []):
        if not isinstance(unit, dict):
            continue
        try:
            unit_id = int(unit.get("unit_id", position))
        except (TypeError, ValueError):
            unit_id = position
        for tray in unit.get("trays", []) or []:
            if not isinstance(tray, dict):
                continue
            material = str(tray.get("tray_type", "") or "").strip()
            if not material:
                continue
            try:
                slot = int(tray.get("slot", -1))
            except (TypeError, ValueError):
                continue
            if slot < 0:
                continue
            hex6 = normalize_hex(tray.get("tray_color"))
            if hex6 == _UNREAD_SENTINEL:
                hex6 = None
            remain: int | None
            try:
                remain = int(tray.get("remain")) if tray.get("remaining_known", True) else None
            except (TypeError, ValueError):
                remain = None
            try:
                name = _bambu_tray_name(unit_id, slot)
            except ValueError:
                continue
            out.append(Tray(slot=slot, material=material.upper(), hex6=hex6, remain=remain, unit=unit_id, name=name))
    out.sort(key=lambda t: (t.unit, t.slot))
    return out


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------


@dataclass
class AmsPlan:
    """Which AMS slot feeds each extruder, and what could not be decided."""

    mapping: list[int] | None
    matches: list[dict[str, Any]] = field(default_factory=list)
    #: Extruders (0-indexed) that no loaded tray can honestly stand in for.
    unmatched: list[int] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.mapping is not None

    @property
    def summary(self) -> str:
        """``white → slot A1, red → slot B1, black → slot A2``; unmatched named."""
        parts: list[str] = []
        for m in self.matches:
            if m["slot"] is None:
                parts.append(f"{m['wanted']} → no spool loaded")
            else:
                parts.append(f"{m['wanted']} → slot {m.get('name') or m['slot'] + 1}")
        return ", ".join(parts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "mapping": list(self.mapping) if self.mapping is not None else None,
            "summary": self.summary,
            "matches": list(self.matches),
            "unmatched": list(self.unmatched),
            "warnings": list(self.warnings),
        }


def plan_ams_mapping(
    wanted: list[Filament],
    trays: list[Tray],
    *,
    tolerance: float = MATCH_DELTA_E,
) -> AmsPlan:
    """Map each wanted filament to a distinct loaded tray."""
    plan = AmsPlan(mapping=None)
    if not wanted:
        return plan
    if not trays:
        plan.unmatched = list(range(len(wanted)))
        plan.warnings.append("No AMS trays report loaded filament.")
        plan.matches = [
            {"extruder": i, "wanted": f.label, "slot": None, "name": None, "tray": None,
             "delta_e": None, "exact": False, "warning": "no spool loaded"}
            for i, f in enumerate(wanted)
        ]
        return plan

    # Every (extruder, tray) pairing scored; None = not an acceptable match
    def _score(f: Filament, t: Tray) -> tuple[float, float | None, str | None] | None:
        # An unread material on EITHER side is unjudgeable, not a disagreement
        same_mat = (
            f.material is None
            or t.material.upper() == UNREAD_MATERIAL
            or f.material.upper() == t.material.upper()
        )
        if f.hex6 is None:
            return (tolerance, None, None) if same_mat else None
        if t.hex6 is None:
            if not same_mat:
                return None
            if t.material.upper() == UNREAD_MATERIAL:
                return None
            return (
                2 * tolerance + 1.0,
                None,
                f"{t.where}'s colour was not read; matched on material only",
            )
        de = _delta_e(f.hex6, t.hex6)
        if de > tolerance:
            return None
        if not same_mat:
            return (
                de + tolerance,
                de,
                f"{f.label} matched {t.label}: colour agrees, material differs",
            )
        return (de, de, None)

    scored: list[tuple[float, int, int, float | None, str | None]] = []
    for i, f in enumerate(wanted):
        for t in trays:
            s = _score(f, t)
            if s is not None:
                scored.append((s[0], i, t.tray_id, s[1], s[2]))
    scored.sort(key=lambda x: (x[0], x[1], x[2]))

    chosen: dict[int, tuple[Tray, float | None, str | None]] = {}
    used_slots: set[int] = set()
    by_slot = {t.tray_id: t for t in trays}
    for _rank, i, slot, de, warning in scored:
        if i in chosen or slot in used_slots:
            continue
        chosen[i] = (by_slot[slot], de, warning)
        used_slots.add(slot)

    mapping: list[int] = []
    for i, f in enumerate(wanted):
        if i in chosen:
            tray, de, warning = chosen[i]
            plan.matches.append(
                {
                    "extruder": i,
                    "wanted": f.label,
                    "slot": tray.tray_id,
                    "name": tray.name,
                    "tray": tray.label,
                    "delta_e": round(de, 1) if de is not None else None,
                    "exact": de is not None and de < 1.0 and warning is None,
                    "warning": warning,
                }
            )
            if warning:
                plan.warnings.append(warning)
            mapping.append(tray.tray_id)
        else:
            nearest = None
            if f.hex6 is not None:
                cands = [(t, _delta_e(f.hex6, t.hex6)) for t in trays if t.hex6]
                if cands:
                    nearest = min(cands, key=lambda c: c[1])
            why = (
                f"no loaded spool is close to {f.label}"
                + (f" (nearest: {nearest[0].label}, ΔE {nearest[1]:.0f})" if nearest else "")
            )
            plan.matches.append(
                {"extruder": i, "wanted": f.label, "slot": None, "name": None, "tray": None,
                 "delta_e": None, "exact": False, "warning": why}
            )
            plan.unmatched.append(i)
            plan.warnings.append(why)

    if not plan.unmatched:
        plan.mapping = mapping
    return plan


# ---------------------------------------------------------------------------
# What a colouring tool says at the moment of intent
# ---------------------------------------------------------------------------


@dataclass
class SpoolAdvisory:
    """Whether colours a tool just applied are loaded on a printer."""

    verdict: str
    message: str
    printer: str | None = None
    missing: list[dict[str, Any]] = field(default_factory=list)
    matched: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "message": self.message,
            "printer": self.printer,
            "missing": list(self.missing),
            "matched": list(self.matched),
        }


def advise_colours(
    colours: Sequence[str | None],
    trays: Sequence[Tray],
    *,
    printer: str | None = None,
    tolerance: float = MATCH_DELTA_E,
) -> SpoolAdvisory | None:
    """Judge *colours* against loaded *trays*; ``None`` when there is nothing to judge."""
    wanted: list[str] = []
    for raw in colours:
        hex6 = normalize_hex(raw)
        if hex6 and hex6 not in wanted:
            wanted.append(hex6)
    if not wanted:
        return None

    where = f" on {printer}" if printer else ""
    if not trays:
        return SpoolAdvisory(
            "empty",
            f"No loaded filament is reported{where}, so these colours could not be checked.",
            printer,
        )
    if not any(t.hex6 for t in trays):
        return SpoolAdvisory(
            "unknown",
            f"Filament is loaded{where} but the printer did not report its "
            "colours, so these colours could not be checked.",
            printer,
        )

    plan = plan_ams_mapping(
        [Filament(hex6=h) for h in wanted], list(trays), tolerance=tolerance,
    )
    matched: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    by_slot = {t.tray_id: t for t in trays}
    for hex6, match in zip(wanted, plan.matches, strict=True):
        entry = {
            "color": f"#{hex6}",
            "name": _colour_name(hex6),
            "nearest": match["tray"],
            "nearest_color": None,
            "slot": match["slot"],
            "slot_name": match["name"],
            "delta_e": match["delta_e"],
        }
        if match["slot"] is None:
            cands = [(t, _delta_e(hex6, t.hex6)) for t in trays if t.hex6]
            nearest = min(cands, key=lambda c: c[1]) if cands else None
            entry["nearest"] = nearest[0].label if nearest else None
            entry["nearest_color"] = f"#{nearest[0].hex6}" if nearest else None
            entry["delta_e"] = round(nearest[1], 1) if nearest else None
            missing.append(entry)
        else:
            tray = by_slot[match["slot"]]
            entry["nearest_color"] = f"#{tray.hex6}" if tray.hex6 else None
            entry["where"] = tray.where
            matched.append(entry)

    if not missing:
        names = ", ".join(m["name"] for m in matched)
        return SpoolAdvisory(
            "true", f"Every colour is loaded{where}: {names}.", printer, missing, matched,
        )
    parts = []
    for m in missing:
        nearest = (
            f" — nearest is {m['nearest']} (ΔE {m['delta_e']:.0f})"
            if m["nearest"] else ""
        )
        parts.append(f"no {m['name']} ({m['color']}) loaded{where}{nearest}")
    message = "; ".join(parts)
    return SpoolAdvisory(
        "mismatch", message[0].upper() + message[1:] + ".", printer, missing, matched,
    )
