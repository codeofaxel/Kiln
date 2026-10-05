"""Which edges of a part get rounded or bevelled, at what size, and which stay sharp and why.

The kernel says what every edge IS (:func:`kiln.cad_edge.survey_step`); this
module turns a request into a plan the kernel can build.  No geometry kernel
here -- a plan is arithmetic on the survey, so it is tested on its own.

**Where an edge sits when the part prints** (Z up, the part resting on its
lowest point): ``vertical`` (runs up the part), ``bottom`` (lies on the bed),
``top`` (level, beside a face that looks up), ``under`` (level, beside a face
that looks down), ``sloped`` (anything else).  And which way the corner goes:
``outside`` or ``inside``.

**Sizes.**  A finish is literal -- the size asked -- unless the part has no
room for it.  Every limit comes from the part and the printer it is for
(:class:`PrintFrame`), none from a table:

* a face keeps one nozzle width of flat beside a finished edge, so an edge
  on a narrow face gets a smaller size, and none when what is left is below
  what the printer shows;
* a bevel stays inside the rounded corners it follows.

**Cautions.**  A finish whose new surface leans out past what the printer
bridges without support is still made, and listed under ``cautions`` with
how much of it overhangs -- on the bed, or facing down.

**Holes** stay as they are: a rim (where the hole meets the outside) is
finished only when the caller selects or names it, the floor of a blind hole
only when named.

A caller may hand in *choose*: a function that reads the classified edges and
answers, per chain, a different finish, a different size, or "leave it".  The
plan applies the same limits to whatever it answers.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

#: A face looks up (or down) when its normal is within this of straight up.
_FACING_DEG = 45.0
#: An edge is level, or on the bed, to this, mm: the survey's own rounding.
_LEVEL_MM = 1e-3
#: An edge runs up the part when it leans less than this from vertical.
_VERTICAL_DEG = 1.0
#: Steps across a rounded edge's new surface when asking how far down it faces.
_ARC_STEPS = 64

FILLET = "fillet"
CHAMFER = "chamfer"

_PLACES = ("vertical", "top", "bottom", "under", "sloped")
_CORNERS = ("outside", "inside")
#: The words a caller selects edges with, besides an edge's own id.
SELECTOR_WORDS = ("all", *_PLACES, *_CORNERS, "holes")


@dataclass(frozen=True)
class PrintFrame:
    """The printer a plan is sized for.

    :param nozzle_mm: Nozzle diameter.
    :param layer_mm: Layer height.
    :param overhang_deg: The steepest overhang that prints without support,
        in degrees from vertical.
    """

    nozzle_mm: float
    layer_mm: float
    overhang_deg: float

    @property
    def flat_kept_mm(self) -> float:
        """The flat a face keeps beside a finished edge: one nozzle width."""
        return self.nozzle_mm

    def smallest_shown_mm(self, place: str) -> float:
        """The smallest size that changes the printed part at an edge in *place*."""
        return self.nozzle_mm / 2.0 if place == "vertical" else self.layer_mm


@dataclass
class EdgePlan:
    """What will be done, and everything that will not be, with reasons."""

    #: One per chain, in the shape :func:`kiln.cad_edge.finish_step` takes,
    #: plus ``asked_mm``, ``place`` and ``corner``.
    treatments: list[dict[str, Any]] = field(default_factory=list)
    #: Selected edges that stay sharp: ``edges``, ``place``, ``corner``, ``reason``.
    left_sharp: list[dict[str, Any]] = field(default_factory=list)
    #: What prints badly: ``kind``, ``edges``, ``message``.
    cautions: list[dict[str, Any]] = field(default_factory=list)
    #: Real edges the selection did not ask for.
    not_selected: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "finished": [
                {"edges": [f"e{i}" for i in t["edges"]], "finish": t["kind"], "size_mm": t["size_mm"],
                 "asked_mm": t["asked_mm"], "place": t["place"], "corner": t["corner"],
                 **({"note": t["note"]} if t.get("note") else {})}
                for t in self.treatments
            ],
            "left_sharp": [
                {"edges": [f"e{i}" for i in s["edges"]], "place": s["place"], "corner": s["corner"], "reason": s["reason"]}
                for s in self.left_sharp
            ],
            "cautions": [
                {"kind": c["kind"], "edges": [f"e{i}" for i in c["edges"]], "message": c["message"]}
                for c in self.cautions
            ],
            "not_selected": self.not_selected,
        }


# ---------------------------------------------------------------------------
# Reading an edge
# ---------------------------------------------------------------------------


def place_of(edge: dict[str, Any], bed_z: float) -> str:
    """Where *edge* sits when the part prints; see the module docstring."""
    z_low, z_high = edge["z"]
    if abs(edge["tangent"][2]) >= math.cos(math.radians(_VERTICAL_DEG)) and edge["curve"] == "line":
        return "vertical"
    if z_high - z_low > _LEVEL_MM:
        return "sloped"
    if z_high - bed_z <= _LEVEL_MM:
        return "bottom"
    facing = math.cos(math.radians(_FACING_DEG))
    up_and_down = [n[2] for pair in edge["normals"] for n in pair]
    if min(up_and_down) <= -facing:
        return "under"
    if max(up_and_down) >= facing:
        return "top"
    return "sloped"


def _setback_per_mm(kind: str, turn_deg: float) -> float:
    """How far a finish of size 1 reaches across each face beside it.

    A bevel is measured along the faces, so it reaches its own size.  A
    round of radius r touches each face r x tan(turn / 2) from the edge: its
    own radius on a square corner, less on a shallow one.
    """
    if kind == CHAMFER:
        return 1.0
    return math.tan(math.radians(min(turn_deg, 179.0)) / 2.0)


def _sweep(n_a: list[float], n_b: list[float]) -> list[tuple[float, float, float]]:
    """Unit normals from one face's to the other's, both included: what a round's surface faces."""
    dot = max(-1.0, min(1.0, sum(a * b for a, b in zip(n_a, n_b, strict=True))))
    angle = math.acos(dot)
    if angle < 1e-9:
        return [tuple(n_a)]
    found = []
    for step in range(_ARC_STEPS + 1):
        s = step / _ARC_STEPS
        wa, wb = math.sin((1 - s) * angle) / math.sin(angle), math.sin(s * angle) / math.sin(angle)
        found.append(tuple(wa * a + wb * b for a, b in zip(n_a, n_b, strict=True)))
    return found


def _away_from_edge(n_here: list[float], n_other: list[float], corner: str) -> tuple[float, float, float]:
    """The direction across a face, straight away from the edge it shares with another.

    On an outside corner the face runs away behind its neighbour; on an
    inside corner it runs out in front of it.
    """
    dot = sum(a * b for a, b in zip(n_here, n_other, strict=True))
    across = [o - dot * h for h, o in zip(n_here, n_other, strict=True)]
    length = math.sqrt(sum(c * c for c in across)) or 1.0
    sign = -1.0 if corner == "outside" else 1.0
    return tuple(sign * c / length for c in across)


def hanging_band_mm(kind: str, size_mm: float, edge: dict[str, Any], frame: PrintFrame) -> float:
    """How tall a band of a finish's new surface leans out past what prints unsupported.

    Zero when none of it does.  A round's surface faces every direction
    between its two faces' normals, and a point on it sits ``radius x
    normal`` from the round's axis -- so the band that leans out too far is
    as tall as the radius times how much further down its normals reach than
    the limit: r x (1 - sin(limit)) for a round on a square corner on the
    bed.  A bevel is one flat; when it leans out too far, all of it does.
    """
    limit = math.sin(math.radians(frame.overhang_deg))
    worst = 0.0
    for n_a, n_b in edge["normals"]:
        if kind == FILLET:
            down = max(-n[2] for n in _sweep(n_a, n_b))
            worst = max(worst, size_mm * max(0.0, down - limit))
            continue
        flat = [a + b for a, b in zip(n_a, n_b, strict=True)]
        length = math.sqrt(sum(c * c for c in flat))
        if length == 0 or -flat[2] / length <= limit + 1e-9:
            continue
        rise = _away_from_edge(n_a, n_b, edge["corner"])[2] - _away_from_edge(n_b, n_a, edge["corner"])[2]
        worst = max(worst, size_mm * abs(rise))
    return worst


# ---------------------------------------------------------------------------
# Selecting
# ---------------------------------------------------------------------------


def parse_selector(edges: str | list[str] | None) -> tuple[set[str], set[int]]:
    """Split a selection into its words and its edge ids.

    ``"all"`` (or nothing) is every sharp edge; words of one kind widen
    (``"top,bottom"``), words of different kinds narrow (``"top,outside"``);
    ``"e12"`` names one edge.  Raises ``ValueError`` on a word it does not know.
    """
    if edges is None:
        return set(), set()
    parts = edges if isinstance(edges, list) else str(edges).replace(";", ",").split(",")
    words: set[str] = set()
    ids: set[int] = set()
    for raw in parts:
        word = str(raw).strip().lower()
        if not word or word == "all":
            continue
        if word[0] == "e" and word[1:].isdigit():
            ids.add(int(word[1:]))
        elif word in SELECTOR_WORDS:
            words.add(word)
        else:
            raise ValueError(
                f"'{raw}' is not an edge selection. Use {', '.join(SELECTOR_WORDS)}, or an edge id like e12."
            )
    return words, ids


def _matches(words: set[str], place: str, corner: str) -> bool:
    places = words & set(_PLACES)
    corners = words & set(_CORNERS)
    return (not places or place in places) and (not corners or corner in corners)


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------

#: What *choose* answers for a chain: ``(kind, size_mm, note)``, or
#: ``(None, 0, reason)`` to leave it sharp.
Choice = tuple[str | None, float, str]


def plan_edges(
    survey: dict[str, Any],
    *,
    kind: str,
    size_mm: float,
    frame: PrintFrame,
    edges: str | list[str] | None = None,
    min_turn_deg: float = 60.0,
    choose: Callable[[list[dict[str, Any]], PrintFrame], dict[int, Choice]] | None = None,
) -> EdgePlan:
    """Plan *kind* (``"fillet"`` or ``"chamfer"``) at *size_mm* over the selected edges of *survey*.

    *edges* is the selection (:func:`parse_selector`); *min_turn_deg* is how
    far the surface must turn across an edge for it to count as sharp.
    *choose*, when given, is handed the selected chains (each with ``chain``,
    ``edges``, ``place``, ``corner``, ``post_mm`` / ``hole_mm``, the ``kind``
    and size asked, and ``hangs_mm``: how tall a band of a round and of a
    bevel of that size would overhang) and the frame, and may answer a
    :data:`Choice` for any of them.  A hole rim nobody selected is handed
    over too, marked ``held``: it stays sharp unless the answer finishes it.
    Raises ``ValueError`` for a size that is not positive or a selection it
    cannot read.
    """
    if kind not in (FILLET, CHAMFER):
        raise ValueError(f"kind must be '{FILLET}' or '{CHAMFER}'")
    if not size_mm > 0:
        raise ValueError("the size must be above 0 mm")
    words, ids = parse_selector(edges)
    bed_z = survey["box"][2]
    by_id = {e["id"]: e for e in survey["edges"]}
    unknown = sorted(ids - set(by_id))
    if unknown:
        raise ValueError(f"this part has no edge {', '.join(f'e{i}' for i in unknown)}")

    plan = EdgePlan()
    for i in sorted(ids):
        if by_id[i]["corner"] == "smooth":
            plan.left_sharp.append({
                "edges": [i], "place": place_of(by_id[i], bed_z), "corner": "smooth",
                "reason": "the surface runs smoothly across it: there is no corner here to finish",
            })
    chains: dict[int, list[dict[str, Any]]] = {}
    for edge in survey["edges"]:
        if edge["corner"] != "smooth":
            chains.setdefault(edge["chain"], []).append(edge)

    selected: list[dict[str, Any]] = []
    for chain_id, members in sorted(chains.items()):
        named = any(e["id"] in ids for e in members)
        places = {place_of(e, bed_z) for e in members}
        place = places.pop() if len(places) == 1 else "sloped"
        corner = members[0]["corner"]
        hole = next((e["hole_mm"] for e in members if "hole_mm" in e), None)
        floor = any(e.get("hole_edge") == "floor" for e in members)
        entry = {
            "chain": chain_id, "edges": [e["id"] for e in members], "members": members,
            "place": place, "corner": corner, "hole_mm": hole,
            "post_mm": next((e["post_mm"] for e in members if "post_mm" in e), None),
            "asked_mm": size_mm, "kind": kind,
        }
        if not named:
            only_holes = words == {"holes"}
            mouth = hole is not None and not floor
            asked_by_word = (
                (words or not ids)
                and min(e["turn_deg"] for e in members) >= min_turn_deg
                and (mouth if only_holes else _matches(words, place, corner))
            )
            if not asked_by_word:
                plan.not_selected += len(members)
                continue
            if floor:
                # The blind end of a hole is part of the hole: only by name.
                entry["left"] = f"it is the floor of a {hole:g} mm hole; it is finished only when named"
                entry["held"] = "hole_floor"
            elif mouth and "holes" not in words:
                # Held: sharp unless a chooser answers for it.
                entry["left"] = f"it rims a {hole:g} mm hole; a hole's rim is finished only when named or selected with 'holes'"
                entry["held"] = "hole_rim"
        selected.append(entry)

    if choose is not None and selected:
        asked = [
            {
                **{k: v for k, v in s.items() if k not in ("members", "left")},
                # How tall a band of each finish would lean out past what
                # prints unsupported, at the size asked: the facts a choice
                # between them turns on.
                "hangs_mm": {
                    finish: max(hanging_band_mm(finish, s["asked_mm"], e, frame) for e in s["members"])
                    for finish in (FILLET, CHAMFER)
                },
            }
            for s in selected
        ]
        for chain_id, (new_kind, new_size, note) in (choose(asked, frame) or {}).items():
            for entry in selected:
                if entry["chain"] != chain_id:
                    continue
                if new_kind is None:
                    entry["left"] = note
                else:
                    entry.pop("left", None)
                    entry.update(kind=new_kind, asked_mm=float(new_size), note=note)

    for entry in [s for s in selected if "left" in s]:
        plan.left_sharp.append({**entry, "reason": entry["left"]})
    selected = [s for s in selected if "left" not in s]

    sizes = _fit_sizes(selected, frame)
    followed = _bevels_inside_rounds(selected, sizes, frame)
    for entry in selected:
        size, narrow = sizes[entry["chain"]]
        shown = frame.smallest_shown_mm(entry["place"])
        corner_mm = followed.get(entry["chain"])
        if corner_mm is not None:
            why_smaller = (
                f"it follows corners rounded at {corner_mm:g} mm, and a bevel has to stay inside the corner it follows "
                f"by the {frame.smallest_shown_mm('vertical'):g} mm this nozzle turns in"
            )
        else:
            why_smaller = f"the face beside it is {narrow:g} mm wide and keeps one {frame.nozzle_mm:g} mm nozzle width of flat"
        if size < entry["asked_mm"] and size < shown:
            plan.left_sharp.append({
                **entry,
                "reason": (
                    f"{why_smaller}, which leaves room for {max(size, 0.0):.2f} mm; "
                    f"less than {shown:g} mm does not show in the print"
                ),
            })
            continue
        treatment = {
            "edges": entry["edges"], "mids": [e["mid"] for e in entry["members"]],
            "kind": entry["kind"], "size_mm": round(size, 4), "asked_mm": entry["asked_mm"],
            "place": entry["place"], "corner": entry["corner"],
        }
        notes = [entry["note"]] if entry.get("note") else []
        if size < entry["asked_mm"]:
            notes.append(f"{size:.2f} mm, not the {entry['asked_mm']:g} asked: {why_smaller}")
        if notes:
            treatment["note"] = "; ".join(notes)
        plan.treatments.append(treatment)
        _caution(plan, entry, size, frame)
    return plan


def _fit_sizes(selected: list[dict[str, Any]], frame: PrintFrame) -> dict[int, tuple[float, float]]:
    """Chain id -> (the size it gets, the narrowest face that set it).

    Each face beside an edge has ``room`` to its next boundary.  The finish
    may reach across it until one nozzle width of flat is left -- and when
    the edge across that room is being finished too, the two share it.  A
    first pass gives every chain half of any shared room; a second, in chain
    order, hands each the part of the room its neighbour did not use.
    """
    chain_of_edge = {i: s["chain"] for s in selected for i in s["edges"]}
    kind_of = {s["chain"]: s["kind"] for s in selected}
    turn_of = {e["id"]: e["turn_deg"] for s in selected for e in s["members"]}

    def reach(edge_id: int, size: float) -> float:
        return size * _setback_per_mm(kind_of[chain_of_edge[edge_id]], turn_of[edge_id])

    def limit(entry: dict[str, Any], current: dict[int, float] | None) -> tuple[float, float]:
        best, narrow = entry["asked_mm"], math.inf
        for edge in entry["members"]:
            per_mm = _setback_per_mm(entry["kind"], edge["turn_deg"])
            for room, across in zip(edge["room_mm"], edge["across"], strict=True):
                free = room - frame.flat_kept_mm
                other = chain_of_edge.get(across) if across is not None else None
                if other is not None and other == entry["chain"]:
                    free /= 2.0  # both sides of this face belong to this chain
                elif other is not None:
                    free = free / 2.0 if current is None else free - reach(across, current[other])
                allowed = free / per_mm if per_mm > 0 else math.inf
                if allowed < best:
                    best, narrow = allowed, room
        return best, narrow

    first = {s["chain"]: limit(s, None) for s in selected}
    current = {c: max(size, 0.0) for c, (size, _) in first.items()}
    final: dict[int, tuple[float, float]] = {}
    for entry in sorted(selected, key=lambda s: s["chain"]):
        size, narrow = limit(entry, current)
        # Never below the first pass: the neighbour was held to its half too.
        size = max(size, first[entry["chain"]][0])
        current[entry["chain"]] = max(size, 0.0)
        final[entry["chain"]] = (size, narrow if narrow < math.inf else first[entry["chain"]][1])
    return final


def _bevels_inside_rounds(
    selected: list[dict[str, Any]], sizes: dict[int, tuple[float, float]], frame: PrintFrame,
) -> dict[int, float]:
    """Hold each bevel inside the rounded corners it runs through; chain id -> that corner's radius.

    A bevel along an outline follows it around every rounded corner, and
    moves the outline in by its own size -- so at a corner rounded at r the
    new outline turns at r minus the bevel.  At zero the corner collapses and
    the kernel cannot build it (measured: a 1.5 mm bevel around 1.5 mm
    corners fails on every part tried, 1.4 builds).  So a bevel stays smaller
    than the tightest round it meets by the smallest radius the nozzle turns
    in.  *sizes* is updated in place; the answer names the chains it held
    back.
    """
    rounds_at: dict[int, float] = {}
    for entry in selected:
        if entry["kind"] != FILLET:
            continue
        radius = sizes[entry["chain"]][0]
        for edge in entry["members"]:
            for corner in edge["ends"]:
                rounds_at[corner] = min(radius, rounds_at.get(corner, math.inf))
    held: dict[int, float] = {}
    for entry in selected:
        if entry["kind"] != CHAMFER:
            continue
        met = [rounds_at[c] for edge in entry["members"] for c in edge["ends"] if c in rounds_at]
        if not met:
            continue
        size, narrow = sizes[entry["chain"]]
        room = min(met) - frame.smallest_shown_mm("vertical")
        if size > room:
            sizes[entry["chain"]] = (room, narrow)
            held[entry["chain"]] = min(met)
    return held


def _caution(plan: EdgePlan, entry: dict[str, Any], size: float, frame: PrintFrame) -> None:
    """Add what will print badly about this finish, if anything will."""
    kind, place = entry["kind"], entry["place"]
    shown = frame.smallest_shown_mm(place)
    if size < shown:
        plan.cautions.append({
            "kind": "too_small_to_show", "edges": entry["edges"],
            "message": (
                f"A {size:g} mm {'round' if kind == FILLET else 'bevel'} on a {place} edge is below what this printer shows "
                f"({shown:g} mm with a {frame.nozzle_mm:g} mm nozzle at {frame.layer_mm:g} mm layers): it is in the file and will not be in the print."
            ),
        })
    band = max(hanging_band_mm(kind, size, edge, frame) for edge in entry["members"])
    if band < frame.layer_mm:
        return
    what = "round" if kind == FILLET else "bevel"
    if place == "bottom":
        plan.cautions.append({
            "kind": "on_the_bed", "edges": entry["edges"],
            "message": (
                f"This {what} is on the bed. Its lowest {band:.2f} mm leans out more than {frame.overhang_deg:g} degrees, "
                f"so those layers print over air and droop."
            ),
        })
    else:
        plan.cautions.append({
            "kind": "overhang", "edges": entry["edges"],
            "message": (
                f"This {what} faces down: {band:.2f} mm of it leans out more than {frame.overhang_deg:g} degrees "
                "and needs support under it to print cleanly."
            ),
        })


__all__ = [
    "CHAMFER",
    "FILLET",
    "SELECTOR_WORDS",
    "Choice",
    "EdgePlan",
    "PrintFrame",
    "hanging_band_mm",
    "parse_selector",
    "place_of",
    "plan_edges",
]
