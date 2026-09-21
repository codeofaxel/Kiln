"""A standing window: a person's "yes, for a while", kept apart from a yes.

A yes is for one print.  The owner's rule, verbatim: "a yes should not
automatically be 'ok for this time window' unless the user asks for that
specifically."  So the thing that lets an unattended agent start prints
for the next two hours is not a yes that was stretched — it is a separate
record, opened on purpose, by a person, through a door the agent does not
hold.

What a window is:

* **Opened only by a person, through one of two doors.**  ``open_window``
  refuses unless stdin and stdout are both terminals — the same test the
  CLI's y/N uses.  An agent's subprocess, ``yes |``, and a flag it could
  type all fail it.  ``open_window_from_dialog`` takes the answer the
  host's approval dialog came back with, when the person picked "yes,
  and for the next while": that answer travels the elicitation channel —
  the server asks the CLIENT, and only the client's response answers —
  so the agent is not holding the pen there either.  There is no
  environment variable, no option and no tool that stands in for the
  person; a window that could be opened without one would be the old
  hole with a longer name.
* **Scoped.**  One printer, a named list, or the fleet.  A person names
  it; nothing defaults to "everything".  The dialog door opens for the
  one printer the print was aimed at, and nothing wider — a fleet window
  is a bigger decision and stays a terminal command.
* **Timed.**  It has an ``until``.  A person can extend it; nothing
  else can.  It can be revoked at any time, from anywhere: closing is the
  safe direction, so the agent is given a tool for it.
* **Signed.**  It records who opened it — locally that is the OS user,
  and the record says ``os_user:`` so nobody mistakes it for an account
  — through which door (``source``), and when, and every extension.
* **Local — or the account's.**  On the hosted multi-tenant server the
  file under ``~/.kiln`` is nobody's, so nothing here reads or writes it
  there.  What answers instead, when kiln-pro registers one, is the
  signed-in account's own store (:class:`WindowStore`): the dialog door
  opens through it, the gate reads through it, and a window it holds is
  the account's yes — grade A, like the account's approval.  With no
  store registered the hosted server keeps no windows and offers none.
* **Capped.**  Twenty-four hours at most, at every door
  (:func:`kiln.print_consent.check_window_length`); a person who wants
  longer opens another when it runs out.

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

from kiln.print_consent import (
    SOURCE_ELICITED,
    SOURCE_TERMINAL,
    DialogAnswer,
    check_window_length,
    parse_duration,
    window_seconds_for,
)

logger = logging.getLogger(__name__)

__all__ = [
    "SCOPE_FLEET",
    "SOURCE_DOORS",
    "SOURCE_WEB",
    "NotAPerson",
    "NotTheFleetTier",
    "Window",
    "WindowStore",
    "all_windows",
    "covering",
    "describe",
    "describe_scope",
    "extend_window",
    "get_window",
    "is_live",
    "live_windows",
    "local_identity",
    "normalize_scope",
    "open_window",
    "open_window_from_dialog",
    "parse_duration",
    "person_at_terminal",
    "register_window_store",
    "revoke_all",
    "revoke_covering",
    "revoke_window",
    "scope_covers",
    "window_store",
]

SCOPE_FLEET = "fleet"

#: A window opened on the account's own web page (the hosted store writes
#: it; the page is the web user's door, the way a terminal is the local
#: user's).
SOURCE_WEB = "user_web"

#: How a window was opened — which door.  The same words the print gate
#: uses for who held the pen: a terminal, the host's dialog, the web page.
SOURCE_DOORS = (SOURCE_TERMINAL, SOURCE_ELICITED, SOURCE_WEB)

Scope = tuple[str, ...] | str

_lock = threading.Lock()


class NotTheFleetTier(RuntimeError):
    """A window over several printers, or the fleet, below the tier that
    runs several printers at once."""


def _fleet_tier_allows() -> bool:
    """Whether this install's tier runs more than one printer at once.

    The same read the print gate's fleet-concurrency check makes: the
    licence's printer cap.  Absent kiln-pro the cap is one, so a plain
    install answers False without guessing.  Never raises.
    """
    try:
        from kiln.licensing import get_tier, max_printers_for_tier

        return int(max_printers_for_tier(get_tier()) or 1) > 1
    except Exception:  # noqa: BLE001 — no licence module, no fleet
        return False


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
    #: Which door opened it: a terminal (the default — every record
    #: written before the dialog door existed came through one) or the
    #: host's dialog.
    source: str = SOURCE_TERMINAL

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
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Window | None:
        try:
            scope = normalize_scope(raw.get("scope"))
            if scope is None:
                # No scope on record covers nothing.  Kept, so status can
                # show it and revoke can remove it; never matched.
                scope = ()
            source = str(raw.get("source") or SOURCE_TERMINAL)
            return cls(
                id=str(raw.get("id") or ""),
                set_by=str(raw.get("set_by") or ""),
                set_at=float(raw.get("set_at") or 0.0),
                until=float(raw.get("until") or 0.0),
                scope=scope,
                revoked_at=float(raw["revoked_at"]) if raw.get("revoked_at") is not None else None,
                extensions=[e for e in (raw.get("extensions") or []) if isinstance(e, dict)],
                source=source if source in SOURCE_DOORS else SOURCE_TERMINAL,
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
    """Whether this process is the hosted server, asked outside any handler.

    Never wrapped: a broad except around the guard turns "refuse on the
    shared disk" into "carry on", which is the one failure a guard must
    not have.  runtime_env is public Kiln's own and always imports.
    """
    from kiln.runtime_env import is_hosted_multitenant

    return is_hosted_multitenant()


class WindowStore:
    """What the hosted server's window store answers — a hook, not an
    implementation.  Public Kiln ships none; kiln-pro registers one that
    keeps each signed-in account's windows and resolves the account
    itself from the request it is serving, the way the approval hook
    does.  Every method answers for THAT account only.  ``open`` records
    ``set_by`` as the account (``account:acct_…``) and ``source`` as the
    door the answer came through."""

    def covering(self, printer_name: str | None) -> Window | None:  # pragma: no cover - contract
        raise NotImplementedError

    def live(self) -> list[Window]:  # pragma: no cover - contract
        raise NotImplementedError

    def open(self, *, seconds: float, scope: Scope, source: str) -> Window:  # pragma: no cover - contract
        raise NotImplementedError

    def revoke(self, window_id: str) -> Window:  # pragma: no cover - contract
        raise NotImplementedError


_store: WindowStore | None = None


def register_window_store(store: WindowStore | None) -> None:
    """Install (or, with ``None``, remove) the hosted window store."""
    global _store  # noqa: PLW0603
    _store = store


def window_store() -> WindowStore | None:
    """The hosted store, when this process is the hosted server and one
    is registered; ``None`` everywhere else.  Locally the file is the
    store, and a hook is never consulted for the local user's windows."""
    return _store if _store is not None and _hosted() else None


def _require_not_hosted() -> None:
    if _hosted():
        raise NotAPerson("the hosted server has no terminal and keeps no standing windows")


def _require_person() -> None:
    _require_not_hosted()
    if not person_at_terminal():
        raise NotAPerson(
            "a standing window is opened by a person at a terminal (stdin and stdout "
            "both a TTY) or through the host's approval dialog; nothing else can open "
            "or extend one"
        )


# ---------------------------------------------------------------------------
# Writing — a person, at a terminal or through the host's dialog
# ---------------------------------------------------------------------------


def _checked_scope(scope: Any) -> Scope:
    """The scope a window may be written for.  A yes over several
    machines is the fleet tier's, the same way running several machines
    at once is: one printer is every tier's.  The tier read is
    per-request on the hosted server, so the account's own tier decides
    there."""
    normalized = normalize_scope(scope)
    if normalized is None:
        raise ValueError("a window names the printer(s) it covers, or the fleet")
    if (normalized == SCOPE_FLEET or len(normalized) > 1) and not _fleet_tier_allows():
        raise NotTheFleetTier(
            "A window over several printers, or the whole fleet, is a Business feature — "
            "running more than one printer at once is what that tier adds. Open a window "
            "for one printer (--printer NAME), or see https://kiln3d.com/pricing."
        )
    return normalized


def _open_window(*, seconds: float, scope: Any, source: str) -> Window:
    """The one writer both doors share.  Each door does its own guarding
    BEFORE calling this; nothing here asks who is calling.  The length
    cap and the tier gate live here so neither door can be the lenient
    one."""
    seconds = check_window_length(seconds)
    normalized = _checked_scope(scope)
    now = _now()
    window = Window(
        id=f"w_{secrets.token_hex(6)}",
        set_by=local_identity(),
        set_at=now,
        until=now + float(seconds),
        scope=normalized,
        source=source,
    )
    with _lock:
        windows = _read()
        windows.append(window)
        _write(windows)
    logger.info(
        "standing consent window %s opened by %s (%s) for %s, until %s",
        window.id, window.set_by, source, describe_scope(window.scope), time.ctime(window.until),
    )
    return window


def open_window(*, seconds: float, scope: Any) -> Window:
    """The terminal door: open a window for *seconds* over *scope*.  Raises
    :class:`NotAPerson` off a terminal, ``ValueError`` for no time or no
    scope."""
    _require_person()
    return _open_window(seconds=seconds, scope=scope, source=SOURCE_TERMINAL)


def open_window_from_dialog(answer: DialogAnswer, *, printer_name: str) -> Window:
    """The dialog door: open the window the person asked for when they
    answered the approval dialog with "yes, and for a while".

    *answer* is what :func:`kiln.print_consent.answer_from_content` built
    from the person's response — the only place one is built — and only
    an answer that is a yes asking for a window opens anything; everything
    else is ``ValueError``.  The window covers *printer_name*, the machine
    the print was aimed at — or every printer, when the person chose that
    on a form that offered it (the tier gate in the writer refuses it
    below the fleet tier whichever door it came through).  A typed length
    that cannot be read, or is past the cap, is ``ValueError`` in the
    person's words; the caller reports it and the yes to THIS print
    stands.  On the hosted server the account's store opens it, or
    :class:`NotAPerson` when there is none.
    """
    if not isinstance(answer, DialogAnswer) or not answer.opens_window:
        raise ValueError("only a person's yes with a 'for a while' answer opens a window")
    name = str(printer_name or "").strip()
    if not name:
        raise ValueError("a window from the dialog covers the one printer the print was aimed at")
    seconds = window_seconds_for(answer.choice, typed=answer.typed_duration)
    scope: Scope = SCOPE_FLEET if answer.wants_every_printer else (name,)
    if _hosted():
        store = window_store()
        if store is None:
            raise NotAPerson("the hosted server has no window store; the account approves each print")
        w = store.open(
            seconds=check_window_length(seconds), scope=_checked_scope(scope), source=SOURCE_ELICITED,
        )
        logger.info("standing consent window %s opened by %s (%s) for %s, until %s",
                    w.id, w.set_by, w.source, describe_scope(w.scope), time.ctime(w.until))
        return w
    return _open_window(seconds=seconds, scope=scope, source=SOURCE_ELICITED)


def extend_window(window_id: str, *, seconds: float) -> Window:
    """Move a live window's ``until`` to now + *seconds*.  A person only."""
    _require_person()
    seconds = check_window_length(seconds)
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
                source=w.source,
            )
            windows[i] = longer
            _write(windows)
            return longer
    raise KeyError(f"no window {window_id}")


def revoke_window(window_id: str) -> Window:
    """Close a window now.  Anyone may close one: revoking is the safe
    direction, and a revoke nobody can perform is a window nobody can stop.
    On the hosted server the account's store closes it."""
    if _hosted():
        store = window_store()
        if store is None:
            raise KeyError(f"no window {window_id}")
        return store.revoke(window_id)
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
                source=w.source,
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


def revoke_covering(printer_name: str | None) -> list[Window]:
    """Close every live window that covers *printer_name* — a fleet window
    included, since it covers this printer too.  The list closed, possibly
    empty.  Safe direction, so no guard."""
    closed: list[Window] = []
    for w in live_windows():
        if w.covers(printer_name):
            closed.append(revoke_window(w.id))
    return closed


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def all_windows() -> list[Window]:
    """Every record locally; on the hosted server, the account's live
    windows from the store (a store keeps no history for the gate to read)
    or nothing."""
    if _hosted():
        store = window_store()
        return list(store.live()) if store is not None else []
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
    """The live window that covers *printer_name*, or ``None``.  On the
    hosted server the file is nobody's, so the account's store answers,
    or nothing does."""
    if _hosted():
        store = window_store()
        if store is None:
            return None
        try:
            w = store.covering(printer_name)
        except Exception:  # noqa: BLE001 — a store that fails has no window
            logger.debug("hosted window store could not answer", exc_info=True)
            return None
        return w if isinstance(w, Window) and w.live(now) and w.covers(printer_name) else None
    for w in live_windows(now):
        if w.covers(printer_name):
            return w
    return None


def describe(w: Window, now: float | None = None) -> dict[str, Any]:
    """One window as a person reads it — the shape ``kiln consent status``,
    the status tool and the line on a print result all share, so the
    same window is described the same way at every door."""
    now = _now() if now is None else now
    return {
        "id": w.id,
        "scope": describe_scope(w.scope),
        "set_by": w.set_by,
        "opened_via": {SOURCE_ELICITED: "host_dialog", SOURCE_WEB: "web"}.get(w.source, "terminal"),
        "set_at": time.strftime("%Y-%m-%d %H:%M", time.localtime(w.set_at)),
        "until": time.strftime("%Y-%m-%d %H:%M", time.localtime(w.until)),
        "until_clock": time.strftime("%H:%M", time.localtime(w.until)),
        "remaining_minutes": max(0, int((w.until - now) // 60)),
        "live": w.live(now),
        "revoked": w.revoked_at is not None,
        "extensions": len(w.extensions),
    }


def _reset_for_tests() -> None:
    """Nothing is cached in memory but the hosted store hook; here so
    fixtures read the same way as the sibling ledgers'."""
    register_window_store(None)
