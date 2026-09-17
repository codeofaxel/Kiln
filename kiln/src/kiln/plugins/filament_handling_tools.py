"""Filament handling tools plugin — load, unload, purge, wipe.

The four doors a user meets when a spool has to move or a tip has to be
cleaned: feed a slot to the nozzle, pull it back out, push a short length
through to learn whether the melt zone is clear, or wipe the tip on the
machine's own pad.  Each is one call into the adapter's gated template
(:meth:`~kiln.printers.base.PrinterAdapter.load_filament` and siblings),
so the safety gate — not mid-print, safety-profile ceiling, the spool's
own temperature window, the cold-extrusion floor — runs the same way
here, from the CLI, and from a recovery flow.

Purge doubles as the clog test.  ``extrusion_verified`` in the answer is
``True`` / ``False`` only from a signal the printer genuinely produced
and ``None`` when it produced none; ``error_hint`` carries the printer's
own fault code in plain language.  Every answer says where the plastic
went — parked over the model's own purge chute, or in place and why —
and ``purge_station`` in the answer carries that as data.

Auto-discovered by :func:`~kiln.plugin_loader.register_all_plugins` —
no manual imports needed.  The tool bodies are module-level functions so
``kiln filament …`` can call the very same code the MCP tool runs.
"""

from __future__ import annotations

import logging
import os
from typing import Any

_logger = logging.getLogger(__name__)

#: Wall-clock ceiling on how long a filament tool WATCHES the printer for
#: its answer.  Hosts bound the MCP tool CALL (~60 s observed 2026-08-19;
#: the same window ``model_visualizer._CALL_BUDGET_S`` fits inside), and a
#: watch longer than that finishes on the server after the client has
#: given up, so the answer is lost.  Measured 2026-09-15: ``load_filament
#: (wait_seconds=180)`` on an A1 came back "Request timed out" while the
#: load itself ran to completion -- the caller got no step, no fault
#: code, no extrusion_verified.  The adapters assume no window (their own
#: load / unload defaults are longer than it), so this door is where the
#: window is declared, through the plan every backend reads.  A watch
#: that runs out answers ``outcome: "accepted"`` and names the read that
#: finishes it.  Env-tunable; 0 disables.  The default leaves room inside
#: the ~60 s for the AMS and state reads around the watch.
_WAIT_BUDGET_S = float(os.environ.get("KILN_FILAMENT_WAIT_BUDGET_S", "40") or 0)


def _outcome(result: Any) -> str:
    """``confirmed`` / ``accepted`` / ``failed`` -- the one field the
    other control verbs (``set_temperature``, ``start_print``) branch on.

    ``confirmed``: the printer showed the effect (``extrusion_verified``
    True).  ``failed``: the gate or the printer refused, or a fault was
    raised.  ``accepted``: the command was sent and not refused and the
    printer has not shown the effect -- a purge on a machine with no flow
    sensor, or a watch that ran out before the AMS answered.
    """
    if result.extrusion_verified is True:
        return "confirmed"
    if result.success or result.verification_source == "timeout_no_signal":
        return "accepted"
    return "failed"

#: Rate limits in server.py's ``(min_interval_ms, max_per_minute)`` form —
#: these move heaters and steppers, so they get the pause/resume cadence.
_RATE_LIMITS: dict[str, tuple[int, int]] = {
    "load_filament": (5000, 6),
    "unload_filament": (5000, 6),
    "purge_filament": (5000, 6),
    "wipe_nozzle": (5000, 6),
}

#: The adapter method (and MCP tool name) behind each action word.
_TOOL_NAMES: dict[str, str] = {
    "load": "load_filament",
    "unload": "unload_filament",
    "purge": "purge_filament",
    "wipe": "wipe_nozzle",
}


def run_filament_op(
    action: str,
    *,
    printer_name: str | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """The one door every surface calls.

    Resolves the target machine the way the other control verbs do,
    refuses on an emergency latch, calls the adapter's gated template, and
    returns the structured envelope: ``success`` + the
    :class:`~kiln.printers.base.FilamentOpResult` fields on success, the
    standard ``error`` block (with the result riding in ``filament``) when
    the printer refused or the operation failed.

    Auth, rate limit, and confirmation are the MCP tool's business and run
    before this; the CLI, like ``kiln fan``, goes through the tool.
    """
    import kiln.server as _srv
    from kiln.hotend_safety import MOLTEN_FILAMENT_WARNING
    from kiln.printers.base import FilamentHandlingUnsupported, PlateClearRequired, PrinterError
    from kiln.registry import PrinterNotFoundError

    verb = {
        "load": "load filament on",
        "unload": "unload filament from",
        "purge": "purge filament on",
        "wipe": "wipe the nozzle on",
    }[action]
    what = "the nozzle" if action == "wipe" else "filament"
    tool_name = _TOOL_NAMES[action]
    if block := _srv._emergency_latch_error(
        tool_name, _srv._resolve_effective_printer_name(printer_name)
    ):
        return block
    try:
        try:
            adapter, target_name = _srv._resolve_control_target(printer_name)
        except PrinterNotFoundError:
            return _srv._unknown_printer_error(printer_name, verb)
        if block := _srv._emergency_latch_error(tool_name, target_name):
            return block
        if not adapter.capabilities.can_handle_filament:
            return _srv._error_dict(
                f"{adapter.name} does not support filament handling through Kiln. "
                "Use the printer's own screen or web UI for this step.",
                code="UNSUPPORTED",
            )
        # Declare the host's window on the plan, once, for all three verbs.
        # The adapter bounds its watch by it; a caller who asked for more
        # is told what was applied instead of being answered by a timeout
        # with nothing in it.
        asked = kwargs.get("wait_seconds")
        clamp_note: dict[str, Any] = {}
        if _WAIT_BUDGET_S > 0:
            kwargs["wait_ceiling_seconds"] = _WAIT_BUDGET_S
            if asked is not None and float(asked) > _WAIT_BUDGET_S:
                clamp_note = {
                    "wait_seconds_requested": asked,
                    "wait_seconds_applied": _WAIT_BUDGET_S,
                    "wait_note": (
                        f"wait_seconds={asked:g} is longer than the "
                        f"{_WAIT_BUDGET_S:g}s a tool call can wait inside the "
                        "client's request window, so the watch was bounded "
                        f"to {_WAIT_BUDGET_S:g}s. If the printer has not answered "
                        "by then the result says what to read next. "
                        "KILN_FILAMENT_WAIT_BUDGET_S sets the ceiling; 0 removes it."
                    ),
                }
        method = getattr(adapter, tool_name)
        result = method(**kwargs)
        if _srv._is_heater_watchdog_machine(adapter):
            _srv._get_heater_watchdog().notify_heater_set()
        _srv._audit(
            tool_name,
            "executed" if result.success else "failed",
            details={"printer": target_name, **result.to_dict()},
        )
        # The burn warning, at the one door every surface goes through — the
        # MCP tools and the CLI both land here, so neither can be forgotten.
        #
        # Narrow on purpose. A purge IS the clog test: a person stands over
        # the nozzle watching for a clean stream, so it always warns. A load
        # or unload that SUCCEEDED was machine-driven and needs nothing; one
        # that FAILED is the moment a human starts pulling at things, so that
        # warns too. Warning on a clean spool change would be the noise that
        # teaches people to skip the line.
        #
        # troubleshoot_printer names this tool as the next step in the very
        # branch that fires its own warning — so without this, the warning
        # stopped one call short of the moment it describes.
        warn = (action in ("purge", "wipe") or not result.success) and not kwargs.get("plan_only")
        outcome = _outcome(result)
        if not result.success:
            extra: dict[str, Any] = {
                "printer_name": target_name,
                "outcome": outcome,
                "filament": result.to_dict(),
                **clamp_note,
            }
            # A watch that ran out is not a refusal: the routine is most
            # likely still running.  Lift the pointer to the top so the
            # caller reads the printer next instead of sending the command
            # again.
            if result.verification_source == "timeout_no_signal":
                code = "FILAMENT_UNCONFIRMED"
                if result.details.get("next_read"):
                    extra["next_read"] = result.details["next_read"]
            else:
                code = "FILAMENT_FAULT" if result.error_code else "FILAMENT_OP_FAILED"
            if warn:
                extra["safety"] = MOLTEN_FILAMENT_WARNING
            return _srv._error_dict(result.message, code=code, extra=extra)
        payload = {
            "success": True,
            "printer_name": target_name,
            "outcome": outcome,
            **result.to_dict(),
            **clamp_note,
        }
        if warn:
            payload["safety"] = MOLTEN_FILAMENT_WARNING
        return payload
    except FilamentHandlingUnsupported as exc:
        return _srv._error_dict(str(exc), code="UNSUPPORTED", extra={"outcome": "failed"})
    except PlateClearRequired as exc:
        # The plate record (or the plan's own "this presses the plate")
        # stopped the motion before anything moved: the same envelope the
        # homing door returns, so a caller reads one code, looks at one
        # camera frame, and answers with plate_clear=true on either.
        return _srv._error_dict(
            str(exc), code="PLATE_CLEAR_REQUIRED",
            extra={"outcome": "failed", "snapshot_path": exc.snapshot_path, "plate_clear_required": True},
        )
    except (PrinterError, RuntimeError) as exc:
        # The gate refused (or the transport did) before anything moved.
        return _srv._error_dict(
            f"Failed to {action} {what}: {exc}", extra={"outcome": "failed"}
        )
    except Exception as exc:
        _logger.exception("Unexpected error in %s", tool_name)
        return _srv._error_dict(f"Unexpected error in {tool_name}: {exc}", code="INTERNAL_ERROR")


def _gated(tool_name: str, args: dict[str, Any]) -> dict[str, Any] | None:
    """Auth → rate limit → confirmation, in server.py's order.

    ``plan_only`` sends nothing, so it skips the rate limit and the
    confirmation -- but not auth: it still reads the printer's state and
    hands back the sequence, which is a read and is gated as one.
    """
    import kiln.server as _srv

    if args.get("plan_only"):
        return _srv._check_auth("read")
    if err := _srv._check_auth("temperature"):
        return err
    if err := _srv._check_rate_limit(tool_name):
        return err
    return _srv._check_confirmation(tool_name, args)


def load_filament(
    slot: int | None = None,
    material: str | None = None,
    temperature: float | None = None,
    length_mm: float | None = None,
    wait_seconds: float | None = None,
    keep_hot: bool = False,
    printer_name: str | None = None,
) -> dict:
    """Feed filament to the nozzle — an AMS tray on Bambu, a manual feed elsewhere.

    On Bambu Lab the firmware's own change-filament routine runs (retract,
    feed, purge) and the answer is read from the AMS: ``extrusion_verified``
    is ``True`` once ``tray_now`` reports the tray feeding the nozzle,
    ``False`` with the fault code in plain language if the routine raised
    one (the same HMS codes the touchscreen shows), ``None`` if neither
    arrived in ``wait_seconds``.  On Klipper a ``LOAD_FILAMENT`` / ``M701``
    macro from the printer's own config is used when it exists; otherwise,
    and on OctoPrint / Duet / serial, the hotend is heated and
    ``length_mm`` is fed at 3 mm/s — push the filament into the extruder
    first.

    Temperature is checked against the printer's safety profile, the
    spool's own ``nozzle_temp_min/max`` (AMS) or Kiln's material table,
    and the 170 °C cold-extrusion floor; refused outside any of them.
    Refused while a print is running (paused is fine).

    Branch on ``outcome`` — one field, three values, the same shape
    ``set_temperature`` and ``start_print`` use:

    - ``"confirmed"``: the printer showed the effect (the AMS reports the
      tray feeding the nozzle).
    - ``"accepted"``: the command was sent and not refused, and the printer
      has not shown the effect yet.  A Bambu load takes a minute or two
      and a tool call cannot wait that long, so this is the normal answer
      when the watch runs out: the routine is still running.  Do NOT send
      the load again — read ``next_read`` (``ams_status`` for ``tray_now``,
      ``printer_status`` for ``print_error``) to finish the answer.
    - ``"failed"``: the gate or the printer refused, or a fault was raised
      (``error_code`` / ``error_hint``).

    On Bambu Lab that check binds what Kiln sends, not what the routine
    does: the firmware heats to its own flush temperature for a filament
    change (250 °C observed on an A1 asked for 215, the ``M109 S250`` its
    own start sequence sets) and drops to the requested target only when
    the routine finishes.  The answer reports the temperature the firmware
    actually used (``firmware_hotend_target_c``) beside the one Kiln
    validated; the gate is not widened to match it.

    Where the declared model's own purge position is served one plan at a time through Kiln's hosted service for a printer paired to your Kiln sign-in, free, and kept on your machine so it works offline, the head is parked over the purge chute
    before the routine runs, so its purge falls into the chute instead of
    hanging off the nozzle at home; the answer says where the head was
    either way.

    Args:
        slot: AMS tray id as ``ams_status`` numbers them (0–3 on the first
            unit, 4–7 on the second).  Omit for the external / single spool.
        material: e.g. ``"PLA"`` — picks a temperature when none is given
            and no spool report supplies one.
        temperature: Hotend target in °C.  Omit to use the middle of the
            spool's or material's window.
        length_mm: Feed distance for the generic G-code path (default 60;
            bowden machines need more).  Ignored where the firmware's own
            routine decides.
        wait_seconds: How long to watch for the AMS to confirm.  Bounded to
            what a tool call can wait inside the client's request window
            (40 s unless ``KILN_FILAMENT_WAIT_BUDGET_S`` says otherwise); a
            longer ask is applied at the bound and the response says so.
        keep_hot: Leave the heater ON afterwards, for a caller about to do
            something else hot.  Off by default: every op ends with the
            heater off and says so; where the machine's own cool-down is
            served, the fan runs until the nozzle has cooled before the answer.
        printer_name: Which printer.  Omit for the default one.
    """
    args = {
        "slot": slot,
        "material": material,
        "temperature": temperature,
        "length_mm": length_mm,
        "wait_seconds": wait_seconds,
        "printer_name": printer_name,
    }
    if gate := _gated("load_filament", args):
        return gate
    kwargs: dict[str, Any] = {"slot": slot, "material": material, "temperature": temperature, "length_mm": length_mm}
    if wait_seconds is not None:
        kwargs["wait_seconds"] = wait_seconds
    if keep_hot:
        kwargs["keep_hot"] = True
    return run_filament_op("load", printer_name=printer_name, **kwargs)


def unload_filament(
    material: str | None = None,
    temperature: float | None = None,
    length_mm: float | None = None,
    wait_seconds: float | None = None,
    keep_hot: bool = False,
    printer_name: str | None = None,
) -> dict:
    """Pull filament out of the hotend — back into the AMS on Bambu, a heated retract elsewhere.

    Same temperature gate, verification, ``outcome`` vocabulary and wait
    bound as ``load_filament``: on Bambu ``extrusion_verified`` is ``True``
    once the AMS reports no tray feeding the nozzle (``tray_now`` 255); on
    Klipper an ``UNLOAD_FILAMENT`` / ``M702`` macro is used when the config
    has one; otherwise the hotend is heated and ``length_mm`` is retracted
    (default 80).

    Args:
        material / temperature / length_mm / wait_seconds / keep_hot /
            printer_name: as ``load_filament``.  Unload never retracts at
            the end (the filament is already out) but still turns the
            heater off unless ``keep_hot`` asks otherwise.
    """
    args = {
        "material": material,
        "temperature": temperature,
        "length_mm": length_mm,
        "wait_seconds": wait_seconds,
        "printer_name": printer_name,
    }
    if gate := _gated("unload_filament", args):
        return gate
    kwargs: dict[str, Any] = {"material": material, "temperature": temperature, "length_mm": length_mm}
    if wait_seconds is not None:
        kwargs["wait_seconds"] = wait_seconds
    if keep_hot:
        kwargs["keep_hot"] = True
    return run_filament_op("unload", printer_name=printer_name, **kwargs)


def purge_filament(
    length_mm: float = 30.0,
    material: str | None = None,
    temperature: float | None = None,
    slot: int | None = None,
    wait_seconds: float | None = None,
    keep_hot: bool = False,
    printer_name: str | None = None,
) -> dict:
    """Heat the nozzle and extrude a short length — the clog test.

    Reports what the printer could honestly say, not that a command was
    sent: ``extrusion_verified`` is ``False`` with ``error_hint`` when the
    firmware refused the move (cold-extrusion guard, Klipper
    ``can_extrude=false``) or raised an extrusion fault during or right
    after the purge (Bambu HMS codes, decoded to plain language);
    ``None`` when the move was accepted and the machine reports no flow
    signal — every backend without a flow sensor — in which case look at
    the nozzle for a clean stream.  It is never ``True`` on a purge alone.

    Refused while printing (paused is fine), below 170 °C, above the
    printer's safety ceiling, outside the loaded spool's own temperature
    window, and beyond 150 mm.

    The answer always says where the purge went.  Where the declared
    model's own purge position is served one plan at a time through Kiln's hosted service for a printer paired to your Kiln sign-in, free, and kept on your machine so it works offline — the head is parked over the purge chute before the
    heater is touched, the machine's own tail snap and shake follow the
    extrude, and ``purge_station.status`` is ``"parked"``.  Otherwise it is
    ``"in_place"`` with the reason (a paused print, or no served position
    for the model) and the head's coordinates where the printer reports
    any; no coordinate is ever inferred, and no model borrows a sibling's.
    A pad wipe is a separate door: ``wipe_nozzle``.

    Args:
        length_mm: Extrusion length, 1–150 mm (default 30).
        material: e.g. ``"PLA"`` — picks a temperature when none is given.
        temperature: Hotend target in °C.  Omit to use the middle of the
            spool's (AMS) or material's window.
        slot: AMS tray whose temperature window applies (Bambu).  Omit to
            use the tray currently feeding the nozzle.
        wait_seconds: How long after the purge to watch for a fault code
            on Bambu (default 10).  Bounded the same way as
            ``load_filament``'s.
        keep_hot: Leave the heater ON afterwards, for a caller about to do
            something else hot.  Off by default: every op ends with the
            heater off and says so; where the machine's own cool-down is
            served, the fan runs until the nozzle has cooled before the answer.
        printer_name: Which printer.  Omit for the default one.
    """
    args = {
        "length_mm": length_mm,
        "material": material,
        "temperature": temperature,
        "slot": slot,
        "wait_seconds": wait_seconds,
        "printer_name": printer_name,
    }
    if gate := _gated("purge_filament", args):
        return gate
    kwargs: dict[str, Any] = {"length_mm": length_mm, "material": material, "temperature": temperature, "slot": slot}
    if wait_seconds is not None:
        kwargs["wait_seconds"] = wait_seconds
    if keep_hot:
        kwargs["keep_hot"] = True
    return run_filament_op("purge", printer_name=printer_name, **kwargs)


def wipe_nozzle(
    material: str | None = None,
    temperature: float | None = None,
    slot: int | None = None,
    wait_seconds: float | None = None,
    keep_hot: bool = False,
    plate_clear: bool = False,
    step: int | None = None,
    plan_only: bool = False,
    printer_name: str | None = None,
) -> dict:
    """Clean the nozzle tip on the printer's own wipe pad.

    Heats to the loaded material's window (or the temperature given), parks
    over the purge chute while heating, snaps the tail with the vendor's
    own retract, then runs the wipe-pad pass the machine's start sequence
    runs and reports where the head ended up.  Only where the declared
    model's pad pass is served one plan at a time through Kiln's hosted service for a printer paired to your Kiln sign-in, free, and kept on your machine so it works offline
    (``printer_model`` in config.yaml names the model).  Any other model is
    refused with the reason rather than moved to a guessed coordinate: use
    the printer's own screen, or start a print — its start sequence wipes
    on the pad.  No model ever borrows a sibling's position.

    Same gate as ``purge_filament``: refused while printing, below 170 °C,
    above the safety ceiling, outside the loaded spool's own window.  Also
    refused while paused — the head is parked over the part and Kiln does
    not travel mid-print.  The pad is touched the machine's own way, on the
    way down in temperature.  ``extrusion_verified`` stays ``None``: Bambu
    acknowledges no G-code, so "no fault raised" is the strongest thing the
    printer can say — look at the tip.

    Args:
        material: e.g. ``"PLA"`` — picks a temperature when none is given.
        temperature: Hotend target in °C.  Omit to use the middle of the
            spool's (AMS) or material's window.
        slot: AMS tray whose temperature window applies (Bambu).  Omit to
            use the tray currently feeding the nozzle.
        wait_seconds: How long after the wipe to watch for a fault code
            (default 45).
        keep_hot: Leave the heater ON afterwards, for a caller about to do
            something else hot.  Off by default: every op ends with the
            heater off and says so; where the machine's own cool-down is
            served, the fan runs until the nozzle has cooled before the answer.
        plate_clear: A PERSON's statement that the plate is empty, given on
            THIS call.  Read on every call by a wipe whose plan presses the
            plate (its Z datum is taken on the plate, or the head has to
            cross the plate to reach the pad), whatever the plate record
            says -- the record cannot see a print started from the printer's
            own screen; without it the tool refuses with
            ``PLATE_CLEAR_REQUIRED`` and, where the printer has a camera,
            ``snapshot_path`` -- look, then call again with it set.  Never
            set it on a person's behalf.
        step: Send only this step of the wipe's plan (1-based); the answer
            describes the next one, and the finish (heater off, the
            cool-down) runs after the last step.  Step mode is how a wipe
            no one has yet run on a real machine is benched -- one motion,
            one report, one go -- and the only way such a wipe runs.
        plan_only: Describe the steps; send nothing.
        printer_name: Which printer.  Omit for the default one.
    """
    args = {
        "material": material,
        "temperature": temperature,
        "slot": slot,
        "wait_seconds": wait_seconds,
        "plate_clear": plate_clear,
        "step": step,
        "plan_only": plan_only,
        "printer_name": printer_name,
    }
    if gate := _gated("wipe_nozzle", args):
        return gate
    kwargs: dict[str, Any] = {"material": material, "temperature": temperature, "slot": slot}
    if wait_seconds is not None:
        kwargs["wait_seconds"] = wait_seconds
    if keep_hot:
        kwargs["keep_hot"] = True
    if plate_clear:
        kwargs["plate_clear"] = True
    if step is not None:
        kwargs["step"] = step  # the adapter refuses anything but a whole number from 1, by name
    if plan_only:
        kwargs["plan_only"] = True
    return run_filament_op("wipe", printer_name=printer_name, **kwargs)


class _FilamentHandlingToolsPlugin:
    """Load, unload, purge, and wipe — with purge as the clog test.

    Tools:
        - load_filament
        - unload_filament
        - purge_filament
        - wipe_nozzle
    """

    @property
    def name(self) -> str:
        return "filament_handling_tools"

    @property
    def description(self) -> str:
        return "Load, unload, purge, and wipe the nozzle; purge doubles as the clog test"

    def register(self, mcp: Any) -> None:
        """Register the four tools and their rate limits."""
        import kiln.server as _srv

        for tool_name, limits in _RATE_LIMITS.items():
            _srv._TOOL_RATE_LIMITS.setdefault(tool_name, limits)
        mcp.tool()(load_filament)
        mcp.tool()(unload_filament)
        mcp.tool()(purge_filament)
        mcp.tool()(wipe_nozzle)


plugin = _FilamentHandlingToolsPlugin()
