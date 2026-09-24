"""The executor: run a motion plan document against a printer and say what happened.

A plan (``schema: "motion_plan/1"``, see :mod:`kiln._pro_motion_bridge`)
is a list of described steps with their G-code, plus what the sequence
homes, heats and leaves.  Where the plan came from -- local kiln-pro, the
hosted service, or the on-disk cache -- makes no difference here: the
lines are sent the same way, the fault watch is the same, the homed flags
are read the same, and the person is told the same things before and
after each motion.

Step mode is local: the whole plan is held, ``plan_only`` sends nothing
and describes every step, ``step=N`` sends step N and describes N+1, and
a full run sends the concatenation -- the same lines, pinned by the plan
builder so the two cannot drift.

Nothing here decides whether a motion is allowed.  The doors decide that
(printing / paused, engagement, the plate gate, the person's consent) and
call in with a plan the gate has already passed.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from kiln.gcode import axis_word_pattern
from kiln.printers.base import FilamentOpPlan, FilamentOpResult, HomeResult, HomeStep, PrinterError
from kiln.printers.command_verdict import CommandVerdict

logger = logging.getLogger(__name__)

#: How long a home or park watches for a fault code after sending, unless asked.
DEFAULT_FAULT_WATCH_S: float = 10.0


def steps_of(doc: dict[str, Any]) -> list[HomeStep]:
    """The plan's steps as :class:`HomeStep`, in order.  Malformed steps are refused."""
    out: list[HomeStep] = []
    for raw in doc.get("steps") or []:
        if not isinstance(raw, dict):
            raise PrinterError("The motion plan carried a malformed step; nothing was sent.")
        gcode = raw.get("gcode")
        if not isinstance(gcode, list) or not gcode or not all(isinstance(line, str) and line.strip() for line in gcode):
            raise PrinterError("The motion plan carried a step with no G-code lines; nothing was sent.")
        out.append(HomeStep(
            number=int(raw.get("number") or len(out) + 1),
            label=str(raw.get("label") or f"step {len(out) + 1}"),
            you_will_see=str(raw.get("you_will_see") or ""),
            stops_when=str(raw.get("stops_when") or ""),
            gcode=list(gcode),
            leaves=[str(x) for x in (raw.get("leaves") or [])],
        ))
    if not out:
        raise PrinterError("The motion plan carried no steps; nothing was sent.")
    return out


def _homes_up_to(doc: dict[str, Any], steps: list[HomeStep], upto: int) -> list[str]:
    """Axes homed by steps 1..*upto*.

    A step may say what it homes (``homes``); otherwise it is read off its
    own G-code: ``G28 X`` counts as X and Y (the plan's own step text says
    when a bed moves with it), ``G28 Z`` as Z, a bare ``G28`` as everything
    the plan says it homes.
    """
    raw_steps = doc.get("steps") or []
    homed: list[str] = []
    for index in range(min(upto, len(steps))):
        raw = raw_steps[index] if index < len(raw_steps) and isinstance(raw_steps[index], dict) else {}
        declared = raw.get("homes")
        axes: list[str]
        if isinstance(declared, list):
            axes = [str(a).upper() for a in declared]
        else:
            axes = []
            for line in steps[index].gcode:
                head = line.split(";", 1)[0].strip().upper()
                if head == "G28":
                    axes += [a for a in (doc.get("homed_axes") or [])]
                elif head.startswith("G28 X"):
                    axes += ["X", "Y"]
                elif head.startswith("G28 Z"):
                    axes += ["Z"]
                elif head.startswith("G28 Y"):
                    axes += ["Y"]
        for a in axes:
            if a in "XYZ" and a not in homed:
                homed.append(a)
    return [a for a in "XYZ" if a in homed]


def read_homed_axes(adapter: Any, bits: Any) -> set[str] | None:
    """Which axes the printer's own status says are homed, or ``None``.

    *bits* is the plan's ``homed_flag_bits`` (axis → bit index): the bit
    layout of the status field the maker's own tooling reads.
    Zero means unknown, as that tooling reads it.  ``None`` without a
    layout, without the field, or on a backend that keeps no status cache.
    """
    if not isinstance(bits, dict) or not bits:
        return None
    lock = getattr(adapter, "_state_lock", None)
    status = getattr(adapter, "_last_status", None)
    if lock is None or not isinstance(status, dict):
        return None
    with lock:
        raw = status.get("home_flag")
    if not isinstance(raw, int) or isinstance(raw, bool) or raw == 0:
        return None
    try:
        return {str(axis).upper() for axis, bit in bits.items() if (raw >> int(bit)) & 1}
    except (TypeError, ValueError):
        return None


def _watch_for_fault(adapter: Any, faults_before: Any, watch: float) -> tuple[str, str] | None:
    """A ``(code, kind)`` fault raised inside *watch* seconds, or ``None``.

    Only backends that snapshot fault codes (``_snapshot_faults``) are
    watched; the rest answer after the send.
    """
    snapshot = getattr(adapter, "_snapshot_faults", None)
    if not callable(snapshot) or faults_before is None:
        return None
    deadline = time.monotonic() + watch
    while True:
        new_faults = set(snapshot()) - set(faults_before)
        if new_faults:
            ordered = sorted(new_faults, key=lambda pair: (pair[1] != "hms", pair[0]))
            return ordered[0]
        if time.monotonic() >= deadline:
            return None
        time.sleep(1.0)


def _describe_fault(adapter: Any, code: str, kind: str) -> str:
    try:
        from kiln.printers.bambu import describe_bambu_filament_fault

        hint, _page = describe_bambu_filament_fault(code, kind=kind)
        return hint
    except Exception:  # noqa: BLE001 -- the code alone is still the honest answer
        return f"the printer raised {code}"


def note_motion_outcome(adapter: Any, verb: str, kind: str, detail: Any = None) -> None:
    """Count what a served plan did on this machine, for the bench evidence.

    The walk of a derived sequence happens on the owner's own printer and
    never touches a server, so this counter (``motion_outcomes`` in
    :mod:`kiln.daily_stats`) is the only way anyone learns that a sequence
    derived from a vendor's file runs clean on the machine it was derived
    for.  Classes only -- the model token, the verb, how far a walk got,
    which code stopped it, why a run was refused.  Never raises: a
    counter never blocks a motion.
    """
    try:
        from kiln import daily_stats

        model = getattr(adapter, "_printer_model", None)
        if kind == "step_sent":
            step, total = detail
            daily_stats.record_motion_step(model, verb, int(step), int(total))
        elif kind == "fault":
            daily_stats.record_motion_fault(model, verb, detail)
        else:
            daily_stats.record_motion_outcome(model, verb, kind, detail)
    except Exception as exc:  # noqa: BLE001 -- the motion already happened; the count is best effort
        logger.debug("motion outcome not recorded: %s", exc)


def run_home_plan(
    adapter: Any,
    doc: dict[str, Any],
    *,
    axes: str,
    options: dict[str, Any],
    action: str = "home",
    steps: list[HomeStep] | None = None,
) -> HomeResult:
    """Run a home or park plan: all of it, one step of it, or none of it.

    *steps* may replace the plan's own (a served detour around a recorded
    part); then ``sequence_source`` says so and no axis is claimed homed.
    """
    detour = steps is not None
    plan_steps = steps if steps is not None else steps_of(doc)
    homed = [] if detour else [str(a) for a in (doc.get("homed_axes") or []) if str(a) in "XYZ"]
    printer_id = str(doc.get("printer_id") or getattr(adapter, "_printer_model", "") or "this printer")
    heats = doc.get("heats_nozzle_to_c") if not detour else None
    common: dict[str, Any] = dict(
        axes=axes,
        mechanism=str(doc.get("mechanism") or "gcode"),
        sequence_source="kiln_pro_motion_plan" if detour else str(doc.get("sequence_source") or "served_plan"),
        heats_nozzle_to_c=float(heats) if isinstance(heats, (int, float)) else None,
        steps=[s.to_dict() for s in plan_steps],
        action=action,
    )
    if doc.get("from_cache"):
        # The plan Kiln kept from its last serve, and why the service was
        # not the source this time -- said, not silently substituted.
        common["details"] = {"plan_source": "cache"}
        if doc.get("cache_because"):
            common["details"]["because"] = str(doc["cache_because"])
    step = options.get("step")
    if options.get("plan_only"):
        return HomeResult(
            success=True, outcome="accepted", homed_axes=[],
            message=(f"Plan only -- nothing sent. {len(plan_steps)} steps for {printer_id}; "
                     f"send them one at a time with step=1 … step={len(plan_steps)}, or all at once without it."),
            step_sent=None, next_step=plan_steps[0].to_dict(),
            details={**common.pop("details", {}), "sent": False}, **common,
        )
    if step is not None:
        if step > len(plan_steps):
            raise PrinterError(f"The sequence has {len(plan_steps)} steps; step {step} does not exist.")
        chosen: HomeStep | None = plan_steps[step - 1]
        lines = list(chosen.gcode)
        nxt = plan_steps[step].to_dict() if step < len(plan_steps) else None
    else:
        chosen = None
        lines = [line for s in plan_steps for line in s.gcode]
        nxt = None
    snapshot = getattr(adapter, "_snapshot_faults", None)
    faults_before = set(snapshot()) if callable(snapshot) else None
    verdict = CommandVerdict.coerce(adapter.send_gcode(lines), what=action)
    details_base = dict(common.pop("details", {}))
    verb = action if action in ("home", "park") else "home"
    if not verdict.ok:
        if not detour:
            note_motion_outcome(adapter, verb, "fault", "rejected")
        return HomeResult(
            success=False, outcome="failed", homed_axes=[],
            message=f"The printer refused the {action} script: {verdict.message}",
            step_sent=step, next_step=nxt,
            details={**details_base, "gcode": lines, "verdict": verdict.to_dict()}, **common,
        )
    watch = float(options.get("wait_seconds") or DEFAULT_FAULT_WATCH_S)
    ceiling = options.get("wait_ceiling_seconds")
    if ceiling is not None and float(ceiling) > 0:
        watch = min(watch, float(ceiling))
    fault = _watch_for_fault(adapter, faults_before, watch)
    if fault is not None:
        code, kind = fault
        hint = _describe_fault(adapter, code, kind)
        if not detour:
            note_motion_outcome(adapter, verb, "fault", code)
        return HomeResult(
            success=False, outcome="failed", homed_axes=homed,
            message=f"The printer raised {code} during the {action}: {hint}",
            error_code=code, error_hint=hint, step_sent=step, next_step=nxt,
            leaves=list(chosen.leaves) if chosen else [],
            details={**details_base, "gcode": lines, "fault": {"code": code, "kind": kind}}, **common,
        )
    if chosen is not None:
        done_axes = _homes_up_to(doc, plan_steps, step) if not detour else []
        after = (f" Next: step {nxt['number']} -- {nxt['label']}: {nxt['you_will_see']}." if nxt
                 else " That was the last step; the sequence is complete.")
        if not detour:
            note_motion_outcome(adapter, verb, "step_sent", (step, len(plan_steps)))
        return HomeResult(
            success=True, outcome="accepted", homed_axes=done_axes,
            message=(f"Step {step} of {len(plan_steps)} sent ({chosen.label}): {chosen.you_will_see}. "
                     f"Stops when {chosen.stops_when}. Raised no fault in {watch:g}s.{after}"
                     + (" Left armed: " + "; ".join(chosen.leaves) + "." if chosen.leaves else "")),
            step_sent=step, next_step=nxt, leaves=list(chosen.leaves),
            resting_position={"described": f"wherever step {step} ends -- see you_will_see"},
            details={**details_base, "gcode": lines, "fault_watch_seconds": watch}, **common,
        )
    flags = read_homed_axes(adapter, doc.get("homed_flag_bits"))
    if detour:
        return HomeResult(
            success=True, outcome="accepted", homed_axes=[],
            message=(f"Ran a {len(plan_steps)}-step plan around the part on the plate instead of the "
                     f"model's own sequence ({'; '.join(s.label for s in plan_steps)}). Kiln's own homing "
                     "commands addressed no axis; the head rests where the last step describes. "
                     f"Raised no fault in {watch:g}s."),
            resting_position={"described": plan_steps[-1].you_will_see},
            step_sent=None, next_step=None, leaves=list(plan_steps[-1].leaves),
            details={**details_base, "gcode": lines, "fault_watch_seconds": watch, "verification_source": "not_read_back",
                     **({"firmware_homed_axes": sorted(flags)} if flags is not None else {})},
            **common,
        )
    note_motion_outcome(adapter, verb, "full_run")
    wanted = set(homed)
    if flags is not None and wanted <= flags:
        outcome, source = "confirmed", "homed_flag_bits"
        flag_note = f" The printer's own homed flags report {', '.join(sorted(flags))} homed."
    elif flags is not None:
        outcome, source = "accepted", "homed_flag_bits_partial"
        flag_note = (f" The printer's homed flags report {', '.join(sorted(flags)) or 'no axis'} homed and "
                     f"not {', '.join(sorted(wanted - flags))} -- read printer_status again; the routine may still be running.")
    else:
        outcome, source = "accepted", "not_read_back"
        flag_note = " The printer reported no homed flag Kiln could read this time."
    summary = str(doc.get("summary") or f"Ran the {action} sequence for {printer_id}.")
    resting = doc.get("resting_position") if isinstance(doc.get("resting_position"), dict) else {}
    return HomeResult(
        success=True, outcome=outcome, homed_axes=homed,
        message=f"{summary}{flag_note} Raised no fault in {watch:g}s.",
        resting_position=dict(resting),
        step_sent=None, next_step=None, leaves=[],
        details={**details_base, "gcode": lines, "fault_watch_seconds": watch, "verification_source": source,
                 **({"firmware_homed_axes": sorted(flags)} if flags is not None else {})},
        **common,
    )


def run_finish(adapter: Any, result: Any, finish: dict[str, Any]) -> str | None:
    """The plan's cool-down after the heater goes off: fan on, wait for the
    hand-off temperature, fan off -- and the sentence for the answer.

    ``None`` when the block is unusable, so the caller falls back to the
    plain "still hot" floor rather than claim a cool-down.

    The wait does not happen inside the request.  Measured 2026-09-18 on
    an A1: the purge's frame was still waiting for the hand-off when the
    host restarted the server, and the fan-off died with it.  The fan-on
    is sent here; the watch and the fan-off run in a thread the request
    cannot take with it (:func:`kiln.printers.routine_ledger.start_cooldown`),
    registered so a shutdown settles it and marked on disk so the next
    ``printer_status`` finishes it if this process is killed outright.
    A nozzle already at or below the hand-off gets its fan-off right here.
    """
    try:
        fan_on_line = str(finish["fan_on"])
        fan_off_line = str(finish["fan_off"])
        threshold = float(finish["handoff_c"])
        timeout = float(finish.get("timeout_s") or 150.0)
    except (KeyError, TypeError, ValueError):
        return None
    waiter = getattr(adapter, "_wait_for_hotend_below", None)
    if not callable(waiter):
        return None
    try:
        fan_on = bool(getattr(adapter.send_gcode([fan_on_line]), "ok", True))
    except Exception:  # noqa: BLE001 -- report it, do not hide it
        fan_on = False
    reached, reading = waiter(threshold, timeout=0.0)
    result.details["hotend_at_answer"] = reading
    placement = result.details.get("purge_station")
    parked = isinstance(placement, dict) and placement.get("status") == "parked"
    over = bool(finish.get("over_chute", True)) and parked
    where = (
        " The head is over the chute; anything that let go on the way down landed there."
        if over else
        " The head did not move; anything that let go on the way down is under the nozzle."
    )
    shown = "unknown" if reading is None else f"{reading:g} °C"
    if reached:
        try:
            fan_off = bool(getattr(adapter.send_gcode([fan_off_line]), "ok", True))
        except Exception:  # noqa: BLE001
            fan_off = False
        result.details["cooled_below_c"] = threshold
        result.details["fan"] = "off" if fan_off else "ON -- the fan-off command was refused"
        return (
            f"{'Part fan on full' if fan_on else 'Fan command refused'}; the nozzle already read {shown}, "
            f"at or below the {threshold:g} °C hand-off the machine's own start sequence waits for, "
            f"so fan {'off' if fan_off else 'NOT off'}.{where}"
        )
    from kiln.printers.routine_ledger import start_cooldown

    start_cooldown(adapter, fan_off=fan_off_line, handoff_c=threshold, timeout_s=timeout)
    result.details["cooled_below_c"] = None
    result.details["fan"] = f"on full (cooling; Kiln turns it off at or below {threshold:g} °C)"
    result.details["cooldown"] = {
        "status": "running",
        "handoff_c": threshold,
        "timeout_s": timeout,
        "fan_off": fan_off_line,
    }
    return (
        f"Part fan {'on full' if fan_on else 'command refused'} while it cools; the nozzle read {shown} "
        f"when this answer left. Kiln keeps watching from the server and turns the fan off on its own once "
        f"the nozzle reads at or below the {threshold:g} °C hand-off the machine's own start sequence waits "
        f"for; printer_status shows the fan, and finishes the cool-down itself if this server was stopped "
        f"first.{where}"
    )


# ---------------------------------------------------------------------------
# A wipe, one step at a time
# ---------------------------------------------------------------------------

#: How long one wipe step is watched for a fault after it is sent.
WIPE_STEP_WATCH_S: float = 10.0
#: A G-code word that moves the extruder (``G1 E-1 F500``, ``G1 E-.8``).
_E_WORD = axis_word_pattern("E")


def wipe_steps_of(doc: dict[str, Any]) -> list[dict[str, Any]]:
    """The wipe plan's steps as dicts, in order -- or ``[]`` for a plan that
    describes none (a full run only).  A step that ``heats`` carries no
    lines: the executor heats to the op's gated temperature and waits on
    the thermistor, as the full run does.  Every other step is checked the
    way :func:`steps_of` checks a home's."""
    raw_steps = doc.get("steps") or []
    if not raw_steps:
        return []
    out: list[dict[str, Any]] = []
    for raw in raw_steps:
        if not isinstance(raw, dict):
            raise PrinterError("The wipe plan carried a malformed step; nothing was sent.")
        gcode = raw.get("gcode")
        if not isinstance(gcode, list) or not all(isinstance(line, str) and line.strip() for line in gcode):
            raise PrinterError("The wipe plan carried a step with no G-code list; nothing was sent.")
        if not gcode and not raw.get("heats"):
            raise PrinterError("The wipe plan carried an empty step that does not heat; nothing was sent.")
        out.append({
            "number": int(raw.get("number") or len(out) + 1),
            "label": str(raw.get("label") or f"step {len(out) + 1}"),
            "you_will_see": str(raw.get("you_will_see") or ""),
            "stops_when": str(raw.get("stops_when") or ""),
            "gcode": list(gcode),
            "leaves": [str(x) for x in (raw.get("leaves") or [])],
            "touches_plate": bool(raw.get("touches_plate")),
            "heats": bool(raw.get("heats")),
        })
    return out


def wipe_plan_only(doc: dict[str, Any], plan: FilamentOpPlan, steps: list[dict[str, Any]]) -> FilamentOpResult:
    """Describe the wipe's steps and send nothing."""
    printer_id = str(doc.get("printer_id") or "this printer")
    only = doc.get("step_mode_only") if isinstance(doc.get("step_mode_only"), dict) else None
    return FilamentOpResult(
        success=True, action=plan.action,
        message=(f"Plan only -- nothing sent. {len(steps)} steps for {printer_id}; send them one at a time "
                 f"with step=1 … step={len(steps)}"
                 + (". This wipe has not been run on a real machine, so it runs in step mode only."
                    if only else ", or all at once with no step named.")),
        extrusion_verified=None, verification_source="plan_only",
        slot=plan.slot, material=plan.material, temperature=plan.temperature,
        steps=steps, step_sent=None, next_step=steps[0],
        details={"sent": False, "purge_station": dict(doc.get("placement") or {}),
                 **{k: v for k, v in (doc.get("details") or {}).items() if k != "resting_position"}},
    )


def run_wipe_step(
    adapter: Any, doc: dict[str, Any], plan: FilamentOpPlan, steps: list[dict[str, Any]], step: int,
) -> FilamentOpResult:
    """Send step *step* of the wipe plan and describe the next one.

    A step that ``heats`` is the op's own heat: the gated material
    temperature set on the adapter and waited for on the thermistor -- the
    same wait the full run makes in :meth:`_gcode_filament_move`.  Every
    other step is its lines, sent, then watched for a fault.  The last
    step's answer carries what the finish needs (the plan's own retract is
    not doubled; the resting place names where the head was left).
    """
    if step > len(steps):
        raise PrinterError(f"The sequence has {len(steps)} steps; step {step} does not exist.")
    chosen = steps[step - 1]
    nxt = steps[step] if step < len(steps) else None
    common: dict[str, Any] = dict(
        action=plan.action, extrusion_verified=None, slot=plan.slot, material=plan.material,
        temperature=plan.temperature, steps=steps, step_sent=step, next_step=nxt,
        leaves=list(chosen["leaves"]),
    )
    details: dict[str, Any] = {
        "gcode": list(chosen["gcode"]), "purge_station": dict(doc.get("placement") or {}),
        **{k: v for k, v in (doc.get("details") or {}).items() if k != "resting_position"},
    }
    watch = plan.wait_seconds(WIPE_STEP_WATCH_S)
    if chosen["heats"]:
        target = float(plan.temperature)
        try:
            adapter.set_tool_temp(target)
            # Hot between steps, across requests: a shutdown settles it.
            hold_heater = getattr(adapter, "_hold_heater", None)
            if callable(hold_heater):
                hold_heater(target)
        except PrinterError as exc:
            return FilamentOpResult(
                success=False, message=f"Could not set the hotend to {target:g}°C: {exc}",
                verification_source="heater_command_rejected", error_hint=str(exc), details=details, **common,
            )
        reached, reading = adapter._wait_for_hotend(target)
        if not reached:
            return FilamentOpResult(
                success=False,
                message=(f"The hotend did not reach {target:g}°C (last reading "
                         f"{'unknown' if reading is None else f'{reading:g}°C'}). Nothing was sent."),
                verification_source="thermistor", details={**details, "last_hotend_reading": reading}, **common,
            )
        details["hotend_reading"] = reading
        fault = None
    else:
        # A step that moves the extruder (the snap) needs the nozzle at the
        # op's temperature, read now on the thermistor -- the check every
        # extrude here makes, kept when a person walks the steps out of
        # order or skips the heat.  One reading, no wait: the heat step is
        # where the waiting happens.
        if any(_E_WORD.search(line) for line in chosen["gcode"]):
            target = float(plan.temperature)
            hot, reading = adapter._wait_for_hotend(target, timeout=0.0)
            if not hot:
                heat = next((s for s in steps if s["heats"]), None)
                first = f"run step {heat['number']} ({heat['label']}) first" if heat else "heat it first"
                note_motion_outcome(adapter, "wipe", "refused", "cold_extruder")
                return FilamentOpResult(
                    success=False,
                    message=(f"Step {step} ({chosen['label']}) moves the extruder and the nozzle reads "
                             f"{'nothing' if reading is None else f'{reading:g}°C'}, not the {target:g}°C the "
                             f"sequence heats to: {first}. Nothing was sent."),
                    verification_source="thermistor", details={**details, "last_hotend_reading": reading}, **common,
                )
        snapshot = getattr(adapter, "_snapshot_faults", None)
        faults_before = set(snapshot()) if callable(snapshot) else None
        verdict = CommandVerdict.coerce(adapter.send_gcode(list(chosen["gcode"])), what="wipe")
        if not verdict.ok:
            note_motion_outcome(adapter, "wipe", "fault", "rejected")
            return FilamentOpResult(
                success=False, message=f"The printer refused step {step} ({chosen['label']}): {verdict.message}",
                verification_source="firmware_rejected_move", error_hint=verdict.message,
                details={**details, "verdict": verdict.to_dict()}, **common,
            )
        fault = _watch_for_fault(adapter, faults_before, watch)
        if fault is not None:
            code, kind = fault
            hint = _describe_fault(adapter, code, kind)
            note_motion_outcome(adapter, "wipe", "fault", code)
            return FilamentOpResult(
                success=False, message=f"The printer raised {code} during step {step} ({chosen['label']}): {hint}",
                extrusion_verified=False, verification_source="bambu_fault_code",
                error_code=code, error_hint=hint, details={**details, "fault": {"code": code, "kind": kind}},
                **{k: v for k, v in common.items() if k != "extrusion_verified"},
            )
    after = (f" Next: step {nxt['number']} -- {nxt['label']}: {nxt['you_will_see']}." if nxt
             else " That was the last step; the sequence is complete.")
    details["fault_watch_seconds"] = watch
    note_motion_outcome(adapter, "wipe", "step_sent", (step, len(steps)))
    if nxt is None:
        # The last step: the door's finish (heater off, the plan's cool-down)
        # runs after this answer and must not pull back again -- the snap
        # step already sent the plan's own retract.
        details["end_retract_mm"] = float(doc.get("end_retract_mm") or 0.0)
        resting = (doc.get("details") or {}).get("resting_position")
        if isinstance(resting, dict):
            details["resting_position"] = dict(resting)
        if isinstance(doc.get("finish"), dict):
            details["finish"] = dict(doc["finish"])
    return FilamentOpResult(
        success=True,
        message=(f"Step {step} of {len(steps)} sent ({chosen['label']}): {chosen['you_will_see']}. "
                 f"Stops when {chosen['stops_when']}. Raised no fault in {watch:g}s.{after}"
                 + (" Left armed: " + "; ".join(chosen["leaves"]) + "." if chosen["leaves"] else "")),
        verification_source="no_fault_within_window", details=details, **common,
    )
