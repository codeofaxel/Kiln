"""Help Kiln get to know your printer: what a guided session observes, and where it goes.

Kiln judges where a second part may go on an occupied plate from a record
of how each printer model moves its head on its own -- where it parks when
it pauses, what it does when a print is cancelled or ends.  A record with
a BLANK (a block nobody has described, a head nobody has measured) is
judged at the worst case, which is honest and often a dead end.  The
guided session (``printer_bench``, :mod:`kiln.plugins.printer_bench_tools`)
fills the blanks for THIS unit: it prints a coin-sized square, pauses,
resumes, cancels and ends it, and watches where the head goes.

This module is everything under that tool that is not the conversation:

* the OBSERVATION document -- one per block, numbers only, never a photo
  (a frame is read by eyes on this machine; only the reading travels);
* where it lives -- ``~/.kiln/bench/<unit>.json`` -- and how it rides
  every placement request for this unit (``printer_observations``), so the
  served verdict uses it at once, the way it uses a Klipper printer's own
  settings;
* the zone drawing an owner of a closed-firmware printer answers with:
  nine numbered cells on the plate and a ring of sixteen around it,
  because an A1 parks LEFT of its plate over the chute and many printers
  park off the plate -- drawn here, and resolved to the same rectangle on
  the served side (pinned to each other by test);
* the head-measuring sketch for the two head measurements;
* the position log a Klipper or Marlin printer gives instead of any ask;
* what the record still lacks for a printer (``blanks_for``), read from
  the verdict itself so the session asks only about blanks;
* the one offer every door makes (:func:`offer`): only where a lesson can
  teach what stopped the person, worded as what they gain, never as what
  Kiln lacks, and kept in a ledger so nobody is nagged;
* the lesson funnel's counts (:func:`count`), under the one telemetry
  switch, with no printer model in them; and
* the send: one validated RPC into ``printer_motion_observations``, under
  the one telemetry switch, after the heartbeat and at the session's end.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_logger = logging.getLogger(__name__)

#: The observation document the served door reads.
OBSERVATION_FORMAT = "printer_motion_observation/1"
#: The blocks a session can observe, in the order it asks.
BLOCKS = ("pause", "cancel", "end", "filament_change", "head")
#: The blocks a record can have a blank for.
RECORD_BLOCKS = ("pause", "cancel", "end", "filament_change")
#: The blanks a short session can fill.  A colour change is never
#: triggered (no printer offers a way to without unloading by hand), and the
#: Z travel and the quiet start are not things a test print can show.
TEACHABLE = ("pause", "cancel", "end", "head")
#: What a printer does in each teachable block, as the end of "when it ...".
_BLOCK_VERBS = {"pause": "pauses", "cancel": "cancels a print", "end": "finishes a print"}
#: The four ways an observation is made.
HOWS = ("position_log", "camera", "owner_zone", "owner_measure")
#: What each lift word means as a LOWER bound in millimetres: the verdict
#: reads the head no higher than this, whatever the real lift was.
LIFT_WORDS_MM = {"barely": 0.5, "finger": 10.0, "hand": 100.0}
#: Zones on the plate (a 3 x 3 grid, 1-9) and the ring around it (10-25).
PLATE_ZONES = 9
RING_ZONES = 16
#: What an agent is told about the session.  One home, read by the connect
#: instructions, ``get_started``, the skill manifest and the tool's own
#: description; SKILL.md carries :data:`AGENT_LEARN_LINE` verbatim.
LEARN_LINE = (
    "Kiln can get to know a printer: `printer_bench` is a short guided session "
    "that teaches Kiln what it does not yet know about how a printer moves, for "
    "a printer new to Kiln and for one it knows only in part. What it learns is "
    "used for that printer at once. A second lesson, a small test print the "
    "person reads a few numbers off, teaches Kiln how steep, how far and how "
    "thin that printer prints."
)

#: The agent half: when to reach for it, and how to carry it.
LEARN_WHEN = (
    "Offer it once per printer, and never before the printer's first print: "
    "when a reply carries an offer of it, say that offer as written, and offer "
    "it when the person asks why Kiln cannot do something on their printer or "
    "wants to teach Kiln their printer. "
    "`printer_bench()` with no answer starts or resumes it, or says there is "
    "nothing to teach; `printer_bench(lesson=\"limits\")` starts the second "
    "lesson on its own. Relay each ask word for word, show its pictures, and "
    "take 'later' as an answer."
)

AGENT_LEARN_LINE = LEARN_LINE + " " + LEARN_WHEN

#: The RPC the documents go through, and its posture: best effort.
_RPC = "record_printer_motion_observation"
_TIMEOUT_S = 5
#: The position log's cadence and patience.
LOG_HZ = 10.0
LOG_SETTLE_S = 4.0
LOG_MAX_S = 120.0

__all__ = [
    "BLOCKS", "FOOTPRINT_MARGIN_MM", "HOWS", "LESSON_TEACHES", "LIFT_WORDS_MM", "LIMITS", "MOTION",
    "OBSERVATION_FORMAT", "OFFER_DOORS", "OFFER_QUIET_S", "PUSH_MM", "PositionLog", "RECORD_BLOCKS", "RISE_MM",
    "TEACHABLE", "bed_of", "bench_dir", "blanks_for", "block_start", "can_log_positions", "count",
    "draw_head_sketch", "draw_zones", "ended_block", "fingerprint_of", "firmware_of", "gain_sentence",
    "head_observation", "hold_kind", "keep_observation", "lesson_for", "logged_block", "logs_positions", "may_offer",
    "note_learned", "note_offer", "observation", "observations_for_request", "observations_of", "offer",
    "offer_after_first_print", "offer_after_registration", "offer_fields", "offer_for", "parse_lift", "parse_zone",
    "position_of", "position_reading", "print_footprint", "refusal_blanks", "send_after_heartbeat", "send_pending",
    "sharing", "take_news", "unit_from_machine", "unit_of", "way_back", "zone_of", "zone_rect",
]


# ---------------------------------------------------------------------------
# The zone drawing, as numbers (the served side resolves the same rectangles)
# ---------------------------------------------------------------------------


def zone_rect(zone: int, bed_mm: tuple[float, float]) -> tuple[float, float, float, float]:
    """The rectangle zone *zone* covers, in bed millimetres.

    Zones 1-9 tile the plate three by three, numbered like a keypad seen
    from the printer's front: 1 is back-left, 3 back-right, 7 front-left,
    9 front-right.  Zones 10-25 ring the plate one cell deep -- each ring
    cell as wide as a plate cell -- clockwise from the back-left corner
    (10), along the back edge (11-13) to the back-right corner (14), down
    the right side (15-17) to the front-right corner (18), along the front
    (19-21) to the front-left corner (22) and up the left side (23-25).
    """
    bx, by = float(bed_mm[0]), float(bed_mm[1])
    cx, cy = bx / 3.0, by / 3.0
    if 1 <= zone <= PLATE_ZONES:
        col, row = (zone - 1) % 3, (zone - 1) // 3          # row 0 is the back
        x0 = col * cx
        y1 = by - row * cy
        return (x0, y1 - cy, x0 + cx, y1)
    if PLATE_ZONES < zone <= PLATE_ZONES + RING_ZONES:
        ring = [(c, 0) for c in range(5)] + [(4, r) for r in range(1, 5)] + \
               [(c, 4) for c in range(3, -1, -1)] + [(0, r) for r in range(3, 0, -1)]
        col, row = ring[zone - PLATE_ZONES - 1]
        x0 = (col - 1) * cx
        y1 = by - (row - 1) * cy
        return (x0, y1 - cy, x0 + cx, y1)
    raise ValueError(f"zone must be 1-{PLATE_ZONES + RING_ZONES}, not {zone}")


def zone_of(x: float, y: float, bed_mm: tuple[float, float]) -> int | None:
    """Which zone (x, y) falls in, or ``None`` beyond the ring."""
    for zone in range(1, PLATE_ZONES + RING_ZONES + 1):
        x0, y0, x1, y1 = zone_rect(zone, bed_mm)
        if x0 <= x < x1 and y0 <= y < y1:
            return zone
    return None


def parse_zone(answer: Any) -> int | None:
    """The zone number in an owner's answer ("zone 24", "24", "it's in 24")."""
    import re

    text = str(answer or "").strip().lower()
    found = re.findall(r"\b(\d{1,2})\b", text)
    for token in found:
        n = int(token)
        if 1 <= n <= PLATE_ZONES + RING_ZONES:
            return n
    return None


def parse_lift(answer: Any) -> float | None:
    """How high the head lifted, from a word or a number in the answer, as
    a LOWER bound in millimetres; ``None`` when the answer says nothing."""
    import re

    text = str(answer or "").strip().lower()
    if not text:
        return None
    number = re.search(r"(-?\d+(?:\.\d+)?)\s*(mm|cm)?", text)
    if number:
        value = float(number.group(1))
        if number.group(2) == "cm":
            value *= 10.0
        return max(0.0, value)
    for word, mm in LIFT_WORDS_MM.items():
        if word in text:
            return mm
    if "span" in text or "palm" in text:
        return LIFT_WORDS_MM["hand"]
    if "not" in text or "didn't" in text or text == "no":
        return 0.0
    return None


# ---------------------------------------------------------------------------
# Who this is about
# ---------------------------------------------------------------------------


def bench_dir() -> Path:
    """``~/.kiln/bench`` (``KILN_HOME`` honoured).  Named, not created."""
    from kiln.startup_failure import kiln_home

    return kiln_home() / "bench"


def unit_from_machine(machine: str | None) -> str | None:
    """A one-way hash of a machine's durable identity (the plate record's
    key), so one printer counts once and nothing that names it leaves the
    machine."""
    if not machine:
        return None
    return hashlib.sha256(str(machine).encode("utf-8")).hexdigest()[:32]


def unit_of(adapter: Any) -> str | None:
    """:func:`unit_from_machine` for *adapter*'s machine."""
    from kiln.plate_state import machine_id

    try:
        return unit_from_machine(machine_id(adapter))
    except Exception:  # noqa: BLE001
        return None


def firmware_of(adapter: Any) -> str | None:
    """The firmware version the printer reports, as a short label, or ``None``."""
    import re

    try:
        reader = getattr(adapter, "reported_firmware_version", None)
        version = reader() if callable(reader) else None
    except Exception:  # noqa: BLE001
        return None
    text = re.sub(r"[^A-Za-z0-9._-]", "", str(version or "")).strip()[:40]
    return text or None


def bed_of(printer_id: str | None) -> tuple[float, float] | None:
    """The catalogue's plate size for *printer_id*, or ``None``."""
    if not printer_id:
        return None
    try:
        from kiln.printers.bed_fit import get_build_volume

        volume = get_build_volume(printer_id)
    except Exception:  # noqa: BLE001
        return None
    if not volume:
        return None
    return (float(volume[0]), float(volume[1]))


# ---------------------------------------------------------------------------
# The observation document
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def observation(
    model: str, block: str, how: str, *, bed_mm: tuple[float, float], layer_z_mm: float,
    points: list[tuple[float, float, float]] | None = None, zones: list[int] | None = None,
    lift_mm: float | None = None, returns: bool = False, return_how: str | None = None,
    unit: str | None = None, firmware: str | None = None, when: str | None = None,
) -> dict[str, Any]:
    """One block's observation, in the shape the served door reads."""
    if block not in RECORD_BLOCKS:
        raise ValueError(f"block must be one of {RECORD_BLOCKS}")
    if how not in HOWS:
        raise ValueError(f"how must be one of {HOWS}")
    doc: dict[str, Any] = {
        "format": OBSERVATION_FORMAT, "model": str(model).strip().lower()[:80], "block": block, "how": how,
        "firmware": firmware, "unit": unit, "when": when or _now(),
        "bed_mm": [float(bed_mm[0]), float(bed_mm[1])], "layer_z_mm": round(float(layer_z_mm), 3),
        "returns": bool(returns), "return_how": return_how,
    }
    if how in ("position_log", "owner_measure"):
        if not points:
            raise ValueError("a logged or measured observation needs points")
        doc["points"] = [[round(float(x), 2), round(float(y), 2), round(float(z), 3)] for x, y, z in points]
        doc["zones"] = None
        doc["lift_mm"] = None
    else:
        if not zones:
            raise ValueError("a camera or zone observation needs zones")
        doc["points"] = None
        doc["zones"] = [int(z) for z in zones]
        doc["lift_mm"] = None if lift_mm is None else round(float(lift_mm), 2)
    return doc


def head_observation(
    model: str, width_mm: float, rod_height_mm: float | None, *, unit: str | None = None,
    firmware: str | None = None, when: str | None = None,
) -> dict[str, Any]:
    """The two head figures, in the shape the served door reads."""
    return {
        "format": OBSERVATION_FORMAT, "model": str(model).strip().lower()[:80], "block": "head", "how": "owner_measure",
        "firmware": firmware, "unit": unit, "when": when or _now(),
        "head_mm": {"width": round(float(width_mm), 2),
                    "rod_height": None if rod_height_mm is None else round(float(rod_height_mm), 2)},
    }


def fingerprint_of(doc: dict[str, Any]) -> str:
    """One fingerprint per distinct document, the way the service names
    it: the document without the fields that name a unit or a moment."""
    body = {k: v for k, v in doc.items() if k not in ("unit", "when", "firmware", "share")}
    text = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Where it lives: ~/.kiln/bench/<unit>.json
# ---------------------------------------------------------------------------


def _store_path(unit: str) -> Path:
    return bench_dir() / f"{unit}.json"


def _load_store(unit: str) -> dict[str, Any]:
    path = _store_path(unit)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("observations"), list):
            return data
    except (OSError, ValueError):
        pass
    return {"unit": unit, "observations": [], "sent": {}}


def _save_store(unit: str, store: dict[str, Any]) -> Path:
    path = _store_path(unit)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(store, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)
    return path


def keep_observation(adapter: Any, doc: dict[str, Any], *, seen: str = "session") -> Path | None:
    """Write *doc* for *adapter*'s unit, replacing an earlier observation
    of the same block: the newest look is the one that stands -- except
    that one taken in real use (*seen* ``"real_use"``, :mod:`kiln.head_moves`)
    never replaces what the owner taught Kiln in a guided session.
    ``None`` when nothing was written."""
    unit = unit_of(adapter)
    if not unit:
        return None
    store = _load_store(unit)
    block = doc.get("block")
    seen_by = store.get("seen") if isinstance(store.get("seen"), dict) else {}
    held = any(d.get("block") == block for d in store["observations"] if isinstance(d, dict))
    if seen == "real_use" and held and seen_by.get(block, "session") != "real_use":
        return None
    store["model"] = doc.get("model") or store.get("model")
    store["observations"] = [d for d in store["observations"] if d.get("block") != block] + [doc]
    store["seen"] = {**seen_by, block: seen}
    return _save_store(unit, store)


def observations_of(adapter: Any) -> list[dict[str, Any]]:
    """Every observation on file for *adapter*'s unit, as written."""
    unit = unit_of(adapter)
    if not unit:
        return []
    return [d for d in _load_store(unit)["observations"] if isinstance(d, dict)]


def observations_for_request(adapter: Any) -> list[dict[str, Any]] | None:
    """What a placement request for *adapter* carries: this unit's
    observations, so its own verdict uses them at once.  While this
    install does not share (:func:`sharing`) the documents still travel
    for the verdict but carry nothing that names the unit and ask not to
    be kept (``share: false``); the switches cover what Kiln learns from,
    never what Kiln answers."""
    docs = observations_of(adapter)
    if not docs:
        return None
    if sharing():
        return docs
    return [{**d, "unit": None, "firmware": None, "share": False} for d in docs]


# ---------------------------------------------------------------------------
# What the record still lacks
# ---------------------------------------------------------------------------

#: The sentence the engine writes for a block judged at the worst case.
_WORST_CASE = "is not on record, so the worst case is assumed"


def refusal_blanks(verdict: Any) -> list[str]:
    """The blank blocks a verdict's refusals rest on -- named by the
    record line when the server writes one, else read from the sentences
    the engine writes for a block nobody has described."""
    if not isinstance(verdict, dict):
        return []
    record = verdict.get("record")
    blanks = [b for b in (record.get("blanks") or []) if isinstance(b, str)] if isinstance(record, dict) else []
    out: list[str] = []
    for refusal in verdict.get("refusals") or []:
        sentence = str(refusal.get("sentence") or "") if isinstance(refusal, dict) else ""
        for block in (b for b in TEACHABLE if b != "head"):
            said = block.replace("_", " ")
            if ((said in sentence or block in sentence) and (_WORST_CASE in sentence or block in blanks)
                    and block not in out):
                out.append(block)
    if not out and any(r.get("code") == "PLACEMENT_UNKNOWN_PRINTER" for r in verdict.get("refusals") or [] if isinstance(r, dict)):
        return list(TEACHABLE)
    return out


def blanks_for(adapter: Any, printer_id: str | None) -> tuple[list[str], dict[str, Any] | None]:
    """What a session could still teach about *printer_id*, asked of the
    placement service as a session: a probe request on a clear plate, with
    this unit's own settings and observations riding it, so a blank the
    machine or its owner already filled does not count.  ``(blanks,
    verdict)``; an unknown printer is every blank; no verdict at all is
    ``([], None)`` -- offline is not a reason to nag.  ``quiet_start`` is
    added when the record says a start beside a part would not stand, since
    the payoff line reads it."""
    try:
        from kiln import _pro_placement_bridge as bridge

        part = {"size_mm": [20.0, 20.0, 2.0], "layer_height_mm": 0.2, "tower_mm": None, "colour_changes_at_mm": [],
                "skirt_mm": 2.0}
        request = bridge.request_for(adapter, printer_id, placement="auto", part=part)
        request["plate"] = {"status": "clear", "job": None, "jobs": [], "fingerprint": "", "since": None}
        request["occupant_gcode"] = None
        request["session"] = True
        verdict, _miss = bridge.ask(request)
    except Exception:  # noqa: BLE001
        return [], None
    if not isinstance(verdict, dict):
        return [], None
    refusals = verdict.get("refusals") or []
    if any(isinstance(r, dict) and r.get("code") == "PLACEMENT_UNKNOWN_PRINTER" for r in refusals):
        return list(RECORD_BLOCKS) + ["head"], verdict
    record = verdict.get("record")
    if not isinstance(record, dict):
        return [], verdict
    teach = record.get("teach")
    if isinstance(teach, list):
        blanks = [b for b in teach if isinstance(b, str)]
        if record.get("quiet_start") is not True:
            blanks.append("quiet_start")
        return blanks, verdict
    # A service that answers in the older words.
    return [b for b in (record.get("blanks") or []) if isinstance(b, str)], verdict


def session_hold(verdict: Any) -> dict[str, Any] | None:
    """Why the service will not start a session for this printer yet, as it
    said it (a ``code``, one ``sentence`` to say as written, and what to do
    next), or ``None``.  A held session is not a finished one: nothing is
    offered and nothing is marked done until it can start."""
    record = verdict.get("record") if isinstance(verdict, dict) else None
    hold = record.get("teach_hold") if isinstance(record, dict) else None
    if isinstance(hold, dict) and hold.get("code") and hold.get("sentence"):
        return hold
    return None


def _joined(words: list[str], last: str = "or") -> str:
    """``a``, ``a or b``, ``a, b or c``."""
    if len(words) <= 1:
        return "".join(words)
    return ", ".join(words[:-1]) + f" {last} " + words[-1]


#: The two lessons, as the session and the counters name them.
MOTION = "motion"
LIMITS = "limits"
#: The gaps each lesson can teach, in the words a door names a gap with.  A
#: gap no lesson can teach is absent, so a door that meets it offers nothing:
#: a home, a park, a wipe or a purge is a sequence for the printer to run,
#: which no test print shows.
LESSON_TEACHES: dict[str, tuple[str, ...]] = {MOTION: TEACHABLE, LIMITS: ("limits",)}
#: Every door that may offer a lesson unasked.  ``first_print`` is the moment
#: after a printer's first print has taught what it can; ``lesson`` is the
#: session's own offer of the second lesson once the first is done.
OFFER_DOORS = ("registration", "first_print", "print_beside", "setup_check", "motion_refusal", "lesson")
#: The doors that offer once, ever.
_ONCE_DOORS = ("registration", "first_print")
#: A door that meets a gap while the person is trying something offers at
#: most once in this long; an answer of any kind ends its offers.
OFFER_QUIET_S = 7 * 24 * 3600
#: How each block the first lesson teaches is named in its offer.
_OFFER_VERBS = {"pause": "pauses", "cancel": "cancels", "end": "finishes"}
#: What the first lesson buys, by the caller's plan: whether a second part
#: fits on every plan, where it fits on Pro and up.
_FITS_FREE = "tell you whether a second part fits beside one already on the plate"
_FITS_PRO = "find where a second part fits beside one already on the plate"


def lesson_for(gaps: Any) -> str | None:
    """The lesson that can teach any of *gaps*, or ``None`` when none can."""
    words = [str(g) for g in (gaps or []) if isinstance(g, str)]
    for lesson, taught in LESSON_TEACHES.items():
        if any(g in taught for g in words):
            return lesson
    return None


def has_pro() -> bool:
    """Whether the caller's plan is Pro or above, read the caller-aware way
    (:func:`kiln.licensing.check_tier`); an unreadable plan is Free."""
    try:
        from kiln.licensing import LicenseTier, check_tier

        allowed, _ = check_tier(LicenseTier.PRO)
        return bool(allowed)
    except Exception:  # noqa: BLE001 -- an unreadable plan is Free
        return False


def gain_sentence(lesson: str, gaps: Any, model_name: str, *, pro: bool | None = None) -> str:
    """The offer, as what the person gains for this printer: the blocks still
    missing that the lesson teaches, named and nothing else, and what knowing
    them buys on the caller's plan (*pro*, read with :func:`has_pro` when not
    given)."""
    if lesson == LIMITS:
        return (f"Kiln can learn how steep, how far and how thin your {model_name} really prints from one small "
                "test print; the printer_bench tool runs it.")
    words = [g for g in (gaps or []) if isinstance(g, str)]
    verbs = [_OFFER_VERBS[b] for b in ("pause", "cancel", "end") if b in words]
    fits = _FITS_PRO if (has_pro() if pro is None else pro) else _FITS_FREE
    if not verbs:
        return f"Two measurements with calipers or a ruler teach Kiln how big your {model_name}'s head is, so it can {fits}."
    what = f"how your {model_name} {_joined(verbs, 'and')}"
    if "head" in words:
        what += ", and how big its head is"
    return f"A few minutes with a coin-sized test print teaches Kiln {what}, so it can {fits}."


def _offer_key(unit: str | None, lesson: str) -> str | None:
    """The offers ledger's line for *lesson* on *unit*."""
    if not unit:
        return None
    return unit if lesson == MOTION else f"{lesson}:{unit}"


def offer(unit: str | None, gaps: Any, door: str, *, model_name: str,
          printer_name: str | None = None) -> dict[str, Any] | None:
    """The one place a door decides whether to offer a lesson, and words it.

    *gaps* are what stopped the person (or, at registration, what a lesson
    could teach); a gap no lesson can teach offers nothing.  The ledger
    decides the rest (:func:`may_offer`).  An offer made is written down and
    counted; ``None`` otherwise.  Never raises."""
    try:
        lesson = lesson_for(gaps)
        if lesson is None or door not in OFFER_DOORS:
            return None
        key = _offer_key(unit, lesson)
        if not may_offer(key, door):
            return None
        note_offer(key, door=door)
        count("offered", lesson, door)
        taught = [g for g in gaps if isinstance(g, str) and g in LESSON_TEACHES[lesson]]
        out: dict[str, Any] = {"tool": "printer_bench", "lesson": lesson,
                               "sentence": gain_sentence(lesson, taught, model_name)}
        if printer_name:
            out["printer_name"] = printer_name
        return out
    except Exception:  # noqa: BLE001 -- an offer is a courtesy; the door's answer stands without it
        _logger.debug("bench: offer skipped", exc_info=True)
        return None


def offer_for(adapter: Any, gaps: Any, door: str, *, printer_name: str | None = None) -> dict[str, Any] | None:
    """:func:`offer` for the printer behind *adapter*, named the catalogue's way."""
    try:
        if lesson_for(gaps) is None:
            return None
        from kiln.plate_state import declared_model_of

        printer_id = declared_model_of(adapter)
        name = _display_name(printer_id) if printer_id else "printer"
        return offer(unit_of(adapter), gaps, door, model_name=name, printer_name=printer_name)
    except Exception:  # noqa: BLE001
        _logger.debug("bench: offer skipped", exc_info=True)
        return None


def offer_fields(adapter: Any, gaps: Any, door: str) -> dict[str, Any]:
    """``{"learn": <offer>}`` for a refusal envelope, or ``{}``."""
    found = offer_for(adapter, gaps, door)
    return {"learn": found} if found else {}


# ---------------------------------------------------------------------------
# The funnel: counts only, under the one telemetry switch
# ---------------------------------------------------------------------------


def hold_kind(code: Any) -> str:
    """What kind of wait a hold's code names: a sign-in, an allowance, or another."""
    text = str(code or "").upper()
    if "ACCOUNT" in text or "SIGNIN" in text or "SIGN_IN" in text:
        return "signin"
    if "LIMIT" in text:
        return "limit"
    return "other"


def count(stage: str, lesson: str, detail: str | None = None) -> None:
    """Count one step of the lesson funnel for today
    (:func:`kiln.daily_stats.record_bench_funnel`).  Never raises."""
    try:
        from kiln.daily_stats import record_bench_funnel

        record_bench_funnel(stage, lesson, detail)
    except Exception:  # noqa: BLE001 -- a counter never touches a lesson
        _logger.debug("bench: funnel not counted", exc_info=True)


# ---------------------------------------------------------------------------
# Never nagged twice
# ---------------------------------------------------------------------------


def _offers_path() -> Path:
    return bench_dir() / "offers.json"


def _load_offers() -> dict[str, Any]:
    try:
        data = json.loads(_offers_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def note_offer(unit: str | None, *, answer: str | None = None, door: str | None = None) -> None:
    """Record that a lesson was offered to *unit* (through *door* when one
    offered it), and the answer when there is one (``later``, ``declined``,
    ``done``)."""
    if not unit:
        return
    offers = _load_offers()
    entry = dict(offers.get(unit) or {})
    entry.setdefault("offered_at", _now())
    if door:
        doors = dict(entry.get("doors") or {}) if isinstance(entry.get("doors"), dict) else {}
        doors[door] = _now()
        entry["doors"] = doors
    if answer:
        entry["answer"] = answer
        entry["answered_at"] = _now()
    offers[unit] = entry
    path = _offers_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(offers, indent=2, sort_keys=True), encoding="utf-8")
    except OSError:
        _logger.debug("bench: offers ledger not written", exc_info=True)


def _age_s(stamp: Any) -> float | None:
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(str(stamp))).total_seconds()
    except (TypeError, ValueError):
        return None


def may_offer(unit: str | None, door: str) -> bool:
    """Whether a lesson may be offered to *unit* (an offers-ledger line)
    through *door*.

    Any answer ends every unasked offer: ``later`` and ``declined`` are the
    person's, ``done`` is a lesson run or one with nothing to teach.
    ``registration`` and ``first_print`` offer once, ever.  ``lesson`` is
    the session's own offer of its second lesson to someone already in a
    session.  Every
    other door meets the gap while the person is trying something, and
    offers there at most once in :data:`OFFER_QUIET_S`."""
    if not unit:
        return False
    entry = _load_offers().get(unit) or {}
    if entry.get("answer") in ("later", "declined", "done"):
        return False
    if door in _ONCE_DOORS:
        return not entry
    if door == "lesson":
        return True
    doors = entry.get("doors") if isinstance(entry.get("doors"), dict) else {}
    age = _age_s(doors.get(door)) if doors.get(door) else None
    return age is None or age >= OFFER_QUIET_S


# ---------------------------------------------------------------------------
# What a real print taught, said once
# ---------------------------------------------------------------------------

#: What a printer does in each block, as the end of "how your <printer> ...".
_LEARNED_VERBS = {"pause": "pauses a print", "cancel": "cancels a print", "end": "ends a print"}


def _learned_path() -> Path:
    return bench_dir() / "learned.json"


def note_learned(adapter: Any, printer_id: str | None, block: str) -> None:
    """A real print taught Kiln *block* on this printer: counted, and kept
    to be said once on the person's next reply (:func:`take_news`).
    Never raises."""
    if block not in _LEARNED_VERBS:
        return
    count("learned", MOTION, "real_print")
    try:
        unit = unit_of(adapter)
        name = _display_name(printer_id) if printer_id else "printer"
        pending = [p for p in _read_pending() if (p.get("unit"), p.get("block")) != (unit, block)]
        pending.append({"unit": unit, "name": name, "block": block, "at": _now()})
        path = _learned_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(pending[-20:], indent=2), encoding="utf-8")
    except Exception:  # noqa: BLE001
        _logger.debug("bench: what was learned is not kept to be said", exc_info=True)


def _read_pending() -> list[dict[str, Any]]:
    try:
        pending = json.loads(_learned_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [p for p in pending if isinstance(p, dict)] if isinstance(pending, list) else []


def take_news() -> dict[str, list[Any]]:
    """What is waiting to be said on the person's next reply, forgotten once
    taken: ``said``, one sentence per thing a real print taught Kiln, and
    ``offers``, the first lesson waiting to be offered after a printer's
    first print (unit, printer and the blocks still missing).  Never raises."""
    pending = _read_pending()
    with contextlib.suppress(OSError):
        _learned_path().unlink()
    said = [f"Kiln learned how your {p.get('name') or 'printer'} {_LEARNED_VERBS[p['block']]}."
            for p in pending if p.get("block") in _LEARNED_VERBS]
    offers = [p for p in pending if isinstance(p.get("missing"), list) and p.get("unit")]
    return {"said": said, "offers": offers}


def offer_after_first_print(adapter: Any, printer_name: str) -> None:
    """After a printer's first print has taught what it can: when the first
    lesson still has something to teach, keep its offer for the person's next
    reply (:func:`take_news`), naming only what is still missing.  Nothing
    missing is never offered, and is not asked again.  Offline, or a session
    the service will not start yet, spends nothing: the next print asks again.
    Once per printer (``first_print``).  Never raises."""
    try:
        from kiln.plate_state import declared_model_of

        unit = unit_of(adapter)
        if not may_offer(unit, "first_print"):
            return
        printer_id = declared_model_of(adapter)
        if not printer_id:
            return
        blanks, verdict = blanks_for(adapter, printer_id)
        if verdict is None or session_hold(verdict) is not None:
            return
        missing = [b for b in blanks if b in TEACHABLE]
        if not missing:
            note_offer(unit, answer="done")       # nothing a test print could teach: never offered
            return
        pending = [p for p in _read_pending() if not (p.get("unit") == unit and "missing" in p)]
        pending.append({"unit": unit, "printer_id": printer_id, "printer_name": printer_name,
                        "missing": missing, "at": _now()})
        path = _learned_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(pending[-20:], indent=2), encoding="utf-8")
    except Exception:  # noqa: BLE001
        _logger.debug("bench: the offer after a first print was not kept", exc_info=True)


def offer_after_registration(adapter: Any, printer_name: str) -> dict[str, Any] | None:
    """The one-time offer a registration answers with: the second lesson,
    for a printer whose moves a test print cannot teach anything more about.
    A printer with something left to teach about its moves is offered
    nothing yet: its first print teaches what it can, and what it leaves is
    offered after it (``first_print``).  ``None`` when it was offered before,
    nothing can be read right now, or the service will not start a session
    yet.  Never raises."""
    try:
        from kiln.plate_state import declared_model_of

        unit = unit_of(adapter)
        if not may_offer(unit, "registration"):
            return None
        printer_id = declared_model_of(adapter)
        if not printer_id:
            return None
        blanks, verdict = blanks_for(adapter, printer_id)
        if verdict is None or session_hold(verdict) is not None:
            return None                           # offline, or not startable yet; nothing is marked
        if not blanks:
            note_offer(unit, answer="done")       # nothing to teach about the moves; never ask
        if any(b in TEACHABLE for b in blanks):
            return None                           # the first print teaches first; nothing is spent
        # Blanks no session can fill leave the first lesson unoffered and its
        # one offer unspent; the second lesson has something for every printer.
        return offer(unit, ["limits"], "registration", model_name=_display_name(printer_id),
                     printer_name=printer_name)
    except Exception:  # noqa: BLE001
        _logger.debug("bench: registration offer skipped", exc_info=True)
        return None


def _display_name(printer_id: str) -> str:
    try:
        from kiln.printers.bed_fit import get_printer_display_name

        return str(get_printer_display_name(printer_id) or printer_id)
    except Exception:  # noqa: BLE001
        return str(printer_id)


# ---------------------------------------------------------------------------
# The pictures
# ---------------------------------------------------------------------------


def _pil() -> Any:
    try:
        from PIL import Image, ImageDraw, ImageFont  # type: ignore[import-not-found]
    except Exception:  # noqa: BLE001
        return None
    return Image, ImageDraw, ImageFont


def draw_zones(bed_mm: tuple[float, float], *, size_px: int = 720) -> bytes | None:
    """The plate seen from above, with nine numbered zones on it and a ring
    of sixteen around it, the front at the bottom -- what an owner answers
    "where did the head stop?" with.  PNG bytes, or ``None`` without Pillow."""
    deps = _pil()
    if deps is None:
        return None
    Image, ImageDraw, ImageFont = deps
    bx, by = float(bed_mm[0]), float(bed_mm[1])
    span_x, span_y = bx * 5 / 3, by * 5 / 3
    scale = (size_px - 80) / max(span_x, span_y)
    width, height = int(span_x * scale) + 80, int(span_y * scale) + 100
    img = Image.new("RGB", (width, height), (250, 250, 248))
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 22)
        small = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 16)
    except Exception:  # noqa: BLE001
        font = ImageFont.load_default()
        small = font
    ox, oy = 40, 40

    def px(x: float, y: float) -> tuple[float, float]:
        # bed x to the right; bed y up the page (front at the bottom)
        return (ox + (x + bx / 3) * scale, oy + (by * 4 / 3 - y) * scale)

    for zone in range(1, PLATE_ZONES + RING_ZONES + 1):
        x0, y0, x1, y1 = zone_rect(zone, (bx, by))
        (ax, ay), (bx2, by2) = px(x0, y1), px(x1, y0)
        on_plate = zone <= PLATE_ZONES
        draw.rectangle([ax, ay, bx2, by2], fill=(225, 236, 246) if on_plate else (240, 240, 236),
                       outline=(120, 130, 140), width=1)
        label = str(zone)
        tw = draw.textlength(label, font=font)
        draw.text(((ax + bx2) / 2 - tw / 2, (ay + by2) / 2 - 12), label, fill=(30, 40, 60) if on_plate else (110, 110, 110),
                  font=font)
    (ax, ay), (bx2, by2) = px(0.0, by), px(bx, 0.0)
    draw.rectangle([ax, ay, bx2, by2], outline=(40, 80, 140), width=4)
    draw.text((ax, by2 + 8), "FRONT of the printer (you stand here)", fill=(40, 80, 140), font=small)
    draw.text((ax, ay - 24), "back", fill=(110, 110, 110), font=small)
    draw.text((8, (ay + by2) / 2), "L", fill=(110, 110, 110), font=small)
    draw.text((width - 22, (ay + by2) / 2), "R", fill=(110, 110, 110), font=small)
    draw.text((ox, height - 26), f"blue = the plate ({bx:.0f} x {by:.0f} mm); grey = off the plate", fill=(90, 90, 90), font=small)
    import io

    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def draw_head_sketch(*, size_px: int = 640) -> bytes | None:
    """A print head seen from the front, with the two figures the caliper
    asks want: the widest part left to right, and the nozzle tip up to the
    lowest bar that crosses the plate.  PNG bytes, or ``None`` without Pillow."""
    deps = _pil()
    if deps is None:
        return None
    Image, ImageDraw, ImageFont = deps
    w, h = size_px, int(size_px * 0.75)
    img = Image.new("RGB", (w, h), (250, 250, 248))
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 18)
    except Exception:  # noqa: BLE001
        font = ImageFont.load_default()
    grey, blue, red = (120, 120, 120), (40, 80, 140), (190, 60, 50)
    # the bar (gantry rod), the head body hanging from it, the nozzle
    bar_y = int(h * 0.22)
    d.rectangle([int(w * 0.08), bar_y - 10, int(w * 0.92), bar_y + 10], fill=(200, 200, 200), outline=grey)
    body = [int(w * 0.34), bar_y + 10, int(w * 0.66), int(h * 0.62)]
    d.rectangle(body, fill=(225, 236, 246), outline=blue, width=3)
    d.polygon([(int(w * 0.47), body[3]), (int(w * 0.53), body[3]), (int(w * 0.5), int(h * 0.70))], fill=blue)
    tip = (int(w * 0.5), int(h * 0.70))
    d.line([(int(w * 0.08), int(h * 0.80)), (int(w * 0.92), int(h * 0.80))], fill=grey, width=3)
    d.text((int(w * 0.08), int(h * 0.82)), "plate", fill=grey, font=font)
    # width arrow
    ay = body[1] + 24
    d.line([(body[0], ay), (body[2], ay)], fill=red, width=3)
    d.text((body[0], ay - 26), "A: widest part, left to right", fill=red, font=font)
    # nozzle-to-bar arrow
    ax = int(w * 0.76)
    d.line([(ax, tip[1]), (ax, bar_y + 10)], fill=red, width=3)
    d.line([(int(w * 0.5), tip[1]), (ax, tip[1])], fill=red, width=1)
    d.text((ax + 8, int((tip[1] + bar_y) / 2) - 10), "B: nozzle tip up to", fill=red, font=font)
    d.text((ax + 8, int((tip[1] + bar_y) / 2) + 12), "the lowest bar", fill=red, font=font)
    import io

    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


# ---------------------------------------------------------------------------
# The position log: what a Klipper or Marlin printer says instead of an ask
# ---------------------------------------------------------------------------


def position_reading(adapter: Any) -> tuple[float, float, float, float | None] | None:
    """Where the head is right now, and the extruder with it, from the
    printer's own word (:meth:`~kiln.printers.base.PrinterAdapter.get_tool_position`:
    Klipper's live position through Moonraker, Marlin's ``M114`` over USB,
    RepRapFirmware's object model).  The fourth value is the extruder's
    position, ``None`` when the printer gives none.  ``None`` for a printer
    that does not say (a Bambu, an OctoPrint server).  The served side
    judges a log by the lowest height it saw away from the part, never by
    its line, so a waypoint that falls between two samples cannot make a
    verdict looser."""
    try:
        reader = getattr(adapter, "get_tool_position", None)
        if callable(reader):
            found = reader()
            if isinstance(found, dict) and all(k in found for k in ("x", "y", "z")):
                return (float(found["x"]), float(found["y"]), float(found["z"]), _extruder(found.get("e")))
        if _is_adapter(adapter):
            return None
        # A Moonraker-shaped object that is not an adapter (a scripted
        # stand-in): read the live position the way the adapter does.
        query = getattr(adapter, "_get_json", None)
        if callable(query):
            payload = query("/printer/objects/query", params={"motion_report": "live_position"})
            raw = payload.get("result", {}).get("status", {}).get("motion_report", {}).get("live_position")
            if isinstance(raw, (list, tuple)) and len(raw) >= 3:
                return (float(raw[0]), float(raw[1]), float(raw[2]), _extruder(raw[3] if len(raw) >= 4 else None))
    except Exception:  # noqa: BLE001
        _logger.debug("bench: position read failed", exc_info=True)
    return None


def _is_adapter(adapter: Any) -> bool:
    try:
        from kiln.printers.base import PrinterAdapter

        return isinstance(adapter, PrinterAdapter)
    except Exception:  # noqa: BLE001
        return False


def _extruder(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def position_of(adapter: Any) -> tuple[float, float, float] | None:
    """Where the head is right now (:func:`position_reading` without the extruder)."""
    reading = position_reading(adapter)
    return None if reading is None else reading[:3]


def logs_positions(adapter: Any) -> bool:
    """Whether *adapter*'s backend says where the head AND the extruder are
    while the head moves -- decided from what the backend states about
    itself (``PrinterAdapter.head_position``), without asking the printer
    anything, so a printer that cannot say is never polled."""
    return _is_adapter(adapter) and getattr(type(adapter), "head_position", "") == "head_and_extruder"


def can_log_positions(adapter: Any) -> bool:
    """Whether this printer will say where its head is -- one read settles it."""
    return position_of(adapter) is not None


class PositionLog(threading.Thread):
    """Poll the head's position at :data:`LOG_HZ` until it has been still
    for :data:`LOG_SETTLE_S` (after at least one move), a stop is asked
    for, or :data:`LOG_MAX_S` has passed.  ``points`` is the path, and
    ``extruder`` the extruder's position at each point (``None`` where the
    printer gave none).

    ``complete`` is True only when the log ended the honest way: the head
    moved, then read back still, with no failed read, for the whole settle
    window -- or *stop_when*, asked after every new point, said the log has
    seen what it was for.  A log cut short -- a stop, the time limit, a
    printer that stopped answering mid-move, no movement at all within
    *quiet_s* -- may have missed where the head went last, and nothing is
    written from it."""

    def __init__(self, adapter: Any, *, settle_s: float = LOG_SETTLE_S, max_s: float = LOG_MAX_S,
                 hz: float = LOG_HZ, until_idle: Any = None, stop_when: Any = None,
                 quiet_s: float | None = None) -> None:
        super().__init__(name="bench-position-log", daemon=True)
        self._adapter = adapter
        self._settle = settle_s
        self._max = max_s
        self._period = 1.0 / max(1.0, hz)
        self._until_idle = until_idle
        self._stop_when = stop_when
        self._quiet = quiet_s
        # Not ``_stop``: through Python 3.12 threading.Thread has a private
        # ``_stop()`` method that join() calls, and an Event there breaks it.
        self._halt = threading.Event()
        self.points: list[tuple[float, float, float]] = []
        self.extruder: list[float | None] = []
        self.moved = False
        self.complete = False
        self.finished_at: float | None = None

    def stop(self) -> None:
        self._halt.set()

    def _changed(self, reading: tuple[float, float, float, float | None]) -> bool:
        if not self.points:
            return True
        if any(abs(a - b) > 0.05 for a, b in zip(reading[:3], self.points[-1], strict=True)):
            return True
        e, last_e = reading[3], self.extruder[-1]
        return e is not None and last_e is not None and abs(e - last_e) > PUSH_MM / 2.0

    def run(self) -> None:  # pragma: no cover - timing; the pure parts are tested through `read`
        started = time.monotonic()
        last_change = started
        last_failure = started
        while not self._halt.is_set() and time.monotonic() - started < self._max:
            reading = position_reading(self._adapter)
            now = time.monotonic()
            if reading is None:
                last_failure = now                 # a gap: the settle window starts again
            elif self._changed(reading):
                if self.points:
                    self.moved = True
                last_change = now
                self.points.append(reading[:3])
                self.extruder.append(reading[3])
                if self._stop_when is not None and self.moved:
                    try:
                        if self._stop_when(self):
                            self.complete = True
                            break
                    except Exception:  # noqa: BLE001
                        _logger.debug("bench: stop check failed", exc_info=True)
            if self._quiet is not None and not self.moved and now - started >= self._quiet:
                break
            idle = False
            if self._until_idle is not None:
                try:
                    idle = bool(self._until_idle())
                except Exception:  # noqa: BLE001
                    idle = False
            if (self._stop_when is None and self.moved and now - max(last_change, last_failure) >= self._settle
                    and (self._until_idle is None or idle)):
                self.complete = True
                break
            time.sleep(self._period)
        self.finished_at = time.monotonic()


# ---------------------------------------------------------------------------
# Reading a log that began while the printer was printing
# ---------------------------------------------------------------------------

#: How far above where a log began the head must go to count as having
#: risen: the block's own lift, not a hop between two lines of the print.
RISE_MM = 1.0
#: The least forward push of filament between two points that counts as
#: printing.
PUSH_MM = 0.01
#: How far outside the running print's footprint a point may sit and
#: still be over the print: the plate record's box is the file's, and the
#: head is sampled, not followed.
FOOTPRINT_MARGIN_MM = 2.0


def print_footprint(adapter: Any) -> list[float] | None:
    """``[x0, y0, x1, y1]`` of the print running on *adapter*'s plate, as
    the plate record holds it for a print Kiln started; ``None`` for a
    print Kiln did not start or whose file gave no footprint."""
    try:
        from kiln import plate_state

        state = plate_state.read(adapter)
    except Exception:  # noqa: BLE001
        return None
    if not state.occupied or state.source != "kiln_started_print" or state.job is None:
        return None
    box = state.job.footprint_mm
    return [float(v) for v in box] if box and len(box) == 4 else None


def _pushed(extruder: list[float | None], i: int) -> bool:
    before, now = extruder[i - 1], extruder[i]
    return before is not None and now is not None and now - before > PUSH_MM


def _over(point: tuple[float, ...], box: list[float]) -> bool:
    m = FOOTPRINT_MARGIN_MM
    return box[0] - m <= point[0] <= box[2] + m and box[1] - m <= point[1] <= box[3] + m


def block_start(points: list[tuple[float, float, float]], extruder: list[float | None],
                footprint: list[float]) -> tuple[int, float] | str:
    """Where a pause, cancel or end begins in a log started while the
    printer was still printing, and the layer it began at -- or why the
    log cannot say.

    A printer finishes the lines it has queued before it acts, so a log
    begun as the command goes out opens on the print, not the block.  The
    block begins at the last point where filament was still going forward
    before the head first rose more than :data:`RISE_MM` -- or at the next
    point, when that one is still over the print, so the climb is seen from
    where the head stopped.  Every point left out is over the print
    (inside *footprint*) and no higher than where the log began.  A log
    with no filament going forward before the climb began after the block
    had, and one that shows the head off the print first cannot tell the
    print from the block: neither says anything.  The layer is the highest
    point up to the start, so a lift already under way is never counted
    as part of the climb."""
    if len(points) < 2 or len(extruder) != len(points):
        return "the log has no extruder readings to tell the print from the block"
    z0 = points[0][2]
    rise = next((i for i, p in enumerate(points) if p[2] > z0 + RISE_MM), len(points))
    pushes = [i for i in range(1, rise) if _pushed(extruder, i)]
    if not pushes:
        return "the log saw no filament going forward before the head rose, so it began after the block had"
    last = pushes[-1]
    start = last + 1 if last + 1 < rise and _over(points[last + 1], footprint) else last
    if not all(_over(p, footprint) for p in points[: start + 1]):
        return "the log saw the head away from the print before the block began"
    return start, max(p[2] for p in points[: start + 1])


def way_back(points: list[tuple[float, float, float]], extruder: list[float | None], layer_z: float) -> int | None:
    """How many points of a log taken while a paused print resumed are the
    way back: every one before filament first goes forward again at the
    layer.  ``None`` when the log never saw the print resume."""
    if len(extruder) != len(points):
        return None
    for i in range(1, len(points)):
        if _pushed(extruder, i) and points[i][2] <= layer_z + RISE_MM:
            return i
    return None


def logged_block(logs: list[Any], footprint: list[float] | None, *, returns: bool) -> tuple[list[tuple[float, float, float]], float] | str:
    """One block's points and layer from its logs -- the block's own log,
    then for a block that comes back (a pause) the log of the way back --
    or the reason nothing can be written: a log cut short, no footprint
    for the print that was running, a log that cannot tell the print from
    the block (:func:`block_start`), a way back that was not seen, or no
    movement at all."""
    if not logs or any(log is None or not getattr(log, "complete", False) for log in logs):
        return "the position log was cut short before the head settled"
    if footprint is None:
        return "Kiln has no footprint for the print that was running, so the log can't tell the print from the block"
    first = logs[0]
    found = block_start(list(first.points), list(getattr(first, "extruder", []) or []), footprint)
    if isinstance(found, str):
        return found
    start, layer = found
    points = list(first.points[start:])
    if returns:
        if len(logs) < 2:
            return "the way back was not seen"
        back = logs[1]
        n = way_back(list(back.points), list(getattr(back, "extruder", []) or []), layer)
        if n is None:
            return "the log never saw the print resume"
        points += list(back.points[:n])
    if len({tuple(round(v, 1) for v in p) for p in points}) < 2:
        return "the position log caught no movement"
    return points, layer


def ended_block(log: Any, footprint: list[float] | None) -> tuple[list[tuple[float, float, float]], float] | str:
    """An end's points and the layer it began at, from a log begun in a
    print's last minutes -- or the reason nothing can be written.

    A log begun minutes before the end climbs a layer at a time, so the
    first rise is not the end's.  The end begins at the last point where
    filament went forward over the print (or the next point, when that one
    is still over it), and the layer is the highest point filament went
    forward at.  A log cut short, no footprint, no filament going forward
    over the print, the head away from the print first, or no movement
    after the last line says nothing."""
    if log is None or not getattr(log, "complete", False):
        return "the position log was cut short before the head settled"
    if footprint is None:
        return "Kiln has no footprint for the print that was running, so the log can't tell the print from the block"
    points = list(log.points)
    extruder = list(getattr(log, "extruder", []) or [])
    if len(points) < 2 or len(extruder) != len(points):
        return "the log has no extruder readings to tell the print from the block"
    printed = [i for i in range(1, len(points)) if _pushed(extruder, i) and _over(points[i], footprint)]
    if not printed:
        return "the log saw no filament going forward over the print"
    last = printed[-1]
    start = last + 1 if last + 1 < len(points) and _over(points[last + 1], footprint) else last
    if not all(_over(p, footprint) for p in points[: start + 1]):
        return "the log saw the head away from the print before the end began"
    tail = points[start:]
    if len({tuple(round(v, 1) for v in p) for p in tail}) < 2:
        return "the position log caught no movement after the last line"
    return tail, max(points[i][2] for i in printed)


# ---------------------------------------------------------------------------
# The send: one RPC, under the one switch
# ---------------------------------------------------------------------------


def _rpc_args(doc: dict[str, Any]) -> dict[str, Any]:
    body = {k: v for k, v in doc.items() if k != "share"}
    return {
        "p_printer_id": str(doc.get("model") or "").strip().lower()[:80],
        "p_block": doc.get("block"),
        "p_fingerprint": fingerprint_of(doc),
        "p_document": body,
        "p_how": doc.get("how"),
        "p_unit": doc.get("unit"),
        "p_firmware": doc.get("firmware"),
    }


def sharing() -> bool:
    """Whether this install shares what it observes: telemetry on AND
    community sharing on (``KILN_COMMUNITY_OPT_IN``).  Either one off keeps
    every observation on this machine."""
    try:
        from kiln.community_sync import community_sharing_enabled
        from kiln.heartbeat import _telemetry_enabled

        return bool(_telemetry_enabled()) and bool(community_sharing_enabled())
    except Exception:  # noqa: BLE001
        return False


def send_pending(supabase_url: str, anon_key: str) -> int:
    """Post every observation on this machine that has not landed yet;
    how many landed.  Only while this install shares (:func:`sharing`);
    never raises.  Each document is built field by field
    (:func:`observation`): words from fixed vocabularies, numbers, a
    one-way hash of the machine and the firmware's own version label --
    which is why it is not run through :func:`kiln.outbound_guard.scrub`,
    whose address rule reads a dotted firmware version (``01.04.00.00``)
    as a network address and would get the row refused."""
    if not sharing():
        return 0
    import urllib.request

    landed = 0
    try:
        folder = bench_dir()
        paths = sorted(folder.glob("*.json")) if folder.is_dir() else []
    except OSError:
        return 0
    for path in paths:
        if path.name in ("offers.json", "learned.json"):
            continue
        try:
            store = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(store, dict) or not isinstance(store.get("observations"), list):
            continue
        sent = store.get("sent") if isinstance(store.get("sent"), dict) else {}
        changed = False
        for doc in store["observations"]:
            if not isinstance(doc, dict) or not doc.get("model"):
                continue
            fp = fingerprint_of(doc)
            if fp in sent:
                continue
            try:
                req = urllib.request.Request(
                    f"{supabase_url.rstrip('/')}/rest/v1/rpc/{_RPC}",
                    data=json.dumps(_rpc_args(doc)).encode("utf-8"),
                    headers={"Content-Type": "application/json", "apikey": anon_key,
                             "Authorization": f"Bearer {anon_key}"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
                    if resp.status < 300:
                        sent[fp] = _now()
                        landed += 1
                        changed = True
            except Exception as exc:  # noqa: BLE001 -- non-fatal, like the heartbeat
                _logger.debug("bench: observation not sent (non-fatal): %s", exc)
        if changed:
            store["sent"] = sent
            with contextlib.suppress(OSError):
                _save_store(store.get("unit") or path.stem, store)
    return landed


def send_after_heartbeat(supabase_url: str, anon_key: str) -> int:
    """What the heartbeat calls once it has been sent for the day."""
    return send_pending(supabase_url, anon_key)
