"""Printer management tools plugin.

Extracts printer discovery, trust whitelist, and lock management MCP tools
from server.py into a focused plugin module.

Auto-discovered by :func:`~kiln.plugin_loader.register_all_plugins` --
no manual imports needed.
"""

from __future__ import annotations

import logging
from typing import Any

_logger = logging.getLogger(__name__)


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

        @mcp.tool()
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
            add the printer with ``register_printer``.
            """
            try:
                from kiln.discovery import discover_printers as _discover
                from kiln.printer_backends import connection_guide

                results = _discover(timeout=timeout)
                printers = []
                for found in results:
                    entry = found.to_dict()
                    entry["address"] = found.address
                    entry["to_connect"] = connection_guide(
                        found.printer_type,
                        found={"host": found.host, "serial": found.serial},
                        discovered=True,
                    )
                    printers.append(entry)
                return {
                    "success": True,
                    "machines": printers,
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

        @mcp.tool()
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

        @mcp.tool()
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

        @mcp.tool()
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

        @mcp.tool()
        def acquire_printer_lock(
            printer_name: str,
            *,
            holder: str = "agent",
            timeout_seconds: float = 30.0,
        ) -> dict:
            """Acquire an exclusive lock on a printer for safe concurrent access.

            Prevents multiple agents from controlling the same printer simultaneously.

            Args:
                printer_name: Printer to lock.
                holder: Identifier of the lock holder.
                timeout_seconds: Maximum time to wait for the lock.
            """
            if err := _srv._check_auth("write"):
                return err

            try:
                from kiln.state_lock import get_state_lock_manager

                mgr = get_state_lock_manager()
                acquired = mgr.acquire(printer_name, holder=holder, timeout=timeout_seconds)
                if not acquired:
                    return _srv._error_dict(
                        f"Could not acquire lock on {printer_name!r} within {timeout_seconds}s",
                        code="LOCK_TIMEOUT",
                    )
                return {"success": True, "printer": printer_name, "holder": holder, "locked": True}
            except Exception as exc:
                _logger.exception("Error in acquire_printer_lock")
                return _srv._error_dict(f"Failed to acquire printer lock: {exc}", code="LOCK_ERROR")

        # ------------------------------------------------------------------
        # hand_back_printer
        # ------------------------------------------------------------------

        @mcp.tool()
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
                    reason_in_english,
                )

                engagement = current()
                if printer_name is None:
                    if engagement is None:
                        return {
                            "success": True,
                            "engaged_with": None,
                            "message": "Kiln is not working with a printer right now.",
                        }
                    return {
                        "success": True,
                        "engaged_with": engagement.label,
                        "since": engagement.since,
                        "because": reason_in_english(engagement.reason),
                        "message": (
                            f"Kiln is working with {engagement.label}. "
                            f"Hand it back to move Kiln to another printer."
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
                    ),
                    "returns_left": left,
                }
            except Exception as exc:
                _logger.exception("Error in hand_back_printer")
                return _srv._error_dict(
                    f"Failed to hand back printer: {exc}", code="INTERNAL_ERROR",
                )

        # ------------------------------------------------------------------
        # set_machine_repair
        # ------------------------------------------------------------------

        @mcp.tool()
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

        @mcp.tool()
        def release_printer_lock(printer_name: str, *, holder: str = "agent") -> dict:
            """Release an exclusive lock on a printer.

            Args:
                printer_name: Printer to unlock.
                holder: Identifier of the lock holder (must match acquire).
            """
            if err := _srv._check_auth("write"):
                return err

            try:
                from kiln.state_lock import get_state_lock_manager

                mgr = get_state_lock_manager()
                released = mgr.release(printer_name, holder=holder)
                return {"success": True, "printer": printer_name, "released": released}
            except Exception as exc:
                _logger.exception("Error in release_printer_lock")
                return _srv._error_dict(
                    f"Failed to release printer lock: {exc}",
                    code="LOCK_ERROR",
                )

        _logger.debug("Registered printer management tools")


plugin = _PrinterManagementToolsPlugin()
