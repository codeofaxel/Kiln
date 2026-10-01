"""What a material asks of the slicer: temperatures, melt rate and cooling.

Until 2026-10-01 a slice declared for TPU was a PLA slice with TPU's density.
Every slicing door took ``material`` for the weight alone, so the printer
profile's PLA temperatures, cooling and speeds went out under a TPU label --
measured on a Bambu Lab A1: 220 °C, a 65 °C bed and 150 mm/s outer walls,
where Kiln's own data said 225 °C, a 40-60 °C bed and "print slowly".  Kiln
knew the answer three times over -- the curated material ranges, its
settings per printer, a product's own figures through the filament resolver
-- and none of them reached the slice.  The one door that applied any of it
(``build_material_overrides``) kept a speed table of its own, which slowed
the inner walls of a TPU part and left the outer wall at 150 mm/s.

So the settings a material needs are decided HERE, once, and written at the
chokepoint every slice passes through
(:func:`kiln.slicer_filament.ensure_profile_filament`):

* **Temperatures** -- the product's own when the caller named one
  (``bambu_tpu_95a``), else Kiln's settings for the material on this
  printer, else the middle of the material's printing range.  Never above
  what the printer is rated for, and a material whose lowest printing
  temperature the hotend cannot reach is refused, by the verdict the print
  gate itself gives (:func:`kiln.printers.print_gate.check_material_temp`).
* **Melt rate** -- ``filament_max_volumetric_speed``, the most plastic a
  second the material can be pushed through a nozzle.  PrusaSlicer holds
  every extruding move under it -- measured on 2.9.4 at 3.6 mm³/s: outer
  walls, bridges, overhangs, infill and the first layer all came out at 3.6
  or under -- and OrcaSlicer takes it as its filament's own ceiling
  (:mod:`kiln.slicer_orca`).  One number slows a whole print to suit the
  material, with no second speed table to keep in step with the profile's.
* **Cooling** -- the part fan's ceiling.

Three rules keep it honest:

* A setting the caller stated is theirs.  A caller who states any key of a
  concept (:data:`CONCEPT_KEYS`) owns the concept, and the response names it
  as kept.
* A printer profile's own numbers stand for the material it was tuned for
  (:data:`PROFILE_MATERIAL`) wherever they sit inside that material's range:
  they are its author's tuning for that machine, and a generic figure is not
  better than a measured one.
* A profile that is the caller's own file is never edited -- every value in
  it is the author speaking -- only compared, and the response says where it
  differs from what the material needs.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

#: The material Kiln's bundled printer profiles are tuned for.  Pinned by
#: ``test_slicer_material.py``: every bundled profile's temperatures sit in
#: this material's range, or the profile says what it is tuned for instead.
PROFILE_MATERIAL = "pla"

#: Each thing a material decides, and the slicer keys that carry it.  The
#: hotend's own ceiling (``max_volumetric_speed``, written for every printer
#: profile) is in the flow concept so that a caller who states it is heard as
#: speaking about flow; the material writes only its own key, and both
#: slicers take the lower of the two.
CONCEPT_KEYS: dict[str, tuple[str, ...]] = {
    "nozzle": ("temperature", "first_layer_temperature"),
    "bed": ("bed_temperature", "first_layer_bed_temperature"),
    "flow": ("filament_max_volumetric_speed", "max_volumetric_speed"),
    "fan": ("min_fan_speed", "max_fan_speed"),
}

#: Where a value came from.  The response carries these words.
SOURCE_PRODUCT = "product"
SOURCE_PRINTER = "printer"
SOURCE_MATERIAL = "material"

#: What the slicing chokepoint did with a material's settings.
APPLIED = "applied"
PROFILE = "profile"
YOURS = "yours"
UNKNOWN = "unknown"


class MaterialRefused(Exception):
    """The printer cannot print the material: its hotend never gets hot enough.

    Carries the print gate's verdict -- ``code`` and the reason in words --
    so a slice refuses with exactly the sentence a print would.
    """

    def __init__(self, verdict: Mapping[str, Any]) -> None:
        super().__init__(str(verdict.get("reason") or "This printer cannot print that material."))
        self.code = str(verdict.get("code") or "MATERIAL_EXCEEDS_HOTEND")
        self.verdict = dict(verdict)


@dataclass(frozen=True)
class Need:
    """One concept's settings, and why they are what they are."""

    concept: str
    values: tuple[tuple[str, str], ...]
    source: str
    why: str
    #: A sentence when the value was held under the printer's rating.
    held: str = ""


@dataclass(frozen=True)
class MaterialNeeds:
    """What a material asks of the slicer on one printer."""

    #: The material as a person names it: ``"TPU"``, ``"Bambu Lab TPU 95A"``.
    label: str
    #: The curated catalog id, ``"tpu"``.
    material_id: str
    #: True when the caller named a product, whose own figures were used.
    product: bool
    needs: tuple[Need, ...]
    nozzle_range: tuple[int, int] | None
    bed_range: tuple[int, int] | None
    flow_ceiling: float | None
    #: The print gate's verdict when the printer cannot melt the material.
    refusal: dict[str, Any] | None = None

    def values(self) -> dict[str, str]:
        """Every setting this material asks for, as slicer keys."""
        return {key: value for need in self.needs for key, value in need.values}


@dataclass(frozen=True)
class MaterialReport:
    """What the slicing chokepoint did with a material's settings, in words."""

    material: str
    outcome: str
    note: str
    #: ``(key, before, after, source)`` for each value written.
    changed: tuple[tuple[str, str, str, str], ...] = ()
    #: ``(key, value)`` the caller stated, left as they were.
    kept: tuple[tuple[str, str], ...] = ()
    #: ``(setting, value in the profile, what the material needs)`` -- a
    #: caller's own profile, compared and not edited.
    differs: tuple[tuple[str, str, str], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"material": self.material, "outcome": self.outcome, "note": self.note}
        if self.changed:
            d["changed"] = [
                {"setting": k, "from": before, "to": after, "because": source}
                for k, before, after, source in self.changed
            ]
        if self.kept:
            d["kept"] = [{"setting": k, "value": v, "because": "stated"} for k, v in self.kept]
        if self.differs:
            d["differs"] = [{"setting": s, "profile": p, "material": m} for s, p, m in self.differs]
        return d


# ---------------------------------------------------------------------------
# Resolving a material
# ---------------------------------------------------------------------------


def _number(value: Any) -> float | None:
    """The first number in a scalar or a ``,``/``;`` vector, or ``None``."""
    for raw in str(value if value is not None else "").replace(";", ",").split(","):
        raw = raw.strip()
        if raw:
            try:
                return float(raw)
            except ValueError:
                return None
    return None


def _span(pair: Any) -> tuple[int, int] | None:
    if isinstance(pair, (list, tuple)) and len(pair) >= 2:
        return int(pair[0]), int(pair[1])
    return None


def _middle(span: tuple[int, int] | None) -> int | None:
    return (span[0] + span[1]) // 2 if span else None


def _short_name(display_name: str) -> str:
    """``"TPU (Thermoplastic Polyurethane)"`` -> ``"TPU"``."""
    return display_name.split(" (")[0].strip() or display_name


def _catalog_profile(word: str) -> Any:
    """The curated material *word* names, through the cost table's family as a last resort.

    ``catalog_keys`` matches spellings and never a family, which is right for
    an engineering table.  A slice is told what a filament IS, and "PLA
    Basic", "Rapid PETG" and "Hyper PLA" are PLA and PETG to a slicer -- so
    the one lookup the weight already uses (:func:`kiln.cost_estimator.resolve_material`)
    answers when the catalog has no spelling for the word.
    """
    from kiln.cost_estimator import resolve_material
    from kiln.design_intelligence import get_material_profile

    profile = get_material_profile(word)
    if profile is None:
        row = resolve_material(word)
        if row is not None:
            profile = get_material_profile(row.name)
    return profile


def _printer_settings(printer_id: str, catalog_id: str) -> tuple[Any, str] | None:
    """Kiln's settings for the material on *printer_id*, and the printer's name.

    The printer table spells materials the way printers and their makers do
    (``"PLA-CF"``, ``"PA-CF"``, ``"PEI 9085"``); each is matched to the
    catalog through :func:`kiln.catalog_keys.resolve_material_key`, so one
    vocabulary decides both.  A printer the table does not know answers
    nothing: its fallback profile describes no machine in particular.
    """
    from kiln.catalog_keys import resolve_material_key
    from kiln.design_intelligence import _get_kb
    from kiln.printer_intelligence import get_printer_intel

    try:
        intel = get_printer_intel(printer_id)
    except KeyError:
        return None
    if intel.id == "default":
        return None
    catalog = list(_get_kb().materials)
    for spelled, settings in intel.materials.items():
        if resolve_material_key(spelled, catalog) == catalog_id:
            return settings, intel.display_name
    return None


def material_needs(
    material: str | None,
    *,
    printer_id: str | None = None,
    label: str | None = None,
) -> MaterialNeeds | None:
    """What *material* asks of the slicer on *printer_id*, or ``None`` when Kiln knows nothing of it.

    *material* is a caller's word for a filament -- a catalog id (``"tpu"``),
    a printer's spelling (``"PLA-CF"``), or a product (``"bambu_tpu_95a"``).
    *label* is the name the response should use; the material's own short
    name when omitted.  Never raises.
    """
    word = str(material or "").strip()
    if not word:
        return None
    try:
        return _resolve(word, printer_id=printer_id, label=label)
    except Exception:  # noqa: BLE001 -- a lookup must never fail a slice
        logger.debug("Material settings for %r could not be resolved", word, exc_info=True)
        return None


def _resolve(word: str, *, printer_id: str | None, label: str | None) -> MaterialNeeds | None:
    from kiln.design_intelligence import get_brand_filament_profile

    product = get_brand_filament_profile(word.lower())
    catalog = _catalog_profile(product.parent_material if product else word)
    if catalog is None:
        return None
    # The product's name when the caller named one; the material's for every
    # figure that is the material's, whatever the product.
    material_name = label if label and not product else _short_name(catalog.display_name)
    name = f"{product.brand} {product.product_name}" if product else material_name
    thermal = catalog.thermal or {}
    slicing = catalog.slicing or {}
    nozzle_range = _span(thermal.get("print_temp_range_c"))
    bed_range = _span(thermal.get("bed_temp_range_c"))

    tuned = _printer_settings(printer_id, catalog.material_id) if printer_id else None
    printer_name = tuned[1] if tuned else ""
    on_printer = tuned[0] if tuned else None
    limits = _limits(printer_id)

    def pick(product_value: Any, printer_value: Any, material_value: Any, *, material_why: str) -> tuple[Any, str, str]:
        if product is not None and product_value is not None:
            return product_value, SOURCE_PRODUCT, f"{name}'s own settings"
        if printer_value is not None:
            return printer_value, SOURCE_PRINTER, f"Kiln's settings for {material_name} on the {printer_name}"
        return material_value, SOURCE_MATERIAL, material_why

    needs: list[Need] = []
    nozzle, source, why = pick(
        product.nozzle_temp_optimal_c if product else None,
        on_printer.hotend if on_printer else None,
        _middle(nozzle_range),
        material_why=f"the middle of {material_name}'s printing range",
    )
    if nozzle is not None:
        nozzle, held = _hold(int(nozzle), limits.get("hotend"), "hotend", name)
        needs.append(Need("nozzle", (("temperature", str(nozzle)), ("first_layer_temperature", str(nozzle))), source, why, held))
    bed, source, why = pick(
        product.bed_temp_optimal_c if product else None,
        on_printer.bed if on_printer else None,
        _middle(bed_range),
        material_why=f"the middle of {material_name}'s bed range",
    )
    if bed is not None:
        bed, held = _hold(int(bed), limits.get("bed"), "bed", name)
        needs.append(Need("bed", (("bed_temperature", str(bed)), ("first_layer_bed_temperature", str(bed))), source, why, held))
    flow, source, why = pick(
        product.max_volumetric_speed_mm3s if product else None,
        None,
        slicing.get("max_volumetric_speed_mm3s"),
        material_why=f"the most cautious figure slicer makers give {material_name}",
    )
    if flow:
        needs.append(Need("flow", (("filament_max_volumetric_speed", f"{float(flow):g}"),), source, why))
    fan, source, why = pick(
        None,
        on_printer.fan if on_printer else None,
        slicing.get("fan_max_pct"),
        material_why=f"{material_name}'s cooling",
    )
    if fan is not None:
        values = [("max_fan_speed", str(int(fan)))]
        if slicing.get("fan_min_pct") is not None and source == SOURCE_MATERIAL:
            values.insert(0, ("min_fan_speed", str(int(slicing["fan_min_pct"]))))
        needs.append(Need("fan", tuple(values), source, why))

    return MaterialNeeds(
        label=name,
        material_id=catalog.material_id,
        product=product is not None,
        needs=tuple(needs),
        nozzle_range=nozzle_range,
        bed_range=bed_range,
        flow_ceiling=float(flow) if flow else None,
        refusal=_refusal(printer_id, catalog.material_id),
    )


def _limits(printer_id: str | None) -> dict[str, float]:
    """The printer's rated hotend and bed ceilings, through the one door for printer limits."""
    if not printer_id:
        return {}
    from kiln.safety_profiles import get_profile

    try:
        profile = get_profile(printer_id)
    except KeyError:
        return {}
    return {"hotend": profile.max_hotend_temp, "bed": profile.max_bed_temp}


def _hold(value: int, ceiling: float | None, heater: str, name: str) -> tuple[int, str]:
    """*value*, held at the printer's rated *ceiling* when it asks for more."""
    if ceiling is None or value <= ceiling:
        return value, ""
    held = int(ceiling)
    return held, (
        f"{name} wants {value} °C on the {heater}; this printer's {heater} is rated to {held} °C, "
        f"so the {heater} is set to {held} °C."
    )


def _refusal(printer_id: str | None, catalog_id: str) -> dict[str, Any] | None:
    if not printer_id:
        return None
    try:
        from kiln.printers.print_gate import check_material_temp

        return check_material_temp(printer_id, catalog_id)
    except Exception:  # noqa: BLE001 -- the gate soft-passes on anything it cannot read
        return None


# ---------------------------------------------------------------------------
# Writing them into a profile
# ---------------------------------------------------------------------------


def _within(settings: Mapping[str, str], needs: MaterialNeeds) -> bool:
    """True when the profile states both temperatures and they sit inside the material's ranges.

    A profile that states none (an overrides-only file, a bare slice) has
    no tuning to keep: the slicer's own defaults would answer, and those
    are a 0 °C bed.
    """
    nozzle = _number(settings.get("temperature"))
    bed = _number(settings.get("bed_temperature"))
    if nozzle is None or bed is None:
        return False
    lo_hi = ((nozzle, needs.nozzle_range), (bed, needs.bed_range))
    return all(span is None or span[0] <= v <= span[1] for v, span in lo_hi)


def apply_material_needs(
    settings: dict[str, str],
    needs: MaterialNeeds,
    *,
    stated: frozenset[str] = frozenset(),
) -> MaterialReport:
    """Write *needs* into *settings*, in place, and say what was done.

    Concepts with a stated key are left alone and named as kept.  When the
    material is the one the profile was tuned for and the profile's own
    temperatures suit it, nothing is written.  A start block that is Kiln's
    own warm-up floor is written again at the new temperatures -- the floor
    quotes them literally, and left as it was it would heat the printer for
    the old material before the first layer asked for the new one.
    """
    from kiln.slicer_profiles import start_floor

    if not needs.needs:
        return unknown_material_report(needs.label)
    if needs.material_id == PROFILE_MATERIAL and not needs.product and _within(settings, needs):
        return MaterialReport(
            material=needs.label,
            outcome=PROFILE,
            note=(
                f"{needs.label}: the printer profile's own temperatures, cooling and speeds, "
                f"which are tuned for {needs.label}."
            ),
        )

    floor = start_floor(settings)
    floor_is_kilns = floor is not None and settings.get("start_gcode") == floor
    changed: list[tuple[str, str, str, str]] = []
    kept: list[tuple[str, str]] = []
    applied: list[Need] = []
    for need in needs.needs:
        mine = [k for k in CONCEPT_KEYS[need.concept] if k in stated]
        if mine:
            kept.extend((k, str(settings.get(k, ""))) for k in mine)
            continue
        applied.append(need)
        for key, value in need.values:
            before = str(settings.get(key, ""))
            after = _per_slot(settings, key, value)
            if before != after:
                settings[key] = after
                changed.append((key, before, after, need.source))
        if need.concept == "fan":
            _fan_floor_under_ceiling(settings, changed, need.source)

    if floor_is_kilns and any(k in CONCEPT_KEYS["nozzle"] + CONCEPT_KEYS["bed"] for k, *_ in changed):
        settings["start_gcode"] = start_floor(settings) or settings["start_gcode"]

    return MaterialReport(
        material=needs.label,
        outcome=APPLIED,
        note=_applied_sentence(needs, applied, kept),
        changed=tuple(changed),
        kept=tuple(kept),
    )


def _per_slot(settings: Mapping[str, str], key: str, value: str) -> str:
    """*value* once per extruder slot, in the spelling the key already uses.

    A multi-extruder profile states its per-extruder keys as vectors
    (``220;220;220;220``); a scalar written over one would leave three slots
    to the slicer's own default.  A key the profile does not state yet takes
    one value per nozzle the profile declares.
    """
    existing = str(settings.get(key, ""))
    for sep in (";", ","):
        if sep in existing:
            return sep.join([value] * len(existing.split(sep)))
    slots = len([v for v in str(settings.get("nozzle_diameter", "")).replace(";", ",").split(",") if v.strip()])
    return ",".join([value] * slots) if slots > 1 and not existing else value


def _fan_floor_under_ceiling(settings: dict[str, str], changed: list, source: str) -> None:
    """Keep the fan's running speed at or under the material's ceiling."""
    ceiling = _number(settings.get("max_fan_speed"))
    floor = _number(settings.get("min_fan_speed"))
    if ceiling is not None and floor is not None and floor > ceiling:
        before = settings["min_fan_speed"]
        settings["min_fan_speed"] = _per_slot(settings, "min_fan_speed", f"{ceiling:g}")
        changed.append(("min_fan_speed", before, settings["min_fan_speed"], source))


def _accepted(span: tuple[int, int] | None, chosen: str | None) -> tuple[int, int] | None:
    """The temperatures a profile may hold without being called wrong.

    The material's range, widened to take in Kiln's own setting for it on
    this printer: a maker tunes its machines outside a datasheet's band
    (Kiln prints ABS at 260 °C on Bambu machines, against a 230-255 °C
    material range), and a comparison that flagged Kiln's own number in
    someone's profile would be advice Kiln does not follow.
    """
    value = _number(chosen)
    if span is None or value is None:
        return span
    return min(span[0], int(value)), max(span[1], int(value))


def compare_material_needs(settings: Mapping[str, str], needs: MaterialNeeds) -> MaterialReport:
    """Compare a caller's own profile with what the material needs; edit nothing."""
    chosen = needs.values()
    differs: list[tuple[str, str, str]] = []
    for setting, key, span in (
        ("nozzle", "temperature", _accepted(needs.nozzle_range, chosen.get("temperature"))),
        ("bed", "bed_temperature", _accepted(needs.bed_range, chosen.get("bed_temperature"))),
    ):
        value = _number(settings.get(key))
        if value is not None and span and not span[0] <= value <= span[1]:
            differs.append((setting, f"{value:g} °C", f"{span[0]}-{span[1]} °C"))
    if needs.flow_ceiling:
        limits = [v for k in CONCEPT_KEYS["flow"] if (v := _number(settings.get(k))) and v > 0]
        ceiling = min(limits) if limits else None
        if ceiling is None or ceiling > needs.flow_ceiling * 1.001:
            stated = f"{ceiling:g} mm³/s" if ceiling else "no limit"
            differs.append(("melt rate", stated, f"at most {needs.flow_ceiling:g} mm³/s"))
    if differs:
        listed = "; ".join(f"{s} {p} (it needs {m})" for s, p, m in differs)
        note = f"Your profile was used as written. For {needs.label} it differs: {listed}."
    else:
        note = f"Your profile was used as written; its temperatures and melt rate suit {needs.label}."
    return MaterialReport(material=needs.label, outcome=YOURS, note=note, differs=tuple(differs))


def unknown_material_report(word: str) -> MaterialReport:
    return MaterialReport(
        material=word,
        outcome=UNKNOWN,
        note=f"Kiln has no printing settings for {word}, so the printer profile's own were used.",
    )


def preview_material_values(
    printer_id: str | None,
    material: str | None,
    *,
    overrides: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """The temperatures a slice for *material* on *printer_id* will carry.

    For a door that must hand a temperature to something BEFORE the slice
    runs -- a printer's own start macro takes its heat-up temperatures as
    arguments -- so what it hands is what the slice prints at: the bundled
    profile under the caller's *overrides*, with the material written in by
    the very function the slice uses.  Empty when there is nothing to
    preview (no printer profile, no material Kiln knows, or one it refuses).
    """
    if not printer_id or not material:
        return {}
    from kiln.slicer_profiles import get_slicer_profile

    try:
        settings = dict(get_slicer_profile(printer_id).settings)
    except KeyError:
        return {}
    settings.update(overrides or {})
    needs = material_needs(material, printer_id=printer_id)
    if needs is None or needs.refusal is not None:
        return {}
    apply_material_needs(settings, needs, stated=frozenset(overrides or ()))
    return {k: settings[k] for k in CONCEPT_KEYS["nozzle"] + CONCEPT_KEYS["bed"] if k in settings}


# ---------------------------------------------------------------------------
# Words
# ---------------------------------------------------------------------------

_PHRASES: dict[str, str] = {
    "nozzle": "nozzle {temperature} °C",
    "bed": "bed {bed_temperature} °C",
    "flow": "at most {filament_max_volumetric_speed} mm³/s of plastic, which every feature slows to",
    "fan": "part fan at most {max_fan_speed}%",
}


def _applied_sentence(needs: MaterialNeeds, applied: list[Need], kept: list[tuple[str, str]]) -> str:
    """One sentence: each setting once, each reason once.

    ``Set for TPU: nozzle 225 °C, bed 50 °C and part fan at most 50% (Kiln's
    settings for TPU on the Bambu Lab A1); at most 3.6 mm³/s of plastic,
    which every feature slows to (TPU's melt rate).``
    """
    by_reason: dict[str, list[str]] = {}
    for need in applied:
        by_reason.setdefault(need.why, []).append(_PHRASES[need.concept].format(**dict(need.values)))
    groups = [
        f"{', '.join(phrases[:-1])} and {phrases[-1]}" if len(phrases) > 1 else phrases[0]
        for phrases in by_reason.values()
    ]
    clauses = [f"{group} ({why})" for group, why in zip(groups, by_reason, strict=True)]
    sentence = f"Set for {needs.label}: " + "; ".join(clauses) + "." if clauses else (
        f"{needs.label}: every setting it needs was stated."
    )
    if kept:
        sentence += " Kept as stated: " + ", ".join(f"{k} {v}" for k, v in kept) + "."
    held = [n.held for n in applied if n.held]
    if held:
        sentence += " " + " ".join(held)
    return sentence
