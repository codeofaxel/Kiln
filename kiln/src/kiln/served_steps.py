"""Steps a served tool asks this computer to do for it.

A served tool runs on Kiln's servers, which have no slicer and no printer.
When one needs a single thing only this computer can do, it answers
``status: "needs_local_step"`` with a ``local_steps`` list, and the same
tool is called again with what was done (``step_results``).  Nothing is
kept on the servers in between.

This module is the allow-list.  A step is carried out only when its kind is
one named here and everything about it passes that kind's own checks; a
kind this build does not know, or a step that asks for more than its kind
allows, is refused and nothing runs.  No step here commands a printer.

``slice``: slice one model that the servers handed back with the answer,
with a slicer flag list checked flag by flag against what a placement
needs.  The servers decide where the part goes; this computer's slicer
makes the toolpath; the servers then judge the result.

``printer_facts``: say which printers this install has -- each one's name,
connection kind and model, and when asked, a Klipper printer's own
configuration -- with the time they were read.  Never an address, a serial
number or a credential: a served tool needs to know WHAT the printer is,
and nothing here tells it where the printer is or how to reach it.

``print_history``: this install's own recent print records, for a served
tool that learns from them -- which printer and material, how the print
went, and the settings and conditions it ran under.  Never a file's name or
fingerprint, a note, or who recorded it.  They go with the one call that
asked and the servers keep none of it.
"""

from __future__ import annotations

import logging
import re
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: What a served tool answers when it needs a step done first.
NEEDS_LOCAL_STEP = "needs_local_step"
#: The most steps carried out for one answer.
MAX_STEPS = 2
_SLICE_TIMEOUT_S = 300

_NUMBER = r"\d+(?:\.\d+)?"
#: Slicer flags a placement may carry, each with the shape its value must
#: have (``None``: the flag takes no value).  Nothing that names a file, a
#: script or an output place is here, so a step cannot make the slicer
#: read, write or run anything but the model it was handed.
_SLICE_FLAGS: dict[str, str | None] = {
    "--dont-arrange": None,
    "--bed-shape": rf"{_NUMBER}x{_NUMBER}(?:,{_NUMBER}x{_NUMBER}){{3}}",
    "--skirts": r"\d{1,2}",
    "--brim-type": r"no_brim",
    "--first-layer-height": _NUMBER,
}


def _refusal(tool: str, code: str, message: str) -> dict[str, Any]:
    return {
        "success": False, "status": "error", "code": code, "tool": tool,
        "error": message, "why": "refused",
    }


def _checked_slicer_args(args: Any) -> list[str] | None:
    """*args* when every flag is an allowed one with a well-formed value."""
    if not isinstance(args, list) or len(args) > 16:
        return None
    out: list[str] = []
    i = 0
    while i < len(args):
        flag = args[i]
        if not isinstance(flag, str) or flag not in _SLICE_FLAGS:
            return None
        shape = _SLICE_FLAGS[flag]
        out.append(flag)
        i += 1
        if shape is None:
            continue
        if i >= len(args) or not isinstance(args[i], str) or not re.fullmatch(shape, args[i]):
            return None
        out.append(args[i])
        i += 1
    return out


def _slice(tool: str, step: dict[str, Any]) -> tuple[str | None, dict[str, Any] | None]:
    """Slice the model a step names; ``(G-code path, None)`` or a refusal."""
    from kiln import served_makes
    from kiln.slicer import _CLI_BAMBU, SlicerNotFoundError, find_slicer, slicer_cli_family

    args = _checked_slicer_args(step.get("slicer_args"))
    if args is None:
        return None, _refusal(
            tool, "LOCAL_STEP_REFUSED",
            f"{tool} asked this computer's slicer for something Kiln does "
            "not allow a served tool to ask. Nothing was run.",
        )
    model = Path(str(step.get("model_path") or ""))
    try:
        inside = model.resolve().is_relative_to(served_makes.files_dir().resolve())
    except Exception:  # noqa: BLE001
        inside = False
    if not inside or model.suffix.lower() != ".stl" or not model.is_file():
        # Only a model the servers handed back with this answer, which
        # arrival saved here; never a path the answer merely names.
        return None, _refusal(
            tool, "LOCAL_STEP_REFUSED",
            f"{tool} asked for a slice of a model it did not hand over. "
            "Nothing was run.",
        )
    try:
        slicer = find_slicer()
    except SlicerNotFoundError:
        slicer = None
    except Exception:  # noqa: BLE001
        slicer = None
    if slicer is None or slicer_cli_family(slicer) == _CLI_BAMBU:
        return None, _refusal(
            tool, "SLICER_NEEDED",
            f"{tool} needs the added part sliced on this computer, with "
            "PrusaSlicer. Install PrusaSlicer (it is free) and call this "
            "again. Nothing was changed.",
        )
    out = Path(tempfile.mkdtemp(prefix="kiln_step_")) / "slice.gcode"
    cmd = [slicer.path, "--export-gcode", str(model), "--output", str(out), *args]
    try:
        run = subprocess.run(  # noqa: S603 — argv built above from checked parts
            cmd, capture_output=True, text=True, timeout=_SLICE_TIMEOUT_S,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return None, _refusal(
            tool, "LOCAL_STEP_FAILED",
            f"This computer's slicer could not slice the added part: {exc}",
        )
    if run.returncode != 0 or not out.is_file():
        said = ((run.stderr or "").strip() or (run.stdout or "").strip())[:300]
        return None, _refusal(
            tool, "LOCAL_STEP_FAILED",
            "This computer's slicer could not slice the added part"
            + (f": {said}" if said else "."),
        )
    return str(out), None


#: What a ``printer_facts`` step may ask for beyond the basics.
_FACTS_EXTRAS = frozenset({"klipper_config"})
#: How long a printer is given to answer for its configuration.
_FACTS_DEADLINE_S = 8.0
_UNNAMED = frozenset({"", "default", "active"})


def _printer_facts(tool: str, step: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """The printers this install has, as plain facts; never how to reach one."""
    import concurrent.futures
    from datetime import datetime, timezone

    import kiln.server as server

    want = step.get("want") or []
    if not isinstance(want, list) or not set(want) <= _FACTS_EXTRAS:
        return None, _refusal(
            tool, "LOCAL_STEP_REFUSED",
            f"{tool} asked this computer for something about your printer "
            "that Kiln does not hand to a served tool. Nothing was sent.",
        )
    asked = str(step.get("printer_name") or "").strip()
    try:
        entries = server._read_config_printers() or {}
        default = server._resolve_effective_printer_name(None)
    except Exception:  # noqa: BLE001
        entries, default = {}, ""
    target = asked if asked.lower() not in _UNNAMED else default
    printers: list[dict[str, Any]] = []
    for name, entry in entries.items():
        if not isinstance(entry, dict):
            continue
        kind = str(entry.get("type") or entry.get("printer_type") or "").lower()
        fact: dict[str, Any] = {
            "name": str(name),
            "type": kind,
            "model": str(entry.get("printer_model") or ""),
            "is_default": str(name) == default,
        }
        if "klipper_config" in want and kind == "moonraker" and str(name) == target:
            pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
            try:
                adapter = server._get_registry().get(str(name))
                fact["klipper_config"] = pool.submit(adapter.get_printer_config).result(
                    timeout=_FACTS_DEADLINE_S,
                )
            except Exception as exc:  # noqa: BLE001 — said, never guessed
                fact["klipper_config"] = None
                fact["klipper_config_error"] = (
                    "the printer did not answer in time"
                    if isinstance(exc, concurrent.futures.TimeoutError)
                    else "the printer could not be asked"
                )
            finally:
                pool.shutdown(wait=False)
        printers.append(fact)
    return {
        "read_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "printers": printers,
    }, None


#: What one print record carries to a served tool: what was printed in, how
#: it went, and the settings and conditions it ran under.  Never which file
#: it was, its name, its notes or who recorded it.
_HISTORY_FIELDS = (
    "printer_name", "material_type", "outcome", "failure_mode",
    "quality_grade", "settings", "environment", "created_at",
)
#: The most recent records sent, and the most they may weigh as JSON (the
#: servers refuse a reading step over a megabyte).
_HISTORY_RECORDS = 400
_HISTORY_BYTES = 900_000


def _print_history(tool: str, step: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """This install's own print records, for a served tool that learns from
    them.  They are read here and sent with the one call that asked; the
    servers keep none of it."""
    import json
    from datetime import datetime, timezone

    import kiln.server as server
    from kiln.persistence import get_db

    asked = str(step.get("printer_name") or "").strip()
    target = asked
    if asked.lower() in _UNNAMED:
        try:
            target = str(server._resolve_effective_printer_name(None) or "")
        except Exception:  # noqa: BLE001 — an install with no printer set up
            target = ""
    try:
        rows = get_db().list_print_outcomes(limit=_HISTORY_RECORDS + 1)
    except Exception:  # noqa: BLE001 — said, never guessed
        return None, _refusal(
            tool, "PRINT_HISTORY_NOT_READ",
            f"{tool} learns from your print history, and this computer could "
            "not read its own record of it. Nothing was sent.",
        )
    complete = len(rows) <= _HISTORY_RECORDS
    sent = [{k: row.get(k) for k in _HISTORY_FIELDS} for row in rows[:_HISTORY_RECORDS]]
    while sent and len(json.dumps(sent, default=str)) > _HISTORY_BYTES:
        # Oldest first: the list is newest-first, and recent prints say the
        # most about the printer as it is now.
        sent = sent[: max(1, len(sent) * 3 // 4)] if len(sent) > 1 else []
        complete = False
    return {
        "read_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "printer_name": target,
        "outcomes": sent,
        "complete": complete,
    }, None


#: Steps that MAKE a file (sent up, named by a token)...
_STEPS: dict[str, Callable[[str, dict[str, Any]], tuple[str | None, dict[str, Any] | None]]] = {
    "slice": _slice,
}
#: ...and steps that READ facts (sent with the next call as they are).
_READS: dict[str, Callable[[str, dict[str, Any]], tuple[dict[str, Any] | None, dict[str, Any] | None]]] = {
    "printer_facts": _printer_facts,
    "print_history": _print_history,
}


def wanted(answer: Any) -> list[dict[str, Any]]:
    """The steps *answer* asks for, or ``[]`` when it asks for none."""
    if not isinstance(answer, dict) or answer.get("status") != NEEDS_LOCAL_STEP:
        return []
    steps = answer.get("local_steps")
    return [s for s in steps if isinstance(s, dict)] if isinstance(steps, list) else []


def carry_out(
    tool: str,
    steps: list[dict[str, Any]],
    send: Callable[[Path], tuple[str | None, str]],
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Do *steps* and send each result up with *send* (a file -> token).

    Returns ``({step id: {"token": ...} | {"data": {...}}}, None)`` -- a
    token for a step that made a file, the facts themselves for a step that
    read something -- or ``({}, refusal)`` at the first step that is not
    allowed, fails, or cannot be sent.
    """
    if not steps or len(steps) > MAX_STEPS:
        return {}, _refusal(
            tool, "LOCAL_STEP_REFUSED",
            f"{tool} asked this computer for more than Kiln allows a served "
            "tool to ask. Nothing was run.",
        )
    results: dict[str, Any] = {}
    for step in steps:
        kind, step_id = step.get("kind"), step.get("id")
        do = _STEPS.get(kind) if isinstance(kind, str) else None
        read = _READS.get(kind) if isinstance(kind, str) else None
        plain_id = isinstance(step_id, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,40}", step_id)
        if read is not None and plain_id:
            facts, refusal = read(tool, step)
            if refusal is not None or facts is None:
                return {}, refusal
            results[step_id] = {"data": facts}
            continue
        if do is None or not plain_id:
            return {}, _refusal(
                tool, "LOCAL_STEP_REFUSED",
                f"{tool} asked this computer to do something this version "
                "of Kiln does not carry out for a served tool. Nothing was "
                "run. Updating Kiln may add it.",
            )
        made, refusal = do(tool, step)
        if refusal is not None or made is None:
            return {}, refusal
        token, said = send(Path(made))
        if token is None:
            return {}, _refusal(
                tool, "FILE_NOT_SENT",
                f"{tool} needs what this computer just made sent to Kiln's "
                f"servers, which did not work: {said}",
            )
        results[step_id] = {"token": token}
    return results, None
