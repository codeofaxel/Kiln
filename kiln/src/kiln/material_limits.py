"""One plastic's design limits: the one helper every design check reads.

:func:`limits_for` answers, for one plastic at one nozzle, how steep a face
may lean without support, how far a bridge may span, how thin a wall may go
(the thinnest that closes, and the thinnest recommended for strength), how
thin a pin may go, and how fine a thread and how small a hole print.

A figure that is the same for every plastic is answered here, from
:mod:`kiln.generic_limits`, with nothing asked.  A figure that differs by
plastic is asked of kiln-pro (https://kiln3d.com) through
:mod:`kiln._pro_material_limits_bridge`: on this computer when kiln-pro is
installed, otherwise from Kiln's servers.  When no answer comes, every figure
is the general one, ``basis`` says ``general``, and :attr:`MaterialLimits.miss`
says why; the general figures are never called the plastic's own.

A served answer is kept for the life of the process, per plastic and
scenario, so a session asks once.  When kiln-pro is on this computer nothing
is kept: one caller's answer never reaches the next.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from kiln import generic_limits as _generic
from kiln.served_answer import Miss, sentence

OVERHANG = "overhang_unsupported_deg"
BRIDGE = "bridge_max_mm"
WALL_RECOMMENDED = "wall_recommended_mm"
PIN = "pin_min_mm"
ESD_WALL = "esd_wall_min_mm"
WALL_MIN = "wall_min_mm"
BRIDGE_CLEAN = "bridge_clean_mm"
HOLE_FORMS = "hole_forms_mm"
HOLE_USABLE = "hole_usable_mm"
THREAD_PITCH = "thread_min_pitch_mm"
THREAD_DIAMETER = "thread_min_diameter_mm"

#: The quantities that differ by plastic: asked of kiln-pro.
PER_PLASTIC: tuple[str, ...] = (OVERHANG, BRIDGE, WALL_RECOMMENDED, PIN, ESD_WALL)
#: The quantities that are one figure for every plastic: answered here.
GENERAL_ONLY: tuple[str, ...] = (WALL_MIN, BRIDGE_CLEAN, HOLE_FORMS, HOLE_USABLE, THREAD_PITCH, THREAD_DIAMETER)

#: A figure the plastic itself, or its family, has.
OWN_BASES = ("plastic", "family")

_DATA = Path(__file__).resolve().parent / "data" / "design_knowledge" / "materials.json"

#: (material, nozzle, layer, enclosed, pin height) -> the served answer.
_answers: dict[tuple[Any, ...], dict[str, Any]] = {}
#: The causes a miss has already been said for in this process.
_said: set[str] = set()


@dataclass(frozen=True)
class MaterialLimits:
    """One plastic's limits in one scenario.  ``figures`` holds every
    quantity's value (``None`` where it does not apply), ``basis`` where each
    came from (``plastic``, ``family``, ``general``, ``not_applicable``)."""

    material: str
    material_id: str | None
    known: bool
    nozzle_mm: float
    layer_height_mm: float | None
    figures: dict[str, float | None] = field(default_factory=dict)
    basis: dict[str, str] = field(default_factory=dict)
    miss: Miss | None = None

    def value(self, quantity: str) -> float | None:
        """The figure for *quantity*, ``None`` where it does not apply."""
        return self.figures.get(quantity)

    def is_own(self, quantity: str) -> bool:
        """True when *quantity* is the plastic's own figure or its family's."""
        return self.basis.get(quantity) in OWN_BASES

    @property
    def basic(self) -> bool:
        """True when no plastic figure was read: every figure is general."""
        return not any(self.is_own(q) for q in PER_PLASTIC)

    def wall_floor_mm(self) -> float:
        """The thinnest wall this plastic may have: the static-safe grade's
        own electrical minimum where it has one, else the wall that closes."""
        esd = self.value(ESD_WALL)
        wall = self.value(WALL_MIN) or _generic.wall_min_mm(self.nozzle_mm)
        return max(float(esd), float(wall)) if esd else float(wall)

    def design_limits(self) -> dict[str, float]:
        """The figures under the names a design check's reply carries.  The
        recommended wall is there only when it is the plastic's own."""
        out: dict[str, float] = {
            "min_wall_thickness_mm": self.wall_floor_mm(),
            "max_unsupported_overhang_deg": float(self.value(OVERHANG) or _generic.OVERHANG_UNSUPPORTED_DEG),
            "max_bridge_length_mm": float(self.value(BRIDGE) or _generic.BRIDGE_MAX_MM),
            "min_hole_diameter_mm": float(self.value(HOLE_USABLE) or _generic.HOLE_USABLE_MM),
            "min_pin_diameter_mm": float(self.value(PIN) or _generic.PIN_MIN_MM),
            "thread_min_pitch_mm": float(self.value(THREAD_PITCH) or _generic.thread_min_pitch_mm()),
        }
        recommended = self.value(WALL_RECOMMENDED)
        if recommended and self.is_own(WALL_RECOMMENDED):
            out["recommended_wall_thickness_mm"] = float(recommended)
        return out

    def notes(self) -> list[str]:
        """What a reply should say about where its figures came from: why the
        plastic's own were not read (once per cause in a process), and for a
        flexible or static-safe plastic answered generally, what the general
        figures leave out.  Empty when the plastic's own figures were read."""
        lines: list[str] = []
        if self.miss is not None and self.miss.cause not in _said:
            _said.add(self.miss.cause)
            lines.append(miss_sentence(self.miss))
        if self.miss is not None or not self.known:
            category = _category(self.material_id)
            if category == "thermoplastic_elastomer":
                lines.append(
                    "The general figures are for ordinary rigid plastics; a flexible "
                    "plastic needs more margin than they give."
                )
            if self.material_id and self.material_id.endswith("_esd") and self.value(ESD_WALL) is None:
                lines.append(
                    "This static-safe grade's own minimum wall for its electrical "
                    "property is not known here; the wall figure is the general one."
                )
        return lines

    def to_dict(self) -> dict[str, Any]:
        return {
            "material_id": self.material_id,
            "known": self.known,
            "nozzle_mm": self.nozzle_mm,
            "layer_height_mm": self.layer_height_mm,
            "limits": {
                q: {"value": self.figures.get(q), "basis": self.basis.get(q, "general")}
                for q in (*PER_PLASTIC, *GENERAL_ONLY)
            },
        }


def miss_sentence(miss: Miss) -> str:
    """The one sentence for a miss, in the served-answer voice."""
    return sentence(
        miss,
        feature="design limits",
        on_the_line="How steep, how far and how thin a part can go depends on the plastic",
        cannot="get this plastic's own limits",
        wont="checks the part against the general figures and won't call them this plastic's own",
        safe_remedy="leave extra margin",
        then="check again",
    )


@lru_cache(maxsize=1)
def _catalogue() -> dict[str, Any]:
    try:
        raw = json.loads(_DATA.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in raw.items() if not k.startswith("_") and isinstance(v, dict)}


def _public_id(material: str | None) -> str | None:
    if not material:
        return None
    from kiln.catalog_keys import resolve_material_key

    return resolve_material_key(str(material), _catalogue().keys())


def _category(material_id: str | None) -> str | None:
    record = _catalogue().get(material_id or "")
    return record.get("category") if record else None


def _general(nozzle: float, layer: float | None) -> tuple[dict[str, float | None], dict[str, str]]:
    figures: dict[str, float | None] = {
        OVERHANG: _generic.OVERHANG_UNSUPPORTED_DEG,
        BRIDGE: _generic.BRIDGE_MAX_MM,
        WALL_RECOMMENDED: _generic.wall_recommended_mm(nozzle),
        PIN: max(_generic.PIN_MIN_MM, 2.0 * nozzle),
        ESD_WALL: None,
        WALL_MIN: _generic.wall_min_mm(nozzle),
        BRIDGE_CLEAN: _generic.BRIDGE_CLEAN_MM,
        HOLE_FORMS: _generic.HOLE_FORMS_MM,
        HOLE_USABLE: _generic.HOLE_USABLE_MM,
        THREAD_PITCH: _generic.thread_min_pitch_mm(layer),
        THREAD_DIAMETER: _generic.THREAD_MIN_DIAMETER_MM,
    }
    basis = {q: "general" for q in figures}
    basis[ESD_WALL] = "not_applicable"
    return figures, basis


def limits_for(
    material: str | None,
    *,
    nozzle_mm: float | None = None,
    layer_height_mm: float | None = None,
    enclosed: bool | None = None,
    pin_height_mm: float | None = None,
) -> MaterialLimits:
    """*material*'s limits at *nozzle_mm* (the reference nozzle when it is not
    stated; the answer says which).  Never raises.

    An empty material answers the general figures with nothing asked and no
    miss.  A material Kiln cannot place answers the general figures,
    ``known=False``."""
    nozzle = _generic.stated_nozzle_mm(nozzle_mm)
    layer = float(layer_height_mm) if isinstance(layer_height_mm, (int, float)) and layer_height_mm > 0 else None
    figures, basis = _general(nozzle, layer)
    name = str(material).strip() if material else ""
    public_id = _public_id(name)
    if not name:
        return MaterialLimits("", None, False, nozzle, layer, figures, basis)
    from kiln import _pro_material_limits_bridge as bridge

    local = bridge.available()
    key = (name.lower(), round(nozzle, 3), layer, enclosed, pin_height_mm)
    answer = None if local else _answers.get(key)
    miss: Miss | None = None
    if answer is None:
        answer, miss = bridge.ask(
            name, nozzle_mm=nozzle, layer_height_mm=layer, enclosed=enclosed, pin_height_mm=pin_height_mm,
        )
        if answer is not None and not local:
            _answers[key] = answer
    if answer is None:
        return MaterialLimits(name, public_id, public_id is not None, nozzle, layer, figures, basis, miss)
    for quantity, figure in answer["limits"].items():
        figures[quantity] = float(figure["value"]) if figure.get("value") is not None else None
        basis[quantity] = str(figure["basis"])
    return MaterialLimits(
        name, answer.get("material_id") or public_id, bool(answer["known"]), nozzle, layer, figures, basis,
    )


#: What a material lookup says beside the general block.
GENERAL_BLOCK_NOTE = (
    "General figures for any plastic.  The design checks apply this plastic's "
    "own limits where Kiln has them."
)


def general_design_limits() -> dict[str, Any]:
    """The general figures under a design check's reply names, marked as
    general, for a material lookup that shows no plastic's own limits."""
    return {
        **limits_for(None).design_limits(),
        "basis": "general",
        "note": GENERAL_BLOCK_NOTE,
    }


__all__ = [
    "GENERAL_ONLY",
    "PER_PLASTIC",
    "GENERAL_BLOCK_NOTE",
    "MaterialLimits",
    "general_design_limits",
    "limits_for",
    "miss_sentence",
]
