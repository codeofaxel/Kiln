"""One Kiln session's reservation of a printer, honoured by every other session.

Several Kiln sessions can reach the same printer from one computer: each AI
app starts its own Kiln, a terminal runs another, the bridge that carries a
cloud agent's commands is one more.  Without a reservation, any of them can
pause, cancel or re-heat a machine another one is printing on.
``acquire_printer_lock`` reserves one printer for the session that asks;
while it lasts, every OTHER session's command to that printer is refused at
the one place every command passes (``kiln.printers.base``), naming who
reserved it and for how long.

**What a reservation never stops.**  Looking (status, the camera, what the
printer holds) and an emergency stop.  Watching disturbs nothing, and
stopping a machine must not wait on anyone.  The printer's own screen and
power switch are outside Kiln altogether.

**It sits inside the one-printer rule, never over it.**  Below the fleet tier
Kiln works with one printer at a time (:mod:`kiln.printers.engagement`).  A
second printer there is refused for THAT reason, in that rule's words, and a
reservation never speaks over it: it neither hides the rule's refusal nor
lets anyone reserve a printer the rule would not let them use.  So wherever a
reservation does speak, the printer is one the person may use, and what it
says about looking and stopping is true there.

**A reservation always ends.**  It runs out after the minutes it was taken
for (:data:`DEFAULT_HOLD_MINUTES`, at most :data:`MAX_HOLD_MINUTES`); it ends
when the session that took it is gone (its process has exited); and anyone
can end it on purpose (``release_printer_lock`` with ``force``).  Every
uncertainty -- an unreadable record, a holder this computer cannot ask
about -- reads as no reservation, because a lock that refuses when it cannot
prove its case locks a person out of their own printer.

**Kept on disk, in ``~/.kiln/printer_locks.json``**, so every Kiln process on
this computer reads the same reservations; one in a process's memory would
stop nobody else, which is the only thing it is for.  One session is one Kiln
process (:data:`SESSION`): two agents talking to the same Kiln share a
session and cannot reserve a printer against each other.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_STORE_NAME = "printer_locks.json"
_SCHEMA_VERSION = 1

#: A reservation lasts this long unless the caller asks for another length.
DEFAULT_HOLD_MINUTES = 30.0
#: The longest one ask can reserve for; ask again to keep it.
MAX_HOLD_MINUTES = 24 * 60.0
#: The longest one ask waits for another session's reservation to end.
MAX_WAIT_SECONDS = 120.0
_POLL_SECONDS = 0.5

#: This Kiln process.  A reservation names it, and only it can release that
#: reservation without ``force``.
SESSION = uuid.uuid4().hex

#: Commands a reservation never refuses.  Looks never reach :func:`check`.
_NEVER_REFUSED = frozenset({"emergency_stop"})

#: How a reservation is taken, judged by the one-printer rule like a command
#: to the printer: reserving a printer is claiming Kiln's help with it.
_RESERVING = "acquire_printer_lock"


# ---------------------------------------------------------------------------
# The record
# ---------------------------------------------------------------------------


def _kiln_dir() -> Path:
    """``~/.kiln`` (override with ``KILN_HOME``), created on demand."""
    d = Path(os.environ.get("KILN_HOME", "").strip() or (Path.home() / ".kiln"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def _store_path() -> Path:
    return _kiln_dir() / _STORE_NAME


@contextlib.contextmanager
def _exclusive():
    """Hold the record against every other Kiln process while it is read and
    rewritten, so two sessions asking at once cannot both win.  Degrades to
    no lock where none can be taken."""
    from kiln.auth_session import _lock_windows

    try:
        fd = os.open(_store_path().with_suffix(".lock"), os.O_CREAT | os.O_RDWR, 0o600)
    except OSError:
        yield
        return
    locked_windows = False
    try:
        try:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX)
        except ImportError:
            locked_windows = _lock_windows(fd)
        except OSError:
            pass
        yield
    finally:
        if locked_windows:
            with contextlib.suppress(Exception):
                import msvcrt

                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        os.close(fd)


def _read() -> dict[str, dict[str, Any]]:
    """Every reservation on record, keyed by machine.  Never raises: a record
    that cannot be read reads as none."""
    try:
        data = json.loads(_store_path().read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    if not isinstance(data, dict) or data.get("version") != _SCHEMA_VERSION:
        return {}
    holds = data.get("holds")
    return {k: v for k, v in holds.items() if isinstance(v, dict)} if isinstance(holds, dict) else {}


def _write(holds: dict[str, dict[str, Any]]) -> bool:
    """Replace the record atomically; ``False`` if it could not be written."""
    tmp: str | None = None
    try:
        path = _store_path()
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f"{path.name}.", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"version": _SCHEMA_VERSION, "holds": holds}, indent=2, sort_keys=True))
        os.replace(tmp, path)
        tmp = None
        return True
    except (OSError, ValueError, TypeError):
        logger.debug("printer reservation record could not be written", exc_info=True)
        return False
    finally:
        if tmp is not None:
            with contextlib.suppress(OSError):
                os.unlink(tmp)


def _process_alive(pid: Any) -> bool:
    """Whether process *pid* still runs; ``True`` when this computer cannot
    tell, so only the reservation's own time limit ends it then.

    Never ``os.kill`` on Windows: there a signal 0 is not a question but
    ``TerminateProcess``, which would end the holder instead of asking.
    """
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if sys.platform == "win32":
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True  # it exists, owned by someone else
    return True


def _standing(hold: dict[str, Any], now: float) -> bool:
    """Whether *hold* still stands: not run out, and its session still here."""
    expires = hold.get("expires_at")
    if not isinstance(expires, (int, float)) or expires <= now:
        return False
    if hold.get("session") == SESSION:
        return True
    return _process_alive(hold.get("pid"))


def _live(holds: dict[str, dict[str, Any]], now: float) -> dict[str, dict[str, Any]]:
    return {key: hold for key, hold in holds.items() if _standing(hold, now)}


# ---------------------------------------------------------------------------
# Which printer
# ---------------------------------------------------------------------------


def machine_key(adapter: Any, label: str = "") -> str:
    """The key a reservation of *adapter*'s machine is kept under: the
    machine's own identity where it reports one, else the name Kiln knows
    it by."""
    from kiln.printers.engagement import _label_for, machine_id

    found = machine_id(adapter)
    if found:
        return found
    name = label or _label_for(adapter)
    return f"name:{name}" if name else ""


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def check(adapter: Any, action: str) -> dict[str, Any] | None:
    """The refusal for *action* on *adapter*'s printer when ANOTHER session
    has reserved it, else ``None``.

    Silent where the one-printer rule refuses the command: that rule is
    judged next and says why in its own words.  Never raises: a fault here
    allows the command.
    """
    if action in _NEVER_REFUSED:
        return None
    try:
        holds = _read()
        if not holds:
            return None
        key = machine_key(adapter)
        hold = holds.get(key) if key else None
        if hold is None or hold.get("session") == SESSION or not _standing(hold, time.time()):
            return None
        from kiln.printers.engagement import refusal_ahead

        if refusal_ahead(adapter, action) is not None:
            return None
        return _refusal(hold, action)
    except Exception:  # noqa: BLE001 — a reservation never breaks a printer
        logger.debug("printer reservation check allowed the command", exc_info=True)
        return None


def _minutes_left(hold: dict[str, Any]) -> str:
    minutes = max(1, round((float(hold.get("expires_at") or 0) - time.time()) / 60))
    return f"{minutes} more minute{'s' if minutes != 1 else ''}"


def _who(hold: dict[str, Any]) -> str:
    """Who reserved it, in a sentence: the name it was given, said as another
    Kiln session, or just that when it was given none."""
    holder = str(hold.get("holder") or "").strip()
    return f"{holder}, another Kiln session on this computer," if holder else "another Kiln session on this computer"


def _refusal(hold: dict[str, Any], action: str) -> dict[str, Any]:
    from kiln.printers.engagement import _starts_sentence

    label = str(hold.get("label") or "this printer")
    return {
        "blocked": True,
        "code": "PRINTER_LOCKED",
        "reason": (
            f"{_starts_sentence(label)} is reserved by {_who(hold)} for up to "
            f"{_minutes_left(hold)}, so Kiln didn't send this command."
        ),
        "held_by": str(hold.get("holder") or ""),
        "expires_at": hold.get("expires_at"),
        "action": action,
        "suggestions": [
            "You can still check its status and camera, and an emergency stop always "
            "goes through. To use it now, ask that session to let it go, or take it over.",
        ],
        "agent_hint": (
            "To take it over, call release_printer_lock with force=true. To wait "
            "for it, call acquire_printer_lock with timeout_seconds."
        ),
    }


# ---------------------------------------------------------------------------
# Taking and ending a reservation
# ---------------------------------------------------------------------------


class Refused(Exception):
    """A reservation not taken or not ended, with its refusal."""

    def __init__(self, verdict: dict[str, Any]) -> None:
        super().__init__(str(verdict.get("reason") or "Not reserved."))
        self.verdict = verdict


def acquire(
    adapter: Any,
    label: str,
    *,
    holder: str = "",
    hold_minutes: float = DEFAULT_HOLD_MINUTES,
    wait_seconds: float = 0.0,
) -> dict[str, Any]:
    """Reserve *adapter*'s printer for this session, or renew its reservation.

    Waits up to *wait_seconds* (at most :data:`MAX_WAIT_SECONDS`) for another
    session's reservation to end.  Raises
    :class:`~kiln.printers.base.PrinterEngagementError` for a printer the
    one-printer rule would not let this computer use, and :class:`Refused`
    when another session keeps it.  Blocking: call it off the event loop.
    """
    from kiln.printers.base import PrinterEngagementError
    from kiln.printers.engagement import _starts_sentence, refusal_ahead

    ruled = refusal_ahead(adapter, _RESERVING)
    if ruled is not None:
        raise PrinterEngagementError(ruled)
    key = machine_key(adapter, label)
    if not key:
        raise Refused({"code": "PRINTER_UNIDENTIFIED",
                       "reason": f"Kiln can't tell which machine {label} is, so it can't reserve it."})
    minutes = min(max(float(hold_minutes or DEFAULT_HOLD_MINUTES), 1.0), MAX_HOLD_MINUTES)
    deadline = time.monotonic() + min(max(float(wait_seconds or 0.0), 0.0), MAX_WAIT_SECONDS)
    while True:
        with _exclusive():
            now = time.time()
            holds = _live(_read(), now)
            other = holds.get(key)
            if other is None or other.get("session") == SESSION:
                holds[key] = {
                    "label": label,
                    "holder": holder,
                    "session": SESSION,
                    "pid": os.getpid(),
                    "acquired_at": now if other is None else other.get("acquired_at", now),
                    "expires_at": now + minutes * 60.0,
                }
                if not _write(holds):
                    raise Refused({"code": "LOCK_NOT_SAVED",
                                   "reason": "Kiln couldn't save the reservation on this computer, so nothing is reserved."})
                return {
                    "success": True,
                    "printer": label,
                    "holder": holder,
                    "locked": True,
                    "renewed": other is not None,
                    "expires_at": holds[key]["expires_at"],
                    "hold_minutes": minutes,
                    "message": (
                        f"{_starts_sentence(label)} is reserved for this session for "
                        f"{minutes:g} minutes. Other Kiln sessions on this computer can still "
                        "check its status and camera and send an emergency stop, but nothing "
                        "else, until you release it or the time runs out."
                    ),
                }
        if time.monotonic() >= deadline:
            raise Refused(_refusal(other, _RESERVING))
        time.sleep(_POLL_SECONDS)


def release(adapter: Any, label: str, *, force: bool = False) -> dict[str, Any]:
    """End this session's reservation of *adapter*'s printer, or, with
    *force*, any session's.  Raises :class:`Refused` when another session
    keeps it and *force* is off."""
    from kiln.printers.engagement import _starts_sentence

    key = machine_key(adapter, label)
    with _exclusive():
        now = time.time()
        stored = _read()
        holds = _live(stored, now)
        hold = holds.get(key) if key else None
        if hold is None:
            if key in stored:
                _write(holds)  # drop the reservation that had already ended
            return {"success": True, "printer": label, "released": False,
                    "message": f"{_starts_sentence(label)} wasn't reserved by any Kiln session."}
        if hold.get("session") != SESSION and not force:
            verdict = _refusal(hold, "release_printer_lock")
            verdict["reason"] = (
                f"{_starts_sentence(label)} is reserved by {_who(hold)} and only that "
                "session can let it go."
            )
            verdict["suggestions"] = ["To take it over anyway, release it with force."]
            raise Refused(verdict)
        holds.pop(key, None)
        if not _write(holds):
            raise Refused({"code": "LOCK_NOT_SAVED",
                           "reason": "Kiln couldn't update the reservation on this computer, so it still stands."})
        out: dict[str, Any] = {"success": True, "printer": label, "released": True,
                               "message": f"{_starts_sentence(label)} is no longer reserved."}
        if hold.get("session") != SESSION:
            out["took_from"] = str(hold.get("holder") or "another Kiln session")
        return out
