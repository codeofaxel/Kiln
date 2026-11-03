"""Rename a saved printer, and carry everything filed under its name.

A printer's name is the key its records are filed under -- its print
history and outcomes, what is loaded in it, its queued jobs, its stop latch,
the retained copy of the file it last printed, and the standing permissions
that name it.  Removing a printer and adding it again under a new name
leaves all of that behind under the old one.  This module is the ONE door a
rename goes through, so every store that keys by the name moves together:

* ``~/.kiln/config.yaml``  -- the saved entry, keeping its place; the active
  printer follows it.
* ``~/.kiln/kiln.db``      -- every name-keyed table, in one transaction,
  with a row in ``printer_renames`` saying what the printer used to be
  called (:meth:`kiln.persistence.KilnDB.rename_printer_records`).
* ``~/.kiln/queue.db``     -- queued and finished jobs aimed at the name.
* the emergency-stop latch, the retained print (``monitor_twin``), the
  engagement and reservation labels, and the standing windows
  (:func:`kiln.consent_windows.rename_printer` says which follow).
* the live registry, in this process (:meth:`PrinterRegistry.rename`).
* any carrier another package registered (:func:`register_carrier`, or the
  ``kiln.printer_rename`` entry-point group).

Stores keyed by the MACHINE's identity (serial, else address -- the plate
record, the bench, heater readings, hardware stops, repair marks) need
nothing: the machine did not change.

**All or nothing.**  Each store is moved by a step that knows how to put
itself back; a step that fails undoes every earlier one, so a rename that
did not finish leaves the old name exactly as it was.  The standing
windows go last, because a window closed on the account cannot be
reopened by Kiln.

**Idle only.**  A printer mid-print, one a watcher is following, or one
with a print approval waiting is refused: bookkeeping about a print in
flight is filed under its name as it goes, and a name that changes under
it would split that record in two.

**A name is checked before it is taken** -- the same rule every setup door
applies (:mod:`kiln.printer_names`): a new name that carries the printer's
address, serial or a code is refused with a clean one suggested, and a
name another printer already has is refused.
"""

from __future__ import annotations

import contextlib
import importlib.metadata
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kiln.cli_command import kiln_command

logger = logging.getLogger(__name__)

__all__ = [
    "ENTRY_POINT_GROUP",
    "Carried",
    "RenameRefused",
    "register_carrier",
    "rename_on_account_only",
    "rename_printer",
]

#: Kiln's own alias for whichever printer is active; never a name of the
#: person's own.
DEFAULT_ALIAS = "default"

#: Entry-point group another package names a carrier in: ``name = "pkg.mod:fn"``
#: where ``fn(old_name, new_name, context) -> (moved, undo_or_None)``.
ENTRY_POINT_GROUP = "kiln.printer_rename"

#: ``fn(old_name, new_name, context) -> (moved, undo)``; *undo* is called
#: with no arguments when a later step fails, and may be ``None``.
Carrier = Callable[[str, str, "RenameContext"], tuple[int, Callable[[], None] | None]]

_CARRIERS: list[tuple[str, Carrier]] = []


@dataclass(frozen=True)
class RenameContext:
    """What a carrier may read about the rename."""

    #: The machine under the old name (serial, else address), or ``""``.
    machine: str
    #: The saved config entry, or ``{}`` for a printer registered this session only.
    entry: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Carried:
    """One store the rename moved."""

    store: str
    moved: int

    def to_dict(self) -> dict[str, Any]:
        return {"store": self.store, "moved": self.moved}


class RenameRefused(Exception):
    """The rename was not done, and nothing changed.  ``code`` names why;
    ``extra`` carries what a caller can act on (a suggested name)."""

    def __init__(self, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra

    def to_dict(self) -> dict[str, Any]:
        return {"success": False, "error": self.message, "code": self.code, **self.extra}


def register_carrier(name: str, fn: Carrier) -> None:
    """Add a store the rename must carry.  *name* is what the result calls it."""
    _CARRIERS[:] = [(n, f) for n, f in _CARRIERS if n != name]
    _CARRIERS.append((name, fn))


def _entry_point_carriers() -> list[tuple[str, Carrier]]:
    """Carriers other installed packages declare, loaded fresh each time."""
    found: list[tuple[str, Carrier]] = []
    try:
        eps = importlib.metadata.entry_points()
        group = eps.select(group=ENTRY_POINT_GROUP) if hasattr(eps, "select") else eps.get(ENTRY_POINT_GROUP, [])  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001 -- no metadata is no carriers
        return found
    for ep in group:
        try:
            found.append((str(ep.name), ep.load()))
        except Exception:  # noqa: BLE001 -- a carrier that cannot load is reported, never skipped silently
            logger.warning("printer rename carrier %r could not be loaded", ep.name, exc_info=True)
            found.append((str(ep.name), _unloadable(str(ep.name))))
    return found


def _unloadable(name: str) -> Carrier:
    def _carrier(old: str, new: str, context: RenameContext) -> tuple[int, None]:
        raise RuntimeError(f"the {name} records could not be carried (its carrier failed to load)")

    return _carrier


# ---------------------------------------------------------------------------
# The door
# ---------------------------------------------------------------------------


def rename_printer(
    old_name: str,
    new_name: str,
    *,
    source: str = "person",
    config_path: Path | None = None,
    check_busy: bool = True,
    busy: Callable[[str], str | None] | None = None,
    after: Callable[[str, str], None] | None = None,
) -> dict[str, Any]:
    """Rename the printer *old_name* to *new_name* everywhere on this
    computer.  Returns the report (``success``, what was carried, which
    permissions followed or were closed, and what the person is told);
    raises :class:`RenameRefused` when nothing was changed.

    *source* is who asked (``"person"`` at a terminal, ``"agent"`` through a
    tool), kept on the ledger row.  *busy* is the door's own check for a
    print in flight it can see (a live watch in this process); *after* runs
    once everything moved, for state only the door holds.
    """
    old = str(old_name or "").strip()
    new = str(new_name or "").strip()
    if not old or not new:
        raise RenameRefused("INVALID_ARGS", "A rename needs the printer's current name and its new one.")
    if _hosted():
        # The dispatcher refuses the tool by name on the shared server; this
        # is the same refusal at the door, for any other way in.
        raise RenameRefused(
            "NOT_HERE",
            "A printer is renamed on the computer it is set up on, never on Kiln's servers: "
            f"run `{kiln_command('rename-printer OLD NEW')}` there, or ask the Kiln running there.",
        )
    if old == new:
        raise RenameRefused("NOTHING_TO_DO", f"{old!r} is already this printer's name.")
    if new.lower() == DEFAULT_ALIAS:
        raise RenameRefused(
            "NAME_RESERVED",
            f"{DEFAULT_ALIAS!r} is the name Kiln uses for whichever printer is active; pick a name of your own.",
        )
    if old.lower() == DEFAULT_ALIAS:
        raise RenameRefused(
            "NAME_RESERVED",
            f"{DEFAULT_ALIAS!r} is Kiln's own alias for whichever printer is active, not a printer's name; "
            f"rename the printer by its saved name ({kiln_command('printers')} lists them).",
        )

    from kiln.cli.config import _read_config_file, get_config_path

    path = config_path or get_config_path()
    try:
        saved = _read_config_file(path).get("printers") or {}
    except Exception:  # noqa: BLE001 -- no config yet is no saved printers
        saved = {}
    if not isinstance(saved, dict):
        saved = {}
    entry = saved.get(old) if isinstance(saved.get(old), dict) else None
    if entry is None:
        # Registered this session without saving: its entry is the server's.
        entry = _session_entry(old)

    # Resolving the machine builds the adapter from config where this
    # process has not registered it yet (a terminal command), so every check
    # below sees a live adapter whichever door it came through.
    machine = _machine_under(old)
    registry = _registry()
    registered = registry is not None and old in registry
    if entry is None and not registered:
        known = sorted(n for n in {*saved, *(registry.list_names() if registry is not None else [])} if n)
        raise RenameRefused(
            "PRINTER_NOT_FOUND",
            f"No printer named {old!r} is set up here." + (f" Printers here: {', '.join(known)}." if known else ""),
            printers=known,
        )

    # A name is taken whatever its case: every door matches printer names
    # case-insensitively, so two names that differ only by case are one
    # mention with two answers.
    taken = [n for n in saved if n != old and n.lower() == new.lower()]
    if registry is not None:
        mine = registry.get(old) if registered else None
        taken += [
            n for n in registry.list_names() if n != old and n.lower() == new.lower() and registry.get(n) is not mine
        ]
    if taken:
        raise RenameRefused("NAME_TAKEN", f"A printer named {taken[0]!r} is already set up here; pick another name.")
    still_named = _windows_naming(new, exclude=old)
    if still_named:
        raise RenameRefused(
            "NAME_TAKEN",
            f"A standing permission on this computer still names {new!r} ({', '.join(still_named)}); "
            f"a rename never puts a permission on a different machine. Close it first "
            f"({kiln_command('consent revoke')}) or pick another name.",
            windows=still_named,
        )
    naming_new = _account_delegations(new)
    if naming_new:
        raise RenameRefused(
            "NAME_TAKEN",
            f"A delegation on your Kiln account still names {new!r} ({_delegations_said(naming_new)}); a rename "
            f"never puts a permission on a different machine. End it on your account page, or pick another name.",
            delegations=naming_new,
        )
    always_new = _account_always_allow(new)
    if always_new:
        raise RenameRefused(
            "NAME_TAKEN",
            f"Always allow on your Kiln account is still on for a printer named {new!r}; a rename never puts "
            f"a standing permission on a different machine. Turn it off on your account page, or pick another name.",
            always_allow=always_new,
        )
    if naming_new is not None and always_new is None:
        naming_new = None  # half the account unread is the account unread
    if naming_new is None and not _signed_out():
        # Signed in, and the account's permissions could not be read: a
        # name a delegation or always allow there still covers cannot be
        # ruled out, so nothing moves until it can be.
        raise RenameRefused(
            "ACCOUNT_UNREACHABLE",
            "Kiln could not read your Kiln account just now, and a standing permission there may still "
            f"name {new!r}; try again when online.",
        )
    formerly_used = _was_a_name_here(new) and naming_new is None
    if formerly_used and source == "agent":
        raise RenameRefused(
            "NAME_FORMER",
            f"{new!r} used to be a printer's name on this computer, and a permission on the person's Kiln "
            f"account may still name it. Ask the person to rename at a terminal, or pick another name.",
        )
    # A case-only rename moves the printer's own records; nothing can merge.
    with_records = [] if new.lower() == old.lower() else _records_under(new)
    if with_records:
        raise RenameRefused(
            "NAME_TAKEN",
            f"{new!r} already has records of its own ({', '.join(with_records)}); renaming onto it would "
            "merge two printers' histories. Pick another name.",
        )

    _check_name(new, entry or {})

    if check_busy:
        reason = _busy_reason(old, registry if registered else None, busy)
        if reason:
            raise RenameRefused("PRINTER_BUSY", reason)
    if _emergency_stopped(old):
        # A latch is kept by name in every Kiln process that holds one; a
        # name that changed under a running process would leave its latch
        # on the old name and the new name open.  Clearing first is the
        # person's act, and the one that keeps the stop a stop.
        raise RenameRefused(
            "EMERGENCY_STOPPED",
            f"{old!r} is under an emergency stop. Clear the stop first ({kiln_command('emergency-clear')}), then rename.",
        )

    context = RenameContext(machine=machine, entry=dict(entry or {}))
    carried: list[Carried] = []
    undo: list[tuple[str, Callable[[], None]]] = []
    warnings: list[str] = []

    attempting = {"store": "a store"}

    def _step(name: str, fn: Callable[[], tuple[int, Callable[[], None] | None]]) -> None:
        attempting["store"] = name
        moved, put_back = fn()
        carried.append(Carried(name, int(moved)))
        if put_back is not None:
            undo.append((name, put_back))

    try:
        if entry is not None:
            _step("config", lambda: _carry_config(old, new, path))
        _step("records", lambda: _carry_records(old, new, machine, source))
        _step("queue", lambda: _carry_queue(old, new))
        _step("emergency_stop", lambda: _carry_emergency(old, new))
        _step("retained_print", lambda: _carry_twin(old, new))
        _step("engagement", lambda: _carry_engagement(old, new))
        _step("reservations", lambda: _carry_locks(old, new))
        if registered and registry is not None:
            _step("registry", lambda: _carry_registry(registry, old, new))
        _step("nozzle_milestone", lambda: _carry_milestone(old, new))
        for name, fn in [*_CARRIERS, *_entry_point_carriers()]:
            _step(name, lambda fn=fn: fn(old, new, context))
        _step("account", lambda: _carry_account(old, new, warnings))
        # Last: a window closed on the account cannot be reopened by Kiln.
        attempting["store"] = "standing permissions"
        permissions = _carry_windows(old, new, machine)
    except Exception as exc:
        failed_at = attempting["store"]
        _put_back(undo, warnings)
        logger.warning("rename %r -> %r failed at %s: %s", old, new, failed_at, exc, exc_info=True)
        raise RenameRefused(
            "RENAME_FAILED",
            f"Kiln could not rename {old!r}: {exc}. "
            + (
                f"Everything was put back, so it is still {old!r}."
                if not warnings
                else "Some of it could not be put back: " + "; ".join(warnings)
            ),
        ) from exc

    if after is not None:
        try:
            after(old, new)
        except Exception:  # noqa: BLE001 -- state only this process holds; the rename is done
            logger.debug("rename after-hook failed", exc_info=True)

    if formerly_used:
        warnings.append(
            f"{new!r} was a printer's name on this computer before, and Kiln could not read your account "
            f"just now; if a permission there still names it, that permission now covers this printer. "
            "Check your account page."
        )
    formerly = _formerly(new)
    return {
        "success": True,
        "old_name": old,
        "new_name": new,
        "machine": machine,
        "carried": [c.to_dict() for c in carried if c.moved],
        "permissions": permissions,
        "formerly": formerly,
        "warnings": warnings,
        "message": _message(old, new, carried, permissions) + ("".join(" " + w for w in warnings) if warnings else ""),
    }


def rename_on_account_only(old_name: str, new_name: str) -> dict[str, Any]:
    """Move only the records the person's Kiln account keeps under
    *old_name* (what the printer's nozzle and blade are) to *new_name*:
    the step a rename made while signed out had to leave.  Nothing on this
    computer changes.  Raises :class:`RenameRefused` when signed out, the
    account cannot be reached, or it refuses."""
    old, new = str(old_name or "").strip(), str(new_name or "").strip()
    if not old or not new or old == new:
        raise RenameRefused("INVALID_ARGS", "A rename needs the printer's current name and a different new one.")
    from kiln.bridge_client import account_signed_out, rename_on_account

    if account_signed_out():
        raise RenameRefused(
            "SIGNED_OUT", f"Sign in to your Kiln account first ({kiln_command('signin')}), then run this again."
        )
    answer = rename_on_account(old, new)
    if answer is None:
        raise RenameRefused("ACCOUNT_UNREACHABLE", "Kiln could not reach your account; try again when online.")
    done, why = answer
    if not done:
        raise RenameRefused("NAME_TAKEN", f"Your Kiln account refused the new name: {why}")
    return {
        "success": True,
        "old_name": old,
        "new_name": new,
        "carried": [{"store": "account", "moved": 1}],
        "permissions": {},
        "formerly": [],
        "warnings": [],
        "message": f"Your Kiln account's records for {old!r} are now under {new!r}. Nothing on this computer changed.",
    }


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


def _hosted() -> bool:
    """The shared multi-tenant server, asked the way every sibling store
    asks.  Never wrapped: a handler around the guard is what turns a
    refusal into a carry-on, and runtime_env always imports."""
    from kiln.runtime_env import is_hosted_multitenant

    return bool(is_hosted_multitenant())


def _registry() -> Any | None:
    try:
        from kiln import server as _srv

        return _srv._get_registry()
    except Exception:  # noqa: BLE001 -- no registry (a bare CLI) is no live printers
        return None


def _machine_under(name: str) -> str:
    try:
        from kiln.consent_windows import machine_under

        return machine_under(name)
    except Exception:  # noqa: BLE001 -- an unidentifiable machine carries no machine-bound permission
        return ""


def _records_under(name: str) -> list[str]:
    """The stores already holding records under *name*: the name-keyed
    tables in kiln.db, and the queue's jobs."""
    found: list[str] = []
    try:
        from kiln.persistence import get_db

        found += sorted(t for t, n in get_db().rows_under_printer_name(name).items() if n)
    except Exception:  # noqa: BLE001 -- no records here is nothing to merge
        pass
    try:
        from kiln import server as _srv

        if _srv._get_queue().names_printer(name):
            found.append("queue")
    except Exception:  # noqa: BLE001 -- no queue here is no jobs to merge
        pass
    return found


def _windows_naming(name: str, *, exclude: str = "") -> list[str]:
    """The live standing permissions on this computer whose scope names
    *name* and not *exclude* (the printer's own windows, which follow):
    ids a refusal can name.  A window that names a printer not set up
    here is still a permission, and the rename must not become the machine
    it covers."""
    try:
        from kiln.consent_windows import _norm, live_windows

        return [
            w.id
            for w in live_windows()
            if isinstance(w.scope, tuple)
            and _norm(name) in {_norm(s) for s in w.scope}
            and not (exclude and _norm(exclude) in {_norm(s) for s in w.scope})
        ]
    except Exception:  # noqa: BLE001 -- no windows here is none naming it
        return []


def _session_entry(name: str) -> dict[str, Any] | None:
    try:
        from kiln import server as _srv

        entry = _srv._SESSION_PRINTERS.get(name)
        return dict(entry) if isinstance(entry, dict) else None
    except Exception:  # noqa: BLE001
        return None


def _account_delegations(name: str) -> list[dict[str, Any]] | None:
    """The account's live delegations naming *name*; ``None`` when the
    account cannot be read (signed out, offline)."""
    try:
        from kiln.bridge_client import delegations_naming

        return delegations_naming(name)
    except Exception:  # noqa: BLE001
        return None


def _signed_out() -> bool:
    try:
        from kiln.bridge_client import account_signed_out

        return bool(account_signed_out())
    except Exception:  # noqa: BLE001
        return True


def _account_always_allow(name: str) -> list[str] | None:
    """Ids of the account's live always-allow grants for a printer named
    *name*, in any case; ``None`` when the account cannot be read."""
    try:
        from kiln.bridge_client import always_allow_grants

        grants = always_allow_grants()
    except Exception:  # noqa: BLE001
        return None
    if grants is None:
        return None
    return [g["id"] for g in grants if str(g.get("printer") or "").strip().lower() == name.lower()]


def _delegations_said(rows: list[dict[str, Any]]) -> str:
    return ", ".join(f"a delegation to {d.get('agent') or 'an agent'}" for d in rows)


def _was_a_name_here(name: str) -> bool:
    """Whether *name* was some printer's name on this computer before a
    rename took it away (the ledger's ``old_name``)."""
    try:
        from kiln.persistence import get_db

        row = (
            get_db()
            ._conn.execute("SELECT 1 FROM printer_renames WHERE lower(old_name) = lower(?) LIMIT 1", (name,))
            .fetchone()
        )
        return row is not None
    except Exception:  # noqa: BLE001
        return False


def _emergency_stopped(name: str) -> bool:
    try:
        from kiln.emergency import get_emergency_coordinator

        return bool(get_emergency_coordinator().is_stopped(name))
    except Exception:  # noqa: BLE001 -- no coordinator is no stop
        return False


def _check_name(new: str, entry: dict[str, Any]) -> None:
    """Refuse a name that carries the printer's address, serial or a code."""
    from kiln.printer_names import identifiers_of, name_advice

    advice = name_advice(new, printer_model=entry.get("printer_model"), identifiers=identifiers_of(entry))
    if advice is not None:
        raise RenameRefused("NAME_HAS_IDENTIFIER", advice.sentence, suggested_name=advice.suggested_name)


def _busy_reason(old: str, registry: Any | None, busy: Callable[[str], str | None] | None) -> str | None:
    adapter = None
    with contextlib.suppress(Exception):
        from kiln import server as _srv

        # Builds the adapter from config when this process has not
        # registered it (a terminal command), so the printer IS asked.
        adapter = _srv._resolve_adapter(old)
    if adapter is None and registry is not None:
        with contextlib.suppress(Exception):
            adapter = registry.get(old)
    if adapter is not None:
        from kiln.printers.routine_ledger import machine_is_printing

        if machine_is_printing(adapter):
            return f"{old!r} is printing right now. Rename it once the print is done."
        # Another Kiln session holding a reservation on the machine is
        # working with it under this name; a rename is its to make.
        with contextlib.suppress(Exception):
            from kiln.printer_lock import SESSION as _this_session
            from kiln.printer_lock import _live, _read, machine_key

            hold = _live(_read(), time.time()).get(machine_key(adapter, old))
            if hold is not None and hold.get("session") != _this_session:
                who = str(hold.get("holder") or "another Kiln session")
                return f"{old!r} is reserved by {who} right now. Rename it once that reservation ends."
    with contextlib.suppress(Exception):
        from kiln.ending_watch import _watcher_running

        if _watcher_running(old):
            return f"Kiln is still following a print on {old!r}. Rename it once that print has ended."
    with contextlib.suppress(Exception):
        from kiln.bridge_client import holds_an_ask

        if holds_an_ask(old):
            return f"A print on {old!r} is waiting for approval. Answer that first, then rename."
    if busy is not None:
        reason = busy(old)
        if reason:
            return reason
    return None


# ---------------------------------------------------------------------------
# The carriers, each with its way back
# ---------------------------------------------------------------------------


def _carry_config(old: str, new: str, path: Path) -> tuple[int, Callable[[], None]]:
    from kiln.cli.config import rename_printer as _rename_entry

    _rename_entry(old, new, config_path=path)
    return 1, lambda: _rename_entry(new, old, config_path=path)


def _carry_records(old: str, new: str, machine: str, source: str) -> tuple[int, Callable[[], None]]:
    from kiln.persistence import get_db

    db = get_db()
    moved = db.rename_printer_records(old, new, machine_fingerprint=machine, source=source)
    return sum(moved.values()), lambda: db.undo_printer_rename(old, new)


def _carry_queue(old: str, new: str) -> tuple[int, Callable[[], None] | None]:
    try:
        from kiln import server as _srv

        queue = _srv._get_queue()
    except Exception:  # noqa: BLE001 -- no queue here is no jobs to move
        return 0, None
    moved = queue.rename_printer(old, new)
    return moved, (lambda: queue.rename_printer(new, old)) if moved else None


def _carry_emergency(old: str, new: str) -> tuple[int, Callable[[], None] | None]:
    from kiln.emergency import get_emergency_coordinator

    coordinator = get_emergency_coordinator()
    moved = sum(coordinator.rename_printer(old, new).values())
    return moved, (lambda: coordinator.rename_printer(new, old)) if moved else None


def _carry_twin(old: str, new: str) -> tuple[int, Callable[[], None] | None]:
    from kiln import monitor_twin

    moved = monitor_twin.rename_printer(old, new)
    return int(moved), (lambda: monitor_twin.rename_printer(new, old)) if moved else None


def _carry_engagement(old: str, new: str) -> tuple[int, Callable[[], None] | None]:
    from kiln.printers.engagement import rename_label

    moved = rename_label(old, new)
    return moved, (lambda: rename_label(new, old)) if moved else None


def _carry_locks(old: str, new: str) -> tuple[int, Callable[[], None] | None]:
    from kiln.printer_lock import rename_label

    moved = rename_label(old, new)
    return moved, (lambda: rename_label(new, old)) if moved else None


def _carry_milestone(old: str, new: str) -> tuple[int, Callable[[], None] | None]:
    from kiln import nozzle_milestones

    moved = nozzle_milestones.rename_printer(old, new)
    return moved, (lambda: nozzle_milestones.rename_printer(new, old)) if moved else None


def _carry_account(old: str, new: str, warnings: list[str]) -> tuple[int, Callable[[], None] | None]:
    """The records the person's Kiln account keeps under this printer's
    name (what its nozzle and blade are) follow the name.  Signed in but
    unreachable is refused: those records would otherwise answer to a
    name the printer no longer has.  Signed out, nothing is held there
    for this install to move, and the reply says so."""
    from kiln.bridge_client import account_signed_out, rename_on_account

    try:
        signed_out = bool(account_signed_out())
    except Exception:  # noqa: BLE001
        signed_out = True
    if signed_out:
        warnings.append(
            "Not signed in to a Kiln account, so nothing on an account was renamed; if this printer "
            f"has records there under {old!r}, sign in and run `{kiln_command('rename-printer', old, new, '--account-only')}`."
        )
        return 0, None
    answer = rename_on_account(old, new)
    if answer is None:
        raise RuntimeError(
            "Kiln could not reach your account, which keeps this printer's nozzle and blade records "
            "under its name; try again when online"
        )
    done, why = answer
    if not done:
        raise RuntimeError(f"your Kiln account refused the new name: {why}")

    def _undo() -> None:
        back = rename_on_account(new, old)
        if back is None or not back[0]:
            raise RuntimeError("the account could not be told to rename back")

    return 1, _undo


def _carry_registry(registry: Any, old: str, new: str) -> tuple[int, Callable[[], None]]:
    registry.rename(old, new)
    return 1, lambda: registry.rename(new, old)


def _carry_windows(old: str, new: str, machine: str) -> dict[str, Any]:
    from kiln.consent_windows import rename_printer as _rename_windows

    permissions = _rename_windows(old, new, machine=machine)
    # The account's delegations name printers by name and are the
    # person's to grant, on the account page; the most this computer can
    # do is read them and say exactly which no longer cover the printer.
    naming_old = _account_delegations(old)
    if naming_old:
        one = len(naming_old) == 1
        permissions["account_delegations"] = naming_old
        permissions["account"] = (
            f"On your Kiln account, {_delegations_said(naming_old)} still name{'s' if one else ''} {old!r} and so "
            f"no longer cover{'s' if one else ''} this printer; grant {'it' if one else 'them'} again for {new!r} "
            f"on your account page if you still want {'it' if one else 'them'}."
        )
    elif naming_old is None:
        with contextlib.suppress(Exception):
            from kiln.bridge_client import account_signed_out

            if not account_signed_out():
                permissions["account"] = (
                    f"Kiln could not read your account just now: a delegation there that names {old!r} "
                    f"does not cover {new!r}; grant it again for {new!r} on your account page if you still want it."
                )
    return permissions


def _put_back(undo: list[tuple[str, Callable[[], None]]], warnings: list[str]) -> None:
    for name, fn in reversed(undo):
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 -- said, never hidden
            logger.error("rename: %s could not be put back: %s", name, exc, exc_info=True)
            warnings.append(f"{name} ({exc})")


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def _formerly(name: str) -> list[dict[str, Any]]:
    try:
        from kiln.persistence import get_db

        return get_db().printer_renames(name)
    except Exception:  # noqa: BLE001
        return []


_STORE_WORDS = {
    "records": "print history and records",
    "queue": "queued jobs",
    "emergency_stop": "emergency-stop state",
    "retained_print": "the retained print",
    "reservations": "reservations",
    "nozzle_milestone": "nozzle-wear milestone",
    "account": "records on your Kiln account",
    "nozzle_state": "nozzle record",
    "cutter_state": "blade record",
    "calibration_offsets": "calibration offsets",
    "recovery_outcomes": "recovery outcomes",
}


def _message(old: str, new: str, carried: list[Carried], permissions: dict[str, Any]) -> str:
    moved = [
        _STORE_WORDS.get(c.store, c.store.replace("_", " "))
        for c in carried
        if c.moved and c.store not in ("config", "registry", "engagement")
    ]
    parts = [f"Renamed {old!r} to {new!r}."]
    if moved:
        parts.append("Its " + ", ".join(moved) + " came with it.")
    followed, closed = permissions.get("followed") or [], permissions.get("closed") or []
    if followed:
        parts.append(
            f"{len(followed)} standing permission{'s' if len(followed) != 1 else ''} on this computer now name {new!r}."
        )
    if closed:
        parts.append(
            f"{len(closed)} always-allow entr{'ies' if len(closed) != 1 else 'y'} for {old!r} "
            f"{'were' if len(closed) != 1 else 'was'} turned off, because Kiln does not move a standing "
            f"permission it cannot vouch for; turn it on again for {new!r} if you still want it."
        )
    parts.append(
        "Kiln running elsewhere on this computer (another app, the bridge) learns the new name when it next starts."
    )
    return " ".join(parts)
