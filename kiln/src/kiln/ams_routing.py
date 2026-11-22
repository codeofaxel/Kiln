"""Colour-aware AMS routing — the file's filaments meet the printer's spools."""

from __future__ import annotations

import io
import os
import re
import zipfile
from collections.abc import Callable, Sequence
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
    "SpoolMiss",
    "Tray",
    "advise_colours",
    "empty_slots",
    "loaded_trays",
    "normalize_hex",
    "plan_ams_mapping",
    "plastic_words",
    "read_file_filaments",
    "same_plastic",
    "spool_miss",
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
        if self.hex6 is None and self.material:
            return self.material
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


def empty_slots(ams_info: dict[str, Any] | None) -> list[str]:
    """Studio's names for the slots an ``ams_status`` reading reports empty."""
    out: list[str] = []
    if not isinstance(ams_info, dict):
        return out
    for position, unit in enumerate(ams_info.get("units", []) or []):
        if not isinstance(unit, dict):
            continue
        for tray in unit.get("trays", []) or []:
            if not isinstance(tray, dict) or str(tray.get("tray_type", "") or "").strip():
                continue
            try:
                out.append(_bambu_tray_name(int(unit.get("unit_id", position)), int(tray.get("slot", -1))))
            except (TypeError, ValueError):
                continue
    return out


# ---------------------------------------------------------------------------
# Is this spool that plastic?
# ---------------------------------------------------------------------------


def _is_spool_type(word: str) -> bool:
    """True when *word* spells a filament type a multi-material unit reports."""
    from kiln.catalog_keys import same_material_spelling
    from kiln.printers.bambu_3mf import BAMBU_FILAMENT_TYPES

    return any(same_material_spelling(word, t) for t in BAMBU_FILAMENT_TYPES)


def _reported_as(material: str) -> str | None:
    """The filament type a spool of *material* is reported as, or ``None``
    for a name with no one type in it ("PLA+" and "Silk PLA" load as PLA)."""
    from kiln.catalog_keys import material_label
    from kiln.printers.bambu_3mf import BAMBU_FILAMENT_TYPES, bambu_filament_type

    label = material_label(material)
    if not label:
        return None
    if _is_spool_type(label):
        return label
    named = {w for w in re.findall(r"[A-Z0-9]+", label.replace("NYLON", "PA")) if w in BAMBU_FILAMENT_TYPES}
    typed = bambu_filament_type(label)
    return typed if named == {typed.split("-")[0]} else None


def same_plastic(wanted: str | None, loaded: str | None) -> bool | None:
    """Whether the *loaded* spool is the plastic a job *wanted*.

    ``True`` for one material under two spellings, or a spool of the type
    *wanted* is loaded as; ``False`` when both are known and differ (``PLA``
    is not ``PLA-CF``); ``None`` when either is blank, unread or no type.
    """
    from kiln.catalog_keys import same_material_spelling

    a, b = str(wanted or "").strip(), str(loaded or "").strip()
    if not a or not b or b.upper() == UNREAD_MATERIAL:
        return None
    if same_material_spelling(a, b):
        return True
    word = _reported_as(a)
    if word is None or not _is_spool_type(b):
        return None
    return same_material_spelling(word, b)


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
        # An unstated material on the file's side is unjudgeable, as is an
        # unread one on the tray's; a known disagreement is never a match.
        same = True if f.material is None or t.material.upper() == UNREAD_MATERIAL else same_plastic(f.material, t.material)
        if same is False:
            return None
        if f.hex6 is None:
            return (tolerance, None, None) if same else None
        if t.hex6 is None:
            if not same or t.material.upper() == UNREAD_MATERIAL:
                return None
            return (
                2 * tolerance + 1.0,
                None,
                f"{t.where}'s colour was not read; matched on material only",
            )
        de = _delta_e(f.hex6, t.hex6)
        if de > tolerance:
            return None
        if same is None:
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
            if f.material and not any(same_plastic(f.material, t.material) is not False for t in trays):
                why = f"no loaded spool is {f.material}"
            else:
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
# What a print door says when no loaded spool fits
# ---------------------------------------------------------------------------


_COUNT_WORDS = ("none", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve")

#: Asks whether each loaded plastic suits the part: ``(job's plastic, loaded
#: plastics) -> {"verdicts": {plastic: {"suits", "why"}}, "missed": str}`` or ``None``.
Judge = Callable[[str, list[str]], Any]


@dataclass
class SpoolMiss:
    """What a print door says, and offers, when no loaded spool fits."""

    code: str
    message: str
    options: list[dict[str, Any]] = field(default_factory=list)


def _count(n: int, *, capital: bool = False) -> str:
    word = _COUNT_WORDS[n] if 0 <= n < len(_COUNT_WORDS) else str(n)
    return word[:1].upper() + word[1:] if capital else word


def _listed(words: Sequence[str], conjunction: str = "and") -> str:
    kept = [w for w in words if w]
    if len(kept) <= 1:
        return "".join(kept)
    return f"{', '.join(kept[:-1])} {conjunction} {kept[-1]}"


def _a(words: str) -> str:
    return f"an {words}" if words[:1].upper() in "AEIOU" else f"a {words}"


def plastic_words(material: str | None) -> str:
    """A plastic as a sentence says it: ``PLA``, ``nylon``, ``PLA matte``."""
    from kiln.catalog_keys import material_label
    from kiln.printers.bambu_3mf import BAMBU_FILAMENT_TYPES

    text = str(material or "").strip()
    if not text or text.upper() == UNREAD_MATERIAL:
        return "filament"
    words = (material_label(text) or text.upper()).split()
    return " ".join(w if not w.isalpha() or w in BAMBU_FILAMENT_TYPES or len(w) <= 3 else w.lower() for w in words)


def _spool(colour: str | None, material: str | None) -> str:
    plastic = plastic_words(material) if material else ""
    return " ".join(w for w in (colour, plastic) if w) or "filament"


def _loaded_said(trays: Sequence[Tray]) -> str:
    """``white, black and red PLA, and grey ABS``."""
    groups: dict[str, list[str]] = {}
    for t in trays:
        names = groups.setdefault(plastic_words(t.material), [])
        name = _colour_name(t.hex6) if t.hex6 else ""
        if name and name not in names:
            names.append(name)
    parts = [f"{_listed(names)} {plastic}".strip() for plastic, names in groups.items()]
    if len(parts) > 1 and any(" and " in p for p in parts):
        return f"{', '.join(parts[:-1])}, and {parts[-1]}"
    return _listed(parts)


def _close(hex6: str | None, tray: Tray) -> bool:
    return bool(hex6 and tray.hex6) and _delta_e(hex6, tray.hex6) <= MATCH_DELTA_E


def _nearest(hex6: str | None, trays: Sequence[Tray]) -> list[Tray]:
    read = [t for t in trays if t.hex6]
    return sorted(read, key=lambda t: _delta_e(hex6, t.hex6)) if hex6 else read


def _option(action: str, *, tray: Tray | None = None, material: str | None = None,
            colour: str | None = None, hex6: str | None = None, **more: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"action": action, "slot": tray.name or str(tray.slot + 1) if tray else None}
    if tray is not None:
        out["tray_id"] = tray.tray_id
    out.update({"material": material, "color": colour, "hex": f"#{hex6}" if hex6 else None, **more})
    return out


def _judged(judge: Judge | None, wanted: str | None, stand_ins: list[str]) -> tuple[dict[str, Any], str | None]:
    """``({plastic: {"suits", "why"}}, missed)``; empty when nothing was asked."""
    if judge is None or not wanted or not stand_ins:
        return {}, None
    try:
        answer = judge(wanted, stand_ins) or {}
    except Exception:  # noqa: BLE001 -- an unanswered check offers with the hedge
        return {}, None
    verdicts = answer.get("verdicts") if isinstance(answer.get("verdicts"), dict) else {}
    missed = answer.get("missed") if isinstance(answer.get("missed"), str) else None
    return verdicts, missed


def _fit(verdicts: dict[str, Any], material: str) -> dict[str, Any]:
    """The option fields a goal check adds for *material*."""
    verdict = verdicts.get(material)
    if not isinstance(verdict, dict) or not isinstance(verdict.get("suits"), bool):
        return {}
    return {"suits_goal": verdict["suits"], **({"why": verdict["why"]} if verdict.get("why") else {})}


def spool_miss(
    wanted: Sequence[Filament],
    trays: Sequence[Tray],
    *,
    plan: AmsPlan | None = None,
    names: Sequence[str | None] = (),
    colour_counts: bool = True,
    sliced: bool = True,
    free_slots: Sequence[str] = (),
    noun: str = "part",
    judge: Judge | None = None,
    on_record: dict[int, str] | None = None,
) -> SpoolMiss | None:
    """The sentence and choices for *wanted* when no loaded spool fits, or ``None`` when one does.

    *names* are the colour words the person used; *sliced* whether the file is already sliced;
    *on_record* the person's recorded spool for a filament, by its index.
    """
    trays = list(trays)
    if not wanted or not trays:
        return None
    if len(wanted) >= 2:
        plan = plan or plan_ams_mapping(list(wanted), trays)
        return None if plan.ok else _parts_missing(list(wanted), trays, plan, list(names), free_slots, noun,
                                                   on_record or {})
    want = wanted[0]
    name = (names[0] if names else None) or (_colour_name(want.hex6) if want.hex6 else None)
    hex6 = want.hex6 if colour_counts else None
    plastic = [t for t in trays if want.material is None or same_plastic(want.material, t.material)]
    if plastic and (hex6 is None or any(_close(hex6, t) or not t.hex6 for t in plastic)):
        return None
    shade = [t for t in _nearest(hex6, trays) if _close(hex6, t) and t.material != UNREAD_MATERIAL]
    if plastic:
        return _colour_not_loaded(want, name, plastic, shade, sliced, judge)
    if shade:
        return _colour_in_other_plastic(want, name, shade, sliced, judge)
    return _plastic_not_loaded(want, name if hex6 else None, trays, sliced, judge)


def _colour_not_loaded(want: Filament, name: str | None, plastic: list[Tray], other: list[Tray], sliced: bool,
                       judge: Judge | None) -> SpoolMiss:
    near = _nearest(want.hex6, plastic)[0]
    shade = _colour_name(near.hex6)
    said = "another shade of " + shade if shade == name else shade
    if want.material:
        kind = plastic_words(want.material)
        message = (f"This print wants {name} {kind}. You have {kind} loaded, but no {name}: "
                   f"the closest is {said} in {near.where}.")
    else:
        message = f"This print wants {name}. No {name} spool is loaded: the closest is {said} {plastic_words(near.material)} in {near.where}."
    options = [_option("print_from_slot", tray=near, material=near.material, colour=shade, hex6=near.hex6)]
    alt = other[0] if other and want.material else None
    fit: dict[str, Any] = {}
    if alt is not None:
        verdicts, missed = _judged(judge, want.material, [alt.material])
        fit = {**_fit(verdicts, alt.material), **({"goal_check_missed": missed} if missed else {})}
        options.append(_option("reslice_for", tray=alt, material=alt.material, colour=name, hex6=alt.hex6, **fit))
    choices = [f"Print it in {'that shade' if shade == name else shade}"]
    if alt is not None and fit.get("suits_goal") is not False:
        choices.append(f"{'re-slice' if sliced else 'slice'} it for the {name} {plastic_words(alt.material)} in {alt.where}")
    message += f" {', '.join(choices)}, or load {name} first?"
    options.append(_option("load_spool", material=want.material, colour=name, hex6=want.hex6))
    return SpoolMiss("COLOR_NOT_LOADED", message, options)


def _colour_in_other_plastic(want: Filament, name: str | None, shade: list[Tray], sliced: bool,
                             judge: Judge | None) -> SpoolMiss:
    picks: dict[str, Tray] = {}
    for t in shade:
        picks.setdefault(plastic_words(t.material), t)
    verdicts, missed = _judged(judge, want.material, [t.material for t in picks.values()])
    kind = plastic_words(want.material)
    spools = _listed([f"{name} {p} in {t.where}" for p, t in picks.items()])
    first = (f"This print is set up for {name} {kind}, but the only {name} spool loaded is {spools}."
             if len(picks) == 1 else f"This print is set up for {name} {kind}, but the {name} spools loaded are {spools}.")
    who = f"{next(iter(picks))} can't" if len(picks) == 1 else ("Neither can" if len(picks) == 2 else "None of them can")
    basis = "the file is sliced for" if sliced else "this print is set up for"
    first += f" {who} just stand in, because {basis} {kind}'s temperatures."
    offered = [(p, t) for p, t in picks.items() if _fit(verdicts, t.material).get("suits_goal") is not False]
    unfit = [_fit(verdicts, t.material).get("why") or f"{plastic_words(t.material)} doesn't suit what this part is for"
             for t in picks.values() if _fit(verdicts, t.material).get("suits_goal") is False]
    if offered:
        asks = _listed([f"for {p} and print from {t.name or t.where}" for p, t in offered], "or")
        lead = "re-slice it" if sliced else "slice it"
        aside = f" ({unfit[0]})" if unfit else ""
        message = (f"{first} Want me to {lead} {asks}{'' if sliced else ' instead'}{aside}? "
                   f"Or load {name} {kind} and I'll start it as is.")
    else:
        message = f"{first} {unfit[0]}, so I'd load {name} {kind} and start it as is."
    reslices = [_option("reslice_for", tray=t, material=t.material, colour=name, hex6=t.hex6, **_fit(verdicts, t.material))
                for t in picks.values()]
    load = _option("load_spool", material=want.material, colour=name, hex6=want.hex6)
    options = [*reslices, load] if offered else [load, *reslices]
    if missed:
        for o in reslices:
            o["goal_check_missed"] = missed
    return SpoolMiss("AMS_MATERIAL_NOT_LOADED", message, options)


def _plastic_not_loaded(want: Filament, name: str | None, trays: list[Tray], sliced: bool,
                        judge: Judge | None) -> SpoolMiss:
    kind = plastic_words(want.material)
    needs = f"{name} {kind}" if name else kind
    first = f"This print needs {needs}, and none of your spools are {kind} (you have {_loaded_said(trays)})."
    stand_ins: dict[str, Tray] = {}
    for t in sorted(trays, key=lambda t: -1 if t.remain is None else -t.remain):
        if t.material != UNREAD_MATERIAL:
            stand_ins.setdefault(plastic_words(t.material), t)
    verdicts, missed = _judged(judge, want.material, [t.material for t in stand_ins.values()])
    fits = {p: _fit(verdicts, t.material) for p, t in stand_ins.items()}
    offered = [p for p, f in fits.items() if f.get("suits_goal") is not False]
    unfit = [f.get("why") or f"{p} doesn't suit what this part is for" for p, f in fits.items()
             if f.get("suits_goal") is False]
    load = f"Load {_a(needs)} spool and I'll start it."
    offer = f"Or I can {'re-slice' if sliced else 'slice'} it for {_listed(offered, 'or')}{'' if sliced else ' instead'}"
    if offered and all(fits[p].get("suits_goal") for p in offered):
        message = f"{first} {load} {offer}" + (f" ({unfit[0]})." if unfit else ".")
    elif offered:
        hedge = f"if {offered[0]} suits" if len(offered) == 1 else "if one of them suits"
        message = f"{first} {load} {offer}, {hedge} what this part is for."
    elif unfit:
        message = f"{first} {unfit[0]}, so I'd load {_a(needs)} spool and start it as is."
    else:
        message = f"{first} {load}"
    options = [_option("load_spool", material=want.material, colour=name, hex6=want.hex6 if name else None)]
    for p, t in stand_ins.items():
        options.append(_option("reslice_for", tray=t, material=t.material, colour=_colour_name(t.hex6) if t.hex6 else None,
                               hex6=t.hex6, **fits[p],
                               **({"goal_check_missed": missed} if missed else {})))
    return SpoolMiss("AMS_MATERIAL_NOT_LOADED", message, options)


def _parts_missing(wanted: list[Filament], trays: list[Tray], plan: AmsPlan, names: list[str | None],
                   free_slots: Sequence[str], noun: str, on_record: dict[int, str]) -> SpoolMiss:
    missing = [i for i in plan.unmatched if i < len(wanted)]
    used = {m["slot"] for m in plan.matches if m.get("slot") is not None}
    have, total = len(wanted) - len(missing), len(wanted)
    is_are = "is" if have == 1 else "are"
    message = f"{_count(have, capital=True)} of the {_count(total)} colours {is_are} loaded."

    def colour_of(i: int) -> str | None:
        return (names[i] if i < len(names) else None) or (_colour_name(wanted[i].hex6) if wanted[i].hex6 else None)

    subs: dict[int, list[Tray]] = {}
    for i in missing:
        fits = [t for t in trays if t.tray_id not in used
                and (wanted[i].material is None or same_plastic(wanted[i].material, t.material))]
        subs[i] = _nearest(wanted[i].hex6, fits)[:4]
    spare = [t for t in trays if t.tray_id not in used]
    where = ("into any empty slot" if len(free_slots) > 1 else f"into empty slot {free_slots[0]}" if free_slots
             else "in place of a spool this print doesn't use" if spare else "into the AMS")
    parts = [colour_of(i) or plastic_words(wanted[i].material) for i in missing]
    needs = [_spool(colour_of(i), wanted[i].material) for i in missing]
    if len(missing) == 1:
        message += f" The {parts[0]} {noun} needs {needs[0]}, and there's none loaded."
        mine = on_record.get(missing[0])
        load = f"Load your {mine} {where}" if mine else f"Load {colour_of(missing[0]) or needs[0]} {where}"
        pick = f"pick a loaded colour for that {noun}"
    else:
        none = "neither is" if len(missing) == 2 else "none of them is"
        nouns = noun[:-1] + "ies" if noun.endswith("y") else noun + "s"
        message += f" The {_listed(parts)} {nouns} need {_listed(needs)}, and {none} loaded."
        load = "Load them " + ("into empty slots" if free_slots else where.replace(" a spool", " spools"))
        pick = f"pick loaded colours for those {nouns}"
    message += f" {load}, or {pick}." if any(subs.values()) else f" {load} and I'll start it."

    mapping = [m.get("slot") for m in plan.matches]
    options: list[dict[str, Any]] = []
    for i in missing:
        options.append(_option("load_spool", part=i, material=wanted[i].material, colour=colour_of(i),
                               hex6=wanted[i].hex6, empty_slots=list(free_slots),
                               **({"on_record": on_record[i]} if i in on_record else {})))
        for t in subs[i]:
            whole = [t.tray_id if k == i else s for k, s in enumerate(mapping)] if len(missing) == 1 else None
            options.append(_option("print_from_slot", tray=t, part=i, material=t.material,
                                   colour=_colour_name(t.hex6), hex6=t.hex6,
                                   **({"mapping": whole} if whole else {})))
    return SpoolMiss("AMS_COLOR_MISMATCH", message, options)


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
