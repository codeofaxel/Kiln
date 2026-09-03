"""Filament handling tools plugin — load, unload, purge.

The three doors a user meets when a spool has to move: feed a slot to
the nozzle, pull it back out, or push a short length through to learn
whether the melt zone is clear.  Each is one call into the adapter's
gated template (:meth:`~kiln.printers.base.PrinterAdapter.load_filament`
and siblings), so the safety gate — not mid-print, safety-profile
ceiling, the spool's own temperature window, the cold-extrusion floor —
runs the same way here, from the CLI, and from a recovery flow.

Purge doubles as the clog test.  ``extrusion_verified`` in the answer is
``True`` / ``False`` only from a signal the printer genuinely produced
and ``None`` when it produced none; ``error_hint`` carries the printer's
own fault code in plain language.

Auto-discovered by :func:`~kiln.plugin_loader.register_all_plugins` —
no manual imports needed.  The tool bodies are module-level functions so
``kiln filament …`` can call the very same code the MCP tool runs.
"""

from __future__ import annotations

import logging
from typing import Any

_logger = logging.getLogger(__name__)

#: Rate limits in server.py's ``(min_interval_ms, max_per_minute)`` form —
#: these move heaters and steppers, so they get the pause/resume cadence.
_RATE_LIMITS: dict[str, tuple[int, int]] = {
    "load_filament": (5000, 6),
    "unload_filament": (5000, 6),
    "purge_filament": (5000, 6),
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
    from kiln.printers.base import FilamentHandlingUnsupported, PrinterError
    from kiln.registry import PrinterNotFoundError

    verb = {"load": "load filament on", "unload": "unload filament from", "purge": "purge filament on"}[action]
    tool_name = f"{action}_filament"
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
        method = getattr(adapter, tool_name)
        result = method(**kwargs)
        if _srv._is_heater_watchdog_machine(adapter):
            _srv._get_heater_watchdog().notify_heater_set()
        _srv._audit(
            tool_name,
            "executed" if result.success else "failed",
            details={"printer": target_name, **result.to_dict()},
        )
        if not result.success:
            return _srv._error_dict(
                result.message,
                code="FILAMENT_FAULT" if result.error_code else "FILAMENT_OP_FAILED",
                extra={"printer_name": target_name, "filament": result.to_dict()},
            )
        return {"success": True, "printer_name": target_name, **result.to_dict()}
    except FilamentHandlingUnsupported as exc:
        return _srv._error_dict(str(exc), code="UNSUPPORTED")
    except (PrinterError, RuntimeError) as exc:
        return _srv._error_dict(f"Failed to {action} filament: {exc}")
    except Exception as exc:
        _logger.exception("Unexpected error in %s", tool_name)
        return _srv._error_dict(f"Unexpected error in {tool_name}: {exc}", code="INTERNAL_ERROR")


def _gated(tool_name: str, args: dict[str, Any]) -> dict[str, Any] | None:
    """Auth → rate limit → confirmation, in server.py's order."""
    import kiln.server as _srv

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
        wait_seconds: How long to watch for the AMS to confirm (default 120).
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
    return run_filament_op("load", printer_name=printer_name, **kwargs)


def unload_filament(
    material: str | None = None,
    temperature: float | None = None,
    length_mm: float | None = None,
    wait_seconds: float | None = None,
    printer_name: str | None = None,
) -> dict:
    """Pull filament out of the hotend — back into the AMS on Bambu, a heated retract elsewhere.

    Same temperature gate and verification as ``load_filament``: on Bambu
    ``extrusion_verified`` is ``True`` once the AMS reports no tray feeding
    the nozzle (``tray_now`` 255); on Klipper an ``UNLOAD_FILAMENT`` /
    ``M702`` macro is used when the config has one; otherwise the hotend is
    heated and ``length_mm`` is retracted (default 80).

    Args:
        material / temperature / length_mm / wait_seconds / printer_name: as
            ``load_filament``.
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
    return run_filament_op("unload", printer_name=printer_name, **kwargs)


def purge_filament(
    length_mm: float = 30.0,
    material: str | None = None,
    temperature: float | None = None,
    slot: int | None = None,
    wait_seconds: float | None = None,
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

    Args:
        length_mm: Extrusion length, 1–150 mm (default 30).
        material: e.g. ``"PLA"`` — picks a temperature when none is given.
        temperature: Hotend target in °C.  Omit to use the middle of the
            spool's (AMS) or material's window.
        slot: AMS tray whose temperature window applies (Bambu).  Omit to
            use the tray currently feeding the nozzle.
        wait_seconds: How long after the purge to watch for a fault code
            on Bambu (default 10).
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
    return run_filament_op("purge", printer_name=printer_name, **kwargs)


class _FilamentHandlingToolsPlugin:
    """Load, unload, and purge filament — with purge as the clog test.

    Tools:
        - load_filament
        - unload_filament
        - purge_filament
    """

    @property
    def name(self) -> str:
        return "filament_handling_tools"

    @property
    def description(self) -> str:
        return "Load, unload, and purge filament; purge doubles as the clog test"

    def register(self, mcp: Any) -> None:
        """Register the three tools and their rate limits."""
        import kiln.server as _srv

        for tool_name, limits in _RATE_LIMITS.items():
            _srv._TOOL_RATE_LIMITS.setdefault(tool_name, limits)
        mcp.tool()(load_filament)
        mcp.tool()(unload_filament)
        mcp.tool()(purge_filament)


plugin = _FilamentHandlingToolsPlugin()
