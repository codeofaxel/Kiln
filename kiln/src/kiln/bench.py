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
* the head-measuring sketch for the two caliper asks;
* the position log a Klipper or Marlin printer gives instead of any ask;
* what the record still lacks for a printer (``blanks_for``), read from
  the verdict itself so the session asks only about blanks;
* the offers ledger, so nobody is nagged twice; and
* the send: one validated RPC into ``printer_motion_observations``, under
  the one telemetry switch, after the heartbeat and at the session's end.
"""

from __future__ import annotations

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
#: The blocks a record can have a blank for that a session can fill.
RECORD_BLOCKS = ("pause", "cancel", "end", "filament_change")
#: The four ways an observation is made.
HOWS = ("position_log", "camera", "owner_zone", "owner_measure")
#: What each lift word means as a LOWER bound in millimetres: the verdict
#: reads the head no higher than this, whatever the real lift was.
LIFT_WORDS_MM = {"barely": 0.5, "finger": 10.0, "hand": 100.0}
#: Zones on the plate (a 3 x 3 grid, 1-9) and the ring around it (10-25).
PLATE_ZONES = 9
RING_ZONES = 16
#: The RPC the documents go through, and its posture: best effort.
_RPC = "record_printer_motion_observation"
_TIMEOUT_S = 5
#: The position log's cadence and patience.
LOG_HZ = 10.0
LOG_SETTLE_S = 4.0
LOG_MAX_S = 120.0

__all__ = [
    "BLOCKS", "HOWS", "LIFT_WORDS_MM", "OBSERVATION_FORMAT", "RECORD_BLOCKS", "PositionLog",
    "bed_of", "bench_dir", "blanks_for", "can_log_positions", "draw_head_sketch", "draw_zones",
    "fingerprint_of", "firmware_of", "head_observation", "keep_observation", "may_offer", "note_offer",
    "observation", "observations_for_request", "observations_of", "offer_after_registration",
    "offer_sentence", "parse_lift", "parse_zone", "position_of", "refusal_blanks", "send_after_heartbeat",
    "send_pending", "unit_from_machine", "unit_of", "zone_of", "zone_rect",
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
    if "not" in text or "didn't" in text or "no" == text:
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
    """The two caliper figures, in the shape the served door reads."""
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


def keep_observation(adapter: Any, doc: dict[str, Any]) -> Path | None:
    """Write *doc* for *adapter*'s unit, replacing an earlier observation
    of the same block: the newest look is the one that stands."""
    unit = unit_of(adapter)
    if not unit:
        return None
    store = _load_store(unit)
    store["model"] = doc.get("model") or store.get("model")
    store["observations"] = [d for d in store["observations"] if d.get("block") != doc.get("block")] + [doc]
    return _save_store(unit, store)


def observations_of(adapter: Any) -> list[dict[str, Any]]:
    """Every observation on file for *adapter*'s unit, as written."""
    unit = unit_of(adapter)
    if not unit:
        return []
    return [d for d in _load_store(unit)["observations"] if isinstance(d, dict)]


def observations_for_request(adapter: Any) -> list[dict[str, Any]] | None:
    """What a placement request for *adapter* carries: this unit's
    observations, so its own verdict uses them at once.  While telemetry
    is off the documents still travel for the verdict but carry nothing
    that names the unit and ask not to be kept (``share: false``); the
    one switch covers what Kiln learns from, never what Kiln answers."""
    docs = observations_of(adapter)
    if not docs:
        return None
    try:
        from kiln.heartbeat import _telemetry_enabled

        sharing = _telemetry_enabled()
    except Exception:  # noqa: BLE001
        sharing = False
    if sharing:
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
        for block in RECORD_BLOCKS:
            said = block.replace("_", " ")
            if (said in sentence or block in sentence) and (_WORST_CASE in sentence or block in blanks):
                if block not in out:
                    out.append(block)
    if not out and any(r.get("code") == "PLACEMENT_UNKNOWN_PRINTER" for r in verdict.get("refusals") or [] if isinstance(r, dict)):
        return list(RECORD_BLOCKS) + ["head"]
    return out


def blanks_for(adapter: Any, printer_id: str | None) -> tuple[list[str], dict[str, Any] | None]:
    """What the record for *printer_id* still does not know, read from the
    verdict itself: a probe request on a clear plate, with this unit's
    own settings and observations riding it, so a blank the machine or
    its owner already filled does not count.  ``(blanks, verdict)``; an
    unknown printer is every blank; no verdict at all is ``([], None)``
    -- offline is not a reason to nag."""
    try:
        from kiln import _pro_placement_bridge as bridge

        part = {"size_mm": [20.0, 20.0, 2.0], "layer_height_mm": 0.2, "tower_mm": None, "colour_changes_at_mm": [],
                "skirt_mm": 2.0}
        request = bridge.request_for(adapter, printer_id, placement="auto", part=part)
        request["plate"] = {"status": "clear", "job": None, "jobs": [], "fingerprint": "", "since": None}
        request["occupant_gcode"] = None
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
    return [b for b in (record.get("blanks") or []) if isinstance(b, str)], verdict


def offer_sentence(model_name: str, blanks: list[str]) -> str:
    """The offer, in one sentence: what Kiln does not know and what five
    minutes buys."""
    blocks = [b.replace("_", " ") for b in blanks if b in RECORD_BLOCKS]
    if "head" in blanks:
        blocks.append("head size")
    if blocks:
        what = f"where {model_name} sends its head when it {' or '.join(blocks[:2])}s" if len(blocks) <= 2 and "head size" not in blocks \
            else f"how {model_name} moves its head on its own ({', '.join(blocks)})"
    else:
        what = f"how {model_name} moves its head on its own"
    return (f"Kiln doesn't know {what}, so it assumes the worst. A five-minute session with a coin-sized test print "
            f"teaches it: call printer_bench.")


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


def note_offer(unit: str | None, *, answer: str | None = None) -> None:
    """Record that the session was offered to *unit*, and the answer when
    there is one (``later``, ``declined``, ``done``)."""
    if not unit:
        return
    offers = _load_offers()
    entry = dict(offers.get(unit) or {})
    entry.setdefault("offered_at", _now())
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


def may_offer(unit: str | None, door: str) -> bool:
    """Whether the session may be offered to *unit* through *door*.

    ``registration`` offers once, ever: the first time a printer with
    blanks is registered.  ``refusal`` offers at the point of need, every
    time a refusal rests on a blank -- that is the answer to "why", not a
    nag -- unless the person declined or already ran it.  ``later`` is
    honoured by the registration door and never repeated there."""
    if not unit:
        return False
    entry = _load_offers().get(unit) or {}
    answer = entry.get("answer")
    if answer in ("declined", "done"):
        return False
    if door == "registration":
        return not entry
    return True


def offer_after_registration(adapter: Any, printer_name: str) -> dict[str, Any] | None:
    """The one-time offer a registration answers with when the model has
    blanks: ``None`` when it has none, was offered before, or nothing can
    be read right now.  Never raises."""
    try:
        from kiln.plate_state import declared_model_of

        unit = unit_of(adapter)
        if not may_offer(unit, "registration"):
            return None
        printer_id = declared_model_of(adapter)
        if not printer_id:
            return None
        blanks, verdict = blanks_for(adapter, printer_id)
        if not blanks:
            if verdict is not None:
                note_offer(unit, answer="done")   # nothing to teach; never ask
            return None
        note_offer(unit)
        first = bool(verdict and any(isinstance(r, dict) and r.get("code") == "PLACEMENT_UNKNOWN_PRINTER"
                                     for r in verdict.get("refusals") or []))
        name = _display_name(printer_id)
        sentence = offer_sentence(name, blanks)
        if first:
            sentence = f"Kiln has never met a {name}: you'd be the first to teach it this printer. {sentence}"
        return {"tool": "printer_bench", "printer_name": printer_name, "blanks": blanks, "first": first,
                "minutes": 5, "sentence": sentence}
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


def position_of(adapter: Any) -> tuple[float, float, float] | None:
    """Where the head is right now, from the printer's own word: Klipper's
    ``toolhead.position`` through Moonraker, Marlin's ``M114`` over USB.
    ``None`` for a firmware that does not say (a Bambu, an OctoPrint
    server without a position API)."""
    try:
        query = getattr(adapter, "_get_json", None)
        if callable(query):
            payload = query("/printer/objects/query", params={"toolhead": "position"})
            raw = payload.get("result", {}).get("status", {}).get("toolhead", {}).get("position")
            if isinstance(raw, (list, tuple)) and len(raw) >= 3:
                return (float(raw[0]), float(raw[1]), float(raw[2]))
            return None
        reader = getattr(adapter, "get_tool_position", None)
        if callable(reader):
            found = reader()
            if isinstance(found, dict) and all(k in found for k in ("x", "y", "z")):
                return (float(found["x"]), float(found["y"]), float(found["z"]))
    except Exception:  # noqa: BLE001
        _logger.debug("bench: position read failed", exc_info=True)
    return None


def can_log_positions(adapter: Any) -> bool:
    """Whether this printer will say where its head is -- one read settles it."""
    return position_of(adapter) is not None


class PositionLog(threading.Thread):
    """Poll the head's position at :data:`LOG_HZ` until it has been still
    for :data:`LOG_SETTLE_S` (after at least one move), a stop is asked
    for, or :data:`LOG_MAX_S` has passed.  ``points`` is the path."""

    def __init__(self, adapter: Any, *, settle_s: float = LOG_SETTLE_S, max_s: float = LOG_MAX_S,
                 hz: float = LOG_HZ, until_idle: Any = None) -> None:
        super().__init__(name="bench-position-log", daemon=True)
        self._adapter = adapter
        self._settle = settle_s
        self._max = max_s
        self._period = 1.0 / max(1.0, hz)
        self._until_idle = until_idle
        self._stop = threading.Event()
        self.points: list[tuple[float, float, float]] = []
        self.moved = False
        self.finished_at: float | None = None

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:  # pragma: no cover - timing; the pure parts are tested through `read`
        started = time.monotonic()
        last_change = started
        last: tuple[float, float, float] | None = None
        while not self._stop.is_set() and time.monotonic() - started < self._max:
            pos = position_of(self._adapter)
            now = time.monotonic()
            if pos is not None:
                if last is None or any(abs(a - b) > 0.05 for a, b in zip(pos, last)):
                    if last is not None:
                        self.moved = True
                    last_change = now
                    self.points.append(pos)
                    last = pos
            idle = False
            if self._until_idle is not None:
                try:
                    idle = bool(self._until_idle())
                except Exception:  # noqa: BLE001
                    idle = False
            if self.moved and now - last_change >= self._settle and (self._until_idle is None or idle):
                break
            time.sleep(self._period)
        self.finished_at = time.monotonic()


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


def send_pending(supabase_url: str, anon_key: str) -> int:
    """Post every observation on this machine that has not landed yet;
    how many landed.  Only while telemetry is on; never raises."""
    try:
        from kiln.heartbeat import _telemetry_enabled

        if not _telemetry_enabled():
            return 0
    except Exception:  # noqa: BLE001
        return 0
    import urllib.request

    landed = 0
    try:
        folder = bench_dir()
        paths = sorted(folder.glob("*.json")) if folder.is_dir() else []
    except OSError:
        return 0
    for path in paths:
        if path.name == "offers.json":
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
            try:
                _save_store(store.get("unit") or path.stem, store)
            except OSError:
                pass
    return landed


def send_after_heartbeat(supabase_url: str, anon_key: str) -> int:
    """What the heartbeat calls once it has been sent for the day."""
    return send_pending(supabase_url, anon_key)
