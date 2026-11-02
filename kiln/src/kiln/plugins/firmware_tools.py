"""Machine firmware tools: check, update and roll back.

One tool per job, for one machine or many.  With no machine named, a tool
works on the machine Kiln is connected to; a name picks one of several.
Working on several machines in one call (``machines`` or
``all_machines``) is the fleet half, and is part of Kiln Business.

Every call goes through the machine's own adapter, so what a tool reports
is what the machine said, and the adapter's own guards hold: an update or a
rollback is refused on a machine that is printing, and Kiln's one-machine
rule below Business applies to a named machine like any other command.

Auto-discovered by :func:`~kiln.plugin_loader.register_all_plugins` --
no manual imports needed.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from kiln.tool_annotations import destructive, read_only

_logger = logging.getLogger(__name__)

#: The plan that runs one call across several machines.
FLEET_TIER = "business"


class _FirmwareToolsPlugin:
    """Machine firmware tools.

    Tools:
        - check_machine_firmware
        - update_machine_firmware
        - roll_back_machine_firmware
    """

    @property
    def name(self) -> str:
        return "firmware_tools"

    @property
    def description(self) -> str:
        return "Check, update and roll back machine firmware"

    def register(self, mcp: Any) -> None:
        """Register the machine firmware tools with the MCP server."""

        import kiln.server as _srv
        from kiln.printers.base import PrinterError
        from kiln.registry import PrinterNotFoundError

        # What a machine, or the lookup of one, says when it cannot do the
        # job: answered in its own words, never logged as a fault in Kiln.
        expected = (PrinterError, PrinterNotFoundError, RuntimeError)

        def _targets(
            machine: str | None,
            machines: list[str] | None,
            all_machines: bool,
            subject: str,
        ) -> tuple[list[str | None], bool] | dict:
            """The machines a call covers and whether it is a fleet call,
            or the refusal for a request that cannot be served.  *subject*
            names the fleet call in the plan refusal.

            A fleet call is one that asks for more than one machine in
            itself, whatever the person owns; one name, or none, is never
            one.  ``[None]`` means the machine Kiln is connected to.
            """
            asked = sum((machine is not None, machines is not None, bool(all_machines)))
            if asked > 1:
                return _srv._error_dict(
                    "Name one machine, a list of machines, or all_machines=true, not more than one of these.",
                    code="INVALID_ARGS",
                )
            if machines is not None:
                names = list(dict.fromkeys(n.strip() for n in machines if n and n.strip()))
                if not names:
                    return _srv._error_dict(
                        "machines is empty. Name at least one machine, or leave it out for the one Kiln is connected to.",
                        code="INVALID_ARGS",
                    )
                if len(names) == 1:
                    return [names[0]], False
            elif not all_machines:
                return [machine.strip() if machine and machine.strip() else None], False

            from kiln.licensing import LicenseTier, check_tier

            ok, message = check_tier(LicenseTier.BUSINESS)
            if not ok:
                from kiln.tiers_and_terms import paywall_fields

                # The plan's sentence names "This feature"; say what was
                # reached for instead, and what still works without it.
                message = str(message or "").strip()
                if message.startswith("This feature"):
                    message = subject + message[len("This feature"):]
                if message and message[-1] not in ".!?":
                    message += "."
                return {
                    "success": False,
                    "error": f"{message} One machine at a time works on every plan.",
                    "code": "TIER_REQUIRED",
                    "required_tier": FLEET_TIER,
                    **paywall_fields(FLEET_TIER),
                }
            if machines is not None:
                return names, True
            names = _srv._get_registry().list_machines()
            if not names:
                return _srv._error_dict(
                    "No machines are registered with Kiln yet. Add one with register_printer.",
                    code="NO_PRINTERS",
                )
            return list(names), True

        def _for_each(
            targets: list[str],
            do: Callable[[Any], dict],
            fail: str,
        ) -> dict:
            """Run *do* on each machine in turn and report every one.

            One at a time, because an update restarts a machine's services,
            and a failure on one machine never stops the rest.
            """
            results: list[dict] = []
            for name in targets:
                try:
                    answer = do(_srv._resolve_adapter(name))
                except Exception as exc:
                    answer = _srv._error_dict(f"{fail}: {exc}", code="FIRMWARE_ERROR")
                results.append({"machine": name, **answer})
            done = sum(1 for r in results if r.get("success"))
            return {
                "success": done > 0,
                "machines": results,
                "summary": f"{done} of {len(results)} machine(s) done.",
            }

        def _status_of(adapter: Any) -> dict:
            if not adapter.capabilities.can_update_firmware:
                return _srv._error_dict(
                    "This machine does not report firmware through Kiln.",
                    code="UNSUPPORTED",
                )
            status = adapter.get_firmware_status()
            if status is None:
                return _srv._error_dict("Could not read the firmware status.", code="UNAVAILABLE")
            return {
                "success": True,
                "busy": status.busy,
                "updates_available": status.updates_available,
                "components": [
                    {
                        "name": c.name,
                        "current_version": c.current_version,
                        "remote_version": c.remote_version,
                        "update_available": c.update_available,
                        "rollback_version": c.rollback_version,
                        "component_type": c.component_type,
                        "channel": c.channel,
                    }
                    for c in status.components
                ],
            }

        def _run(change: Callable[[Any], Any]) -> Callable[[Any], dict]:
            """An update or rollback on one adapter, as a reply."""

            def on(adapter: Any) -> dict:
                if not adapter.capabilities.can_update_firmware:
                    return _srv._error_dict(
                        "This machine's firmware cannot be changed through Kiln.",
                        code="UNSUPPORTED",
                    )
                result = change(adapter)
                return {
                    "success": result.success,
                    "message": result.message,
                    "component": result.component,
                }

            return on

        def _one(
            target: str | None,
            do: Callable[[Any], dict],
            fail: str,
            hint: str,
        ) -> dict:
            try:
                answer = do(_srv._resolve_adapter(target))
            except expected as exc:
                return _srv._error_dict(f"{fail}: {exc}. {hint}")
            except Exception as exc:
                _logger.exception("Unexpected error in machine firmware tool")
                return _srv._error_dict(f"{fail}: {exc}", code="INTERNAL_ERROR")
            return {"machine": target, **answer} if target else answer

        # ------------------------------------------------------------------
        # check_machine_firmware
        # ------------------------------------------------------------------

        @mcp.tool(annotations=read_only("Check machine firmware"))
        def check_machine_firmware(
            machine: str | None = None,
            machines: list[str] | None = None,
            all_machines: bool = False,
        ) -> dict:
            """Check a machine's firmware: each component's version, and whether an update or a rollback is available.

            Works on Klipper (Moonraker), OctoPrint and Creality's
            Klipper-based printers.  Bambu Lab and Prusa Link printers manage
            their own firmware and answer ``UNSUPPORTED``.  Checking several
            machines in one call needs Kiln Business.

            Args:
                machine: The machine to check, by name.  Omitted, the machine
                    Kiln is connected to.
                machines: Several machines to check in one call, by name.
                    Each is reported on its own line.
                all_machines: Check every machine registered with Kiln.
            """
            targets = _targets(machine, machines, all_machines, "Checking several machines in one call")
            if isinstance(targets, dict):
                return targets
            names, fleet = targets
            if fleet:
                return _for_each(names, _status_of, "Could not check the firmware")
            return _one(
                names[0], _status_of, "Could not check the firmware",
                "Check that the machine is online.",
            )

        # ------------------------------------------------------------------
        # update_machine_firmware
        # ------------------------------------------------------------------

        @mcp.tool(annotations=destructive("Update machine firmware"))
        def update_machine_firmware(
            component: str | None = None,
            machine: str | None = None,
            machines: list[str] | None = None,
            all_machines: bool = False,
        ) -> dict:
            """Start a firmware update on a machine.  Its services restart while it runs.

            Each component goes to the version its update offers; there is no
            choosing a version.  A machine that is printing is refused and
            left alone.  Updating several machines in one call needs Kiln
            Business: they are updated one at a time, and each is reported on
            its own line.

            Args:
                component: One firmware component to update, as
                    ``check_machine_firmware`` names it (e.g. ``"klipper"``).
                    Omitted, every component with an update available is
                    updated.
                machine: The machine to update, by name.  Omitted, the
                    machine Kiln is connected to.
                machines: Several machines to update in one call, by name.
                all_machines: Update every machine registered with Kiln.
            """
            if err := _srv._check_auth("firmware"):
                return err
            targets = _targets(machine, machines, all_machines, "Updating several machines in one call")
            if isinstance(targets, dict):
                return targets
            names, fleet = targets
            do = _run(lambda a: a.update_firmware(component=component))
            if fleet:
                return _for_each(names, do, "Could not update the firmware")
            return _one(
                names[0], do, "Could not update the firmware",
                "Make sure the machine is idle and online.",
            )

        # ------------------------------------------------------------------
        # roll_back_machine_firmware
        # ------------------------------------------------------------------

        @mcp.tool(annotations=destructive("Roll back machine firmware"))
        def roll_back_machine_firmware(
            component: str,
            machine: str | None = None,
            machines: list[str] | None = None,
            all_machines: bool = False,
        ) -> dict:
            """Roll one firmware component back to the version it had before its last update.

            Only Klipper (Moonraker) machines keep a version to return to;
            ``check_machine_firmware`` shows it as ``rollback_version``.  A
            machine that is printing is refused and left alone.  Rolling back
            several machines in one call needs Kiln Business.

            Args:
                component: The component to roll back, as
                    ``check_machine_firmware`` names it (e.g. ``"klipper"``).
                machine: The machine to roll back, by name.  Omitted, the
                    machine Kiln is connected to.
                machines: Several machines to roll back in one call, by name.
                all_machines: Roll back every machine registered with Kiln.
            """
            if err := _srv._check_auth("firmware"):
                return err
            targets = _targets(machine, machines, all_machines, "Rolling back several machines in one call")
            if isinstance(targets, dict):
                return targets
            names, fleet = targets
            do = _run(lambda a: a.rollback_firmware(component))
            if fleet:
                return _for_each(names, do, "Could not roll back the firmware")
            return _one(
                names[0], do, "Could not roll back the firmware",
                "check_machine_firmware shows which components have a version to return to.",
            )

        _logger.debug("Registered machine firmware tools")


plugin = _FirmwareToolsPlugin()
