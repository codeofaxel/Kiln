"""Which material to print a part in, from what it is for.

:func:`recommend_material` answers "what should I print this in?" from a few
words of intent ("make it strong", "a flexible gasket"), the printer's
enclosure and heated bed, a budget and, when asked, the materials on hand.
The pick, its runners-up and the sentence saying why it wins are Kiln's
material recommendation (https://kiln3d.com), asked through
:mod:`kiln._pro_routing_bridge`.  This module adds what only this computer
knows -- where each material physically is, the nozzle on record -- and the
cautions every install says (abrasive filament, skin contact).
:func:`material_list` is the list behind it.

When Kiln's servers give no answer there is no pick.  The reply is basic: the
plastics the printer can melt, read from the nozzle temperature table Kiln
ships with, unranked, with the food-contact floor and the skin-contact caution
applied as for any pick, and one sentence saying why and what fixes it.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from typing import Any

from kiln.served_answer import Miss

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# The replies
# ---------------------------------------------------------------------------


@dataclass
class MaterialRecommendation:
    """A material pick for a given intent, as Kiln's recommendation gave it,
    with where the material is when the materials on hand were asked."""

    material: dict[str, Any]
    score: float
    reasoning: str
    settings: dict[str, Any]  # layer_height, speed, temps
    estimated_cost_usd: float | None
    success_rate: float | None  # from print DNA if available
    alternatives: list[dict[str, Any]]  # other options
    availability: dict[str, Any] | None = None  # on-hand attribution, if asked

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class BasicMaterials:
    """No pick: the plastics the printer can melt, unranked, and why Kiln
    did not pick one."""

    plastics: list[str]
    note: str
    miss: Miss
    hotend_limit_c: float | None = None
    cautions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        from kiln.served_answer import fields

        out: dict[str, Any] = {
            "success": True,
            "basic": True,
            "message": "\n\n".join([*self.cautions, self.note]),
            "plastics": list(self.plastics),
        }
        if self.hotend_limit_c is not None:
            out["hotend_limit_c"] = self.hotend_limit_c
        out["retryable"] = self.miss.no_answer
        out.update(fields(self.miss))
        return out


# ---------------------------------------------------------------------------
# What this computer adds
# ---------------------------------------------------------------------------


def _format_nozzle_context_line(
    summary: dict[str, Any] | None,
) -> str:
    """Format a one-sentence nozzle context line for the reasoning string.

    The line names the active nozzle material, diameter, provenance,
    approximate grams-through, and an optional low-confidence hedge.
    Capped at ~150 chars.  Returns an empty string when ``summary`` is
    ``None`` or missing required fields so the caller can append
    unconditionally.

    Example output:
        "Nozzle context: brass nozzle (0.4mm, provenance: bambu_mqtt, ~120g through). Settings tuned accordingly."
    """
    if not summary or not isinstance(summary, dict):
        return ""
    material = summary.get("material")
    if not material:
        return ""

    parts: list[str] = []
    diameter = summary.get("diameter_mm")
    if isinstance(diameter, (int, float)) and diameter > 0:
        parts.append(f"{float(diameter):g}mm")
    provenance = summary.get("provenance")
    if provenance:
        parts.append(f"provenance: {provenance}")
    grams = summary.get("grams_through")
    if isinstance(grams, (int, float)) and grams > 0:
        if grams >= 1000:
            parts.append(f"~{grams / 1000:.1f}kg through")
        else:
            parts.append(f"~{int(round(grams))}g through")

    detail = f" ({', '.join(parts)})" if parts else ""
    line = (
        f"Nozzle context: {material} nozzle{detail}. "
        f"Settings tuned accordingly."
    )
    if summary.get("trusted_for_verdicts") is False:
        line = (
            f"Nozzle context: {material} nozzle{detail} "
            f"(low confidence — replace if you've swapped without updating Kiln)."
        )
    # Defensive cap so a future bridge change can't blow the line up.
    return line if len(line) <= 200 else line[:197] + "..."


def _availability_for(
    catalog_name: str,
    on_hand_index: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    """Build the on-hand attribution block for one catalog material.

    Says WHERE the material physically is: which machines have it loaded
    (with the exact inventory string, so "PETG-CF" survives the catalog
    mapping) and which unloaded shelf spools carry it.
    """
    loaded: list[dict[str, Any]] = []
    shelf: list[dict[str, Any]] = []
    as_recorded: list[str] = []
    for entry in on_hand_index.get(catalog_name, []):
        mt = str(entry.get("material_type", ""))
        if mt and mt not in as_recorded:
            as_recorded.append(mt)
        for row in entry.get("loaded_on") or []:
            loaded.append({**row, "material_type": mt})
        for sp in entry.get("shelf_spools") or []:
            shelf.append({**sp, "material_type": mt})
    # Name the machine with the most material first (the one to use),
    # matching find_printers_with_material's most-stock-first ordering.
    loaded.sort(
        key=lambda r: r.get("remaining_grams") or 0.0, reverse=True
    )
    status = "loaded" if loaded else "on_shelf"
    return {
        "status": status,
        "as_recorded": as_recorded,
        "loaded_on": loaded,
        "shelf_spools": shelf,
        "swap_needed": status == "on_shelf",
    }


def _format_on_hand_line(availability: dict[str, Any]) -> str:
    """One honest sentence saying where the recommended material is."""
    names = ", ".join(availability.get("as_recorded", [])) or "material"
    if availability["status"] == "loaded":
        spots = []
        for row in availability["loaded_on"][:3]:
            grams = row.get("remaining_grams")
            where = row.get("printer_name", "?")
            spots.append(
                f"{where} (~{grams:.0f}g)" if isinstance(grams, (int, float))
                else where
            )
        return f"ON HAND: {names} loaded on {', '.join(spots)}."
    count = len(availability.get("shelf_spools", []))
    return (
        f"ON HAND (shelf only): {count} spool(s) of {names} available but "
        f"not currently loaded — a spool swap is needed before printing "
        f"(suggest_spool_swaps can plan it)."
    )


#: The skin-contact caution, around the honesty note for the material.
_SKIN_LEAD = "SKIN CONTACT: no printed part is proven safe on skin."
_SKIN_TAIL = (
    "Keep it out of the mouth, away from the eyes, and off broken skin, "
    "piercings and implants. If your skin reacts, ask a doctor."
)


def _capabilities_for_the_wire(caps: dict[str, Any] | None) -> dict[str, bool] | None:
    """The printer's enclosure and heated bed as the recommendation reads
    them: both said, or ``None`` when nothing was said about the printer."""
    if not caps:
        return None
    return {
        "has_enclosure": bool(caps.get("has_enclosure", False)),
        "has_heated_bed": bool(caps.get("has_heated_bed", True)),
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def recommend_material(
    intent: str,
    *,
    printer_capabilities: dict[str, Any] | None = None,
    budget_usd: float | None = None,
    model_fingerprint: dict[str, Any] | None = None,
    printer_id: str = "",
    on_hand: list[dict[str, Any]] | None = None,
) -> MaterialRecommendation | BasicMaterials:
    """Recommend a material based on user intent and constraints.

    Kiln's material recommendation picks from the intent, the printer's
    capabilities and the budget, and returns the top recommendation with
    alternatives.  When it gives no answer the result is
    :class:`BasicMaterials`: no pick, the plastics the printer can melt, and
    the sentence saying why.

    :param intent: User intent text (e.g. ``"make it strong"``).
    :param printer_capabilities: Optional dict with ``has_enclosure`` and
        ``has_heated_bed`` keys.
    :param budget_usd: Optional max budget per kg in USD.
    :param model_fingerprint: Optional fingerprint dict to check Print DNA
        for historical success rates.
    :param printer_id: Optional active-printer identifier.  When supplied,
        the recommendation consults the printer's nozzle state via
        ``_pro_nozzle_bridge``.  Two enrichments fire, independently and
        silently when there is no nozzle answer:

        1. **Abrasive escalation.** If the top recommendation is an
           abrasive material (CF / GF / wood / metal fill) AND the
           active nozzle is brass, the reasoning gains a prepended
           advisory line warning of the short brass lifetime.

        2. **Nozzle context.** Regardless of abrasive-ness, a single
           sentence is appended to the reasoning naming the active
           nozzle material, diameter, provenance, and approximate
           grams-through, so the caller sees which nozzle the
           recommendation was computed against.  An untrusted-state
           hedge is appended when ``trusted_for_verdicts`` is False.

        A basic answer compares the plastics with this printer's own rated
        hotend ceiling, when Kiln holds one for it.
    :param on_hand: Optional on-hand inventory (each entry the
        ``OnHandMaterial.to_dict()`` shape from
        :func:`kiln.material_inventory.get_on_hand_materials`).  When
        provided, candidacy narrows to materials the caller physically
        has, and the recommendation carries an ``availability`` block
        attributing the answer to the machine(s) holding it (or to the
        shelf, when a spool swap is needed first).  When nothing on hand
        matches, the best catalog pick is returned, clearly labeled
        needs-purchase — never a silent widening.  When the on-hand pick
        is a materially worse fit than a material the user does NOT own,
        that is named too (``better_catalog_option``), so "best of what
        you have" is never mistaken for "right for the job".  Only the
        material names leave this computer.
    """
    from kiln import _pro_routing_bridge

    # Entries with neither a loaded row nor a shelf spool carry no physical
    # material and confer no candidacy.
    physical = [e for e in (on_hand or []) if e.get("loaded_on") or e.get("shelf_spools")]
    on_hand_names = (
        None if on_hand is None
        else sorted({str(e.get("material_type", "")) for e in physical} - {""})
    )
    answer, miss = _pro_routing_bridge.pick(
        intent,
        printer_capabilities=_capabilities_for_the_wire(printer_capabilities),
        budget_usd=budget_usd,
        on_hand_materials=on_hand_names,
        inventory_recorded=bool(on_hand),
    )
    if answer is None:
        return basic_materials(intent, miss or Miss("unanswered"), printer_id=printer_id)

    top = dict(answer["pick"])
    if answer.get("fallback"):
        return MaterialRecommendation(
            material=top,
            score=answer["score"],
            reasoning=answer["sentence"],
            settings=dict(answer["settings"]),
            estimated_cost_usd=None,
            success_rate=None,
            alternatives=[],
        )

    on_hand_catalog = answer.get("on_hand_catalog") or {}
    on_hand_index: dict[str, list[dict[str, Any]]] = {}
    for entry in physical:
        cat = on_hand_catalog.get(str(entry.get("material_type", "")))
        if cat is not None:
            on_hand_index.setdefault(cat, []).append(entry)
    on_hand_status = answer.get("on_hand_status")
    reasoning = answer["sentence"]

    # On-hand attribution — say WHERE the answer physically is, or say
    # out loud that it isn't on hand.  Never silently widen: the
    # needs-purchase fallback keeps the catalog answer but labels it.
    availability: dict[str, Any] | None = None
    if on_hand_status == "on_hand":
        availability = _availability_for(top["name"], on_hand_index)
        reasoning = f"{reasoning} {_format_on_hand_line(availability)}"
        # Being on hand is not the same as being right for the job.  When
        # a material the user does NOT own suits the intent materially
        # better, say so — recommending the best of a poor shelf without
        # mentioning it is the quiet half of silently widening.
        better = answer.get("better_catalog_option")
        if better:
            availability["better_catalog_option"] = {
                "name": better["name"],
                "display_name": better["display_name"],
                "score": better["score"],
                "score_gap": better["score_gap"],
            }
            reasoning = (
                f"POOR FIT ON HAND: "
                f"{better['sentence']} "
                f"{top['display_name']} is the best of what you do "
                f"have.\n\n{reasoning}"
            )
    elif on_hand_status is not None:
        recorded = sorted({
            str(e.get("material_type", "")) for e in (on_hand or [])
            if e.get("material_type")
        })
        availability = {
            "status": on_hand_status,
            "as_recorded": [],
            "loaded_on": [],
            "shelf_spools": [],
            "swap_needed": False,
            "on_hand_recorded": recorded,
        }
        if on_hand_status == "no_inventory_recorded":
            reasoning = (
                "NOT ON HAND: no spools or loaded materials are recorded — "
                "record inventory with add_spool or an AMS/CFS sync to get "
                "on-hand answers. Catalog recommendation follows; purchase "
                f"required.\n\n{reasoning}"
            )
        else:
            have = ", ".join(recorded) or "your recorded materials"
            reasoning = (
                f"NOT ON HAND: nothing you have recorded ({have}) matches "
                f"this request — {top['display_name']} is a catalog pick "
                f"and needs to be purchased.\n\n{reasoning}"
            )

    # Check Print DNA for success rate if fingerprint provided
    success_rate: float | None = None
    if model_fingerprint:
        try:
            from kiln.print_dna import get_success_rate

            file_hash = model_fingerprint.get("file_hash", "")
            # The fingerprint dict already carries the shape signatures, and
            # a material recommendation should weigh how the DESIGN prints,
            # not how one export of it did.
            signature = model_fingerprint.get("geometric_signature", "") or ""
            signature_v2 = model_fingerprint.get("geometric_signature_v2", "") or ""
            if file_hash or signature or signature_v2:
                rate_data = get_success_rate(
                    file_hash,
                    material=top["name"],
                    geometric_signature=signature,
                    geometric_signature_v2=signature_v2,
                )
                if rate_data["total_prints"] > 0:
                    success_rate = rate_data["success_rate"]
        except Exception:
            logger.debug("Could not check Print DNA for success rate", exc_info=True)

    # Alternatives (up to 3, excluding the top pick), with where each is.
    alternatives: list[dict[str, Any]] = []
    for served_alt in answer["alternatives"]:
        alt: dict[str, Any] = dict(served_alt)
        if on_hand_status == "on_hand":
            alt_avail = _availability_for(alt["name"], on_hand_index)
            alt["availability"] = {
                "status": alt_avail["status"],
                "loaded_on": [
                    r.get("printer_name") for r in alt_avail["loaded_on"]
                ],
                "shelf_spools": len(alt_avail["shelf_spools"]),
            }
        alternatives.append(alt)

    # The material the user will ACTUALLY print: in on-hand mode that's
    # the recorded inventory string ("PETG-CF"), whose abrasive-ness the
    # catalog base name ("petg") would understate.
    _filament = top["name"]
    if availability and availability.get("as_recorded"):
        _filament = availability["as_recorded"][0]

    # Nozzle overlays — when the caller supplied a printer_id, consult
    # the bridge (kiln-pro in-process, else Kiln's servers) for (a)
    # abrasive escalation against the top pick (prepended NOZZLE
    # ADVISORY: every plan's warning, Kiln Pro's figure after it, and a
    # free answer's one line saying what Kiln Pro adds) and (b) a
    # one-sentence nozzle-context line (appended after the reasoning).
    # Both surface as reasoning-string mutations to preserve the existing
    # return shape for callers that parse it.  No answer leaves the floor
    # below.
    _nozzle_advisory_shown = False
    if printer_id:
        try:
            from kiln import _pro_nozzle_bridge

            _nozzle_advisory = _pro_nozzle_bridge.consult_abrasive_escalation(
                filament_material=_filament,
                printer_id=printer_id,
            )
            # The verdict's own contract: red, raised by the nozzle (an
            # abrasive on brass).
            if (
                _nozzle_advisory is not None
                and _nozzle_advisory.get("severity") == "red"
                and _nozzle_advisory.get("escalated") is True
            ):
                _advisory_lines = [str(_nozzle_advisory.get("user_warning") or "")]
                # A free answer names what Kiln Pro adds, once; a paid
                # answer carries no such line, so nothing is said twice.
                if _nozzle_advisory.get("pro_adds"):
                    _advisory_lines.append(str(_nozzle_advisory["pro_adds"]))
                reasoning = (
                    f"NOZZLE ADVISORY: {' '.join(_advisory_lines)} "
                    f"\n\n{reasoning}"
                )
                _nozzle_advisory_shown = True

            _nozzle_summary = _pro_nozzle_bridge.consult_nozzle_summary(
                printer_id,
            )
            _nozzle_line = _format_nozzle_context_line(_nozzle_summary)
            if _nozzle_line:
                reasoning = f"{reasoning} | {_nozzle_line}"
        except Exception:
            logger.debug(
                "Nozzle overlay skipped", exc_info=True,
            )

    # Abrasive-filament floor.  The NOZZLE ADVISORY above is the precise
    # answer — it knows which nozzle is fitted and how much life is left —
    # but it needs a recorded nozzle state.  Without one, a filled filament
    # would reach the user with no mention of the nozzle at all, and a
    # missing warning reads as "no concern".  This floor states the
    # material fact and is explicit about the nozzle being the part we
    # cannot see.  Public floor; reaches every install.  Suppressed when the
    # precise advisory already fired, so nobody gets told twice.
    if not _nozzle_advisory_shown:
        try:
            from kiln.material_safety import abrasive_nozzle_floor

            _abrasive_note = abrasive_nozzle_floor(_filament)
            if _abrasive_note:
                reasoning = f"{_abrasive_note}\n\n{reasoning}"
        except Exception:  # noqa: BLE001 — advisory is best-effort
            logger.debug("Abrasive-filament floor skipped", exc_info=True)

    # Skin-contact advisory (worn / handled against skin).  This is NOT a
    # filter — no printed material is skin-safe, so no candidate is dropped
    # or downranked for it.  When the intent implies a worn item the honest
    # caution for the chosen material is prepended, so a material picked for
    # a ring or band carries it.  Public floor; reaches every install.
    # Advisory only, best-effort, never breaks the recommendation.
    try:
        from kiln.design_intelligence import (
            get_skin_contact_floor,
            use_case_implies_skin_contact,
        )

        if use_case_implies_skin_contact(intent):
            _floor = get_skin_contact_floor(top["name"])
            _note = _floor.honesty_note if _floor is not None else ""
            reasoning = f"{_SKIN_LEAD} {_note} {_SKIN_TAIL}\n\n{reasoning}"
    except Exception:  # noqa: BLE001 — advisory is best-effort; never break the recommendation
        logger.debug("Skin-contact advisory skipped", exc_info=True)

    return MaterialRecommendation(
        material=top,
        score=answer["score"],
        reasoning=reasoning,
        settings=dict(answer["settings"]),
        estimated_cost_usd=answer.get("estimated_cost_usd"),
        success_rate=success_rate,
        alternatives=alternatives,
        availability=availability,
    )


def _hotend_limit_c(printer_id: str) -> float | None:
    """The named printer's own rated hotend ceiling, or ``None`` when Kiln
    holds none for it (no printer named, its model unknown, or only the
    generic limits on record)."""
    if not printer_id:
        return None
    try:
        from kiln.printer_model_resolver import resolve_printer_model_for
        from kiln.safety_profiles import get_profile

        model = resolve_printer_model_for(printer_id)
        if not model:
            return None
        profile = get_profile(model)
    except Exception:  # noqa: BLE001 -- unknown limits: the whole table is listed
        logger.debug("printer limits unreadable for %s", printer_id, exc_info=True)
        return None
    if profile.id == "default":
        return None
    return float(profile.max_hotend_temp)


def basic_materials(intent: str, miss: Miss, *, printer_id: str = "") -> BasicMaterials:
    """The answer when Kiln's material recommendation gives none: no pick,
    the plastics the printer can melt (unranked), the food-contact floor and
    the skin-contact caution, and one sentence saying why and what fixes it."""
    from kiln._pro_routing_bridge import FEATURE
    from kiln.material_basics import basic_pick_line, basic_plastics

    limit = _hotend_limit_c(printer_id)
    plastics = basic_plastics(intent, limit)
    cautions: list[str] = []
    try:
        from kiln.design_intelligence import use_case_implies_skin_contact

        if use_case_implies_skin_contact(intent):
            cautions.append(f"{_SKIN_LEAD} {_SKIN_TAIL}")
    except Exception:  # noqa: BLE001 — advisory is best-effort
        logger.debug("Skin-contact advisory skipped", exc_info=True)
    note = basic_pick_line(miss, feature=FEATURE, hotend_known=limit is not None)
    return BasicMaterials(plastics=plastics, note=note, miss=miss, hotend_limit_c=limit, cautions=cautions)


def material_list() -> dict[str, Any]:
    """The ``list_available_materials`` reply: every material Kiln's
    recommendation picks from, with its properties, sorted by name.  When
    Kiln's servers give no answer it is basic: each material's name and
    category, and one sentence saying why and what fixes it."""
    from kiln import _pro_routing_bridge

    materials, miss = _pro_routing_bridge.catalogue()
    if materials is not None:
        return {"success": True, "materials": materials, "count": len(materials)}
    return basic_material_list(miss or Miss("unanswered"))


def basic_material_list(miss: Miss) -> dict[str, Any]:
    """Names and categories only, from the material catalogue Kiln ships with."""
    from kiln import served_answer
    from kiln._pro_routing_bridge import FEATURE

    named: list[dict[str, Any]] = []
    try:
        from kiln.design_intelligence import list_public_material_profiles

        named = [
            {"name": p.material_id, "display_name": p.display_name, "category": p.category}
            for p in list_public_material_profiles()
        ]
    except Exception:  # noqa: BLE001 -- an unreadable catalogue lists nothing, and says why
        logger.debug("material names unreadable", exc_info=True)
    note = served_answer.sentence(
        miss,
        feature=FEATURE,
        on_the_line="Kiln did not list each material's properties",
        cannot="list them",
        wont="lists names and categories only",
        then="ask again for the full list",
    )
    out: dict[str, Any] = {
        "success": True,
        "basic": True,
        "message": note,
        "materials": named,
        "count": len(named),
        "retryable": miss.no_answer,
    }
    out.update(served_answer.fields(miss))
    return out
