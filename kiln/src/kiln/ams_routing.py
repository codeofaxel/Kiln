"""Colour-aware AMS routing — the file's filaments meet the printer's spools.

A multi-colour print file lists its filaments in whatever order the slicer
chose; the printer feeds extruder N from AMS slot N unless told otherwise.
Nothing about those two orders agrees by construction.  Measured
2026-09-01: a painted jar sliced as (white, red, black) on an AMS loaded
(white, black, grey, red) would have printed its red mark in black and
its black lid in grey, and every tool along the way reported success.

This module is the one place that comparison happens:

* :func:`read_file_filaments` reads the colours and material types a
  print file declares — a plain G-code (header + the slicer's config block
  at the tail), a Bambu ``.gcode.3mf`` (the same G-code inside the zip), or
  a painted ``.3mf`` before slicing (its distinct face colours).
* :func:`loaded_trays` reads what the AMS reports, with the firmware's
  "colour not read" sentinel treated as unknown rather than black.
* :func:`plan_ams_mapping` matches one to the other by perceptual colour
  distance and material type, and returns the 0-indexed per-extruder slot
  list the Bambu adapter already accepts — plus, in words, what it decided
  and what it could not.

Every print door goes through :func:`kiln.server._resolve_use_ams`, which
calls the planner whenever it knows what the file wants; the print approval
dialog shows the same plan.  Ahead of time it is a preview; at print start
it is the decision — the AMS at that moment is the only truth about what is
loaded.

The colour maths (CIE76 in CIELAB, match tolerance ΔE 28) is the same as
kiln-pro's palette advisor so the two never disagree about whether a spool
is "close enough".
"""

from __future__ import annotations

import io
import os
import re
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

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

#: A loaded spool counts as the requested colour when its CIE76 distance is
#: within this.  Kept identical to kiln-pro's ``_FILAMENT_MATCH_DE2`` root:
#: a "true" verdict there and a mapping here must mean the same thing.
MATCH_DELTA_E = 28.0

#: Firmware reports a spool it could not colour-read as all-zero RGB.  That
#: is "unknown", never confident black — a real black spool pays the price
#: of being treated as unknown too, which beats fabricating a match.
_UNREAD_SENTINEL = "000000"

_RE_COLOUR_LINE = re.compile(r";\s*filament_colou?r\s*=\s*(.+)", re.IGNORECASE)
_RE_TYPE_LINE = re.compile(r";\s*filament_type\s*=\s*(.+)", re.IGNORECASE)
_HEX6 = re.compile(r"^[0-9A-F]{6}$")

#: How much of a G-code file to read from each end.  OrcaSlicer, Bambu
#: Studio and PrusaSlicer write the config block at the END; the header
#: window is for slicers that put it up front.
_HEAD_BYTES = 64 * 1024
_TAIL_BYTES = 256 * 1024


# ---------------------------------------------------------------------------
# Colour maths
# ---------------------------------------------------------------------------


def normalize_hex(value: Any) -> str | None:
    """``RRGGBB`` upper-hex, alpha dropped; ``None`` when it is not a colour.

    Accepts ``#RRGGBB``, ``RRGGBB`` and Bambu's ``RRGGBBAA`` tray colour.
    """
    if not isinstance(value, str):
        return None
    h = value.strip().lstrip("#").upper()
    if len(h) >= 6:
        h = h[:6]
    if not _HEX6.fullmatch(h):
        return None
    return h


def _srgb_to_linear(channel: int) -> float:
    v = channel / 255.0
    return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4


def _lab(hex6: str) -> tuple[float, float, float]:
    r = _srgb_to_linear(int(hex6[0:2], 16))
    g = _srgb_to_linear(int(hex6[2:4], 16))
    b = _srgb_to_linear(int(hex6[4:6], 16))
    x = (r * 0.4124564 + g * 0.3575761 + b * 0.1804375) / 0.95047
    y = r * 0.2126729 + g * 0.7151522 + b * 0.0721750
    z = (r * 0.0193339 + g * 0.1191920 + b * 0.9503041) / 1.08883

    def pivot(t: float) -> float:
        return t ** (1 / 3) if t > 0.008856 else 7.787 * t + 16 / 116

    fx, fy, fz = pivot(x), pivot(y), pivot(z)
    return (116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz))


def _delta_e(a: str, b: str) -> float:
    la, lb = _lab(a), _lab(b)
    return ((la[0] - lb[0]) ** 2 + (la[1] - lb[1]) ** 2 + (la[2] - lb[2]) ** 2) ** 0.5


def _colour_name(hex6: str | None) -> str:
    """A short name a person recognises, for messages.  Hex when unsure."""
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
    #: Where the answer came from: ``"gcode"``, ``"gcode_3mf"``,
    #: ``"painted_3mf"`` or ``"none"``.
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
    types: list[str] | None = None
    for line in text.splitlines():
        s = line.strip()
        if not s.startswith(";"):
            continue
        if colours is None:
            m = _RE_COLOUR_LINE.match(s)
            if m:
                colours = _split_list(m.group(1))
                continue
        if types is None:
            m = _RE_TYPE_LINE.match(s)
            if m:
                types = _split_list(m.group(1))
        if colours is not None and types is not None:
            break
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


def _gcode_member(zf: zipfile.ZipFile) -> str | None:
    names = [n for n in zf.namelist() if n.lower().endswith(".gcode")]
    plates = sorted(n for n in names if n.startswith("Metadata/plate_"))
    return (plates or names or [None])[0]


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
        # No colour data anywhere in the file: the parser's fallback grey
        # is a display default, not a filament the file asked for.
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
    """The filaments ``path`` declares, or an empty answer.  Never raises.

    Plain G-code is read from both ends (config blocks live at the tail).
    A ``.3mf`` is opened as a zip: if it carries sliced G-code it is a Bambu
    print file and that G-code is read; otherwise it is treated as a painted
    model and its distinct face colours are the answer, with no material.
    """
    if not path or not os.path.isfile(path):
        return FileFilaments()
    lower = path.lower()
    try:
        if lower.endswith(".3mf"):
            with zipfile.ZipFile(path) as zf:
                member = _gcode_member(zf)
                if member is not None:
                    data = zf.read(member)
                    text = (
                        data[:_HEAD_BYTES] + b"\n" + data[-_TAIL_BYTES:]
                    ).decode(errors="replace")
                    fils = _filaments_from_gcode_text(text)
                    if fils:
                        return FileFilaments(fils, source="gcode_3mf")
            fils = _filaments_from_painted_3mf(path)
            return FileFilaments(fils, source="painted_3mf" if fils else "none")
        if lower.endswith((".gcode", ".gco", ".g")):
            fils = _filaments_from_gcode_text(_read_ends(path))
            return FileFilaments(fils, source="gcode" if fils else "none")
    except (OSError, zipfile.BadZipFile, io.UnsupportedOperation):
        return FileFilaments()
    return FileFilaments()


# ---------------------------------------------------------------------------
# What the printer has
# ---------------------------------------------------------------------------


#: A loaded slot whose MATERIAL the printer did not report.  A Bambu tray
#: always names its type, so this only arises on a unit that reports a gate
#: as loaded without saying what is in it (a Klipper MMU gate, a CFS bay).
#: It is a sentinel, not a material: everything that compares materials
#: must read it as "cannot judge", never as a material that disagrees.
UNREAD_MATERIAL = "UNKNOWN"


@dataclass(frozen=True)
class Tray:
    """One loaded AMS tray.  ``hex6`` is ``None`` when the colour was not read."""

    slot: int
    material: str
    hex6: str | None
    remain: int | None = None

    @property
    def label(self) -> str:
        # "red UNKNOWN in slot 1" reads like a material called UNKNOWN.  A
        # slot whose material was never reported is just filament.
        material = "filament" if self.material == UNREAD_MATERIAL else self.material
        return f"{_colour_name(self.hex6)} {material} in slot {self.slot + 1}"


def loaded_trays(ams_info: dict[str, Any] | None) -> list[Tray]:
    """Loaded trays from an ``ams_status`` reading, in slot order.

    A tray is loaded when it reports a material type — the signal the rest
    of the stack keys on (the A1 reports ``remain: 0`` for full spools).
    """
    out: list[Tray] = []
    if not isinstance(ams_info, dict):
        return out
    for unit in ams_info.get("units", []) or []:
        if not isinstance(unit, dict):
            continue
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
            out.append(Tray(slot=slot, material=material.upper(), hex6=hex6, remain=remain))
    out.sort(key=lambda t: t.slot)
    return out


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------


@dataclass
class AmsPlan:
    """Which AMS slot feeds each extruder, and what could not be decided."""

    #: 0-indexed slot per extruder, in the file's extruder order.  ``None``
    #: when any extruder is unmatched — a partial mapping is a wrong print.
    mapping: list[int] | None
    #: One entry per extruder: ``{extruder, wanted, slot, tray, delta_e,
    #: exact, warning}``; ``slot`` is ``None`` for an unmatched extruder.
    matches: list[dict[str, Any]] = field(default_factory=list)
    #: Extruders (0-indexed) that no loaded tray can honestly stand in for.
    unmatched: list[int] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.mapping is not None

    @property
    def summary(self) -> str:
        """``white → slot 1, red → slot 4, black → slot 2``; unmatched named."""
        parts: list[str] = []
        for m in self.matches:
            if m["slot"] is None:
                parts.append(f"{m['wanted']} → no spool loaded")
            else:
                parts.append(f"{m['wanted']} → slot {m['slot'] + 1}")
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
    """Map each wanted filament to a distinct loaded tray.

    Extruders are matched best-first (smallest colour distance across every
    remaining pair), so a close pair is never stolen by a worse one that
    happened to come first.  A tray feeds one extruder.  Rules:

    * A colour within ``tolerance`` on a tray of the same material is a
      match.  A different material within tolerance still matches, with a
      warning naming both — the colour is right, the plastic is the user's
      call.
    * A tray whose colour was not read matches only when nothing readable
      does and its material agrees, with a warning that the colour is
      unverified.
    * A wanted filament with no colour of its own matches by material.
    * Anything else is unmatched, and the plan carries no mapping: a print
      that gets three colours right and one wrong is still a wrong print.
    """
    plan = AmsPlan(mapping=None)
    if not wanted:
        return plan
    if not trays:
        plan.unmatched = list(range(len(wanted)))
        plan.warnings.append("No AMS trays report loaded filament.")
        plan.matches = [
            {"extruder": i, "wanted": f.label, "slot": None, "tray": None,
             "delta_e": None, "exact": False, "warning": "no spool loaded"}
            for i, f in enumerate(wanted)
        ]
        return plan

    # Every (extruder, tray) pairing scored; None = not an acceptable match.
    # The rank is tiered so a same-material colour match (rank <= tolerance)
    # always beats a material mismatch (rank in (tolerance, 2*tolerance]),
    # which always beats an unread-colour tray (rank > 2*tolerance).
    def _score(f: Filament, t: Tray) -> tuple[float, float | None, str | None] | None:
        # An unread material on EITHER side is unjudgeable, not a
        # disagreement.  The file's side has always worked this way; the
        # tray's side did not, so a gate that reported "loaded" without
        # naming its filament produced "colour agrees, material differs"
        # against a perfectly good spool — a mismatch built out of an
        # absence, which is the shape this module exists to avoid.
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
                # Neither the colour nor the material was read, so there is
                # nothing to match ON.  Treating the unread material as
                # "cannot judge" (above) must not turn a slot Kiln knows
                # nothing about into a slot that matches everything, under
                # a message claiming it matched on the material.
                return None
            return (
                2 * tolerance + 1.0,
                None,
                f"slot {t.slot + 1}'s colour was not read; matched on material only",
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
                scored.append((s[0], i, t.slot, s[1], s[2]))
    scored.sort(key=lambda x: (x[0], x[1], x[2]))

    chosen: dict[int, tuple[Tray, float | None, str | None]] = {}
    used_slots: set[int] = set()
    by_slot = {t.slot: t for t in trays}
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
                    "slot": tray.slot,
                    "tray": tray.label,
                    "delta_e": round(de, 1) if de is not None else None,
                    "exact": de is not None and de < 1.0 and warning is None,
                    "warning": warning,
                }
            )
            if warning:
                plan.warnings.append(warning)
            mapping.append(tray.slot)
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
                {"extruder": i, "wanted": f.label, "slot": None, "tray": None,
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
    """Whether colours a tool just applied are loaded on a printer.

    Advice, never a decision.  The print gate decides at print time on
    whatever printer is in front of the job; this says, at the moment a
    colour is chosen, whether that printer could print it as chosen — so
    "make it red" comes back as "made it red; no red is loaded" instead
    of a grey print and a warning nobody read.

    ``verdict``:

    * ``"true"``     — every distinct colour has a loaded spool close
      enough; the design prints as previewed.
    * ``"mismatch"`` — at least one colour has no close spool.  ``missing``
      names each one with the nearest loaded spool and how far off it is.
    * ``"empty"``    — the printer reports no loaded filament.
    * ``"unknown"``  — filament is loaded but its colours were not read.
    """

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
    """Judge *colours* against loaded *trays*; ``None`` when there is nothing to judge.

    Matching goes through :func:`plan_ams_mapping`, the same matcher the
    print gate uses, so the advice a tool gives when a colour is chosen and
    the decision the gate makes when the print starts can never disagree —
    two design colours that would compete for one spool are a mismatch
    here because they will be one there.
    """
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
    by_slot = {t.slot: t for t in trays}
    for hex6, match in zip(wanted, plan.matches, strict=True):
        entry = {
            "color": f"#{hex6}",
            "name": _colour_name(hex6),
            "nearest": match["tray"],
            "nearest_color": None,
            "slot": match["slot"],
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
            tray_hex = by_slot[match["slot"]].hex6
            entry["nearest_color"] = f"#{tray_hex}" if tray_hex else None
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
