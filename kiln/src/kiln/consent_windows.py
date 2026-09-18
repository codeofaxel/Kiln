"""A standing window: a person's "yes, for a while", kept apart from a yes.

A yes is for one print.  The owner's rule, verbatim: "a yes should not
automatically be 'ok for this time window' unless the user asks for that
specifically."  So the thing that lets an unattended agent start prints
for the next two hours is not a yes that was stretched — it is a separate
record, opened on purpose, by a command a person types, at a terminal
they are sitting at.

What a window is:

* **Opened only by a person.**  ``open_window`` refuses unless stdin and
  stdout are both terminals — the same test the CLI's y/N uses.  An
  agent's subprocess, ``yes |``, and a flag it could type all fail it.
  There is no environment variable and no option that stands in for the
  person; a window that could be opened without one would be the old
  hole with a longer name.
* **Scoped.**  One printer, a named list, or the fleet.  A person names
  it; nothing defaults to "everything".
* **Timed.**  It has an ``until``.  A person can extend it; nothing
  else can.  It can be revoked at any time.
* **Signed.**  It records who opened it — locally that is the OS user,
  and the record says ``os_user:`` so nobody mistakes it for an account
  — and when, and every extension.
* **Local.**  On the hosted multi-tenant server the file under
  ``~/.kiln`` is nobody's, so :func:`covering` answers ``None`` there
  and the command refuses to write one.

Two readers: the gate (through :func:`kiln.print_consent.consent_for`)
when a start arrives with a preview and no other yes, and the scheduler
when it dispatches a job that was queued under a window — a window that
has since been revoked or has run out does not start the job.  Every
start that rests on a window is audited with the window's id.
"""

from __future__ import annotations

import contextlib
import getpass
import json
import logging
import os
import secrets
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SCOPE_FLEET = "fleet"

Scope = tuple[str, ...] | str

_lock = threading.Lock()


class NotAPerson(RuntimeError):
    """Raised when a window is opened or extended by something that is not
    a person at a terminal."""


def person_at_terminal() -> bool:
    """A terminal with a person at both ends of it."""
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except Exception:  # noqa: BLE001 — a closed stream is not a person
        return False


def local_identity() -> str:
    """Who this process runs as, labelled so the record cannot be mistaken
    for an account: ``os_user:<name>``."""
    try:
        user = getpass.getuser()
    except Exception:  # noqa: BLE001
        user = ""
    return f"os_user:{user or 'unknown'}"


def _now() -> float:
    return time.time()


def _path() -> Path:
    home = Path(os.environ.get("KILN_HOME", "").strip() or (Path.home() / ".kiln"))
    return home / "consent_windows.json"


def _norm(name: str | None) -> str:
    return str(name or "").strip().lower()


def normalize_scope(scope: Any) -> Scope | None:
    """``("a", "b")`` or ``"fleet"``; ``None`` for anything that names no
    printer.  Read from the file as well as from callers, so a record
    without a scope covers nothing rather than everything."""
    if scope == SCOPE_FLEET:
        return SCOPE_FLEET
    if isinstance(scope, str):
        names = [scope.strip()] if scope.strip() else []
    elif isinstance(scope, (list, tuple)):
        names = [str(s).strip() for s in scope if str(s).strip()]
    else:
        return None
    return tuple(names) or None


def scope_covers(scope: Any, printer_name: str | None) -> bool:
    scope = normalize_scope(scope)
    if scope is None:
        return False
    if scope == SCOPE_FLEET:
        return True
    return _norm(printer_name) in {_norm(s) for s in scope}


def describe_scope(scope: Any) -> str:
    scope = normalize_scope(scope)
    if scope is None:
        return "no printer"
    if scope == SCOPE_FLEET:
        return "the whole fleet"
    return ", ".join(scope)


def parse_duration(text: str) -> float:
    """``2h``, ``30m``, ``90s``, ``1d`` → seconds.  A bare number is hours."""
    raw = str(text or "").strip().lower()
    if not raw:
        raise ValueError("a duration is needed, like 2h or 30m")
    units = {"s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}
    unit = 3600.0
    if raw[-1] in units:
        unit = units[raw[-1]]
        raw = raw[:-1]
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"could not read {text!r} as a duration, like 2h or 30m") from exc
    seconds = value * unit
    if seconds <= 0:
        raise ValueError("a window has to last longer than nothing")
    return seconds


@dataclass(frozen=True)
class Window:
    """One standing window, as written by the person who opened it."""

    id: str
    set_by: str
    set_at: float
    until: float
    scope: Scope
    revoked_at: float | None = None
    extensions: list[dict[str, Any]] = field(default_factory=list)

    def live(self, now: float | None = None) -> bool:
        now = _now() if now is None else now
        return self.revoked_at is None and self.until > now

    def covers(self, printer_name: str | None) -> bool:
        return scope_covers(self.scope, printer_name)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "set_by": self.set_by,
            "set_at": self.set_at,
            "until": self.until,
            "scope": list(self.scope) if isinstance(self.scope, tuple) else self.scope,
            "revoked_at": self.revoked_at,
            "extensions": list(self.extensions),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Window | None:
        try:
            scope = normalize_scope(raw.get("scope"))
            if scope is None:
                # No scope on record covers nothing.  Kept, so status can
                # show it and revoke can remove it; never matched.
                scope = ()
            return cls(
                id=str(raw.get("id") or ""),
                set_by=str(raw.get("set_by") or ""),
                set_at=float(raw.get("set_at") or 0.0),
                until=float(raw.get("until") or 0.0),
                scope=scope,
                revoked_at=float(raw["revoked_at"]) if raw.get("revoked_at") is not None else None,
                extensions=[e for e in (raw.get("extensions") or []) if isinstance(e, dict)],
            )
        except Exception:  # noqa: BLE001 — an unreadable record covers nothing
            return None


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


def _read() -> list[Window]:
    try:
        raw = json.loads(_path().read_text())
    except Exception:  # noqa: BLE001 — no file, or a corrupt one, is no windows
        return []
    rows = raw.get("windows") if isinstance(raw, dict) else None
    out: list[Window] = []
    for row in rows or []:
        if isinstance(row, dict) and (w := Window.from_dict(row)) is not None and w.id:
            out.append(w)
    return out


def _write(windows: list[Window]) -> None:
    """Atomic and private (0600).  Raises: a window that could not be
    written must not be reported as open."""
    import tempfile

    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"windows": [w.to_dict() for w in windows]}
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".consent_windows_")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(payload, fh, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _hosted() -> bool:
    try:
        from kiln.runtime_env import is_hosted_multitenant

        return is_hosted_multitenant()
    except Exception:  # noqa: BLE001
        return False


def _require_person() -> None:
    if _hosted():
        raise NotAPerson("the hosted server has no terminal and keeps no standing windows")
    if not person_at_terminal():
        raise NotAPerson(
            "a standing window is opened by a person at a terminal (stdin and stdout "
            "both a TTY); nothing else can open or extend one"
        )


# ---------------------------------------------------------------------------
# Writing — a person, at a terminal
# ---------------------------------------------------------------------------


def open_window(*, seconds: float, scope: Any) -> Window:
    """Open a window for *seconds* over *scope*.  Raises :class:`NotAPerson`
    off a terminal, ``ValueError`` for no time or no scope."""
    _require_person()
    if not isinstance(seconds, (int, float)) or seconds <= 0:
        raise ValueError("a window has to last longer than nothing")
    normalized = normalize_scope(scope)
    if normalized is None:
        raise ValueError("a window names the printer(s) it covers, or the fleet")
    now = _now()
    window = Window(
        id=f"w_{secrets.token_hex(6)}",
        set_by=local_identity(),
        set_at=now,
        until=now + float(seconds),
        scope=normalized,
    )
    with _lock:
        windows = _read()
        windows.append(window)
        _write(windows)
    logger.info(
        "standing consent window %s opened by %s for %s, until %s",
        window.id, window.set_by, describe_scope(window.scope), time.ctime(window.until),
    )
    return window


def extend_window(window_id: str, *, seconds: float) -> Window:
    """Move a live window's ``until`` to now + *seconds*.  A person only."""
    _require_person()
    if not isinstance(seconds, (int, float)) or seconds <= 0:
        raise ValueError("an extension has to last longer than nothing")
    now = _now()
    with _lock:
        windows = _read()
        for i, w in enumerate(windows):
            if w.id != window_id:
                continue
            if w.revoked_at is not None:
                raise ValueError(f"window {window_id} was revoked; open a new one")
            longer = Window(
                id=w.id, set_by=w.set_by, set_at=w.set_at, until=now + float(seconds), scope=w.scope,
                revoked_at=None,
                extensions=[*w.extensions, {"at": now, "until": now + float(seconds), "by": local_identity()}],
            )
            windows[i] = longer
            _write(windows)
            return longer
    raise KeyError(f"no window {window_id}")


def revoke_window(window_id: str) -> Window:
    """Close a window now.  Anyone may close one: revoking is the safe
    direction, and a revoke nobody can perform is a window nobody can stop."""
    now = _now()
    with _lock:
        windows = _read()
        for i, w in enumerate(windows):
            if w.id != window_id:
                continue
            closed = Window(
                id=w.id, set_by=w.set_by, set_at=w.set_at, until=w.until, scope=w.scope,
                revoked_at=w.revoked_at if w.revoked_at is not None else now,
                extensions=w.extensions,
            )
            windows[i] = closed
            _write(windows)
            return closed
    raise KeyError(f"no window {window_id}")


def revoke_all() -> list[Window]:
    closed: list[Window] = []
    for w in live_windows():
        closed.append(revoke_window(w.id))
    return closed


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def all_windows() -> list[Window]:
    with _lock:
        return _read()


def live_windows(now: float | None = None) -> list[Window]:
    now = _now() if now is None else now
    return [w for w in all_windows() if w.live(now)]


def get_window(window_id: str) -> Window | None:
    return next((w for w in all_windows() if w.id == window_id), None)


def is_live(window_id: str, now: float | None = None) -> bool:
    w = get_window(window_id)
    return bool(w and w.live(now))


def covering(printer_name: str | None, now: float | None = None) -> Window | None:
    """The live window that covers *printer_name*, or ``None``.  Always
    ``None`` on the hosted server, where the file is nobody's."""
    if _hosted():
        return None
    for w in live_windows(now):
        if w.covers(printer_name):
            return w
    return None


def _reset_for_tests() -> None:
    """Nothing is cached in memory; here so fixtures read the same way as
    the sibling ledgers'."""
