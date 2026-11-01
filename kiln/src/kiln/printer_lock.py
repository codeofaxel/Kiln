"""One Kiln session's hold on a printer, honoured by every other session.

Several Kiln sessions can reach the same printer from one computer: each AI
app starts its own Kiln, a terminal runs another, the bridge that carries a
cloud agent's commands is one more.  Without a hold, any of them can pause,
cancel or re-heat a machine another one is printing on.  ``acquire_printer_lock``
gives the session that asks a hold on one printer; while it lasts, every
OTHER session's command to that printer is refused at the one place every
command passes (``kiln.printers.base``), naming who holds it and until when.

**What a hold never stops.**  Looking (status, the camera, what the printer
holds) is never refused: watching a print disturbs nothing.  An emergency
stop is never refused: stopping a machine must not wait on anyone.  And the
printer's own screen and power switch are outside Kiln altogether.

**A hold always ends.**  It runs out after the minutes it was taken for
(:data:`DEFAULT_HOLD_MINUTES`, at most :data:`MAX_HOLD_MINUTES`); it ends
when the session that took it is gone (its process has exited); and anyone
can end it on purpose (``release_printer_lock(force=True)``).  Every
uncertainty -- an unreadable record, a holder this computer cannot ask
about -- reads as no hold, because a lock that refuses when it cannot prove
its case locks a person out of their own printer.

**Kept on disk, in ``~/.kiln/printer_locks.json``**, so every Kiln process on
this computer reads the same holds; a hold in one process's memory would
stop nobody else, which is the only thing a hold is for.  One session is one
Kiln process (:data:`SESSION`): two agents talking to the same Kiln share a
session and cannot hold a printer against each other.
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

#: A hold lasts this long unless the caller asks for another length.
DEFAULT_HOLD_MINUTES = 30.0
#: The longest hold one ask can take; ask again to keep holding.
MAX_HOLD_MINUTES = 24 * 60.0
#: The longest one ask waits for another session's hold to end.
MAX_WAIT_SECONDS = 120.0
_POLL_SECONDS = 0.5

#: This Kiln process.  A hold names it, and only it can release that hold
#: without ``force``.
SESSION = uuid.uuid4().hex

#: Commands a hold never refuses.  Looks are judged by
#: :func:`kiln.printers.engagement.is_look` and never reach :func:`check`.
_NEVER_REFUSED = frozenset({"emergency_stop"})


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
    """Hold the store against every other Kiln process while it is read and
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
    """Every hold on record, keyed by machine.  Never raises: a record that
    cannot be read reads as no holds."""
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
        logger.debug("printer lock record could not be written", exc_info=True)
        return False
    finally:
        if tmp is not None:
            with contextlib.suppress(OSError):
                os.unlink(tmp)


def _process_alive(pid: Any) -> bool:
    """Whether process *pid* still runs; ``True`` when this computer cannot
    tell, so only the hold's own time limit ends it then.

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
    """Whether *hold* still holds: not run out, and its session still here."""
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
    """The key a hold on *adapter*'s machine is kept under: the machine's
    own identity where it reports one, else the name Kiln knows it by."""
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
    holds it, else ``None``.  Never raises: a fault here allows the command."""
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
        return _refusal(hold, action)
    except Exception:  # noqa: BLE001 — a hold never breaks a printer
        logger.debug("printer lock check allowed the command", exc_info=True)
        return None


def _until(hold: dict[str, Any]) -> str:
    minutes = max(1, round((float(hold.get("expires_at") or 0) - time.time()) / 60))
    return f"{minutes} minute{'s' if minutes != 1 else ''}"


def _refusal(hold: dict[str, Any], action: str) -> dict[str, Any]:
    label = str(hold.get("label") or "This printer")
    holder = str(hold.get("holder") or "another Kiln session")
    return {
        "blocked": True,
        "code": "PRINTER_LOCKED",
        "reason": (
            f"{label} is held by {holder} in another Kiln session for up to "
            f"{_until(hold)} more, so Kiln did not send this command."
        ),
        "held_by": holder,
        "expires_at": hold.get("expires_at"),
        "action": action,
        "suggestions": [
            "Its status and camera still work from here, and an emergency stop "
            "always goes through. Wait for the hold to end, ask the session that "
            "holds it to release it, or take it over with "
            "release_printer_lock(force=True).",
        ],
    }


# ---------------------------------------------------------------------------
# Taking and ending a hold
# ---------------------------------------------------------------------------


def acquire(
    adapter: Any,
    label: str,
    *,
    holder: str,
    hold_minutes: float = DEFAULT_HOLD_MINUTES,
    wait_seconds: float = 0.0,
) -> dict[str, Any]:
    """Take (or renew) this session's hold on *adapter*'s printer.

    Waits up to *wait_seconds* (at most :data:`MAX_WAIT_SECONDS`) for another
    session's hold to end.  Blocking: call it off the event loop.
    """
    key = machine_key(adapter, label)
    if not key:
        return {"success": False, "code": "PRINTER_UNIDENTIFIED",
                "error": f"Kiln can't tell which machine {label} is, so it can't hold it."}
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
                    return {"success": False, "code": "LOCK_NOT_SAVED",
                            "error": "Kiln couldn't save the hold on this computer, so nothing is held."}
                return {
                    "success": True,
                    "printer": label,
                    "holder": holder,
                    "locked": True,
                    "renewed": other is not None,
                    "expires_at": holds[key]["expires_at"],
                    "hold_minutes": minutes,
                    "note": (
                        "Other Kiln sessions on this computer can watch this printer but not "
                        "command it until you release it or the hold runs out. An emergency "
                        "stop always goes through."
                    ),
                }
        if time.monotonic() >= deadline:
            refusal = _refusal(other, "acquire_printer_lock")
            return {
                "success": False,
                "code": "PRINTER_LOCKED",
                "error": refusal["reason"],
                "held_by": refusal["held_by"],
                "expires_at": refusal["expires_at"],
                "suggestions": refusal["suggestions"],
            }
        time.sleep(_POLL_SECONDS)


def release(adapter: Any, label: str, *, force: bool = False) -> dict[str, Any]:
    """End the hold on *adapter*'s printer: this session's, or any with *force*."""
    key = machine_key(adapter, label)
    with _exclusive():
        now = time.time()
        stored = _read()
        holds = _live(stored, now)
        hold = holds.get(key) if key else None
        if hold is None:
            if key in stored:
                stored.pop(key, None)
                _write(_live(stored, now))
            return {"success": True, "printer": label, "released": False,
                    "note": f"{label} was not held by any Kiln session."}
        if hold.get("session") != SESSION and not force:
            refusal = _refusal(hold, "release_printer_lock")
            return {
                "success": False,
                "code": "PRINTER_LOCKED",
                "error": (
                    f"{label} is held by {refusal['held_by']} in another Kiln session, "
                    "so this session can't release it."
                ),
                "held_by": refusal["held_by"],
                "expires_at": refusal["expires_at"],
                "suggestions": ["Pass force=True to take the hold away from that session."],
            }
        holds.pop(key, None)
        if not _write(holds):
            return {"success": False, "code": "LOCK_NOT_SAVED",
                    "error": "Kiln couldn't update the hold on this computer, so it still stands."}
        out: dict[str, Any] = {"success": True, "printer": label, "released": True}
        if hold.get("session") != SESSION:
            out["took_from"] = str(hold.get("holder") or "another Kiln session")
        return out
