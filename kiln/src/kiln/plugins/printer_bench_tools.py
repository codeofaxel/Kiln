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

A second lesson teaches Kiln what THIS printer can really print in one
plastic: how steep, how far and how thin.  Kiln's servers make a small test
print for it (:mod:`kiln._pro_limits_bridge`); the person prints it through
the same approval dialog, and reads a few numbers raised on it.  It is
offered after the first lesson, or when the first has nothing to teach, and
``lesson="limits"`` starts it on its own.  It needs a free Kiln account,
where the test print and the readings are kept.

A third lesson learns a home or a park with the person, on a printer Kiln
cannot yet home or park itself (:mod:`kiln.printers.motion_walk`): one small
step at a time, each sent only after the person says the one before did what
Kiln said it would.  The refusal that offers it does so only when Kiln has a
walk on offer for that printer and move; ``lesson="walk"`` starts it.

Auto-discovered by :func:`~kiln.plugin_loader.register_all_plugins`.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

from kiln import bench
from kiln.tool_annotations import destructive

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
    with contextlib.suppress(OSError):
        _session_path(session["unit"]).unlink()


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
            what += ", and you measure the head with calipers or a ruler"
        text = f"{lead}Help Kiln get to know your {model} in a few minutes: {what}."
        text += (" Kiln reads where the head goes from the printer itself." if session["loggable"]
                 else " Kiln will ask you where the head stopped, with a picture.")
    else:
        text = f"{lead}Help Kiln get to know your {model}: two measurements of its print head, with calipers or a ruler."
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
        "options": options or [], "waiting": waiting,
        "learned": (list(session.get("learned") or []) if session.get("lesson") in (LIMITS, WALK)
                    else sorted(session["observed"])),
        "images": shown,
        "next": (f"Call printer_bench again in about {int(CHECK_AGAIN_S)} seconds.") if waiting
        else "Relay the ask word for word, show the picture, and call printer_bench again with the person's answer.",
    }
    if waiting:
        out["check_again_in_seconds"] = CHECK_AGAIN_S
    out.update(extra)
    return out


def _with_pictures(reply: dict[str, Any]) -> dict[str, Any] | list[Any]:
    """A reply as the tool hands it over: the text, then its pictures as images.

    The pictures never ride the text.  A camera frame is ~220,000
    characters of base64, and a host that reads results as text refuses a
    result that size whole -- the ask beside it lost too (measured on the
    monitor door).  The text keeps what each picture is (its
    kind, caption and type, in order); the pictures follow it as images.
    """
    shown = reply.get("images") or []
    if not shown:
        return reply
    import base64

    from kiln.mcp_compat import Image

    pictures = []
    for picture in shown:
        data = base64.b64decode(picture.pop("image_b64"))
        pictures.append(Image(data=data, format=(picture.get("media_type") or "image/jpeg").split("/")[-1]))
    return [reply, *pictures]


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
            x, y, z = v[i]
            lines.append(f"      vertex {x:.3f} {y:.3f} {z:.3f}")
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
    if bench.keep_observation(adapter, doc) is not None:
        session["kept"] = int(session.get("kept") or 0) + 1


def _observed_from_log(session: dict[str, Any], adapter: Any, block: str, *, returns: bool,
                       logs: list[Any], layer_z: float) -> None:
    """Write *block*'s observation from its logs -- only when every log ran
    to a clean finish and the head really moved.  A log cut short may have
    missed where the head went last, and one that saw no movement would
    read as a block that never leaves the part: either would make a
    verdict looser than the machine, so neither is written.

    A printer that reports its extruder too is read the way a pause in
    real use is (:func:`kiln.bench.logged_block`): the square's last lines,
    printed after the command went out, are left out, and so is the print
    after the resume, so the climb is seen from where the head stopped.
    When the log cannot tell the print from the block, it is kept whole,
    as before, which reads the head no higher than the layer."""
    if not logs or any(log is None or not getattr(log, "complete", False) for log in logs):
        session.setdefault("notes", []).append(
            f"{block}: the position log was cut short before the head settled, so nothing was written for it")
        return
    points = [p for log in logs for p in log.points]
    if any(e is not None for log in logs for e in (getattr(log, "extruder", None) or [])):
        found = bench.logged_block(logs, bench.print_footprint(adapter), returns=returns)
        if isinstance(found, str):
            session.setdefault("notes", []).append(f"{block}: {found}, so the whole log was kept")
        else:
            points, layer_z = found
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
    return bench.sharing()


def _has_pro() -> bool:
    """True when the caller's plan is Pro or above (:func:`kiln.bench.has_pro`,
    the one reading the offers use too)."""
    return bench.has_pro()


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


# ---------------------------------------------------------------------------
# The tool
# ---------------------------------------------------------------------------


def printer_bench(
    printer_name: str | None = None,
    answer: str | None = None,
    restart: bool = False,
    lesson: str | None = None,
    gcode_paths: list[str] | None = None,
    move: str | None = None,
) -> dict[str, Any] | list[Any]:
    """Help Kiln get to know your printer in a few minutes: a guided session, one ask at a time.

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
    ``ask`` (one sentence -- relay it word for word), ``images`` (what each
    picture is; the pictures themselves follow the reply as images, in the
    same order -- show them), ``options`` when the answer is a choice, and ``waiting`` with
    ``check_again_in_seconds`` when Kiln is watching the print and there is
    nothing to ask yet.  Hand the person's answer back as ``answer``.  The
    test print is a real print: when the reply says so, show the file with
    ``visualize_model``, get a token with ``issue_preview_token`` and start
    it with ``run_quick_print`` -- the normal approval dialog shows, once.
    ``restart=True`` throws the session away and begins again.

    A second lesson teaches Kiln how steep, how far and how thin this
    printer prints in one plastic, from a small test print the person reads
    a few numbers off.  It is offered when the first lesson ends or has
    nothing to teach; ``lesson="limits"`` starts it on its own.  Kiln makes
    the test print; slice it as the reply's ``how_to_slice`` says and hand
    the sliced files back as ``gcode_paths`` (Kiln reads them before it asks
    anything), then print each plate as ``how_to_start`` says.  It needs a
    free Kiln account, where the test print and the readings are kept.

    A third lesson learns a home or a park with the person, one small step
    at a time, on a printer Kiln cannot yet home or park itself.  A refusal
    from ``home_axes`` or ``park_head`` offers it only when Kiln has one on
    offer for that printer; ``lesson="walk"`` with ``move`` starts it.  Kiln
    sends one step, says what the person should see, and sends the next only
    after the person says the printer did exactly that.  The printer stays
    cold, and its own stop, pause and power switch work at every moment.  It
    needs a free Kiln account, and is the same on every plan.

    Args:
        printer_name: Which printer.  Omit for the default one.
        answer: The person's answer to the previous ask, verbatim.
        restart: Begin again from the first ask.
        lesson: ``"motion"`` (how the printer moves its head),
            ``"limits"`` (what it can really print) or ``"walk"`` (a home
            or a park learned with the person, one step at a time).  Omit
            to start or resume whichever is due.
        gcode_paths: The second lesson's sliced files, one per plate, in
            plate order, when its reply asks for them.
        move: ``"home"`` or ``"park"``: the move a walk learns.

    Returns the next ask, or ``done`` with what Kiln learned and the payoff
    line.
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
        wanted = str(lesson or "").strip().lower() or None
        moving = str(move or "").strip().lower() or None
        if wanted is None and moving is not None:
            wanted = WALK
        if wanted not in (None, MOTION, LIMITS, WALK):
            return _srv._error_dict("lesson must be 'motion', 'limits' or 'walk', or left out.", code="INVALID_ARGS")
        if wanted == WALK and moving is not None and moving not in _walk_verbs():
            return _srv._error_dict("A walk is for a home or a park: move must be 'home' or 'park'.",
                                    code="INVALID_ARGS")
        session = None if restart else _load_session(unit)
        if session is not None and wanted and session.get("lesson", MOTION) != wanted:
            session = None
        if session is not None and wanted == WALK and moving is not None and session.get("move") != moving:
            session = None
        if session is None and wanted == WALK:
            if moving is None:
                return _srv._error_dict("A walk is for a home or a park: say which with move='home' or move='park'.",
                                        code="INVALID_ARGS")
            session = _walk_session(target_name, adapter, unit, moving)
            if session is None:
                return _walk_not_on_offer(target_name, adapter, moving)
        if session is None and wanted == LIMITS:
            session = _limits_session(target_name, adapter, unit, plate_state.declared_model_of(adapter))
            if session is None:
                return _srv._error_dict(_NO_OWN_NAME, code="BENCH_NO_PRINTER_NAME")
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
            hold = bench.session_hold(verdict)
            if hold is not None:
                # The service's own sentence, said as written, and the one
                # thing to do next.  Nothing is marked: the session starts
                # the next time it is asked for.
                tool = str(hold.get("tool") or "")
                bench.count("held", MOTION, bench.hold_kind(hold.get("code")))
                return {
                    "success": True, "step": "held", "printer_name": target_name, "hold": str(hold["code"]),
                    "ask": str(hold["sentence"]), "images": [], "learned": [],
                    "retryable": bool(hold.get("retryable")),
                    **({"upgrade_url": hold["upgrade_url"]} if hold.get("upgrade_url") else {}),
                    "next": (f"Relay the ask word for word. Then call `{tool}`, and call printer_bench again." if tool
                             else "Relay the ask word for word. Call printer_bench again when it can start."),
                }
            bed = bench.bed_of(printer_id)
            if not bed:
                return _srv._error_dict(f"Kiln's catalogue has no plate size for {printer_id}, so the zone drawing cannot be made.",
                                        code="BENCH_NO_BED")
            first = any(isinstance(r, dict) and r.get("code") == "PLACEMENT_UNKNOWN_PRINTER" for r in verdict.get("refusals") or [])
            asked = [b for b in blanks if b in bench.TEACHABLE]
            if not blanks:
                bench.note_offer(unit, answer="done")
            if not asked:
                name = bench._display_name(printer_id)
                lead = (f"Kiln already knows how the {name} moves its head. " if not blanks
                        else f"There is nothing more for a session to teach Kiln about how the {name} moves. ")
                follow = _limits_session(target_name, adapter, unit, printer_id, lead=lead)
                if follow is not None and limits_may_be_offered(unit):
                    return _with_pictures(_advance(follow, adapter, answer, gcode_paths))
                return {"success": True, "step": "done", "printer_name": target_name, "learned": [],
                        "ask": "", "images": [],
                        "message": (f"Kiln already knows how {name} moves its head; there is nothing to teach it." if not blanks
                                    else f"A test print has nothing more to teach Kiln about the {name}, "
                                         "so there is nothing for a session to do.")}
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
        return _with_pictures(_advance(session, adapter, answer, gcode_paths))
    except Exception as exc:  # noqa: BLE001
        _logger.exception("Unexpected error in printer_bench")
        return _srv._error_dict(f"Unexpected error in the bench session: {exc}", code="INTERNAL_ERROR")


def _advance(session: dict[str, Any], adapter: Any, answer: Any,
             gcode_paths: list[str] | None = None) -> dict[str, Any]:  # noqa: C901 -- one step per branch
    from kiln import plate_state

    if session.get("lesson") == LIMITS:
        return _advance_limits(session, adapter, answer, gcode_paths)
    if session.get("lesson") == WALK:
        return _advance_walk(session, adapter, answer)
    step = session["step"]
    name = session["printer_name"]

    # ---- intro ---------------------------------------------------------
    if step == "intro":
        if answer is None or not (_is_yes(answer) or _is_later(answer) or _is_no(answer)):
            return _reply(session, _intro(session), options=["yes", "later", "no"])
        if _is_later(answer):
            bench.note_offer(session["unit"], answer="later")
            bench.count("later", MOTION)
            _end_session(session)
            return {"success": True, "step": "later", "printer_name": name, "ask": "",
                    "message": "No problem. Call printer_bench whenever you have a few minutes; Kiln won't bring it up again on its own."}
        if _is_no(answer):
            bench.note_offer(session["unit"], answer="declined")
            bench.count("declined", MOTION)
            _end_session(session)
            return {"success": True, "step": "declined", "printer_name": name, "ask": "",
                    "message": "Understood. Kiln will keep assuming the worst for this printer and won't ask again."}
        bench.note_offer(session["unit"])
        bench.count("started", MOTION)
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
            return _reply(session, "With calipers or a ruler: the widest part of the head, left to right, in mm (A in the picture)? "
                          "Between two marks, give the bigger one.",
                          pictures=[_picture("head", bench.draw_head_sketch(), "the head from the front: A is the width, B the nozzle-to-bar height")])
        session["answers"]["head_width"] = value
        _next_step(session)
        answer = None
        step = session["step"]

    if step == "head_rod":
        text = _norm(answer)
        value = _mm(answer)
        if not text or (value is None and "none" not in text and "no bar" not in text):
            return _reply(session, "From the nozzle tip up to the lowest bar that crosses the plate, in mm (B in the picture), or 'none'? "
                          "Between two marks, give the smaller one.",
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
        bench.count("finished", MOTION)
        if session.get("kept"):
            bench.count("learned", MOTION, "session")
        _end_session(session)
        _send_in_background()
        learned = _learned_lines(session)
        payoff = _payoff(session) if learned else ""
        done = {
            "success": True, "step": "done", "printer_name": name, "ask": "", "images": [],
            "learned": learned, "notes": session.get("notes", []),
            "payoff": payoff,
            "first": bool(session.get("first")),
            "message": (("Kiln had no record for this printer; now it has yours. " if session.get("first") and learned else "")
                        + ("Learned: " + "; ".join(learned) + ". " + payoff if learned
                           else "Nothing new was observed this time, so Kiln still assumes the worst for this printer.")),
            "kept_at": str(bench.bench_dir()),
        }
        follow = _limits_session(name, adapter, session["unit"], session["printer_id"], after_motion=True)
        if follow is not None and bench.offer(session["unit"], ["limits"], "lesson",
                                              model_name=follow["model_name"]) is not None:
            # The second lesson, offered once the first is done: a new
            # session, so the person's answer to it continues there.
            _save_session(follow)
            done.update(ask=_limits_intro(follow), options=["yes", "later", "no"], lesson_offered=LIMITS,
                        next=("Relay the message, then the ask word for word, and call printer_bench again "
                              "with the person's answer."))
        return done

    return _reply(session, f"Kiln lost its place at step {step!r}; call printer_bench with restart=true to begin again.")


# ---------------------------------------------------------------------------
# The second lesson: what this printer can really print
# ---------------------------------------------------------------------------

#: The lessons a session can be.
MOTION = bench.MOTION
LIMITS = bench.LIMITS
WALK = bench.WALK
#: The test print the second lesson starts with; Kiln's servers name the other.
FIRST_COUPON = "angles_and_spans"
_MATERIAL_OPTIONS = ["PLA", "PETG", "ABS", "ASA", "TPU"]
_NOZZLE_OPTIONS = ["0.2", "0.4", "0.6", "0.8"]
_PRINTED = {"printed", "done", "finished", "it's done", "its done", "it finished", "already printed", "printed it"}
_GCODE_SUFFIXES = (".gcode", ".gco", ".g")


def _limits_key(unit: str) -> str:
    """The second lesson's own line in the offers ledger."""
    return f"limits:{unit}"


def limits_may_be_offered(unit: str | None) -> bool:
    """Whether the second lesson may be offered to *unit*: not after the
    person declined it or already ran it."""
    return bool(unit) and bench.may_offer(_limits_key(str(unit)), "lesson")


#: Said when the printer has no name of its own to keep what it learns under.
_NO_OWN_NAME = ("Kiln keeps what a printer's test print teaches under the printer's own name, and this one is "
                "only Kiln's default. Give it a name with rename_printer, then call printer_bench again.")


def _own_name(name: str) -> str | None:
    """The name the person gave this printer, which is what its test prints
    are kept under: never the ``default`` Kiln falls back to."""
    from kiln.callers_printers import UNNAMED

    if str(name or "").strip().lower() not in UNNAMED:
        return str(name).strip()
    try:
        from kiln.printer_model_resolver import resolve_active_printer_name

        return resolve_active_printer_name()
    except Exception:  # noqa: BLE001 -- no name read is no name
        return None


def _limits_session(name: str, adapter: Any, unit: str, printer_id: str | None, *,
                    after_motion: bool = False, lead: str = "") -> dict[str, Any] | None:
    """A new second-lesson session, or ``None`` when the printer has no name
    of its own to keep what it learns under."""
    own = _own_name(name)
    if not own:
        return None
    bed = bench.bed_of(printer_id) if printer_id else None
    return {
        "lesson": LIMITS, "printer_name": name, "own_name": own, "printer_id": printer_id or "", "unit": unit,
        "model_name": bench._display_name(printer_id) if printer_id else name,
        "bed_mm": list(bed) if bed else None, "firmware": bench.firmware_of(adapter),
        "step": "limits_intro", "after_motion": after_motion, "lead": lead, "coupon": FIRST_COUPON,
        "observed": {}, "learned": [], "readings": {}, "started_at": bench._now(),
    }


def _limits_intro(session: dict[str, Any]) -> str:
    also = "also " if session.get("after_motion") else ""
    return (f"{session.get('lead') or ''}Kiln can {also}learn how steep, how far and how thin your "
            f"{session['model_name']} really prints: it makes a small test print, you print it, and you read a few "
            "numbers off it. Ready?")


def _limits_held(session: dict[str, Any]) -> dict[str, Any]:
    """The second lesson waits for a sign-in: nothing is marked, and the
    session starts where it stopped once the person is signed in."""
    _save_session(session)
    bench.count("held", LIMITS, "signin")
    return {
        "success": True, "step": "held", "lesson": LIMITS, "printer_name": session["printer_name"],
        "hold": "LIMITS_ACCOUNT_REQUIRED", "images": [], "learned": list(session.get("learned") or []),
        "ask": ("Sign in to Kiln to teach it what your printer can print: the test print and what you read off it "
                "are kept in your Kiln account, which is free and takes a minute."),
        "retryable": False,
        "next": "Relay the ask word for word. Then call `kiln_signin`, and call printer_bench again.",
    }


def _limits_missed(session: dict[str, Any], miss: Any, cannot: str) -> dict[str, Any]:
    """No answer from Kiln's servers: said once, in the session's words, and
    the session stays on the same step so it can be asked again."""
    from kiln import _pro_limits_bridge as bridge
    from kiln import served_answer

    return _reply(session, bridge.miss_sentence(miss, cannot=cannot), options=["try again"],
                  retryable=bool(getattr(miss, "no_answer", False)), **served_answer.fields(miss))


def _limits_done(session: dict[str, Any], message: str) -> dict[str, Any]:
    bench.note_offer(_limits_key(session["unit"]), answer="done")
    bench.count("finished", LIMITS)
    if session.get("learned"):
        bench.count("learned", LIMITS, "session")
    _end_session(session)
    return {"success": True, "step": "done", "lesson": LIMITS, "printer_name": session["printer_name"],
            "ask": "", "images": [], "learned": list(session.get("learned") or []), "message": message}


def _nozzle_of(session: dict[str, Any]) -> float | None:
    """The nozzle the test print is made for: the one the person said, else
    the one Kiln finds for the printer; ``None`` when only a default is left,
    and then the person is asked."""
    if session.get("nozzle_mm"):
        return float(session["nozzle_mm"])
    try:
        from kiln.assumed_nozzle import assumed_nozzle

        found = assumed_nozzle(session["printer_name"])
    except Exception:  # noqa: BLE001 -- unread: ask the person
        return None
    return None if found.source == "default" else float(found.diameter_mm)


def _layer_for(nozzle: float) -> float:
    """The layer Kiln slices this nozzle at, the one the test print is made for."""
    from kiln.printability import _fitted_layer_height

    return float(_fitted_layer_height(nozzle))


def _slice_overrides(session: dict[str, Any]) -> dict[str, str]:
    """The slicer settings the test print is sliced and printed with: what
    the servers' ``slice_with`` asks, as slicer keys."""
    asked = session.get("slice_with") or {}
    layer = asked.get("layer_height_mm") or session.get("layer_height_mm")
    out: dict[str, str] = {}
    if asked.get("supports", "off") == "off":
        out["support_material"] = "0"
    if asked.get("hole_compensation", "off") == "off":
        out["xy_size_compensation"] = "0"
    if layer:
        out["layer_height"] = f"{float(layer):g}"
    return out


def _plate_word(session: dict[str, Any], number: int) -> str:
    plates = session.get("plates") or []
    if len(plates) <= 1:
        return "the test print"
    code = next((str(p.get("code")) for p in plates if int(p.get("plate") or 0) == number), str(number))
    return f"plate {code}"


def _gcode_of(path: str) -> str | None:
    """A sliced file Kiln can read: the G-code itself, or the G-code inside
    a printer's 3MF, written beside the session.  ``None`` when it is neither."""
    import zipfile

    local = Path(str(path)).expanduser()
    if not local.is_file():
        return None
    if local.suffix.lower() in _GCODE_SUFFIXES:
        return str(local)
    if local.suffix.lower() != ".3mf":
        return None
    try:
        with zipfile.ZipFile(local) as archive:
            inside = sorted(n for n in archive.namelist() if n.lower().endswith(".gcode"))
            if not inside:
                return None
            out = bench.bench_dir() / "limits" / f"{local.stem.replace('.gcode', '')}.gcode"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(archive.read(inside[0]))
            return str(out)
    except (OSError, zipfile.BadZipFile, KeyError):
        return None


def _questions_now(session: dict[str, Any]) -> list[dict[str, Any]]:
    """The questions to ask now: every one when the whole test print is to
    be read again, else the ones whose last plate has just printed."""
    questions = session.get("questions") or []
    if session.get("all_printed"):
        return questions
    here = int(session.get("plate_n") or 1)
    return [q for q in questions if max(int(p) for p in (q.get("plates") or [q.get("plate") or 1])) == here]


def _question_ask(session: dict[str, Any], question: dict[str, Any]) -> str:
    where = f"On the rows headed {question.get('heading')}"
    if len(session.get("plates") or []) > 1:
        where += " on " + " and ".join(_plate_word(session, int(p)) for p in (question.get("plates") or [1]))
    said = str(question.get("question") or "")
    said = said[:1].lower() + said[1:]
    return f"{where}, {said} {question.get('good_means')} Answer with the number raised beside it, or none, or all."


def _reading_of(answer: Any, steps: list[Any]) -> float | str | None:
    text = _norm(answer)
    if text in ("none", "none of them"):
        return "none"
    if text in ("all", "all of them"):
        return "all"
    value = _mm(answer)
    if value is None:
        return None
    return next((float(step) for step in steps if abs(float(step) - value) < 1e-6), None)


def _start_questions(session: dict[str, Any], *, every: bool = False) -> None:
    session["all_printed"] = every
    session["q_index"] = 0
    session["step"] = "limits_question" if _questions_now(session) else "limits_cleared"


def _advance_limits(session: dict[str, Any], adapter: Any, answer: Any,  # noqa: C901 -- one step per branch
                    gcode_paths: list[str] | None) -> dict[str, Any]:
    from kiln import _pro_limits_bridge as bridge

    step = session["step"]
    name = session["printer_name"]
    own = session.get("own_name") or _own_name(name) or name

    # ---- the offer ---------------------------------------------------------
    if step == "limits_intro":
        if answer is None or not (_is_yes(answer) or _is_later(answer) or _is_no(answer)):
            return _reply(session, _limits_intro(session), options=["yes", "later", "no"])
        if _is_later(answer):
            bench.note_offer(_limits_key(session["unit"]), answer="later")
            bench.count("later", LIMITS)
            _end_session(session)
            return {"success": True, "step": "later", "lesson": LIMITS, "printer_name": name, "ask": "",
                    "message": ("No problem. Call printer_bench with lesson=\"limits\" whenever you have time for "
                                "a test print; Kiln won't bring it up again on its own.")}
        if _is_no(answer):
            bench.note_offer(_limits_key(session["unit"]), answer="declined")
            bench.count("declined", LIMITS)
            _end_session(session)
            return {"success": True, "step": "declined", "lesson": LIMITS, "printer_name": name, "ask": "",
                    "message": "Understood. Kiln won't ask about a test print for this printer again."}
        bench.note_offer(_limits_key(session["unit"]))
        bench.count("started", LIMITS)
        session["step"] = step = "limits_material"
        answer = None

    if step in ("limits_material", "limits_nozzle", "limits_make") and not bridge.signed_in():
        return _limits_held(session)

    # ---- the plastic and the nozzle -----------------------------------------
    if step == "limits_material":
        text = " ".join(str(answer or "").split())
        if not text or len(text) > 80:
            return _reply(session, "Which plastic will you print the test print in? Say it the way the spool does.",
                          options=_MATERIAL_OPTIONS)
        session["material"] = text
        session["step"] = step = "limits_nozzle"
        answer = None

    if step == "limits_nozzle":
        if not session.get("nozzle_mm"):
            said = _mm(answer) if answer is not None else None
            if said is not None and 0.1 <= said <= 2.0:
                session["nozzle_mm"] = said
            else:
                found = _nozzle_of(session)
                if found is None:
                    return _reply(session, "Which nozzle size is on the printer, in mm? 0.4 is the most common.",
                                  options=_NOZZLE_OPTIONS)
                session["nozzle_mm"] = found
        session["step"] = step = "limits_make"
        answer = None

    # ---- the test print, made on Kiln's servers ------------------------------
    if step == "limits_make":
        nozzle = float(session["nozzle_mm"])
        session["layer_height_mm"] = _layer_for(nozzle)
        made, miss = bridge.ask(
            "make", printer_id=own, material=session["material"], coupon=session["coupon"],
            nozzle_mm=nozzle, layer_height_mm=session["layer_height_mm"], firmware=session.get("firmware"),
            printer_model=session.get("printer_id") or None, bed_mm=session.get("bed_mm"),
        )
        if miss is not None:
            return _limits_missed(session, miss, "make the test print")
        if made.get("status") != "ok":
            code, said = str(made.get("code") or ""), str(made.get("error") or "")
            if code in ("UNKNOWN_MATERIAL", "INVALID_INPUT"):
                session["step"] = "limits_material"
                return _reply(session, f"{said} Which plastic will you print it in?", options=_MATERIAL_OPTIONS)
            if code == "NOZZLE_NEEDED":
                session.pop("nozzle_mm", None)
                session["step"] = "limits_nozzle"
                return _reply(session, f"{said} Which size is it, in mm?", options=_NOZZLE_OPTIONS)
            return _reply(session, said or "Kiln's servers could not make the test print.", options=["try again", "later"])
        plates = [p for p in made.get("plates") or [] if isinstance(p, dict)]
        if not plates or not all(Path(str(p.get("stl_path") or "")).is_file() for p in plates):
            from kiln.served_answer import Miss

            return _limits_missed(session, Miss("unanswered", detail="the test print's files did not arrive"),
                                  "bring the test print to this computer")
        session.update(
            test_id=made["test_id"], plates=plates, questions=list(made.get("questions") or []),
            slice_with=dict(made.get("slice_with") or {}), made_message=str(made.get("message") or ""),
            readings={}, plate_n=1, all_printed=False, sliced_files=[],
        )
        session["step"] = step = "limits_slice"
        answer = None
        gcode_paths = None

    # ---- the sliced files, read before anything is asked -----------------------
    if step == "limits_slice":
        plates = session["plates"]
        overrides = _slice_overrides(session)
        if gcode_paths:
            files = [_gcode_of(path) for path in gcode_paths]
            if len(files) != len(plates) or not all(files):
                return _reply(session, f"Kiln needs the sliced file of each plate, in plate order: {len(plates)}.",
                              options=["skip"], print_files=[p["stl_path"] for p in plates])
            read, miss = bridge.ask("sliced", printer_id=own, material=session["material"],
                                    test_id=session["test_id"], gcode_paths=files)
            if miss is not None:
                return _limits_missed(session, miss, "read the sliced files")
            if read.get("status") != "ok":
                return _reply(session, f"{read.get('error') or 'Kiln could not read those files.'} "
                              "Slice each plate as it was made, and send the files again.",
                              options=["skip"], print_files=[p["stl_path"] for p in plates])
            session["questions"] = list(read.get("questions") or session["questions"])
            session["sliced_message"] = str(read.get("message") or "")
            session["step"] = step = "limits_print"
            answer = None
        elif answer is not None and _norm(answer) == "skip":
            session["step"] = step = "limits_print"
            answer = None
        else:
            return _reply(
                session, session.get("made_message") or "The test print is ready.",
                print_files=[p["stl_path"] for p in plates], waiting=False,
                how_to_slice=(f"Show the person the test print with visualize_model. Then slice each plate in order "
                              f"with reslice_with_overrides(input_path=<the plate's file>, printer_name={name!r}, "
                              f"material={session['material']!r}, overrides={overrides!r}) and call printer_bench "
                              "again with gcode_paths set to each slice's raw_gcode_path (else its output_path), in "
                              "plate order. If they cannot be sliced here, call it with answer=\"skip\"."),
            )

    # ---- printing each plate ---------------------------------------------------
    if step == "limits_print":
        here = int(session.get("plate_n") or 1)
        plate = next(p for p in session["plates"] if int(p.get("plate") or 0) == here)
        path = str(plate["stl_path"])
        state, _job = _status(adapter)
        if state == "printing":
            session["step"] = step = "limits_printing"
            answer = None
        elif answer is not None and _norm(answer) in _PRINTED:
            _start_questions(session)
            step = session["step"]
            answer = None
        elif answer is None or not _is_yes(answer):
            said = session.pop("sliced_message", "")
            lead = "" if not said or said.startswith("Every step") else f"{said} "
            return _reply(
                session, f"{lead}Kiln will print {_plate_word(session, here)}. Ready to start it?",
                options=["yes"], print_file=path,
                how_to_start=(f"Show the file with visualize_model(model_path={path!r}), get a token with "
                              f"issue_preview_token, then run_reslice_and_print(model_path={path!r}, "
                              f"printer_name={name!r}, material={session['material']!r}, "
                              f"overrides={_slice_overrides(session)!r}, skip_validation=True, preview_token=...). "
                              "The normal approval dialog shows. Then call printer_bench again with "
                              "answer=\"started\"."),
            )
        else:
            return _reply(session, f"The printer isn't printing yet (it says {state}). Start "
                          f"{_plate_word(session, here)}, then tell me.", options=["started"], print_file=path,
                          waiting=True)

    if step == "limits_printing":
        state, _job = _status(adapter)
        if state in ("printing", "paused") and not (answer is not None and _norm(answer) in _PRINTED):
            return _reply(session, f"{_plate_word(session, int(session.get('plate_n') or 1)).capitalize()} is "
                          "printing; Kiln will ask about it when it finishes.", waiting=True)
        _start_questions(session)
        step = session["step"]
        answer = None

    # ---- the questions, one at a time ------------------------------------------------
    if step == "limits_question":
        asked = _questions_now(session)
        index = int(session.get("q_index") or 0)
        question = asked[index]
        options = [f"{float(s):g}" for s in question.get("steps") or []] + ["none", "all"]
        if answer is None:
            return _reply(session, _question_ask(session, question), options=options)
        reading = _reading_of(answer, list(question.get("steps") or []))
        if reading is None:
            steps = ", ".join(f"{float(s):g}" for s in question.get("steps") or [])
            return _reply(session, f"That isn't one of the numbers on the rows headed {question.get('heading')} "
                          f"({steps}). Which is it, or none, or all?", options=options)
        session["readings"][str(question["reading"])] = reading
        if index + 1 < len(asked):
            session["q_index"] = index + 1
            return _reply(session, _question_ask(session, asked[index + 1]),
                          options=[f"{float(s):g}" for s in asked[index + 1].get("steps") or []] + ["none", "all"])
        session["step"] = step = "limits_cleared"
        answer = None

    if step == "limits_cleared":
        here = int(session.get("plate_n") or 1)
        if not session.get("all_printed") and here < len(session["plates"]):
            if answer is None or not _is_yes(answer):
                return _reply(session, f"Take {_plate_word(session, here)} off the plate and tell me when it's empty.",
                              options=["it's empty"])
            session["plate_n"] = here + 1
            session["step"] = "limits_print"
            return _advance_limits(session, adapter, None, None)
        session["step"] = step = "limits_dried"
        answer = None

    if step == "limits_dried":
        text = _norm(answer)
        if not text:
            return _reply(session, "Was the spool dried before this print: yes, no, or not sure?",
                          options=["yes", "no", "not sure"])
        session["spool_dried"] = True if _is_yes(text) else (False if _is_no(text) else None)
        session["step"] = step = "limits_report"
        answer = None

    # ---- the readings, kept in the person's account -----------------------------------
    if step == "limits_report":
        said, miss = bridge.ask("report", printer_id=own, material=session["material"],
                                test_id=session["test_id"], readings=session["readings"],
                                spool_dried=session.get("spool_dried"))
        if miss is not None:
            return _limits_missed(session, miss, "keep your readings")
        if said.get("status") != "ok":
            # The readings disagree with each other: read the print again.
            session["readings"] = {}
            session["questions"] = list(said.get("questions") or session["questions"])
            _start_questions(session, every=True)
            first = _questions_now(session)[0]
            return _reply(session, f"{said.get('error') or 'Kiln could not keep those readings.'} "
                          + _question_ask(session, first),
                          options=[f"{float(s):g}" for s in first.get("steps") or []] + ["none", "all"])
        session["learned"] = list(session.get("learned") or []) + [str(h) for h in said.get("heard") or []]
        session["report_message"] = str(said.get("message") or "")
        upcoming = said.get("next") if isinstance(said.get("next"), dict) else None
        if upcoming and upcoming.get("coupon"):
            session["next_coupon"] = str(upcoming["coupon"])
            session["step"] = "limits_next"
            return _reply(session, f"{session['report_message']} {upcoming.get('say') or ''} Make it now?".strip(),
                          options=["yes", "later"])
        return _limits_done(session, session["report_message"])

    if step == "limits_next":
        if answer is None or not (_is_yes(answer) or _is_later(answer) or _is_no(answer)):
            return _reply(session, "Make the second test print now?", options=["yes", "later"])
        if not _is_yes(answer):
            return _limits_done(session, session.get("report_message") or "")
        session["coupon"] = session.pop("next_coupon")
        session["step"] = "limits_make"
        return _advance_limits(session, adapter, None, None)

    return _reply(session, f"Kiln lost its place at step {step!r}; call printer_bench with restart=true to begin again.")


# ---------------------------------------------------------------------------
# The third lesson: a home or a park, learned with the person one step at a time
# ---------------------------------------------------------------------------

#: What the printer does in each move, as the end of "how your <printer> ...".
_WALK_DOES = {"home": "homes", "park": "parks its head"}
_TRY_AGAIN = {"try again", "again", "retry"}


def _walk_verbs() -> tuple[str, ...]:
    from kiln.printers.motion_walk import WALK_VERBS

    return WALK_VERBS


def _said(text: Any) -> str:
    """*text* as a sentence: trimmed, ending in a full stop."""
    said = " ".join(str(text or "").split())
    return said if not said or said[-1] in ".!?" else f"{said}."


def _walk_model(adapter: Any, name: str) -> str:
    from kiln import plate_state

    printer_id = plate_state.declared_model_of(adapter)
    return bench._display_name(printer_id) if printer_id else name


def _walk_session(name: str, adapter: Any, unit: str, move: str) -> dict[str, Any] | None:
    """A new walk of *move*, or ``None`` when Kiln has none on offer for this
    printer and move."""
    from kiln import plate_state
    from kiln.printers.motion_walk import walk_offer

    offered = walk_offer(adapter, move)
    if offered.get("offered") is not True:
        return None
    return {
        "lesson": WALK, "move": move, "printer_name": name, "unit": unit,
        "printer_id": plate_state.declared_model_of(adapter) or "", "model_name": _walk_model(adapter, name),
        "steps_total": int(offered.get("steps_total") or 0), "step": "walk_intro", "next_n": 1,
        "observed": {}, "learned": [], "started_at": bench._now(),
    }


def _walk_not_on_offer(name: str, adapter: Any, move: str) -> dict[str, Any]:
    """Nothing to start: said as a plain answer, never as an error."""
    return {
        "success": True, "step": "not_on_offer", "lesson": WALK, "move": move, "printer_name": name,
        "ask": "", "images": [], "learned": [],
        "message": (f"Kiln has no step-by-step {bench.WALK_MOVE_WORDS[move]} on offer for your "
                    f"{_walk_model(adapter, name)} right now, so there is nothing to start. Printing is not affected."),
    }


def _walk_intro(session: dict[str, Any]) -> str:
    total = int(session.get("steps_total") or 0)
    steps = f" It takes {total} small steps." if total > 1 else ""
    return (f"Kiln can learn how your {session['model_name']} {_WALK_DOES[session['move']]} with you, one small step "
            "at a time: it sends one small move, tells you what you should see, and sends the next only after you say "
            "the printer did exactly that. The printer stays cold the whole time, nothing heats or extrudes, and its "
            f"own stop, pause and power switch work at every moment.{steps} Ready?")


def _walk_again(session: dict[str, Any]) -> str:
    return (f"Call printer_bench with lesson=\"walk\" and move=\"{session['move']}\" to start again from the first "
            "step whenever you are at the printer.")


def _walk_ended(session: dict[str, Any], step: str, message: str, **extra: Any) -> dict[str, Any]:
    _end_session(session)
    return {"success": True, "step": step, "lesson": WALK, "move": session["move"],
            "printer_name": session["printer_name"], "ask": "", "images": [],
            "learned": list(session.get("learned") or []), "message": message, **extra}


def _walk_confirm_ask(session: dict[str, Any]) -> str:
    shown = session["shown"]
    n, total = int(session["sent_n"]), int(session["steps_total"])
    text = f"Step {n} of {total}, {shown['label']}: {_said(shown['you_should_see'])}"
    if shown.get("stops_when"):
        stops = _said(shown["stops_when"])
        text += f" It stops when {stops[:1].lower() + stops[1:]}"
    text += f" {_said(shown['confirm'])}"
    if n < total:
        upcoming = f", {shown['next_label']}" if shown.get("next_label") else ""
        return text + f" Say yes and Kiln sends step {n + 1}{upcoming}; say no and the walk stops here."
    return text + " That was the last step: say yes if it did, or no if it did not."


def _walk_send(session: dict[str, Any], adapter: Any, number: int) -> dict[str, Any]:
    """Send step *number* on the person's word, and ask whether it did what
    Kiln said.  Nothing is sent while the printer's emergency stop is latched."""
    import kiln.server as _srv
    from kiln.printers.base import PrinterError
    from kiln.printers.motion_walk import WalkRefused, walk_step

    name, move = session["printer_name"], session["move"]
    if block := _srv._emergency_latch_error("printer_bench", name):
        _save_session(session)
        return block
    try:
        sent = walk_step(adapter, move, number, plate_clear=True, saw_previous=number > 1)
    except WalkRefused as refused:
        if refused.code == "STEP_OUT_OF_ORDER":
            session.update(step="walk_plate", next_n=1)
            return _reply(session, f"{_said(refused)} Is the plate still empty? When you say yes, Kiln sends the "
                          "first step again.", options=["yes", "later"], refused=refused.code)
        session.update(step="walk_retry", next_n=number, refusal=_said(refused))
        return _reply(session, f"{_said(refused)} Say try again when that is sorted, or later.",
                      options=["try again", "later"], refused=refused.code)
    except PrinterError as exc:
        session.update(step="walk_retry", next_n=number, refusal=f"Kiln could not finish step {number}: {_said(exc)}")
        return _reply(session, f"{session['refusal']} Say try again when that is sorted, or later.",
                      options=["try again", "later"])
    if not sent.get("success"):
        code = str(sent.get("error_code") or "")
        what = (f"The printer refused step {number}" if code == "rejected"
                else f"The printer reported a fault ({code}) during step {number}")
        told = " Kiln was told what happened." if sent.get("reported") else " Kiln could not tell its servers just now."
        return _walk_ended(session, "stopped", f"{what}, so the walk stops here and Kiln sends nothing more. Use the "
                           f"printer's own controls from here.{told}", fault=code, reported=bool(sent.get("reported")))
    upcoming = sent.get("next_step") if isinstance(sent.get("next_step"), dict) else {}
    session.update(step="walk_confirm", sent_n=number, steps_total=int(sent["steps_total"]), shown={
        "label": sent["label"], "you_should_see": sent["you_should_see"], "stops_when": sent["stops_when"],
        "confirm": sent["confirm"] or "Did it do that?", "next_label": str(upcoming.get("label") or ""),
    })
    return _reply(session, _walk_confirm_ask(session), options=["yes", "no"], stop=sent["stop"], if_not=sent["if_not"])


def _advance_walk(session: dict[str, Any], adapter: Any, answer: Any) -> dict[str, Any]:  # noqa: C901 -- one step per branch
    from kiln import plate_state
    from kiln.printers.motion_walk import walk_went_wrong

    step = session["step"]
    move = session["move"]
    word = bench.WALK_MOVE_WORDS[move]

    # ---- the offer -------------------------------------------------------------
    if step == "walk_intro":
        if answer is None or not (_is_yes(answer) or _is_later(answer) or _is_no(answer)):
            return _reply(session, _walk_intro(session), options=["yes", "later", "no"])
        if _is_later(answer):
            bench.note_offer(bench.walk_key(session["unit"], move), answer="later")
            bench.count("later", WALK)
            return _walk_ended(session, "later", f"No problem. Call printer_bench with lesson=\"walk\" and "
                               f"move=\"{move}\" whenever you are at the printer; Kiln won't bring it up again on "
                               "its own.")
        if _is_no(answer):
            bench.note_offer(bench.walk_key(session["unit"], move), answer="declined")
            bench.count("declined", WALK)
            return _walk_ended(session, "declined", f"Understood. Kiln won't offer to learn the {word} on this printer "
                               "again.")
        bench.note_offer(bench.walk_key(session["unit"], move))
        bench.count("started", WALK)
        session["step"] = step = "walk_plate"
        answer = None

    # ---- the plate, before the first step ----------------------------------------
    if step == "walk_plate":
        if answer is not None and _is_later(answer):
            return _walk_ended(session, "later", "No problem; nothing was sent. " + _walk_again(session))
        if answer is None:
            return _reply(session, "Is the plate completely empty? Take everything off it first. When you say yes, "
                          "Kiln sends the first step.", pictures=[_camera_frame(adapter)], options=["yes", "no"])
        if not _is_yes(answer):
            return _reply(session, "Take everything off the plate and tell me when it's empty. When you do, Kiln "
                          "sends the first step.", options=["it's empty"])
        plate_state.mark_clear(adapter, "human", note="walk: the owner says the plate is empty")
        return _walk_send(session, adapter, int(session.get("next_n") or 1))

    # ---- after a step: did it do what Kiln said? ----------------------------------
    if step == "walk_confirm":
        number, total = int(session["sent_n"]), int(session["steps_total"])
        if answer is not None and _is_later(answer):
            return _walk_ended(session, "later", f"Stopped after step {number} of {total}; Kiln sends nothing more. "
                               + _walk_again(session))
        if answer is not None and _is_no(answer):
            went = walk_went_wrong(adapter, move, number)
            told = " Kiln was told what happened." if went.get("reported") else " Kiln could not tell its servers just now."
            return _walk_ended(session, "stopped", f"Thank you for saying so. The walk stops at step {number} and "
                               f"Kiln sends nothing more. Use the printer's own controls from here.{told}",
                               reported=bool(went.get("reported")))
        if answer is None or not _is_yes(answer):
            return _reply(session, _walk_confirm_ask(session), options=["yes", "no"])
        if number < total:
            return _walk_send(session, adapter, number + 1)
        bench.note_offer(bench.walk_key(session["unit"], move), answer="done")
        bench.count("finished", WALK)
        bench.count("learned", WALK, "session")
        session["learned"] = [f"{word}: all {total} steps did what Kiln said they would"]
        return _walk_ended(session, "done", f"That was the last step. Kiln learned the {word} on your "
                           f"{session['model_name']} with you: all {total} steps did what Kiln said they would, and "
                           "nothing else. Thank you.")

    # ---- a step Kiln could not send ------------------------------------------------
    if step == "walk_retry":
        number = int(session.get("next_n") or 1)
        if answer is not None and (_is_later(answer) or _is_no(answer)):
            return _walk_ended(session, "later", "No problem; Kiln sends nothing more. " + _walk_again(session))
        if answer is None or not (_norm(answer) in _TRY_AGAIN or _is_yes(answer)):
            return _reply(session, f"{session.get('refusal') or ''} Say try again when that is sorted, or later.".strip(),
                          options=["try again", "later"])
        return _walk_send(session, adapter, number)

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


def _description() -> str:
    """The tool's description: its docstring, with when to reach for it said
    second, where a reader who stops early still meets it."""
    import inspect

    lead, _, rest = inspect.cleandoc(printer_bench.__doc__ or "").partition("\n\n")
    return f"{lead}\n\nWHEN TO REACH FOR IT: {bench.LEARN_WHEN}\n\n{rest}"


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
        return "A short guided session that teaches Kiln how this printer moves its head on its own"

    def register(self, mcp: Any) -> None:
        import kiln.server as _srv

        for tool_name, limits in _RATE_LIMITS.items():
            _srv._TOOL_RATE_LIMITS.setdefault(tool_name, limits)
        # No structured output: a reply with pictures is [text, image, ...],
        # and a schema built from the dict annotation would reject it exactly
        # when there is a picture to show.
        mcp.tool(
            annotations=destructive("Teach Kiln your printer"), structured_output=False, description=_description(),
        )(printer_bench)


plugin = _PrinterBenchPlugin()
