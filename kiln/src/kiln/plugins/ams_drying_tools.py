"""AMS active drying: start or stop the dryer on a Bambu AMS 2 Pro / AMS HT.

``ams_filament_drying`` is a door that touches the person's own printer, so
it lives here and runs on the computer attached to that printer.  A STOP is
the one fixed payload the emergency stop also sends
(:meth:`kiln.emergency.EmergencyCoordinator._build_ams_stop_command`), so a
dryer can always be switched off, offline included.  A START is checked
first against the unit's own limits -- its drying ceiling (AMS 2 Pro 65 C,
AMS HT 85 C), the cooling floor the firmware silently drops a command under,
the zero-duration guard -- which kiln-pro holds (``kiln._pro_ams_bridge``,
in-process where kiln-pro is installed, served everywhere else); a start
that cannot be checked is not sent.

Free on every tier: running the dryer is a basic hardware action, like
setting a temperature.  Which temperature and how long for a material is the
Kiln Pro ``drying_advisor``, named once in a session.

Auto-discovered by :func:`~kiln.plugin_loader.register_all_plugins`.
"""

from __future__ import annotations

import logging
from typing import Any

from kiln.tool_annotations import destructive

_logger = logging.getLogger(__name__)

DRY_MODE_STOP = 0
DRY_MODE_START = 1

#: The one line pointing at the paid half, said once per session.
_ADVISOR_TIP = (
    "Kiln Pro's drying_advisor tells you the exact temperature and time for your "
    "material, and whether it needs drying at all. https://kiln3d.com/pricing"
)


def _unit_module(adapter: Any, ams_id: int) -> str:
    """What the unit reports for itself (``n3f/0`` is an AMS 2 Pro), or
    ``""`` when it cannot be read: the check then holds a start to the
    hottest any AMS dryer allows, never to none."""
    try:
        status = adapter.get_ams_status()
        for unit in (status or {}).get("units", []) or []:
            if isinstance(unit, dict) and int(unit.get("unit_id", -1)) == int(ams_id):
                return str(unit.get("module_name") or "")
    except Exception:  # noqa: BLE001 -- the ceiling must not depend on this read
        _logger.debug("ams_filament_drying: unit read failed", exc_info=True)
    return ""


def ams_filament_drying(
    ams_id: int,
    mode: int,
    duration_min: int = 0,
    temp_c: int | None = None,
    cooling_temp_c: int | None = None,
    humidity: int | None = None,
    rotate_tray: bool | None = None,
    printer_name: str | None = None,
) -> dict:
    """Start or stop an AMS 2 Pro / AMS HT active dry cycle.

    Bambu printers with an AMS 2 Pro or AMS HT dry filament in place.
    This controls that dryer.

    To START (``mode=1``): set ``temp_c`` (drying temperature),
    ``duration_min`` (> 0), and ``cooling_temp_c`` -- which must be
    >= 45, or the printer silently ignores the command.  A start is
    checked against the unit's own ceiling (AMS 2 Pro 65 C, AMS HT 85 C)
    before it is sent; when Kiln cannot run that check, nothing is sent and
    the answer says why.  To STOP (``mode=0``): only ``ams_id`` is needed,
    and a stop is always sent.

    Only Bambu printers with an AMS 2 Pro / AMS HT support this; other
    printers and plain AMS / AMS Lite units return an error.

    :param ams_id: AMS unit index (0-based).
    :param mode: 1 to start drying, 0 to stop.
    :param duration_min: Dry-cycle length in minutes (> 0 to start).
    :param temp_c: Drying temperature in C (required to start; capped
        at the unit's max -- AMS 2 Pro 65 C, AMS HT 85 C).
    :param cooling_temp_c: Cooldown temperature in C (>= 45 to start).
    :param humidity: Optional target humidity.
    :param rotate_tray: Optional tray-rotation flag.
    :param printer_name: Target printer.  Omit for the default printer.
    :returns: Dict with the dispatched command and a status message.
    """
    import kiln.server as _srv

    if err := _srv._check_auth("printer_control"):
        return err
    if mode not in (DRY_MODE_STOP, DRY_MODE_START):
        return _srv._error_dict(
            f"mode must be {DRY_MODE_START} (start) or {DRY_MODE_STOP} (stop), got {mode!r}.",
            code="INVALID_DRYING_COMMAND",
        )
    try:
        adapter, target = _srv._resolve_control_target(printer_name)
    except _srv.PrinterNotFoundError:
        return _srv._unknown_printer_error(printer_name, "run the AMS dryer on")
    except Exception as exc:  # noqa: BLE001 -- the registry raises varied connection errors
        suffix = f" ({printer_name})" if printer_name else ""
        return _srv._error_dict(f"No printer connected{suffix}: {exc}", code="PRINTER_OFFLINE")
    if "bambu" not in type(adapter).__name__.lower() or not hasattr(adapter, "publish_print_command"):
        return _srv._error_dict(
            "AMS filament drying is only available on Bambu printers with an AMS 2 Pro / AMS HT.",
            code="UNSUPPORTED_PRINTER",
        )

    started = mode == DRY_MODE_START
    if started:
        from kiln import _pro_ams_bridge

        checked = _pro_ams_bridge.start_command(
            ams_id=ams_id, mode=mode, duration_min=duration_min, temp_c=temp_c,
            cooling_temp_c=cooling_temp_c, humidity=humidity, rotate_tray=rotate_tray,
            module_name=_unit_module(adapter, ams_id),
        )
        if checked.get("success") is not True:
            return {"status": "error", **{k: v for k, v in checked.items() if k != "success"}, "success": False}
        command = dict(checked["command"])
    else:
        from kiln.emergency import EmergencyCoordinator

        try:
            command = EmergencyCoordinator._build_ams_stop_command(int(ams_id))
        except (TypeError, ValueError):
            return _srv._error_dict(f"ams_id must be an integer, got {ams_id!r}.", code="INVALID_DRYING_COMMAND")

    from kiln.printers.command_verdict import CommandVerdict

    try:
        sent = adapter.publish_print_command("ams_filament_drying", command)
    except Exception as exc:  # noqa: BLE001 -- the adapter raises varied transport errors
        return _srv._error_dict(f"Failed to send the drying command: {exc}", code="DISPATCH_FAILED")
    outcome = CommandVerdict.coerce(sent, what="drying command").to_dict()

    from kiln.tiers_and_terms import claim_once

    result: dict[str, Any] = {
        "success": bool(outcome.get("accepted", True)),
        "status": "success",
        "action": "start" if started else "stop",
        "ams_id": command["ams_id"],
        "printer_name": target,
        "command": command,
        # The AMS reports no drying state Kiln can read back, so this says
        # "sent" and never claims the unit is drying.
        **outcome,
        "message": (
            f"AMS unit {command['ams_id']}: start-drying command sent ({temp_c}C, {duration_min} min)."
            if started
            else f"AMS unit {command['ams_id']}: stop-drying command sent."
        ),
    }
    if started and claim_once("ams_filament_drying.advisor_tip"):
        result["pro_tip"] = _ADVISOR_TIP
    return result


class _AmsDryingToolsPlugin:
    """AMS active-drying control for Bambu AMS 2 Pro / AMS HT.

    Tools:
        - ams_filament_drying
    """

    @property
    def name(self) -> str:
        return "ams_drying_tools"

    @property
    def description(self) -> str:
        return "Start or stop the active dryer on a Bambu AMS 2 Pro / AMS HT"

    def register(self, mcp: Any) -> None:
        mcp.tool(annotations=destructive("Start or stop AMS drying"))(ams_filament_drying)


plugin = _AmsDryingToolsPlugin()
