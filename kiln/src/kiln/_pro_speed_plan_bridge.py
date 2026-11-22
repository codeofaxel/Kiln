"""Public Kiln -> Kiln's servers: the speeds Kiln chooses for a part, by height.

A slice asked with ``speed_by_height="kiln"`` has Kiln choose the ranges.
The choosing is kiln-pro's (https://kiln3d.com/pricing).  This module
measures what the choice is made from, on this computer, asks for it, and
hands the answer to :func:`kiln.speed_by_height.sliced` as its planner:

* the part's outlines at a run of heights (:mod:`kiln.part_outlines`);
* what the slice made without any wish commands at each of those heights:
  a layer's time, and for each kind of line its fastest speed, its fullest
  flow and its share of the printing;
* the slice's own flow limit, cooling time, acceleration and infill.

With kiln-pro installed here (Kiln's servers) the answer is read in process
(:func:`_engine`, which imports kiln-pro by name); on a plain install it is
asked of Kiln's servers as :data:`WIRE_TOOL`, through the door every served
tool uses (:func:`kiln.server._pro_api_call`).  The answer is ranges in the
wish's own form, each with its sentence, and nothing else.

A miss (this computer offline, Kiln signed out, the servers silent or
saying no) refuses the slice in the four causes' words: speeds Kiln did not
choose are never sliced in as if it had.  Nothing here raises anything but
:class:`kiln.speed_by_height.SpeedByHeightError`.
"""

from __future__ import annotations

import bisect
import logging
from collections.abc import Callable, Mapping
from typing import Any

from kiln import plain_words as _plain_words
from kiln.served_answer import Miss, classify_answer, classify_transport_error, local_fault

logger = logging.getLogger(__name__)

#: What a caller passes as ``speed_by_height`` to have Kiln choose.
ASK = "kiln"
#: The served tool that answers the plan for one printer, and the one that
#: answers it for a printer that is one of several the part is sliced for.
WIRE_TOOL = "speed_plan"
FLEET_WIRE_TOOL = "fleet_speed_plan"
#: Where the same answer is read when kiln-pro IS installed here.
LOCAL_MODULE = "kiln_pro.speed_plan.engine"
#: What the sign-in sentence calls the answer asked for.
SIGN_IN_NAME = "the speeds Kiln chooses by height"
#: The door's code for a plan that did not arrive.
NO_PLAN = "SPEED_BY_HEIGHT_NO_PLAN"

_NOT_HERE = object()


def read_ask(wish: Any) -> dict[str, Any] | None:
    """*wish* as an ask for Kiln's own speeds (``"kiln"``, or ``{"by":
    "kiln", ...}`` carrying the printer, the material, whether the part
    carries load, and under ``fleet`` every printer the part is being sliced
    for when there are several), or ``None`` when it is the person's own
    ranges."""
    if isinstance(wish, str) and wish.strip().lower() == ASK:
        return {}
    if isinstance(wish, Mapping) and str(wish.get("by", "")).strip().lower() == ASK:
        return {
            key: wish[key] for key in ("printer", "material", "carries_load", "fleet") if wish.get(key) is not None
        }
    return None


def with_context(wish: Any, *, printer: Any = None, material: Any = None, fleet: Any = None) -> Any:
    """*wish* unchanged, unless it asks for Kiln's own speeds: then the ask
    carrying the printer and material a door knows, where it names none,
    and under ``fleet`` every printer the part is being sliced for: the
    ask's own printers first, then any of *fleet* it does not name."""
    ask = read_ask(wish)
    if ask is None:
        return wish
    for key, value in (("printer", printer), ("material", material)):
        if value and not ask.get(key):
            ask[key] = str(value)
    if fleet:
        named = [str(p) for p in (ask.get("fleet") or []) if p]
        named += [str(p) for p in fleet if p and str(p) not in named]
        ask["fleet"] = list(dict.fromkeys(named))
    return {"by": ASK, **ask}


def _first(value: Any) -> float | None:
    from kiln.speed_by_height import _first_positive

    return _first_positive(None if value is None else str(value))


def sliced_facts(part: Mapping[str, Any], plain: Any) -> dict[str, Any]:
    """What the slice read as *plain* commands at each level of *part*."""
    from kiln.speed_by_height import _JUDGED_MOVE_MM

    heights = [level["z_mm"] for level in part["levels"]]
    edges = [(low + high) / 2.0 for low, high in zip(heights, heights[1:], strict=False)]
    gathered: list[list[Any]] = [[] for _ in heights]
    for layer in plain.layers:
        if layer.moves:
            gathered[bisect.bisect_right(edges, layer.middle)].append(layer)
    levels: list[dict[str, Any] | None] = []
    for layers in gathered:
        moves = [move for layer in layers for move in layer.moves]
        length = sum(move[1] for move in moves)
        kinds: dict[str, dict[str, float]] = {}
        for speed, run, flow, kind in moves:
            row = kinds.setdefault(kind.strip().lower(), {"fastest_mm_s": 0.0, "flow_mm3_s": 0.0, "share": 0.0})
            row["share"] += run / length
            if run >= _JUDGED_MOVE_MM:
                row["fastest_mm_s"] = max(row["fastest_mm_s"], speed)
                row["flow_mm3_s"] = max(row["flow_mm3_s"], flow or 0.0)
        kinds = {name: row for name, row in kinds.items() if row["fastest_mm_s"] > 0}
        if not kinds:
            levels.append(None)
            continue
        levels.append({
            "layer_s": round(sum(layer.seconds for layer in layers) / len(layers), 2),
            "kinds": {
                name: {key: round(value, 3 if key == "share" else 2) for key, value in row.items()}
                for name, row in kinds.items()
            },
        })
    settings = plain.settings
    hardest = [layer.hardest_acceleration for layer in plain.layers if layer.hardest_acceleration]
    infill = _first(str(settings.get("fill_density") or settings.get("sparse_infill_density") or "").rstrip("%"))
    return {
        "layer_mm": _first(settings.get("layer_height")),
        "flow_limit_mm3_s": plain.flow_limit(),
        "cooling_s": plain.cooling_seconds(),
        "acceleration_mm_s2": max(hardest) if hardest else _first(settings.get("default_acceleration")),
        "infill_share": None if infill is None else min(infill / 100.0, 1.0),
        "nozzle_mm": _first(settings.get("nozzle_diameter")),
        "levels": levels,
    }


def planner(
    part_path: str,
    *,
    slicer_id: str,
    ask: Mapping[str, Any],
) -> Callable[[Any], Mapping[str, Any]]:
    """The planner :func:`kiln.speed_by_height.sliced` calls with the plain
    slice's reading: Kiln's ranges for the part at *part_path*, or the
    refusal that says why there are none."""
    from kiln.speed_by_height import SpeedByHeightError

    def _plan(plain: Any) -> Mapping[str, Any]:
        from kiln.part_outlines import PartOutlinesError, outlines_by_height
        from kiln.step_import import resolve_mesh_input

        try:
            # A CAD file is measured as the mesh every mesh tool makes of it.
            mesh_path, _conversion, refused = resolve_mesh_input(part_path)
            if refused:
                raise PartOutlinesError(str(refused.get("error") or refused.get("message") or "no converter"))
            part = outlines_by_height(mesh_path)
        except PartOutlinesError as exc:
            raise SpeedByHeightError(
                f"Kiln could not read this part's shape, so it chose no speeds and nothing was sliced: {_plain_words.said(exc)}",
                code=NO_PLAN,
            ) from exc
        facts = sliced_facts(part, plain)
        nozzle = facts.pop("nozzle_mm", None)
        printer: dict[str, Any] = {
            "printer": str(ask.get("printer") or ""), "material": str(ask.get("material") or ""),
            "slicer": slicer_id, "sliced": facts,
        }
        if nozzle:
            printer["nozzle_mm"] = nozzle
        fields: dict[str, Any] = {"part": part, "printers": [printer]}
        if ask.get("carries_load") is not None:
            fields["carries_load"] = "yes" if ask["carries_load"] in (True, "yes") else "no"
        fleet = ask.get("fleet")
        several = isinstance(fleet, (list, tuple)) and len(set(map(str, fleet))) > 1
        answer, miss = _ask(fields, several=several)
        if miss is not None:
            raise SpeedByHeightError(_refusal(miss), code=NO_PLAN)
        plan = answer["plans"][0]
        return {"ranges": plan.get("ranges") or [], "said": plan.get("said") or [], "ask": answer.get("ask")}

    return _plan


def _refusal(miss: Miss) -> str:
    from kiln import served_answer

    return served_answer.sentence(
        miss, feature="servers",
        on_the_line="Kiln chooses these speeds from the part's shape on its servers",
        cannot="choose them", wont="sliced nothing",
        safe_remedy="Slice without speed_by_height, or give your own ranges",
        # No note that a free account is enough: this answer is part of a paid plan.
        then="slice again",
    )


def _ask(fields: dict[str, Any], *, several: bool = False) -> tuple[dict[str, Any] | None, Miss | None]:
    local = _local(fields, several)
    if isinstance(local, Miss):
        return None, local
    if local is not _NOT_HERE:
        return _read(local)
    return _served(fields, several)


def _read(answer: Any) -> tuple[dict[str, Any] | None, Miss | None]:
    if isinstance(answer, dict) and answer.get("success") is True and answer.get("status") == "ok":
        plans = answer.get("plans")
        if isinstance(plans, list) and plans and isinstance(plans[0], dict):
            return answer, None
        return None, Miss("unanswered", detail="an answer with no plan in it")
    if isinstance(answer, dict) and answer.get("success") is False and answer.get("error"):
        return None, classify_answer(answer) or Miss("refused", str(answer.get("code") or ""), str(answer["error"]))
    return None, classify_answer(answer) or Miss("unanswered", detail="an answer that did not say it succeeded")


def _engine() -> Any:
    """kiln-pro's own answer on this computer, or ``None`` when kiln-pro is
    not installed here."""
    try:
        from kiln_pro.speed_plan.engine import answer_for_caller
    except ImportError:
        return None
    return answer_for_caller


def _local(fields: dict[str, Any], several: bool = False) -> Any:
    answer = _engine()
    if answer is None:
        return _NOT_HERE
    try:
        return answer(**fields, several=several)
    except Exception as exc:  # noqa: BLE001 -- a local fault is a miss to word, never a traceback
        return local_fault(exc, where="Kiln's speeds by height")


def _served(fields: dict[str, Any], several: bool = False) -> tuple[dict[str, Any] | None, Miss | None]:
    try:
        from kiln.server import _pro_api_call
    except Exception as exc:  # noqa: BLE001 -- no served door on this install is a miss
        return None, local_fault(exc, where="the served door on this computer")
    try:
        # The person's own ask: this runs only when a slice is asked for
        # with Kiln's speeds, which is what this fetches.
        answer = _pro_api_call(
            FLEET_WIRE_TOOL if several else WIRE_TOOL, _asked_by_user=True, _feature=SIGN_IN_NAME, **fields,
        )
    except Exception as exc:  # noqa: BLE001 -- the network is a miss to word, never a traceback
        logger.debug("speed plan not served", exc_info=True)
        return None, classify_transport_error(exc)
    return _read(answer)


__all__ = ["ASK", "FLEET_WIRE_TOOL", "LOCAL_MODULE", "NO_PLAN", "WIRE_TOOL", "planner", "read_ask", "sliced_facts", "with_context"]
