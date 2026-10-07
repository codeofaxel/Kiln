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

``act``: one of a short, fixed list of this install's OWN printer tools,
called with arguments checked name by name (:data:`ACT_TOOLS`).  The
servers decide the paid part (a speed checked against the machine's
range, a resume file built with its safety checks, which machines a fleet
stop reaches); this computer does the part only it can do, and does it
through the tool's own door, so every gate that would have fired had the
user's agent called the tool fires here too -- consent, the pre-flight,
the one-printer-at-a-time rule -- and the servers cannot skip or
pre-answer one.  Never firmware, never raw G-code, never a temperature,
and never a file the servers did not hand over with the answer.  The
user's agent is shown each action and its reason BEFORE anything runs
(:mod:`kiln.server` proposes them and runs them only on the agent's next
call); what each tool answered goes back with the same call, and the
servers keep none of it.

A step a served tool needs before it can answer at all (which printer a
speed is for) is read HERE before the first call (:func:`read_first`), so
the one round a call gets can be the round that acts.  Only a reading
kind may be done that way; a make or an act is never run on nobody's
say-so.
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
#: The most steps that MAKE something (a slice) carried out for one answer.
MAX_STEPS = 2
#: The most actions for one answer: a fleet stop names each machine once.
MAX_ACT_STEPS = 24
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


#: What a ``printer_facts`` step may ask for beyond the basics: a Klipper
#: printer's configuration; what the named printer is doing now (``job``);
#: what every printer is doing now (``every_job``, for a fleet-wide
#: action).  A job is its state and how far along it is -- never the
#: file's name, for the same reason the print-history step sends none.
_FACTS_EXTRAS = frozenset({"klipper_config", "job", "every_job"})
#: How long a printer is given to answer for its configuration or its job.
_FACTS_DEADLINE_S = 8.0
#: How many printers are asked at once for a fleet-wide read.
_FACTS_WORKERS = 8
_UNNAMED = frozenset({"", "default", "active"})


def _facts_target(asked: str, entries: dict[str, Any], default: str) -> str:
    """The configured name a step's ``printer_name`` means: the name itself,
    a printer named by its MODEL (what the stub puts in a call that said
    "default", see ``_with_local_printer``), else this install's default."""
    if asked.lower() in _UNNAMED:
        return default
    if asked in entries:
        return asked
    for name, entry in entries.items():
        if isinstance(entry, dict) and str(entry.get("printer_model") or "") == asked:
            return str(name)
    return asked


def _job_fact(adapter: Any) -> dict[str, Any]:
    """What a printer is doing now, as a served tool may know it: its state
    and how far along the job is.  Never the file's name."""
    from kiln.printers.base import read_status

    state, job = read_status(adapter)
    status = getattr(state, "effective_state", None) or getattr(state, "state", None)
    return {
        "state": str(getattr(status, "value", status) or "unknown"),
        "has_job": bool(getattr(job, "file_name", None)),
        "current_layer": getattr(job, "current_layer", None),
        "total_layers": getattr(job, "total_layers", None),
        "completion": getattr(job, "completion", None),
    }


def _did_not_answer(exc: BaseException) -> str:
    import concurrent.futures

    return (
        "the printer did not answer in time"
        if isinstance(exc, concurrent.futures.TimeoutError)
        else "the printer could not be asked"
    )


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
    target = _facts_target(asked, entries, default)
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
                fact["klipper_config_error"] = _did_not_answer(exc)
            finally:
                pool.shutdown(wait=False)
        printers.append(fact)
    if "every_job" in want or "job" in want:
        asked_of = [
            f for f in printers if "every_job" in want or f["name"] == target
        ]
        _read_jobs(server, asked_of)
    return {
        "read_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "printers": printers,
    }, None


def _read_jobs(server: Any, facts: list[dict[str, Any]]) -> None:
    """Fill ``job`` on each of *facts* from the printer itself, every printer
    asked at once and each given the deadline; one that does not answer
    says so.  A fleet read also marks a name that is the same MACHINE as
    an earlier one (the server registers the active printer under
    ``"default"`` and its config name), so a fleet-wide action reaches each
    machine once."""
    import concurrent.futures

    if not facts:
        return

    def resolve(name: str) -> Any:
        return server._resolve_adapter(name)

    pool = concurrent.futures.ThreadPoolExecutor(max_workers=_FACTS_WORKERS)
    try:
        adapters = {f["name"]: pool.submit(resolve, f["name"]) for f in facts}
        concurrent.futures.wait(adapters.values(), timeout=_FACTS_DEADLINE_S)
        built: dict[str, Any] = {}
        for fact in facts:
            future = adapters[fact["name"]]
            try:
                if not future.done():
                    raise concurrent.futures.TimeoutError()
                built[fact["name"]] = future.result()
            except Exception as exc:  # noqa: BLE001 — said, never guessed
                fact["job"] = None
                fact["job_error"] = _did_not_answer(exc)
        if len(facts) > 1:
            _mark_same_machines(facts, built)
        jobs = {name: pool.submit(_job_fact, adapter) for name, adapter in built.items()}
        concurrent.futures.wait(jobs.values(), timeout=_FACTS_DEADLINE_S)
        for fact in facts:
            future = jobs.get(fact["name"])
            if future is None:
                continue
            try:
                if not future.done():
                    raise concurrent.futures.TimeoutError()
                fact["job"] = future.result()
            except Exception as exc:  # noqa: BLE001
                fact["job"] = None
                fact["job_error"] = _did_not_answer(exc)
    finally:
        pool.shutdown(wait=False)


def _mark_same_machines(facts: list[dict[str, Any]], built: dict[str, Any]) -> None:
    """``same_machine_as`` on a fact whose adapter is the machine an earlier
    fact already names.  The fingerprint itself (a serial, an address)
    stays here."""
    try:
        from kiln.registry import machine_fingerprint
    except Exception:  # noqa: BLE001
        return
    seen: dict[str, str] = {}
    for fact in facts:
        adapter = built.get(fact["name"])
        if adapter is None:
            continue
        try:
            key = machine_fingerprint(adapter)
        except Exception:  # noqa: BLE001 — an odd adapter is its own machine
            continue
        if key in seen:
            fact["same_machine_as"] = seen[key]
        else:
            seen[key] = fact["name"]


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
#: ...and the one kind that ACTS: a step names one of this install's own
#: printer tools from :data:`ACT_TOOLS` and runs it through its own door.
ACT_KIND = "act"

# ---------------------------------------------------------------------------
# Acting
# ---------------------------------------------------------------------------

#: A name a step may pass as a printer or a file on the printer: no control
#: characters, no path separators -- a file on the printer is named, never
#: located.
_PLAIN_NAME = re.compile(r"[^\x00-\x1f/\\]{1,160}")
#: The arguments of each tool a served tool may ask this computer to run,
#: and the shape each value must have.  A tool or an argument not here is
#: refused, so the list is the fence: nothing commands firmware, sends
#: G-code, sets a temperature, names a file the servers did not hand over,
#: or speaks for the person (``hardware_confirmed`` is a person's word and
#: is never a server's to give).
ACT_TOOLS: dict[str, dict[str, Any]] = {
    "upload_file": {"file_path": "handed_file", "printer_name": "name"},
    "start_print": {
        "file_name": "name",
        "printer_name": "name",
        "resume_from_paused": bool,
        "use_ams": ("auto", "true", "false"),
        "ams_mapping": "tray_list",
        "bed_leveling": bool,
        "flow_cali": bool,
        "vibration_cali": bool,
        "plate_number": "small_int",
    },
    "pause_print": {"printer_name": "name", "keep_temps": bool},
    "resume_print": {"printer_name": "name", "force": bool},
    "set_speed_profile": {
        "profile": ("silent", "standard", "sport", "ludicrous"),
        "printer_name": "name",
    },
    "set_print_speed": {"percent": "percent", "printer_name": "name"},
    "run_speed_schedule": {
        "schedule": "segments",
        "printer_name": "name",
        "action": ("run", "stop"),
    },
}
#: A name an action takes from an earlier action's answer: the file a
#: start names is the one the upload before it answered with, which the
#: servers cannot know in advance (this computer names what it saves).
_FROM_STEP_FIELDS = frozenset({"file_name"})
#: What of a tool's answer goes back to the servers: whether it worked and
#: what it said, never a path on this computer or the printer's details.
_ACT_RESULT_KEYS = (
    "success", "status", "code", "error", "message", "outcome", "print_start",
    "accepted", "file_name", "confirmation_required", "token", "profile",
    "percent", "percent_actual", "preset", "active",
)
_ACT_TEXT_LIMIT = 400


def _handed_file(value: Any) -> str | None:
    """*value* when it is a file the servers handed over with this answer
    (saved by arrival under :func:`kiln.served_makes.files_dir`); never a
    path the answer merely names."""
    from kiln import served_makes

    if not isinstance(value, str) or not value or len(value) > 4096:
        return None
    path = Path(value)
    try:
        inside = path.resolve().is_relative_to(served_makes.files_dir().resolve())
    except Exception:  # noqa: BLE001
        return None
    return value if inside and path.is_file() else None


def _checked_act_args(shape: dict[str, Any], args: Any) -> dict[str, Any] | None:
    """*args* when every name is one the tool's shape allows and every value
    has that shape; ``None`` otherwise.  An absent or ``None`` value is left
    to the tool's own default."""
    if args is None:
        args = {}
    if not isinstance(args, dict) or len(args) > 16:
        return None
    out: dict[str, Any] = {}
    for name, value in args.items():
        if not isinstance(name, str) or name not in shape:
            return None
        if value is None:
            continue
        want = shape[name]
        if want is bool:
            if not isinstance(value, bool):
                return None
        elif isinstance(want, tuple):
            if not isinstance(value, str) or value not in want:
                return None
        elif want == "name":
            if isinstance(value, dict):
                if (
                    set(value) != {"from_step", "field"}
                    or not isinstance(value["from_step"], str)
                    or value["field"] not in _FROM_STEP_FIELDS
                ):
                    return None
            elif not isinstance(value, str) or not _PLAIN_NAME.fullmatch(value):
                return None
        elif want == "handed_file":
            if _handed_file(value) is None:
                return None
        elif want == "small_int":
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 64:
                return None
        elif want == "tray_list":
            if (
                not isinstance(value, list) or len(value) > 16
                or any(isinstance(v, bool) or not isinstance(v, int) or not -1 <= v <= 255 for v in value)
            ):
                return None
        elif want == "percent":
            if isinstance(value, bool) or not isinstance(value, int) or not 10 <= value <= 300:
                return None
        elif want == "segments":
            from kiln.speed_schedule_runner import ScheduleRefused, parse_segments

            try:
                parse_segments(value)
            except ScheduleRefused:
                return None
        else:  # pragma: no cover — a shape this module does not define
            return None
        out[name] = value
    return out


def _act_result(result: Any) -> dict[str, Any]:
    """What a tool answered, cut to what the servers may hear."""
    if not isinstance(result, dict):
        return {"ran": True, "success": bool(result)}
    out: dict[str, Any] = {"ran": True}
    for key in _ACT_RESULT_KEYS:
        if key not in result:
            continue
        value = result[key]
        if isinstance(value, str):
            value = value[:_ACT_TEXT_LIMIT]
        elif not isinstance(value, (bool, int, float)) and value is not None:
            continue
        out[key] = value
    preflight = result.get("preflight")
    if isinstance(preflight, dict):
        out["preflight"] = {
            "ready": bool(preflight.get("ready")),
            "summary": str(preflight.get("summary") or "")[:_ACT_TEXT_LIMIT],
        }
    return out


def act_succeeded(result: Any) -> bool:
    """Whether an action's answer says it was done: the tool said so, and
    did not stop to ask the person first."""
    if not isinstance(result, dict) or not result.get("ran"):
        return False
    if result.get("confirmation_required"):
        return False
    if result.get("success") is False or result.get("status") == "error":
        return False
    return result.get("success") is True or result.get("status") == "success"


def _named_from(args: dict[str, Any], done: dict[str, dict[str, Any]]) -> tuple[dict[str, Any] | None, str]:
    """*args* with each name taken from an earlier action's answer filled
    in; ``(None, why)`` when that answer did not give one."""
    out: dict[str, Any] = {}
    for key, value in args.items():
        if isinstance(value, dict):
            earlier = done.get(value["from_step"]) or {}
            named = earlier.get(value["field"])
            if not isinstance(named, str) or not _PLAIN_NAME.fullmatch(named):
                return None, f"{value['from_step']} did not answer with a {value['field']}, so this was not run"
            value = named
        out[key] = value
    return out, ""


def _act(
    tool: str, step: dict[str, Any], done: dict[str, dict[str, Any]] | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Run the public tool a step names, through its own door, with checked
    arguments; ``(what it answered, None)`` or a refusal.  A tool that
    refuses (a gate, an offline printer) is an ANSWER, sent back as such;
    only a step outside the fence is refused here."""
    name = step.get("tool")
    shape = ACT_TOOLS.get(name) if isinstance(name, str) else None
    if shape is None:
        return None, _refusal(
            tool, "LOCAL_STEP_REFUSED",
            f"{tool} asked this computer to run {name!r} on your printer, "
            "which Kiln does not let a served tool ask for. Nothing was run.",
        )
    args = _checked_act_args(shape, step.get("args"))
    if args is None:
        return None, _refusal(
            tool, "LOCAL_STEP_REFUSED",
            f"{tool} asked this computer to run {name} with something Kiln "
            "does not let a served tool pass to it. Nothing was run.",
        )
    args, unnamed = _named_from(args, done or {})
    if args is None:
        return {"ran": False, "skipped_because": unnamed}, None
    import kiln.server as server
    from kiln.tool_results import unwrap_tool_result

    door = getattr(server, name, None)
    if not callable(door):
        return None, _refusal(
            tool, "LOCAL_STEP_REFUSED",
            f"{tool} asked this computer to run {name}, which this version "
            "of Kiln does not have. Nothing was run. Updating Kiln may add it.",
        )
    try:
        answered = unwrap_tool_result(door(**args))
    except Exception as exc:  # noqa: BLE001 — the tool's own failure is its answer
        logger.debug("served steps: %s raised", name, exc_info=True)
        answered = {"success": False, "error": f"{type(exc).__name__}: {exc}"}
    return _act_result(answered), None


def actions_in(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The steps of *steps* that act on a printer."""
    return [s for s in steps if s.get("kind") == ACT_KIND]


def describe_actions(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Each action as the user's agent is shown it before it runs: which
    tool, with what, and why -- a file by its name, never its path."""
    shown: list[dict[str, Any]] = []
    for step in actions_in(steps):
        args = step.get("args") if isinstance(step.get("args"), dict) else {}
        said = {
            k: (
                Path(v).name if k == "file_path" and isinstance(v, str)
                else f"<the {v.get('field')} {v.get('from_step')} answers with>" if isinstance(v, dict)
                else v
            )
            for k, v in args.items() if v is not None
        }
        shown.append({
            "id": step.get("id"),
            "tool": step.get("tool"),
            "args": said,
            "why": str(step.get("why") or ""),
            "allowed": isinstance(step.get("tool"), str) and step.get("tool") in ACT_TOOLS,
        })
    return shown


def actions_digest(steps: list[dict[str, Any]]) -> str:
    """A short name for exactly these actions: every tool, argument, reason
    and ordering, hashed.  The agent is shown it with the proposal and hands
    it back to run them; the servers decide the actions again on that call,
    and a list that came out different has a different name, so nothing
    runs that the agent did not see.  Nothing is kept here between the two
    calls: the name is recomputed from what each answer asks for."""
    import hashlib
    import json

    canonical = [
        {
            "id": step.get("id"),
            "tool": step.get("tool"),
            "args": step.get("args") if isinstance(step.get("args"), dict) else {},
            "why": str(step.get("why") or ""),
            "needs": list(step.get("needs") or []),
        }
        for step in actions_in(steps)
    ]
    return hashlib.sha256(json.dumps(canonical, sort_keys=True, default=str).encode()).hexdigest()[:16]


#: What the agent passes to run the actions it was shown: the digest of
#: that proposal.  Checked against the actions the servers decide on the
#: call that runs them.
ACTIONS_CHANGED = "ACTIONS_CHANGED"


def propose(tool: str, steps: list[dict[str, Any]], *, changed: bool = False) -> dict[str, Any]:
    """The answer that shows the user's agent what a served tool wants this
    computer to do on the printer, before any of it runs.  *changed*: the
    agent passed a digest and the servers decided differently this time;
    nothing ran, and this is the new list."""
    actions = describe_actions(steps)
    digest = actions_digest(steps)
    lines = []
    for n, action in enumerate(actions, 1):
        said = ", ".join(f"{k}={v!r}" for k, v in action["args"].items())
        line = f"{n}. {action['tool']}({said})"
        if action["why"]:
            line += f" -- {action['why']}"
        if not action["allowed"]:
            line += " [NOT ALLOWED: this version of Kiln will refuse it]"
        lines.append(line)
    opening = (
        f"Kiln's servers decided {tool}'s actions differently this time, so "
        "the ones you approved were not run. The new list:"
        if changed else
        f"Kiln's servers worked out what {tool} should do and want this "
        f"computer to do {len(actions)} thing(s) on your printer. Nothing "
        "has run yet:"
    )
    return {
        "success": False,
        "status": "actions_proposed",
        "code": ACTIONS_CHANGED if changed else "ACTIONS_PROPOSED",
        "tool": tool,
        "actions": actions,
        "actions_digest": digest,
        "error": (
            opening + "\n" + "\n".join(lines) + "\n\nShow these to the user. "
            f"To carry them out, call {tool} again with the same arguments and "
            f"run_actions=\"{digest}\". That names exactly this list: if the "
            "servers decide differently on that call, nothing runs and you are "
            "shown the new list. Each action goes through Kiln's own tool on "
            "this computer, with every check that tool makes (consent, the "
            "pre-flight, one printer at a time), exactly as if you had called "
            "it yourself."
        ),
    }


def read_first(
    tool: str, reads: Any, *, printer_name: str,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Do the reading steps a served tool's listing says it needs before it
    can answer at all, so the one round its call gets can be the round
    that acts.  ``({step id: {"data": ...}}, None)`` or ``({}, refusal)``.
    Only a reading kind is ever done this way."""
    if not isinstance(reads, list) or len(reads) > MAX_STEPS:
        return {}, _refusal(
            tool, "LOCAL_STEP_REFUSED",
            f"{tool} is listed as needing more from this computer up front "
            "than Kiln does for a served tool. Nothing was sent.",
        )
    results: dict[str, Any] = {}
    for read in reads:
        kind = read.get("kind") if isinstance(read, dict) else None
        do = _READS.get(kind) if isinstance(kind, str) else None
        if do is None:
            return {}, _refusal(
                tool, "LOCAL_STEP_REFUSED",
                f"{tool} is listed as needing this computer to do something "
                "before it is even asked, and only a reading is done that "
                "way. Nothing was sent.",
            )
        step = {
            "kind": kind,
            "id": kind,
            "printer_name": printer_name,
            "want": list(read.get("want") or []),
        }
        facts, refusal = do(tool, step)
        if refusal is not None or facts is None:
            return {}, refusal
        results[kind] = {"data": facts}
    return results, None


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
    *,
    act: bool = False,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Do *steps* and send each result up with *send* (a file -> token).

    Returns ``({step id: {"token": ...} | {"data": {...}}}, None)`` -- a
    token for a step that made a file, the facts themselves for a step that
    read something, the tool's answer for a step that acted -- or
    ``({}, refusal)`` at the first step that is not allowed, fails, or
    cannot be sent.

    An action runs only with *act*: the user's agent has seen the actions
    and said so (``run_actions``).  Actions run after the other steps, in
    order; one that ``needs`` an earlier action runs only when that one
    succeeded, and is otherwise reported as not run -- so a start never
    follows a failed upload, while the machines of a fleet stop are each
    reached whatever the others answered.
    """
    actions = actions_in(steps)
    others = [s for s in steps if s.get("kind") != ACT_KIND]
    if not steps or len(others) > MAX_STEPS or len(actions) > MAX_ACT_STEPS:
        return {}, _refusal(
            tool, "LOCAL_STEP_REFUSED",
            f"{tool} asked this computer for more than Kiln allows a served "
            "tool to ask. Nothing was run.",
        )
    if actions and not act:
        return {}, _refusal(
            tool, "LOCAL_STEP_REFUSED",
            f"{tool} asked this computer to act on your printer and nobody "
            "here said to. Nothing was run.",
        )
    results: dict[str, Any] = {}
    for step in others:
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
    # Every action is checked BEFORE the first one runs: a list with one
    # step outside the fence runs nothing, rather than half of itself.
    for step in actions:
        step_id = step.get("id")
        if not (isinstance(step_id, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,40}", step_id)) or step_id in results:
            return {}, _refusal(
                tool, "LOCAL_STEP_REFUSED",
                f"{tool} asked this computer to act on your printer in a way "
                "Kiln does not carry out for a served tool. Nothing was run.",
            )
        name = step.get("tool")
        shape = ACT_TOOLS.get(name) if isinstance(name, str) else None
        if shape is None or _checked_act_args(shape, step.get("args")) is None:
            _answer, refusal = _act(tool, step)
            return {}, refusal
        needs = step.get("needs") or []
        if not isinstance(needs, list) or any(not isinstance(n, str) for n in needs):
            return {}, _refusal(
                tool, "LOCAL_STEP_REFUSED",
                f"{tool} asked this computer to act on your printer in a way "
                "Kiln does not carry out for a served tool. Nothing was run.",
            )
    done: dict[str, dict[str, Any]] = {}
    for step in actions:
        step_id = str(step.get("id"))
        unmet = [n for n in (step.get("needs") or []) if not act_succeeded(done.get(n))]
        if unmet:
            done[step_id] = {
                "ran": False,
                "skipped_because": f"{', '.join(unmet)} did not succeed, so this was not run",
            }
        else:
            answered, refusal = _act(tool, step, done)
            if refusal is not None or answered is None:
                return {}, refusal
            done[step_id] = answered
        results[step_id] = {"data": done[step_id]}
    return results, None
