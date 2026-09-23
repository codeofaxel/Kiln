"""Help Kiln get to know your printer -- the guided session, one ask at a time.

``printer_bench`` is stateful the way ``design_session`` is: each call
returns the next ask, the agent relays it, the person's answer comes back
in the next call.  The session's state is on disk
(``~/.kiln/bench/sessions/<unit>.json``), so a dropped chat resumes where
it was.  Every ask is one sentence, one action, one observed answer, with
the picture that makes it answerable inline (``image_b64``): the plate
drawn as numbered zones, the head-measuring sketch, the camera frame.

The session is only for a model with BLANKS in its record, and only about
the blanks (:func:`kiln.bench.blanks_for`).  Kiln does the work -- it
prints a coin-sized square, pauses, resumes, cancels and ends it through
the same doors a person uses -- and the person confirms or measures only
what Kiln cannot see.  A Klipper or Marlin printer says where its head is,
so it is asked nothing about the moves; a closed-firmware printer (a
Bambu) is asked which zone the head stopped in and about how high it
lifted, with a ruler reading optional.  The tiny print is a real print:
it goes through the normal approval dialog, and this tool never starts it
-- it hands the agent the file and the door.

What it produces is an observation per block, numbers only
(:mod:`kiln.bench`): kept on this machine, carried by every placement
request for this unit so its own verdicts use it at once, and sent to
Kiln under the one telemetry switch so every owner of the model gets it
once three units agree and a person adopts it.  Runout is never asked.

Auto-discovered by :func:`~kiln.plugin_loader.register_all_plugins`.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

from kiln import bench

_logger = logging.getLogger(__name__)

_RATE_LIMITS: dict[str, tuple[int, int]] = {"printer_bench": (1000, 30)}

#: The layer the square is paused or cancelled at (about 1 mm up).
TRIGGER_LAYER = 5
#: The square: 20 mm across, 2 mm tall, ten layers at 0.2 mm.
SQUARE_MM = (20.0, 20.0, 2.0)
LAYER_HEIGHT_MM = 0.2
#: How long a closed-firmware printer is given to park before the camera
#: looks, and how long a wait step tells the agent to come back in.
PARK_SETTLE_S = 8.0
CHECK_AGAIN_S = 30

_YES = {"yes", "y", "yes.", "ready", "ok", "okay", "go", "empty", "clear", "done", "started", "printing", "it's empty",
        "its empty", "yep", "sure", "started it", "it started"}
_LATER = {"later", "not now", "maybe later", "another time"}
_NO = {"no", "n", "never", "don't", "dont", "no thanks", "stop"}

_LOGS: dict[str, bench.PositionLog] = {}
_LOCK = threading.Lock()

__all__ = ["printer_bench", "plugin"]


# ---------------------------------------------------------------------------
# Session state on disk
# ---------------------------------------------------------------------------


def _sessions_dir() -> Path:
    return bench.bench_dir() / "sessions"


def _session_path(unit: str) -> Path:
    return _sessions_dir() / f"{unit}.json"


def _load_session(unit: str) -> dict[str, Any] | None:
    try:
        data = json.loads(_session_path(unit).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("step") else None


def _save_session(session: dict[str, Any]) -> None:
    path = _session_path(session["unit"])
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(session, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def _end_session(session: dict[str, Any]) -> None:
    try:
        _session_path(session["unit"]).unlink()
    except OSError:
        pass


# ---------------------------------------------------------------------------
# The plan: which asks this printer's blanks need
# ---------------------------------------------------------------------------


def _joined_and(words: list[str]) -> str:
    """``a``, ``a and b``, ``a, b, and c``."""
    if len(words) <= 2:
        return " and ".join(words)
    return ", ".join(words[:-1]) + ", and " + words[-1]


#: What happens to a square in each step, as the session says it.
_ACT_WORDS = {"pause": "pauses and resumes it", "end": "lets it finish", "cancel": "cancels it partway",
              "cancel_quietly": "stops it"}
_ASK_WORDS = {"pause": "pause it partway", "end": "let it finish", "cancel": "cancel it partway",
              "cancel_quietly": "stop it"}


def _prints_in(plan: list[str]) -> list[list[str]]:
    """What the plan does to each square it prints, in order."""
    prints: list[list[str]] = []
    for step in plan:
        if step == "print":
            prints.append([])
        elif prints and step in _ACT_WORDS:
            prints[-1].append(step)
    return prints


def _intro(session: dict[str, Any]) -> str:
    """The consent screen, built from the plan so it promises only what
    the session will do."""
    model = session.get("model_name") or session["printer_id"]
    prints = _prints_in(session["plan"])
    head = "head" in session["asked"]
    parts: list[str] = []
    for i, acts in enumerate(prints):
        noun = "a coin-sized square" if i == 0 else "a second one"
        words = [_ACT_WORDS[a] for a in acts]
        if len(words) >= 2:
            parts.append(f"prints {noun}, " + ", ".join(words[:-1]) + ", and " + words[-1])
        elif words:
            parts.append(f"prints {noun} and {words[0]}")
        else:
            parts.append(f"prints {noun}")
    lead = "" if not session.get("first") else f"Kiln has no record for the {model} yet. "
    if parts:
        what = "it " + ", then ".join(parts) + ", watching where the head goes each time"
        if head:
            what += ", and you measure the head with calipers"
        text = f"{lead}Help Kiln get to know your {model} in about five minutes: {what}."
        text += (" Kiln reads where the head goes from the printer itself." if session["loggable"]
                 else " Kiln will ask you where the head stopped, with a picture.")
    else:
        text = f"{lead}Help Kiln get to know your {model}: two caliper measurements of its print head."
    return text + " Ready?"


def _print_ask(session: dict[str, Any]) -> str:
    """The ask before a square is started: what Kiln will do to THIS one."""
    plan, here = session["plan"], int(session.get("plan_index", 0))
    acts: list[str] = []
    for step in plan[here + 1:]:
        if step in ("print", "clear", "done"):
            break
        if step in _ASK_WORDS:
            acts.append(_ASK_WORDS[step])
    which = "a second coin-sized square" if int(session.get("print_n", 0)) >= 1 else "a coin-sized square"
    if len(acts) >= 2:
        return f"Kiln will print {which}, " + ", then ".join(acts) + ". Ready to start it?"
    if acts:
        return f"Kiln will print {which} and {acts[0]}. Ready to start it?"
    return f"Kiln will print {which}. Ready to start it?"


def _plan(blanks: list[str], loggable: bool) -> list[str]:
    """The steps, in order, for these blanks.  One print covers pause,
    resume and end; a second covers cancel when the first was let to end."""
    steps = ["intro"]
    wants_pause = "pause" in blanks
    wants_end = "end" in blanks
    wants_cancel = "cancel" in blanks
    if wants_pause or wants_end or wants_cancel:
        steps += ["plate", "print"]      # an empty plate matters only to a session that prints
    if wants_pause:
        steps += ["pause", "parked"] + ([] if loggable else ["pause_zone", "pause_lift", "pause_measure"])
        steps += ["resume", "resumed"] + ([] if loggable else ["resume_back"])
    if wants_end:
        steps += ["end", "ended"] + ([] if loggable else ["end_zone", "end_lift"])
        steps.append("clear")
        if wants_cancel:
            steps.append("print")
    elif wants_pause or wants_cancel:
        # The running square is cancelled -- observed when cancel is a blank.
        pass
    if wants_cancel:
        steps += ["cancel", "cancelled"] + ([] if loggable else ["cancel_zone", "cancel_lift"])
        steps.append("clear")
    elif wants_pause and not wants_end:
        steps += ["cancel_quietly", "clear"]
    if "head" in blanks:
        steps += ["head_width", "head_rod"]
    steps.append("done")
    return steps


def _next_step(session: dict[str, Any]) -> str:
    plan = session["plan"]
    i = plan.index(session["step"]) if session["step"] in plan else -1
    # "print" appears up to twice; advance from the CURRENT index.
    current = session.get("plan_index", i)
    nxt = current + 1
    session["plan_index"] = nxt
    session["step"] = plan[nxt] if nxt < len(plan) else "done"
    return session["step"]


# ---------------------------------------------------------------------------
# Answers and pictures
# ---------------------------------------------------------------------------


def _norm(answer: Any) -> str:
    return " ".join(str(answer or "").strip().lower().split())


def _is_yes(answer: Any) -> bool:
    text = _norm(answer)
    return text in _YES or text.startswith(("yes", "ready", "started", "it's empty", "its empty", "plate is empty"))


def _is_later(answer: Any) -> bool:
    return _norm(answer) in _LATER or _norm(answer).startswith("later")


def _is_no(answer: Any) -> bool:
    return _norm(answer) in _NO


def _picture(kind: str, data: bytes | None, caption: str) -> dict[str, Any] | None:
    if not data:
        return None
    import base64

    return {"kind": kind, "caption": caption, "image_b64": base64.b64encode(data).decode("ascii"),
            "media_type": "image/png" if data[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"}


def _camera_frame(adapter: Any) -> dict[str, Any] | None:
    try:
        from kiln import plate_state

        found = plate_state.look(adapter)
    except Exception:  # noqa: BLE001
        return None
    if not found.available or not found.image_b64:
        return None
    return {"kind": "camera", "caption": "the camera's view right now", "image_b64": found.image_b64,
            "media_type": found.media_type or "image/jpeg"}


def _zones_picture(session: dict[str, Any]) -> dict[str, Any] | None:
    bed = tuple(session["bed_mm"])
    return _picture("zones", bench.draw_zones(bed), "the plate from above, front at the bottom: nine zones on it, sixteen around it")


def _reply(session: dict[str, Any], ask: str, *, pictures: list[dict[str, Any] | None] | None = None,
           options: list[str] | None = None, waiting: bool = False, **extra: Any) -> dict[str, Any]:
    _save_session(session)
    shown = [p for p in (pictures or []) if p]
    out: dict[str, Any] = {
        "success": True, "printer_name": session["printer_name"], "step": session["step"], "ask": ask,
        "options": options or [], "waiting": waiting, "blanks": session["blanks"],
        "learned": sorted(session["observed"]), "images": shown,
        "next": ("Call printer_bench again in about %d seconds." % CHECK_AGAIN_S) if waiting
        else "Relay the ask word for word, show the picture, and call printer_bench again with the person's answer.",
    }
    if waiting:
        out["check_again_in_seconds"] = CHECK_AGAIN_S
    if shown:
        out["image_b64"] = shown[0]["image_b64"]
        out["media_type"] = shown[0]["media_type"]
    out.update(extra)
    return out


# ---------------------------------------------------------------------------
# The printer, through the same doors a person uses
# ---------------------------------------------------------------------------


def _status(adapter: Any) -> tuple[str, Any]:
    try:
        state, job = adapter.get_status()
        return str(getattr(state.state, "value", state.state) or "").lower(), job
    except Exception:  # noqa: BLE001
        return "unknown", None


def _layer_now(job: Any) -> int | None:
    layer = getattr(job, "current_layer", None)
    return int(layer) if isinstance(layer, int) and not isinstance(layer, bool) else None


def _far_enough(job: Any, session: dict[str, Any], stage_key: str) -> bool:
    """Whether the square has grown enough to act on: the trigger layer
    when the printer counts layers, else a wait measured from when the
    print was seen running."""
    layer = _layer_now(job)
    if layer is not None:
        return layer >= TRIGGER_LAYER
    completion = getattr(job, "completion", None)
    if isinstance(completion, (int, float)) and completion >= 45.0:
        return True
    since = session.get("printing_since")
    return bool(since) and time.time() - float(since) >= 120.0


def _layer_z(job: Any, session: dict[str, Any], log: bench.PositionLog | None) -> float:
    if log is not None and log.points:
        return float(log.points[0][2])
    layer = _layer_now(job)
    if layer is not None:
        return max(LAYER_HEIGHT_MM, layer * LAYER_HEIGHT_MM)
    return TRIGGER_LAYER * LAYER_HEIGHT_MM


def _write_square(session: dict[str, Any]) -> str:
    """A 20 x 20 x 2 mm square as ASCII STL, in the bench folder."""
    x, y, z = SQUARE_MM
    path = bench.bench_dir() / "kiln_bench_square.stl"
    path.parent.mkdir(parents=True, exist_ok=True)
    v = [(0, 0, 0), (x, 0, 0), (x, y, 0), (0, y, 0), (0, 0, z), (x, 0, z), (x, y, z), (0, y, z)]
    faces = [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4), (1, 2, 6), (1, 6, 5),
             (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]
    lines = ["solid kiln_bench_square"]
    for a, b, c in faces:
        lines.append("  facet normal 0 0 0\n    outer loop")
        for i in (a, b, c):
            lines.append("      vertex %.3f %.3f %.3f" % v[i])
        lines.append("    endloop\n  endfacet")
    lines.append("endsolid kiln_bench_square")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    session["print_file"] = str(path)
    return str(path)


def _door(name: str, printer_name: str) -> dict[str, Any]:
    """The server's own tool for *name*, called as a person would."""
    import kiln.server as _srv

    fn = getattr(_srv, name)
    try:
        out = fn(printer_name=printer_name)
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "error": str(exc)}
    return out if isinstance(out, dict) else {"success": bool(out)}


def _start_log(session: dict[str, Any], adapter: Any, block: str, **kw: Any) -> None:
    if not session.get("loggable"):
        return
    key = f"{session['unit']}:{block}"
    with _LOCK:
        old = _LOGS.get(key)
        if old is not None and old.is_alive():
            return
        log = bench.PositionLog(adapter, **kw)
        _LOGS[key] = log
        log.start()


def _log_for(session: dict[str, Any], block: str) -> bench.PositionLog | None:
    return _LOGS.get(f"{session['unit']}:{block}")


def _log_done(session: dict[str, Any], block: str) -> bool:
    log = _log_for(session, block)
    return log is None or not log.is_alive()


# ---------------------------------------------------------------------------
# Building the documents
# ---------------------------------------------------------------------------


def _keep(session: dict[str, Any], adapter: Any, doc: dict[str, Any]) -> None:
    session["observed"][doc["block"]] = doc
    bench.keep_observation(adapter, doc)


def _observed_from_log(session: dict[str, Any], adapter: Any, block: str, *, returns: bool,
                       logs: list[Any], layer_z: float) -> None:
    """Write *block*'s observation from its logs -- only when every log ran
    to a clean finish and the head really moved.  A log cut short may have
    missed where the head went last, and one that saw no movement would
    read as a block that never leaves the part: either would make a
    verdict looser than the machine, so neither is written."""
    if not logs or any(log is None or not getattr(log, "complete", False) for log in logs):
        session.setdefault("notes", []).append(
            f"{block}: the position log was cut short before the head settled, so nothing was written for it")
        return
    points = [p for log in logs for p in log.points]
    if len({tuple(round(v, 1) for v in p) for p in points}) < 2:
        session.setdefault("notes", []).append(f"{block}: the position log caught no movement, so nothing was written for it")
        return
    doc = bench.observation(
        session["printer_id"], block, "position_log", bed_mm=tuple(session["bed_mm"]), layer_z_mm=layer_z,
        points=points, returns=returns, unit=session["unit"], firmware=session.get("firmware"),
    )
    _keep(session, adapter, doc)


def _observed_from_answers(session: dict[str, Any], adapter: Any, block: str, *, returns: bool) -> None:
    answers = session["answers"]
    zone = answers.get(f"{block}_zone")
    if zone is None:
        return
    measured = answers.get(f"{block}_measure")
    lift = answers.get(f"{block}_lift")
    layer_z = float(session.get(f"{block}_layer_z") or TRIGGER_LAYER * LAYER_HEIGHT_MM)
    if isinstance(measured, (list, tuple)) and len(measured) == 2:
        z_park = layer_z + float(lift or 0.0)
        points = [(float(measured[0]), float(measured[1]), z_park)]
        doc = bench.observation(
            session["printer_id"], block, "owner_measure", bed_mm=tuple(session["bed_mm"]), layer_z_mm=layer_z,
            points=[(-1.0, -1.0, layer_z)] + points, returns=returns,
            return_how=answers.get(f"{block}_back"), unit=session["unit"], firmware=session.get("firmware"),
        )
        # The first point is the start marker; the served reader keeps a
        # point at the block's own start relative.  Use the square's spot.
        doc["points"][0] = [float(session.get("square_x", 0.0)), float(session.get("square_y", 0.0)), layer_z]
    else:
        doc = bench.observation(
            session["printer_id"], block, "camera" if session.get("camera") else "owner_zone",
            bed_mm=tuple(session["bed_mm"]), layer_z_mm=layer_z, zones=[int(zone)], lift_mm=lift,
            returns=returns, return_how=answers.get(f"{block}_back"), unit=session["unit"],
            firmware=session.get("firmware"),
        )
    _keep(session, adapter, doc)


# ---------------------------------------------------------------------------
# The asks
# ---------------------------------------------------------------------------


def _ask_zone(session: dict[str, Any], adapter: Any, block: str, what: str) -> dict[str, Any]:
    return _reply(
        session, f"Where did the head stop {what}? Answer with the zone number from the picture.",
        pictures=[_zones_picture(session), _camera_frame(adapter)],
    )


def _ask_lift(session: dict[str, Any], what: str) -> dict[str, Any]:
    return _reply(
        session, f"About how high did it lift {what}, before it moved sideways: barely, a finger, or a hand span?",
        options=["barely", "a finger", "a hand span", "a number in mm"],
    )


def _payoff(session: dict[str, Any]) -> str:
    """What the session bought, true on this person's tier and printer.

    Every tier gets the verdict: the head kept clear of what is on the
    plate, and whether a second part fits.  WHERE it fits is the plan's
    tier, and starting it beside the first part also needs a quiet start
    Kiln has seen this printer run.  The community half is said only when
    the numbers go to Kiln, and says when they help anyone else: once
    enough owners agree and a person adopts them."""
    name = session.get("model_name") or session["printer_id"]
    if _has_pro():
        line = (f"Kiln now knows how your {name} moves, so it keeps the head clear of what's on the plate and can "
                "tell you where a second part fits")
        if "quiet_start" not in session["blanks"]:
            line += ", and can start it for you"
    else:
        line = (f"Kiln now knows how your {name} moves, so it keeps the head clear of what's on the plate and can "
                "tell you whether a second part fits")
    line += "."
    if _sharing():
        line += f" The numbers go to Kiln; once enough {name} owners agree, every {name} owner gets them."
    else:
        line += " The numbers stay on this machine."
    return line


def _sharing() -> bool:
    try:
        from kiln.heartbeat import _telemetry_enabled

        return bool(_telemetry_enabled())
    except Exception:  # noqa: BLE001
        return False


def _has_pro() -> bool:
    try:
        from kiln_pro.pro_gate import _TIER_CHECKERS  # type: ignore[import-not-found]

        return _TIER_CHECKERS["pro"]("") is None
    except Exception:  # noqa: BLE001
        return False


def _learned_lines(session: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for block, doc in session["observed"].items():
        if block == "head":
            h = doc["head_mm"]
            rod = "no bar over the plate" if h["rod_height"] is None else f"the lowest bar {h['rod_height']:g} mm above the nozzle"
            lines.append(f"head: {h['width']:g} mm wide, {rod}")
            continue
        if doc.get("points"):
            top = max(p[2] for p in doc["points"])
            last = doc["points"][-1]
            lines.append(f"{block.replace('_', ' ')}: the whole path, {len(doc['points'])} points, up to {top:g} mm, "
                         f"ending at X{last[0]:g} Y{last[1]:g}")
        else:
            lift = "an unknown lift" if doc.get("lift_mm") is None else f"a lift of at least {doc['lift_mm']:g} mm"
            back = " and came back over the print first" if doc.get("return_how") == "level" else ""
            lines.append(f"{block.replace('_', ' ')}: parks in zone {doc['zones'][0]} after {lift}{back}")
    return lines


def _unknown_lines(session: dict[str, Any]) -> list[str]:
    words = {"pause": "where it parks on a pause", "cancel": "where it parks on a cancel", "end": "where it parks when a print ends",
             "filament_change": "how it changes colour (no way to trigger one without unloading by hand)",
             "head": "the head's size", "z_travel": "how far it travels in Z", "quiet_start": "whether it can start quietly beside a part"}
    return [words.get(b, b) for b in session["blanks"] if b not in session["observed"]]


# ---------------------------------------------------------------------------
# The tool
# ---------------------------------------------------------------------------


def printer_bench(
    printer_name: str | None = None,
    answer: str | None = None,
    restart: bool = False,
) -> dict[str, Any]:
    """Help Kiln get to know your printer: a five-minute guided session, one ask at a time.

    Kiln judges where a second part can go on an occupied plate from a
    record of how each printer moves its head on its own.  Where that
    record has a blank -- a pause, cancel or end nobody has described, a
    head nobody has measured -- Kiln assumes the worst.  This session
    fills the blanks for THIS printer: it prints a coin-sized square,
    pauses, resumes, cancels and ends it, and watches where the head goes.
    A Klipper or Marlin printer says where its head is, so nothing about
    the moves is asked; a closed-firmware printer is asked which numbered
    zone the head stopped in (a picture is included) and about how high it
    lifted.  Numbers only ever leave the machine, and only while telemetry
    is on.  Runout is never tested.

    Call it with no ``answer`` to start or resume.  Every reply carries
    ``ask`` (one sentence -- relay it word for word), ``images`` to show,
    ``options`` when the answer is a choice, and ``waiting`` with
    ``check_again_in_seconds`` when Kiln is watching the print and there is
    nothing to ask yet.  Hand the person's answer back as ``answer``.  The
    test print is a real print: when the reply says so, show the file with
    ``visualize_model``, get a token with ``issue_preview_token`` and start
    it with ``run_quick_print`` -- the normal approval dialog shows, once.
    ``restart=True`` throws the session away and begins again.

    Args:
        printer_name: Which printer.  Omit for the default one.
        answer: The person's answer to the previous ask, verbatim.
        restart: Begin again from the first ask.

    Returns the next ask, or ``done`` with what Kiln learned, the payoff
    line, and what it still does not know.
    """
    import kiln.server as _srv
    from kiln import plate_state
    from kiln.registry import PrinterNotFoundError

    if err := _srv._check_auth("control"):
        return err
    try:
        try:
            adapter, target_name = _srv._resolve_control_target(printer_name)
        except PrinterNotFoundError:
            return _srv._unknown_printer_error(printer_name, "bench")
        unit = bench.unit_of(adapter)
        if not unit:
            return _srv._error_dict("This printer has no durable identity (no serial or address), so a session cannot be kept for it.",
                                    code="BENCH_NO_IDENTITY")
        session = None if restart else _load_session(unit)
        if session is None:
            printer_id = plate_state.declared_model_of(adapter)
            if not printer_id:
                return _srv._error_dict(
                    f"{target_name} was registered without a printer model, so Kiln cannot tell what its record lacks; "
                    "register it with printer_model set.", code="BENCH_NO_MODEL")
            blanks, verdict = bench.blanks_for(adapter, printer_id)
            if verdict is None:
                return _srv._error_dict(
                    "Kiln could not read this printer's record right now (the placement service did not answer), "
                    "so it cannot say what the session would teach; try again when online.", code="BENCH_NO_VERDICT")
            bed = bench.bed_of(printer_id)
            if not bed:
                return _srv._error_dict(f"Kiln's catalogue has no plate size for {printer_id}, so the zone drawing cannot be made.",
                                        code="BENCH_NO_BED")
            first = any(isinstance(r, dict) and r.get("code") == "PLACEMENT_UNKNOWN_PRINTER" for r in verdict.get("refusals") or [])
            if not blanks:
                bench.note_offer(unit, answer="done")
                return {"success": True, "step": "done", "printer_name": target_name, "blanks": [], "learned": [],
                        "ask": "", "images": [],
                        "message": f"Kiln already knows how {bench._display_name(printer_id)} moves its head; there is nothing to teach it."}
            asked = [b for b in blanks if b in bench.TEACHABLE]
            if not asked:
                return {"success": True, "step": "done", "printer_name": target_name, "blanks": blanks, "learned": [],
                        "ask": "", "images": [],
                        "message": (f"Kiln still doesn't know {', '.join(b.replace('_', ' ') for b in blanks)} for "
                                    f"the {bench._display_name(printer_id)}, but a test print can't show those, so "
                                    "there is nothing for a session to do.")}
            loggable = bench.can_log_positions(adapter)
            session = {
                "printer_name": target_name, "printer_id": printer_id, "unit": unit,
                "model_name": bench._display_name(printer_id), "bed_mm": list(bed), "blanks": blanks,
                "asked": asked, "first": first, "loggable": loggable, "camera": plate_state.camera_of(adapter) is not None,
                "firmware": bench.firmware_of(adapter), "plan": _plan(asked, loggable), "plan_index": 0,
                "step": "intro", "print_n": 0, "observed": {}, "answers": {}, "notes": [], "started_at": bench._now(),
            }
            for doc in bench.observations_of(adapter):
                if doc.get("block") in session["blanks"]:
                    session["observed"][doc["block"]] = doc
        return _advance(session, adapter, answer)
    except Exception as exc:  # noqa: BLE001
        _logger.exception("Unexpected error in printer_bench")
        return _srv._error_dict(f"Unexpected error in the bench session: {exc}", code="INTERNAL_ERROR")


def _advance(session: dict[str, Any], adapter: Any, answer: Any) -> dict[str, Any]:  # noqa: C901 -- one step per branch
    from kiln import plate_state

    step = session["step"]
    name = session["printer_name"]
    model = session.get("model_name") or session["printer_id"]

    # ---- intro ---------------------------------------------------------
    if step == "intro":
        if answer is None or not (_is_yes(answer) or _is_later(answer) or _is_no(answer)):
            return _reply(session, _intro(session), options=["yes", "later", "no"])
        if _is_later(answer):
            bench.note_offer(session["unit"], answer="later")
            _end_session(session)
            return {"success": True, "step": "later", "printer_name": name, "ask": "",
                    "message": "No problem. Call printer_bench whenever you have five minutes; Kiln won't bring it up again on its own."}
        if _is_no(answer):
            bench.note_offer(session["unit"], answer="declined")
            _end_session(session)
            return {"success": True, "step": "declined", "printer_name": name, "ask": "",
                    "message": "Understood. Kiln will keep assuming the worst for this printer and won't ask again."}
        bench.note_offer(session["unit"])
        _next_step(session)
        answer = None
        step = session["step"]

    # ---- the plate ------------------------------------------------------
    if step == "plate":
        if answer is None:
            state = plate_state.read(adapter)
            hint = "" if state.status == "clear" else (" The plate record says something is still on it." if state.status == "occupied" else "")
            return _reply(session, f"Is the plate empty?{hint}", pictures=[_camera_frame(adapter)], options=["yes", "no"])
        if not _is_yes(answer):
            return _reply(session, "Take everything off the plate and tell me when it's empty.", options=["it's empty"])
        plate_state.mark_clear(adapter, "human", note="bench session: the owner says the plate is empty")
        _next_step(session)
        answer = None
        step = session["step"]

    # ---- the tiny print ---------------------------------------------------
    if step == "print":
        state, job = _status(adapter)
        if answer is None or not _is_yes(answer):
            if state == "printing":
                session["printing_since"] = time.time()
                session["print_n"] = int(session.get("print_n", 0)) + 1
                _next_step(session)
                return _advance(session, adapter, None)
            path = session.get("print_file") or _write_square(session)
            return _reply(
                session, _print_ask(session),
                options=["yes"], print_file=path,
                how_to_start=(f"Show the file with visualize_model(model_path={path!r}), get a token with "
                              f"issue_preview_token, then run_quick_print(model_path={path!r}, printer_name={name!r}, "
                              "preview_token=...). The normal approval dialog shows. Then call printer_bench again "
                              "with answer=\"started\"."),
            )
        if state != "printing":
            return _reply(session, f"The printer isn't printing yet (it says {state}). Start the square, then tell me.",
                          options=["started"], print_file=session.get("print_file"), waiting=True)
        session["printing_since"] = time.time()
        session["print_n"] = int(session.get("print_n", 0)) + 1
        _next_step(session)
        answer = None
        step = session["step"]

    # ---- pause ------------------------------------------------------------
    if step == "pause":
        state, job = _status(adapter)
        if state != "printing":
            return _reply(session, f"Waiting for the square to print (the printer says {state}).", waiting=True)
        if not _far_enough(job, session, "pause"):
            return _reply(session, "The square is printing; Kiln will pause it a few layers up.", waiting=True)
        _start_log(session, adapter, "pause")
        session["pause_layer_z"] = _layer_z(job, session, None)
        session["pause_sent_at"] = time.time()
        sent = _door("pause_print", name)
        if not sent.get("success", False):
            return _reply(session, f"Kiln could not pause the print ({sent.get('error') or sent.get('message') or 'no answer'}); "
                          "pause it from the printer's own screen or app, then tell me.", options=["paused"])
        _next_step(session)
        answer = None
        step = session["step"]

    if step == "parked":
        state, job = _status(adapter)
        settled = time.time() - float(session.get("pause_sent_at") or 0) >= PARK_SETTLE_S
        if state != "paused" or not settled or not _log_done(session, "pause"):
            return _reply(session, "The head is parking; Kiln is watching where it goes.", waiting=True)
        if session["loggable"]:
            session["pause_layer_z"] = _layer_z(job, session, _log_for(session, "pause"))
        _next_step(session)
        answer = None
        step = session["step"]

    if step == "pause_zone":
        zone = bench.parse_zone(answer) if answer is not None else None
        if zone is None:
            return _ask_zone(session, adapter, "pause", "when it paused")
        session["answers"]["pause_zone"] = zone
        _next_step(session)
        answer = None
        step = session["step"]

    if step == "pause_lift":
        lift = bench.parse_lift(answer) if answer is not None else None
        if answer is None or lift is None:
            return _ask_lift(session, "when it paused")
        session["answers"]["pause_lift"] = lift
        _next_step(session)
        answer = None
        step = session["step"]

    if step == "pause_measure":
        if answer is None:
            return _reply(
                session,
                "Optional, to be exact: with a ruler, the nozzle's distance from the plate's left edge and from its "
                "front edge, in mm (a minus sign if it's outside), or say skip.",
                options=["skip"],
            )
        pair = _pair(answer)
        if pair is not None:
            session["answers"]["pause_measure"] = pair
        _next_step(session)
        answer = None
        step = session["step"]

    # ---- resume -----------------------------------------------------------
    if step == "resume":
        _start_log(session, adapter, "resume")
        session["resume_sent_at"] = time.time()
        sent = _door("resume_print", name)
        if not sent.get("success", False):
            return _reply(session, f"Kiln could not resume the print ({sent.get('error') or sent.get('message') or 'no answer'}); "
                          "resume it from the printer's own screen or app, then tell me.", options=["resumed"])
        _next_step(session)
        answer = None
        step = session["step"]

    if step == "resumed":
        state, job = _status(adapter)
        settled = time.time() - float(session.get("resume_sent_at") or 0) >= PARK_SETTLE_S
        if state not in ("printing", "idle") or not settled or not _log_done(session, "resume"):
            return _reply(session, "The head is coming back; Kiln is watching the way back.", waiting=True)
        if session["loggable"]:
            _observed_from_log(session, adapter, "pause", returns=True,
                               logs=[_log_for(session, "pause"), _log_for(session, "resume")],
                               layer_z=float(session.get("pause_layer_z") or 1.0))
        _next_step(session)
        answer = None
        step = session["step"]

    if step == "resume_back":
        text = _norm(answer)
        if not text or not any(w in text for w in ("over", "first", "while", "travel", "diagon", "straight", "down")):
            return _reply(
                session,
                "When it came back, did the head move back over the square first and then come down, or come down while it travelled?",
                options=["over the square first", "down while travelling"],
            )
        session["answers"]["pause_back"] = "level" if ("over" in text or "first" in text) and "while" not in text else "diagonal"
        _observed_from_answers(session, adapter, "pause", returns=True)
        _next_step(session)
        answer = None
        step = session["step"]

    # ---- end --------------------------------------------------------------
    if step == "end":
        state, job = _status(adapter)
        completion = getattr(job, "completion", None) if job is not None else None
        if state == "printing":
            if isinstance(completion, (int, float)) and completion >= 85.0:
                _start_log(session, adapter, "end", max_s=900.0, settle_s=6.0,
                           until_idle=lambda: _status(adapter)[0] == "idle")
                session["end_layer_z"] = SQUARE_MM[2]
            return _reply(session, "Kiln is letting the square finish and will watch where the head goes at the end.", waiting=True)
        if state != "idle":
            return _reply(session, f"Waiting for the print to end (the printer says {state}).", waiting=True)
        session.setdefault("ended_at", time.time())
        _next_step(session)
        answer = None
        step = session["step"]

    if step == "ended":
        settled = time.time() - float(session.get("ended_at") or 0) >= PARK_SETTLE_S
        if not settled or not _log_done(session, "end"):
            return _reply(session, "The print has ended; Kiln is watching the head settle.", waiting=True)
        if session["loggable"]:
            _observed_from_log(session, adapter, "end", returns=False, logs=[_log_for(session, "end")],
                               layer_z=float(session.get("end_layer_z") or SQUARE_MM[2]))
        _next_step(session)
        answer = None
        step = session["step"]

    if step == "end_zone":
        zone = bench.parse_zone(answer) if answer is not None else None
        if zone is None:
            return _ask_zone(session, adapter, "end", "when the print ended")
        session["answers"]["end_zone"] = zone
        session["end_layer_z"] = SQUARE_MM[2]
        _next_step(session)
        answer = None
        step = session["step"]

    if step == "end_lift":
        lift = bench.parse_lift(answer) if answer is not None else None
        if answer is None or lift is None:
            return _ask_lift(session, "at the end")
        session["answers"]["end_lift"] = lift
        _observed_from_answers(session, adapter, "end", returns=False)
        _next_step(session)
        answer = None
        step = session["step"]

    # ---- cancel -----------------------------------------------------------
    if step == "cancel_quietly":
        state, _job = _status(adapter)
        if state in ("printing", "paused"):
            _door("cancel_print", name)
            return _reply(session, "Kiln is stopping the square; it already knows what this printer does on a cancel.", waiting=True)
        _next_step(session)
        answer = None
        step = session["step"]

    if step == "cancel":
        state, job = _status(adapter)
        if state == "paused":
            pass
        elif state != "printing":
            return _reply(session, f"Waiting for the square to print (the printer says {state}).", waiting=True)
        elif not _far_enough(job, session, "cancel"):
            return _reply(session, "The square is printing; Kiln will cancel it a few layers up.", waiting=True)
        _start_log(session, adapter, "cancel")
        session["cancel_layer_z"] = _layer_z(job, session, None)
        session["cancel_sent_at"] = time.time()
        sent = _door("cancel_print", name)
        if not sent.get("success", False):
            return _reply(session, f"Kiln could not cancel the print ({sent.get('error') or sent.get('message') or 'no answer'}); "
                          "cancel it from the printer's own screen or app, then tell me.", options=["cancelled"])
        _next_step(session)
        answer = None
        step = session["step"]

    if step == "cancelled":
        state, job = _status(adapter)
        settled = time.time() - float(session.get("cancel_sent_at") or 0) >= PARK_SETTLE_S
        if state not in ("idle", "error") or not settled or not _log_done(session, "cancel"):
            return _reply(session, "The print is cancelling; Kiln is watching where the head goes.", waiting=True)
        if session["loggable"]:
            _observed_from_log(session, adapter, "cancel", returns=False, logs=[_log_for(session, "cancel")],
                               layer_z=float(session.get("cancel_layer_z") or 1.0))
        _next_step(session)
        answer = None
        step = session["step"]

    if step == "cancel_zone":
        zone = bench.parse_zone(answer) if answer is not None else None
        if zone is None:
            return _ask_zone(session, adapter, "cancel", "after the cancel")
        session["answers"]["cancel_zone"] = zone
        _next_step(session)
        answer = None
        step = session["step"]

    if step == "cancel_lift":
        lift = bench.parse_lift(answer) if answer is not None else None
        if answer is None or lift is None:
            return _ask_lift(session, "on the cancel")
        session["answers"]["cancel_lift"] = lift
        _observed_from_answers(session, adapter, "cancel", returns=False)
        _next_step(session)
        answer = None
        step = session["step"]

    # ---- the plate again ----------------------------------------------------
    if step == "clear":
        if answer is None or not _is_yes(answer):
            return _reply(session, "Take the square off the plate and tell me when it's empty.", options=["it's empty"])
        plate_state.mark_clear(adapter, "human", note="bench session: the owner took the test square off")
        _next_step(session)
        # The next step may be the second print, whose branch sits above
        # this one: go round again with no answer.
        return _advance(session, adapter, None)

    # ---- the head ---------------------------------------------------------
    if step == "head_width":
        value = _mm(answer)
        if value is None or value < 1.0:
            return _reply(session, "With calipers: the widest part of the head, left to right, in mm (A in the picture)?",
                          pictures=[_picture("head", bench.draw_head_sketch(), "the head from the front: A is the width, B the nozzle-to-bar height")])
        session["answers"]["head_width"] = value
        _next_step(session)
        answer = None
        step = session["step"]

    if step == "head_rod":
        text = _norm(answer)
        value = _mm(answer)
        if not text or (value is None and "none" not in text and "no bar" not in text):
            return _reply(session, "From the nozzle tip up to the lowest bar that crosses the plate, in mm (B in the picture), or 'none'?",
                          pictures=[_picture("head", bench.draw_head_sketch(), "B: nozzle tip up to the lowest bar")])
        rod = None if value is None else value
        doc = bench.head_observation(session["printer_id"], session["answers"]["head_width"], rod,
                                     unit=session["unit"], firmware=session.get("firmware"))
        _keep(session, adapter, doc)
        _next_step(session)
        answer = None
        step = session["step"]

    # ---- done -------------------------------------------------------------
    if step == "done":
        bench.note_offer(session["unit"], answer="done")
        _end_session(session)
        _send_in_background()
        learned = _learned_lines(session)
        unknown = _unknown_lines(session)
        payoff = _payoff(session) if learned else ""
        return {
            "success": True, "step": "done", "printer_name": name, "ask": "", "images": [],
            "learned": learned, "still_unknown": unknown, "notes": session.get("notes", []),
            "payoff": payoff,
            "first": bool(session.get("first")),
            "message": (("Kiln had no record for this printer; now it has yours. " if session.get("first") and learned else "")
                        + ("Learned: " + "; ".join(learned) + ". " + payoff if learned
                           else "Nothing new was observed this time, so Kiln still assumes the worst for this printer.")
                        + (" Still unknown: " + "; ".join(unknown) + "." if unknown else "")),
            "kept_at": str(bench.bench_dir()),
        }

    return _reply(session, f"Kiln lost its place at step {step!r}; call printer_bench with restart=true to begin again.")


def _mm(answer: Any) -> float | None:
    import re

    m = re.search(r"(-?\d+(?:\.\d+)?)", str(answer or ""))
    return float(m.group(1)) if m else None


def _pair(answer: Any) -> tuple[float, float] | None:
    import re

    found = re.findall(r"(-?\d+(?:\.\d+)?)", str(answer or ""))
    if len(found) >= 2:
        return (float(found[0]), float(found[1]))
    return None


def _send_in_background() -> None:
    try:
        from kiln.heartbeat import _SUPABASE_ANON_KEY, _SUPABASE_URL

        threading.Thread(target=bench.send_pending, args=(_SUPABASE_URL, _SUPABASE_ANON_KEY),
                         name="bench-send", daemon=True).start()
    except Exception:  # noqa: BLE001
        _logger.debug("bench: send not started", exc_info=True)


class _PrinterBenchPlugin:
    """Help Kiln get to know your printer.

    Tools:
        - printer_bench
    """

    @property
    def name(self) -> str:
        return "printer_bench_tools"

    @property
    def description(self) -> str:
        return "A five-minute guided session that teaches Kiln how this printer moves its head on its own"

    def register(self, mcp: Any) -> None:
        import kiln.server as _srv

        for tool_name, limits in _RATE_LIMITS.items():
            _srv._TOOL_RATE_LIMITS.setdefault(tool_name, limits)
        mcp.tool()(printer_bench)


plugin = _PrinterBenchPlugin()
