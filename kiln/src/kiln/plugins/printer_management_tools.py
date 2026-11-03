"""Printer management tools plugin.

Extracts printer discovery, trust whitelist, and lock management MCP tools
from server.py into a focused plugin module.

Auto-discovered by :func:`~kiln.plugin_loader.register_all_plugins` --
no manual imports needed.
"""

from __future__ import annotations

import logging
from typing import Any

from kiln.tool_annotations import creates, destructive, read_only

_logger = logging.getLogger(__name__)


def _reservation_refused(verdict: dict[str, Any]) -> dict:
    """A reservation not taken or not ended, in the envelope every door uses:
    the person's words (what happened, then what they can do) as the message,
    and who reserved it, until when and how an agent takes it over beside it."""
    from kiln import server as _srv
    from kiln.printers.engagement import refusal_words

    extra = {key: verdict[key] for key in ("held_by", "expires_at", "agent_hint") if verdict.get(key) is not None}
    return _srv._error_dict(refusal_words(verdict), code=str(verdict.get("code") or "PRINTER_LOCKED"), extra=extra)


class _PrinterManagementToolsPlugin:
    """Printer discovery, trust whitelist, and printer lock tools.

    Tools:
        - discover_printers
        - list_trusted_printers
        - trust_printer
        - untrust_printer
        - acquire_printer_lock
        - release_printer_lock
        - hand_back_printer
        - rename_printer
    """

    @property
    def name(self) -> str:
        return "printer_management_tools"

    @property
    def description(self) -> str:
        return "Printer discovery, trust whitelist, and printer lock tools"

    def register(self, mcp: Any) -> None:
        """Register printer management tools with the MCP server."""

        import kiln.server as _srv

        # ------------------------------------------------------------------
        # discover_printers
        # ------------------------------------------------------------------

        @mcp.tool(annotations=read_only("Discover printers"))
        def discover_printers(timeout: float = 5.0) -> dict:
            """Scan the local network for 3D printers.

            Uses mDNS/Bonjour and HTTP subnet probing to find OctoPrint,
            Moonraker/Creality, Bambu Lab, Elegoo, and Prusa printers on
            the local network.

            Args:
                timeout: Maximum scan duration in seconds (default 5).

            Returns a list of discovered printers with host, port, type, and
            whether the API is reachable.  Each one also carries
            ``address`` -- pass it as ``register_printer``'s ``host``; it
            keeps the port the printer answered on -- and ``to_connect``:
            what the person still has to supply and where they find it
            (``still_needed``), and anything to switch on in the printer
            first (``first``).  Ask for exactly those, in plain words, then
            add the printer with ``register_printer``.  ``suggested_name``
            is a name for it with no address or serial in it; propose that
            name, and use the person's own name when they give one.
            """
            try:
                from kiln.discovery import discover_printers as _discover
                from kiln.printer_backends import connection_guide
                from kiln.printer_names import discovered_name

                results = _discover(timeout=timeout)
                printers = []
                for found in results:
                    entry = found.to_dict()
                    entry["address"] = found.address
                    entry["suggested_name"] = discovered_name(found)
                    entry["to_connect"] = connection_guide(
                        found.printer_type,
                        found={"host": found.host, "serial": found.serial},
                        discovered=True,
                    )
                    printers.append(entry)
                return {
                    "success": True,
                    "printers": printers,
                    "count": len(results),
                    "message": f"Found {len(results)} printer(s) on the network.",
                }
            except Exception as exc:
                _logger.exception("Unexpected error in discover_printers")
                return _srv._error_dict(
                    f"Unexpected error in discover_printers: {exc}",
                    code="INTERNAL_ERROR",
                )

        # ------------------------------------------------------------------
        # list_trusted_printers
        # ------------------------------------------------------------------

        @mcp.tool(annotations=read_only("List trusted printers"))
        def list_trusted_printers() -> dict:
            """Return the list of trusted printer hostnames/IPs.

            Trusted printers are used to flag discovered printers that have been
            explicitly approved by the user, preventing spoofed-printer attacks.
            """
            if err := _srv._check_auth("config"):
                return err
            try:
                from kiln.cli.config import get_trusted_printers

                trusted = get_trusted_printers()
                return {"success": True, "trusted_printers": trusted, "count": len(trusted)}
            except Exception as exc:
                _logger.exception("Unexpected error in list_trusted_printers")
                return _srv._error_dict(
                    f"Unexpected error in list_trusted_printers: {exc}",
                    code="INTERNAL_ERROR",
                )

        # ------------------------------------------------------------------
        # trust_printer
        # ------------------------------------------------------------------

        @mcp.tool(annotations=destructive("Trust a printer"))
        def trust_printer(host: str) -> dict:
            """Add a printer hostname/IP to the trusted whitelist.

            Trusted printers are flagged during network discovery.  Connecting
            to an untrusted printer should raise a warning.

            Args:
                host: The hostname or IP address to trust.
            """
            if err := _srv._check_auth("config"):
                return err
            try:
                from kiln.cli.config import add_trusted_printer

                add_trusted_printer(host)
                return {"success": True, "host": host}
            except ValueError as exc:
                return _srv._error_dict(f"Failed to trust printer: {exc}", code="VALIDATION_ERROR")
            except Exception as exc:
                _logger.exception("Unexpected error in trust_printer")
                return _srv._error_dict(
                    f"Unexpected error in trust_printer: {exc}",
                    code="INTERNAL_ERROR",
                )

        # ------------------------------------------------------------------
        # untrust_printer
        # ------------------------------------------------------------------

        @mcp.tool(annotations=destructive("Untrust a printer"))
        def untrust_printer(host: str) -> dict:
            """Remove a printer hostname/IP from the trusted whitelist.

            Args:
                host: The hostname or IP address to untrust.
            """
            if err := _srv._check_auth("config"):
                return err
            try:
                from kiln.cli.config import remove_trusted_printer

                remove_trusted_printer(host)
                return {"success": True, "host": host}
            except ValueError as exc:
                return _srv._error_dict(f"Failed to untrust printer: {exc}", code="NOT_FOUND")
            except Exception as exc:
                _logger.exception("Unexpected error in untrust_printer")
                return _srv._error_dict(
                    f"Unexpected error in untrust_printer: {exc}",
                    code="INTERNAL_ERROR",
                )

        # ------------------------------------------------------------------
        # acquire_printer_lock
        # ------------------------------------------------------------------

        @mcp.tool(annotations=creates("Lock a printer"))
        async def acquire_printer_lock(
            printer_name: str,
            *,
            holder: str = "",
            hold_minutes: float = 30.0,
            timeout_seconds: float = 0.0,
        ) -> dict:
            """Reserve a printer for this Kiln session, so no other session commands it.

            Every AI app, terminal and bridge on this computer runs its own
            Kiln session.  While this session has the printer reserved, the
            others can still check its status and camera and send an
            emergency stop, but their other commands to it are refused, naming
            who reserved it.  The reservation ends when it is released, when
            ``hold_minutes`` run out, or when this session exits; call again to
            renew it.  It never lets anyone use a printer their plan would not
            (one printer at a time below Kiln Business).

            Args:
                printer_name: Printer to reserve.
                holder: A name for who reserved it (for example the app),
                    shown to the sessions that are refused.
                hold_minutes: How long it lasts (1 to 1440, default 30).
                timeout_seconds: How long to wait if another session has it
                    (default 0: answer at once with who has it; at most 120).
            """
            if err := _srv._check_auth("write"):
                return err

            import asyncio

            from kiln import printer_lock
            from kiln.printers.base import PrinterEngagementError

            try:
                adapter = _srv._get_registry().get(printer_name)
                return await asyncio.to_thread(
                    printer_lock.acquire,
                    adapter,
                    printer_name,
                    holder=holder,
                    hold_minutes=hold_minutes,
                    wait_seconds=timeout_seconds,
                )
            except printer_lock.Refused as refused:
                return _reservation_refused(refused.verdict)
            except PrinterEngagementError as exc:
                # The one-printer rule, in its own words (_error_dict reads it).
                return _srv._error_dict(str(exc), code="TIER_SINGLE_PRINTER_LIMIT")
            except Exception as exc:
                _logger.exception("Error in acquire_printer_lock")
                return _srv._error_dict(f"Failed to acquire printer lock: {exc}", code="LOCK_ERROR")

        # ------------------------------------------------------------------
        # hand_back_printer
        # ------------------------------------------------------------------

        @mcp.tool(annotations=destructive("Hand a printer back"))
        def hand_back_printer(printer_name: str | None = None) -> dict:
            """Tell Kiln you are taking a printer from here, so it can move on.

            Below the fleet tier Kiln works with one printer at a time: the
            machine it started a print on, or one it is watching for you.
            This hands that machine back — you keep the print, Kiln stops
            being the one driving it, and its attention is free for another
            printer.

            Nothing is cancelled and nothing is paused.  The print carries on
            exactly as it was; this only changes which machine Kiln considers
            itself responsible for.

            Called with no arguments it reports which printer Kiln is working
            with, without changing anything, so you can always find out where
            its attention is before moving it.

            It moves the queue too.  Below the fleet tier the queue works on
            one printer and stays there; a queued job for another printer
            waits until the person moves Kiln to it.  When they say they are
            done with a printer, or ask for the queue to move on, hand that
            printer back: the next job in line then starts on its own printer.

            One thing worth knowing before you do it: Kiln will come back to
            this print once if you need it to, and after that it stays with
            whatever machine it moved to until this print finishes.  Going
            back and forth between two running printers is what the fleet
            tier is for.

            Args:
                printer_name: Printer to hand back.  Omit to report only.
            """
            if err := _srv._check_auth("write"):
                return err

            try:
                from kiln.printers.engagement import (
                    current,
                    hand_back,
                    multi_machine_tier,
                    queue_printer,
                    reason_in_english,
                )

                engagement = current()
                if printer_name is None:
                    on = queue_printer() if not multi_machine_tier() else None
                    queue_line = (
                        f" Kiln's queue is on {on['label']}; hand it back to move the queue to the next job's printer."
                        if on else ""
                    )
                    if engagement is None:
                        return {
                            "success": True,
                            "engaged_with": None,
                            "queue_on": on["label"] if on else None,
                            "message": "Kiln is not working with a printer right now." + queue_line,
                        }
                    return {
                        "success": True,
                        "engaged_with": engagement.label,
                        "since": engagement.since,
                        "because": reason_in_english(engagement.reason),
                        "queue_on": on["label"] if on else None,
                        "message": (
                            f"Kiln is working with {engagement.label}. "
                            f"Hand it back to move Kiln to another printer."
                            + ("" if on and on["label"] == engagement.label else queue_line)
                        ),
                    }

                adapter = _srv._get_registry().get(printer_name)

                # Handing a machine back means Kiln stops watching it, so any
                # live watch on it ends HERE rather than discovering the
                # change on its next poll.  The watcher copes with that race
                # on its own, but a background thread finding out by being
                # refused is a worse way to end something the user asked to
                # end.
                stopped_watches = []
                try:
                    from kiln.printers.engagement import machine_id

                    target = machine_id(adapter)
                    for watch_id, watcher in list(_srv._watchers.items()):
                        watched = getattr(watcher, "_adapter", None)
                        if watched is not None and machine_id(watched) == target:
                            _srv._watchers.pop(watch_id, None)
                            watcher.stop()
                            stopped_watches.append(watch_id)
                except Exception:
                    _logger.debug("could not stop watches on hand-back", exc_info=True)

                report = hand_back(adapter)
                if not report.get("released"):
                    return _srv._error_dict(
                        report.get("reason", "Nothing to hand back."),
                        code="NOT_ENGAGED",
                    )
                name = report.get("printer", printer_name)
                if report.get("queue_only"):
                    return {
                        "success": True,
                        "printer": name,
                        "stopped_watches": stopped_watches,
                        "queue_released": True,
                        "message": (
                            f"Kiln's queue has moved off {name}. The next job in line starts on its own "
                            "printer once that plate is clear."
                        ),
                        "returns_left": 0,
                    }
                left = report.get("returns_left", 0)
                # State the consequence HERE, not when they discover it.
                consequence = (
                    "Kiln can come back to this print once if you need it to."
                    if left
                    else "Kiln has already come back to this print once, so it "
                    "will stay with the next printer until this one finishes."
                )
                return {
                    "success": True,
                    "printer": name,
                    "stopped_watches": stopped_watches,
                    "message": (
                        f"Kiln has stepped off {name}. The print carries on exactly "
                        f"as it was, and Kiln is free for another printer. {consequence}"
                        + (" The queue moves on with it: the next job in line starts on its own printer."
                           if report.get("queue_released") else "")
                    ),
                    "queue_released": bool(report.get("queue_released")),
                    "returns_left": left,
                }
            except Exception as exc:
                _logger.exception("Error in hand_back_printer")
                return _srv._error_dict(
                    f"Failed to hand back printer: {exc}", code="INTERNAL_ERROR",
                )

        # ------------------------------------------------------------------
        # rename_printer
        # ------------------------------------------------------------------

        @mcp.tool(annotations=destructive("Rename a printer"))
        def rename_printer(printer_name: str, new_name: str) -> dict:
            """Give a saved printer a new name; its records come with it.

            The printer's print history and outcomes, what is loaded in it,
            its queued jobs, its emergency-stop state and the standing
            permissions on this computer that name it all move to the new
            name, so the machine's record stays one record.  The reply
            says what came along (``carried``), which standing permissions
            followed and which were turned off (``permissions``), and the
            names the printer has had (``formerly``).  The machine itself,
            its address and its credentials are untouched.

            Refused, with nothing changed, when the printer is printing or
            Kiln is following a print on it (``PRINTER_BUSY``), when the new
            name is already another printer's (``NAME_TAKEN``), or when the
            new name carries the printer's address, serial or a code
            (``NAME_HAS_IDENTIFIER``: the reply's ``suggested_name`` is a
            clean one to offer instead).  Runs on the computer the printer
            is set up on, never on Kiln's servers.

            Args:
                printer_name: The printer's current name.
                new_name: What to call it from now on.
            """
            if err := _srv._check_auth("config"):
                return err
            try:
                from kiln.printer_rename import RenameRefused
                from kiln.printer_rename import rename_printer as _rename

                try:
                    return _rename(
                        printer_name,
                        new_name,
                        source="agent",
                        busy=_srv._printer_busy_in_this_process,
                        after=_srv._rename_printer_state,
                    )
                except RenameRefused as refused:
                    return _srv._error_dict(refused.message, code=refused.code, extra=refused.extra)
            except Exception as exc:
                _logger.exception("Error in rename_printer")
                return _srv._error_dict(f"Failed to rename printer: {exc}", code="INTERNAL_ERROR")

        # ------------------------------------------------------------------
        # set_machine_repair
        # ------------------------------------------------------------------

        @mcp.tool(annotations=destructive("Set a printer's repair state"))
        def set_machine_repair(
            machine_name: str,
            status: str = "",
            kind: str | None = None,
            note: str | None = None,
            when: str | None = None,
            same_as: str = "",
            different_from: str = "",
        ) -> dict:
            """Tell Kiln a printer needs repair, or that you have repaired it.

            Kiln judges a printer by its own prints.  Mark it ``needed`` when
            you are taking it out of service: routing stops choosing it, and
            a print you start on it yourself still runs, with a warning.
            Mark it ``done`` once it is fixed: from that moment it is judged
            by what it prints next, and its earlier prints stop counting
            against it.  Nothing is deleted, and the record stays yours.

            Signed in, a mark follows the printer to your Kiln account and
            to your other computers, and marks made elsewhere (another
            computer, the web) come back here.  When your account cannot
            tell whether a printer marked from another computer is this
            one, the reply carries a ``questions`` list; answer one with
            ``same_as`` (it is this printer) or ``different_from`` (it is
            not), passing the question's id and no status.

            Args:
                machine_name: The printer, by its name here.
                status: ``needed`` (out of service until repaired), ``done``
                    (repaired; ``kind`` required), or ``clear`` (take back
                    the latest mark).  Left out only to answer a question.
                kind: What was repaired: nozzle, hotend, extruder, bed,
                    belts, or other.  Required for ``done``; optional for
                    ``needed``.
                note: What you did, in your own words (optional; kept on
                    this computer, never sent to your account).
                when: When it happened, ISO 8601 (``2026-10-20T14:00``).
                    Left out, now.  A repair may be dated up to 30 days
                    back; never ahead.
                same_as: The id of a question from an earlier reply: this
                    printer IS the one marked from the other computer.
                different_from: The id of a question: it is NOT (also undoes
                    a wrong yes; never asked again for that pair).

            Returns ``recorded``, the printer, ``status``, ``kind``,
            ``marked_at``, the printer's ``condition`` now, ``next`` (one
            sentence), for ``done`` a ``reset`` block saying how many
            earlier prints no longer count, and, signed in, ``account``
            (whether the mark reached it) and any ``questions``.  Refuses,
            by name, a printer Kiln cannot tell apart from another, a time
            ahead or too far back, and the hosted server, where the record
            is not kept.
            """
            if err := _srv._check_auth("write"):
                return err

            from kiln.machine_repairs import RepairRefused, answer_identity, mark

            try:
                if same_as or different_from:
                    if same_as and different_from:
                        return _srv._error_dict(
                            "Answer one question at a time: same_as or different_from, not both.",
                            code="REPAIR_ANSWER_AMBIGUOUS",
                        )
                    if status:
                        return _srv._error_dict(
                            "Answering a question takes no status; mark the printer in a second call.",
                            code="REPAIR_ANSWER_AMBIGUOUS",
                        )
                    result = answer_identity(machine_name, same_as or different_from, same=bool(same_as))
                    return {"success": True, **result}
                if not status:
                    return _srv._error_dict(
                        "status is needed, done or clear; or answer a question with same_as / different_from.",
                        code="REPAIR_STATUS_UNKNOWN",
                    )
                result = mark(machine_name, status, kind=kind, note=note, when=when)
                result.update(_account_after_mark())
                return {"success": True, **result}
            except RepairRefused as exc:
                return _srv._error_dict(exc.message, code=exc.code)
            except Exception as exc:
                _logger.exception("Error in set_machine_repair")
                return _srv._error_dict(
                    f"Failed to record the printer's repair: {exc}", code="INTERNAL_ERROR",
                )

        def _account_after_mark() -> dict:
            """The mark's trip to the account, done now so the reply can say
            whether it arrived and carry the account's questions.  Nothing
            when nobody is signed in; the local record stands either way."""
            try:
                from kiln.auth_session import resolve_api_bearer

                if not resolve_api_bearer().token:
                    return {}
                from kiln.machine_repair_courier import _ACCOUNT_REPLY_WAIT_S, sync

                synced = sync(timeout=_ACCOUNT_REPLY_WAIT_S)
            except Exception as exc:  # noqa: BLE001 -- the account is a copy; the record is here
                _logger.debug("set_machine_repair: account not reached", exc_info=True)
                return {"account": {"synced": False, "why": "not_reached", "error": str(exc)}}
            out: dict = {"account": {k: v for k, v in synced.items() if k != "questions"}}
            if synced.get("questions"):
                out["questions"] = synced["questions"]
            return out

        # ------------------------------------------------------------------
        # release_printer_lock
        # ------------------------------------------------------------------

        @mcp.tool(annotations=destructive("Release a printer lock"))
        def release_printer_lock(
            printer_name: str, *, holder: str = "", force: bool = False,
        ) -> dict:
            """End this session's reservation of a printer, or, with ``force``, any session's.

            Args:
                printer_name: Printer to release.
                holder: Unused: a reservation belongs to the Kiln session that
                    made it.  Accepted so earlier calls keep working.
                force: Also end a reservation another Kiln session made.
            """
            if err := _srv._check_auth("write"):
                return err

            from kiln import printer_lock

            try:
                adapter = _srv._get_registry().get(printer_name)
                return printer_lock.release(adapter, printer_name, force=force)
            except printer_lock.Refused as refused:
                return _reservation_refused(refused.verdict)
            except Exception as exc:
                _logger.exception("Error in release_printer_lock")
                return _srv._error_dict(
                    f"Failed to release printer lock: {exc}",
                    code="LOCK_ERROR",
                )

        _logger.debug("Registered printer management tools")


plugin = _PrinterManagementToolsPlugin()
