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
  one printer the print was aimed at, or for every printer when the
  person chose that on a form that offered it — offered, and honoured,
  on the tier that runs several printers at once only.  A named list is
  the terminal command's.
* **Timed.**  It has an ``until``.  A person can extend it; nothing
  else can.  It can be revoked at any time, from anywhere: closing is the
  safe direction, so the agent is given a tool for it.  The one entry
  with no ``until`` is *always allow* (below).
* **Signed.**  It records who opened it — locally that is the OS user,
  and the record says ``os_user:`` so nobody mistakes it for an account
  — through which door (``source``), and when, and every extension.
* **Local — or the account's.**  On the hosted multi-tenant server the
  file under ``~/.kiln`` is nobody's, so nothing here reads or writes it
  there.  What stands in, when kiln-pro registers one, is the signed-in
  account's own store (:class:`WindowStore`): the dialog door opens
  through it (a standing permission the account grants the calling
  agent — one printer is every tier's; several or the fleet is the
  fleet tier's, judged here as at every door), the status tool and the
  line on a print result read through it, and revoke closes through it.  Whether a hosted print may START is not this
  module's question there: the account's yes — an approval, or the
  standing permission it granted — comes back through the hosted
  approval hook (:mod:`kiln.print_consent`), graded A.  With no store
  registered the hosted server keeps no windows and offers none.
* **Capped.**  Twenty-four hours at most, at every door
  (:func:`kiln.print_consent.check_window_length`); a person who wants
  longer opens another when it runs out — or turns on always allow.
* **Always allow.**  The same record with no end, for ONE printer
  (:func:`open_always`), and part of Kiln Pro: it lets prints start with
  nobody asked, from wherever the person is, which is what Pro sells
  (:func:`always_allow_is_yours`, asked at the door and at every start).
  Three things keep it from being the cap with a hole in it.  Only a person's own door opens one — a terminal, where
  they type the printer's name, or their signed-in account's page, whose
  record this computer keeps a copy of (:func:`mirror_account_always`)
  and confirms with the account at every start.  The dialog and the
  typed code are answers an assistant relays, so neither can write one,
  and an entry in the file that claims either door reads as closed.  It is for a machine, not a
  label: it records the machine's identity (serial, else address) and
  covers a start only while the name it was turned on for still reaches
  that machine and the start is aimed at that machine.  And it turns
  itself off when a different machine appears under the name, recording
  why (:data:`REASON_MACHINE_CHANGED`), so the refusal that follows can
  say so.  Turning it off is the ordinary revoke, from anywhere.
  Several printers can be turned on in one go, each named
  (:func:`open_always_for_several`, the fleet tier's): one entry apiece,
  so each stands, turns itself off and is turned off on its own.

  A start under it has nobody looking at the printer, so where the
  machine has a camera the plate is looked at first
  (:func:`look_at_bed`): the start goes ahead only on a plate seen clear
  in a fresh frame, waits for eyes on a frame that has not been judged,
  and falls back to asking the person when the camera gives nothing
  usable.  :mod:`kiln.print_consent` and the gate hold that rule.

  Whether a machine HAS a camera is something Kiln knows, not something
  it guesses from one picture.  It knows from the backend (one whose
  every machine ships with a camera says so), from having had a picture
  through the printer's own connection before, from a camera the person
  registered beside the printer, and -- only when none of those says, and
  no picture can be had -- from asking the person, once, at the terminal
  (:func:`bed_camera`, :func:`kiln.plate_state.knows_a_camera`).  The
  entry records what the person was told (``bed_check``).  A start goes
  ahead unlooked only under an entry turned on for a printer with no
  camera, and only while Kiln still knows of none; under every other
  entry, nothing seen means the person is asked -- a camera that is not
  answering, or has been taken away, never turns "Kiln looks first" into
  "Kiln does not look".

Two readers: the gate (through :func:`kiln.print_consent.consent_for`)
when a start arrives with a preview and no other yes, and the scheduler
when it dispatches a job that was queued under a window — a window that
has since been revoked or has run out does not start the job.  Every
start that rests on a window is audited with the window's id.
"""

from __future__ import annotations

import contextlib
import dataclasses
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

from kiln import camera_words
from kiln.print_consent import (
    SOURCE_CODE,
    SOURCE_ELICITED,
    SOURCE_TERMINAL,
    DialogAnswer,
    check_window_length,
    parse_duration,
    window_seconds_for,
)

logger = logging.getLogger(__name__)

__all__ = [
    "ALWAYS_DOORS",
    "BED_CAMERA",
    "BED_NO_CAMERA",
    "CAMERA_NO",
    "CAMERA_UNSURE",
    "CAMERA_YES",
    "REASON_MACHINE_CHANGED",
    "REASON_OFF_ON_ACCOUNT",
    "SCOPE_FLEET",
    "SOURCE_DOORS",
    "SOURCE_WEB",
    "ALWAYS_ALLOW_NEEDS_PRO",
    "NotAPerson",
    "NotTheFleetTier",
    "NotTheProTier",
    "Window",
    "WindowStore",
    "account_confirms",
    "all_windows",
    "always_allow_is_yours",
    "always_for",
    "always_waiting_on_pro",
    "bed_camera",
    "bed_goes_unchecked",
    "covering",
    "describe",
    "describe_scope",
    "extend_window",
    "get_window",
    "is_live",
    "live_windows",
    "local_identity",
    "look_at_bed",
    "mirror_account_always",
    "machine_under",
    "normalize_scope",
    "open_always",
    "open_always_for_several",
    "open_window",
    "open_window_from_dialog",
    "person_says_camera",
    "signing_in_would_tell",
    "parse_duration",
    "person_at_terminal",
    "register_window_store",
    "revoke_all",
    "revoke_covering",
    "revoke_window",
    "scope_covers",
    "settle_copy_bed_check",
    "standing_now",
    "summary_line",
    "sync_account_always",
    "turned_itself_off",
    "turned_off_recently",
    "window_store",
]

SCOPE_FLEET = "fleet"

#: A window opened on the account's own web page (the hosted store writes
#: it; the page is the web user's door, the way a terminal is the local
#: user's).
SOURCE_WEB = "user_web"

#: How a window was opened — which door.  The same words the print gate
#: uses for who held the pen: a terminal, the host's dialog, the web page.
SOURCE_DOORS = (SOURCE_TERMINAL, SOURCE_ELICITED, SOURCE_WEB, SOURCE_CODE)
#: The word for each door on a status line.  One entry per door: a door
#: left out would be read back as the terminal, which is how a window a
#: typed code opened once said "opened via terminal".
_OPENED_VIA = {
    SOURCE_TERMINAL: "terminal",
    SOURCE_ELICITED: "host_dialog",
    SOURCE_WEB: "web",
    SOURCE_CODE: "screen_code",
}

#: The doors an entry with no end may come through: the ones only a
#: person holds — a terminal on this computer, or the signed-in account's
#: own page (kept here as a copy of the account's record; see
#: :func:`mirror_account_always`).  An entry in the file with no end and
#: any other door is read as closed.
ALWAYS_DOORS = (SOURCE_TERMINAL, SOURCE_WEB)

#: Why Kiln closed an always-allow entry itself: the name it was turned on
#: for now reaches a different machine.
REASON_MACHINE_CHANGED = "machine_changed"
#: An always-allow entry closed because the person turned it on again for
#: the same machine; the newer entry is the one in force.
REASON_REPLACED = "replaced"
#: A copy of the account's always allow closed because the account no
#: longer holds it: the person turned it off on their account page.
REASON_OFF_ON_ACCOUNT = "turned_off_on_account"
#: What an always-allow entry records about the bed (``Window.bed_check``).
BED_CAMERA = "camera"
BED_NO_CAMERA = "none"

#: What the account is told when always allow is turned off on this
#: computer (its own word for it).
_TOLD_TURNED_OFF_HERE = "turned_off_at_home"

#: How long a refusal and the status surfaces go on saying that always
#: allow turned itself off.  Long enough to be heard by someone who was
#: away when it happened; after that the printer simply asks, like any
#: other.
TURNED_OFF_SAID_FOR_SECONDS = 24 * 3600.0

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


class NotTheProTier(RuntimeError):
    """Always allow below Kiln Pro."""


#: What a person below Pro is told when they try to turn always allow on:
#: what it does, the tier once, and what Free does instead.
ALWAYS_ALLOW_NEEDS_PRO = (
    "Always allow lets your assistant start prints on a printer without asking you first, "
    "wherever you are. It is part of Kiln Pro (https://kiln3d.com/pricing). On Free, Kiln asks "
    "before each print, or you can allow prints for a while, up to a day: "
    "kiln consent window --for 2h --printer NAME"
)

def always_allow_is_yours() -> bool:
    """Whether the person on this install is on Kiln Pro or above -- what
    always allow needs.  Asked at the door and again at every start, so an
    entry stops starting prints unasked once the account leaves Pro, and
    starts again if it comes back.

    One read, the same one every gate makes (``kiln.licensing.get_tier``):
    the licence where kiln-pro is installed, else the plan of the account
    signed in on this machine (``kiln signin``) while that sign-in stands;
    one the server refused grants nothing.  Never raises."""
    try:
        from kiln.licensing import LicenseTier, get_tier

        return get_tier() >= LicenseTier.PRO
    except Exception:  # noqa: BLE001 — an unreadable plan grants nothing
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
    #: When it runs out.  ``None`` only on an always-allow entry.
    until: float | None
    scope: Scope
    revoked_at: float | None = None
    extensions: list[dict[str, Any]] = field(default_factory=list)
    #: Which door opened it: a terminal (the default — every record
    #: written before the dialog door existed came through one) or the
    #: host's dialog.
    source: str = SOURCE_TERMINAL
    #: Always allow: no end, one printer, a person-only door.  Set by
    #: :func:`open_always`; read back from the file only when the record
    #: is all of those things (:meth:`from_dict`).
    always: bool = False
    #: The machine an always-allow entry was turned on for — serial, else
    #: address (:func:`machine_under`).
    machine: str = ""
    #: Why it was closed, when Kiln or a newer entry closed it rather than
    #: a person.
    revoked_reason: str = ""
    #: The account's own record this entry is a copy of, when always allow
    #: was turned on from the account page rather than at this terminal.
    account_grant: str = ""
    #: A copy closed here whose closing the account has not been told yet.
    account_owed: bool = False
    #: What the person was told about the bed when always allow was turned
    #: on: :data:`BED_CAMERA` (Kiln looks before each print, and asks when
    #: it cannot see) or :data:`BED_NO_CAMERA` (no picture could be had, the
    #: screen said Kiln cannot check the bed, and they turned it on knowing
    #: that).  Empty on a record that says neither, which reads as the
    #: first: a look that shows nothing means the person is asked.
    bed_check: str = ""

    def live(self, now: float | None = None) -> bool:
        if self.revoked_at is not None:
            return False
        if self.always:
            return True
        now = _now() if now is None else now
        return self.until is not None and self.until > now

    def covers(self, printer_name: str | None) -> bool:
        return scope_covers(self.scope, printer_name)

    def to_dict(self) -> dict[str, Any]:
        row: dict[str, Any] = {
            "id": self.id,
            "set_by": self.set_by,
            "set_at": self.set_at,
            "until": self.until,
            "scope": list(self.scope) if isinstance(self.scope, tuple) else self.scope,
            "revoked_at": self.revoked_at,
            "extensions": list(self.extensions),
            "source": self.source,
        }
        if self.always:
            row["always"] = True
            row["machine"] = self.machine
            if self.bed_check:
                row["bed_check"] = self.bed_check
        if self.revoked_reason:
            row["revoked_reason"] = self.revoked_reason
        if self.account_grant:
            row["account_grant"] = self.account_grant
            row["account_owed"] = self.account_owed
        return row

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Window | None:
        try:
            scope = normalize_scope(raw.get("scope"))
            if scope is None:
                # No scope on record covers nothing.  Kept, so status can
                # show it and revoke can remove it; never matched.
                scope = ()
            source = str(raw.get("source") or SOURCE_TERMINAL)
            machine = str(raw.get("machine") or "").strip()
            # No end is honoured only as the whole of what open_always
            # writes: marked, with no ``until``, through a person-only
            # door, for one printer, with the machine on record.  Anything
            # short of that is an ordinary record whose ``until`` is
            # missing, which has run out.
            account_grant = str(raw.get("account_grant") or "").strip()
            bed_check = str(raw.get("bed_check") or "")
            always = (
                raw.get("always") is True
                and raw.get("until") is None
                and source in ALWAYS_DOORS
                and isinstance(scope, tuple)
                and len(scope) == 1
                and bool(machine)
                # The web door's entry is a copy of a record on the account,
                # and names it; one that names none is nobody's.
                and (source != SOURCE_WEB or bool(account_grant))
            )
            return cls(
                id=str(raw.get("id") or ""),
                set_by=str(raw.get("set_by") or ""),
                set_at=float(raw.get("set_at") or 0.0),
                until=None if always else float(raw.get("until") or 0.0),
                scope=scope,
                revoked_at=float(raw["revoked_at"]) if raw.get("revoked_at") is not None else None,
                extensions=[e for e in (raw.get("extensions") or []) if isinstance(e, dict)],
                source=source if source in SOURCE_DOORS else SOURCE_TERMINAL,
                always=always,
                machine=machine if always else "",
                revoked_reason=str(raw.get("revoked_reason") or ""),
                account_grant=account_grant if always else "",
                account_owed=bool(raw.get("account_owed")) if always else False,
                bed_check=bed_check if always and bed_check in (BED_CAMERA, BED_NO_CAMERA) else "",
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


def machine_under(printer_name: str | None) -> str:
    """The machine this process reaches under *printer_name* — serial, else
    address — or ``""`` when the name is not set up here or the machine
    reports neither.

    Names are labels: a different printer can be set up under a name that
    was already in use.  This is the one question always allow asks of a
    name, and it is asked of the same resolver every start uses to find
    its adapter (the live registry, else the saved configuration), so the
    answer is about the machine a print would actually reach.  ``""`` is
    never a match for anything.  Never raises.
    """
    name = str(printer_name or "").strip()
    if not name:
        return ""
    try:
        import kiln.server as _srv
        from kiln.printers.engagement import machine_id

        return str(machine_id(_srv._resolve_adapter(name)) or "")
    except Exception:  # noqa: BLE001 — a name that cannot be resolved is no machine
        return ""


def look_at_bed(printer_name: str | None) -> Any:
    """The plate of the machine under *printer_name*, for a start nobody
    is being asked about (:func:`kiln.plate_state.look_for_unasked_start`)
    -- through the resolver a start uses, so it is the plate a print
    would land on.  A name that cannot be resolved has no camera Kiln can
    reach and nothing seen: blind.  Raises nothing of its own."""
    from kiln import plate_state

    try:
        import kiln.server as _srv

        adapter = _srv._resolve_adapter(str(printer_name or "").strip())
    except Exception:  # noqa: BLE001 -- no machine, no look
        return plate_state.UnaskedStartLook(plate_state.LOOK_BLIND, why="Kiln could not reach this printer")
    look = plate_state.look_for_unasked_start(adapter)
    if look.verdict not in (plate_state.LOOK_BLIND, plate_state.LOOK_NO_CAMERA):
        return look
    # Nothing was seen.  Whether that stops the print or not is decided by
    # what the person was told when always allow was turned on, which the
    # entry records -- never by how the printer looks to Kiln today.
    #
    # An entry turned on for a printer with NO camera (the screen said Kiln
    # cannot check its bed) starts prints unlooked, for as long as Kiln
    # still knows of no camera on that machine.  One it has since had a
    # picture from, been told of, or that is registered beside it is a
    # camera: the day it does not answer, the person is asked.
    #
    # Every other entry was turned on with the person told that Kiln looks.
    # Under those, nothing seen means the person is asked -- whether the
    # camera did not answer, or is no longer set up at all.  A camera that
    # has gone away does not turn "Kiln looks first" into "Kiln does not
    # look".  An entry that cannot be read is treated the same way.
    from kiln.errors import HostedUnavailableError

    try:
        entry = always_for(printer_name)
        told_no_camera = entry is not None and entry.bed_check == BED_NO_CAMERA
        unknown_here = entry is None
        knows = plate_state.knows_a_camera(adapter) is not None
    except HostedUnavailableError:
        raise  # a refusal to serve this store is not a fault to work around
    except Exception:  # noqa: BLE001 -- unreadable: the person is asked
        told_no_camera, unknown_here, knows = False, False, True
    if unknown_here:
        return look  # not a start under always allow: the look stands as taken
    if told_no_camera and not knows:
        return plate_state.UnaskedStartLook(
            plate_state.LOOK_NO_CAMERA, None,
            why="Kiln knows of no camera on this printer, and it gives no picture now",
        )
    if look.verdict == plate_state.LOOK_NO_CAMERA:
        return plate_state.UnaskedStartLook(
            plate_state.LOOK_BLIND, None,
            why="the camera Kiln looked through is no longer set up for this printer",
        )
    return look


#: What Kiln can say about a printer's camera before always allow is
#: turned on (:func:`bed_camera`).
CAMERA_YES = "yes"
CAMERA_NO = "no"
CAMERA_UNSURE = "unsure"


def bed_camera(printer_name: str | None) -> str:
    """Whether this printer has a camera Kiln can look at the bed through.

    :data:`CAMERA_YES` when Kiln knows of one
    (:func:`kiln.plate_state.knows_a_camera`: fitted at the factory, seen
    before, said by the person, or registered beside the printer) -- whether
    or not it answers right now -- or when a picture comes back now.
    :data:`CAMERA_NO` when the printer's connection cannot carry a picture
    and no camera is registered, or the printer cannot be reached at all.
    :data:`CAMERA_UNSURE` for what is left: a connection that could carry a
    picture, no camera Kiln knows of, and none answering.  Printer software
    that can serve a camera says so whether or not one is plugged in, so
    that case is not "no camera"; it is a question for the person.  Never
    raises.
    """
    from kiln import plate_state

    try:
        import kiln.server as _srv

        adapter = _srv._resolve_adapter(str(printer_name or "").strip())
    except Exception:  # noqa: BLE001 -- a printer that cannot be reached shows nothing
        return CAMERA_NO
    if plate_state.knows_a_camera(adapter) is not None:
        return CAMERA_YES
    if plate_state.camera_of(adapter) is None:
        return CAMERA_NO
    # Kiln's catalogue knows, for the models it lists, whether each ships
    # with a camera.  Asked once here (the answer is kept on this computer);
    # with no answer the rest goes on as if the model were not listed.
    word = _catalogue_camera_word(adapter)
    if word == camera_words.FITTED:
        return CAMERA_YES
    plate_state.look(adapter)  # a real picture is remembered by the look itself
    if plate_state.knows_a_camera(adapter) is not None:
        return CAMERA_YES
    # No picture.  A model the catalogue says has no camera, and that shows
    # none, has none: nothing to ask.  Anything else -- one its maker sells a
    # camera for, one the catalogue does not know -- is the person's to say.
    return CAMERA_NO if word == camera_words.NONE else CAMERA_UNSURE


def _catalogue_camera_word(adapter: Any) -> str | None:
    """The catalogue's word for this adapter's declared model, or ``None``
    when no model is declared or nothing answered.  Never raises."""
    try:
        from kiln import _pro_camera_bridge

        return _pro_camera_bridge.catalogue_word(adapter.camera_catalogue_model())
    except Exception:  # noqa: BLE001 -- no word is no knowledge
        return None


def signing_in_would_tell(printer_name: str | None) -> bool:
    """Whether Kiln had to leave the catalogue out for this printer only
    because nobody is signed in -- so the person can be told that signing
    in (free) lets Kiln look their model up instead of asking.  Never
    raises."""
    try:
        import kiln.server as _srv
        from kiln import _pro_camera_bridge

        adapter = _srv._resolve_adapter(str(printer_name or "").strip())
        return _pro_camera_bridge.why_unanswered(adapter.camera_catalogue_model()) == "signed_out"
    except Exception:  # noqa: BLE001
        return False


def person_says_camera(printer_name: str | None) -> None:
    """The person, at their terminal, says this printer has a camera Kiln
    could not reach just now.  Remembered for the machine, so Kiln neither
    asks again nor ever records it as having none.  A person's door only
    (:class:`NotAPerson` anywhere else): what is remembered here decides
    whether a print may start on a bed nobody saw."""
    _require_person()
    import kiln.server as _srv
    from kiln import plate_state

    adapter = _srv._resolve_adapter(str(printer_name or "").strip())
    plate_state.remember_camera(adapter, "printer", plate_state.CAMERA_PERSON_SAID)
    with contextlib.suppress(Exception):
        from kiln.streaming import note_owner_said_camera

        note_owner_said_camera(str(printer_name or "").strip())
    _audit("camera_said_by_person", {"printer": str(printer_name or ""), "by": local_identity(), "at": _now()})


def bed_goes_unchecked(w: Window) -> bool:
    """Whether prints under this always-allow entry start with the bed not
    looked at: it was turned on for a printer with no camera, and Kiln
    still knows of none.  For the status surfaces.  Never raises."""
    if not (w.always and w.bed_check == BED_NO_CAMERA and w.scope):
        return False
    from kiln import plate_state

    try:
        import kiln.server as _srv

        return plate_state.knows_a_camera(_srv._resolve_adapter(w.scope[0])) is None
    except Exception:  # noqa: BLE001 -- a printer that cannot be reached is not looked at either
        return True


def _audit(action: str, details: dict[str, Any]) -> None:
    """The audit table every door writes to.  Bookkeeping: never raises."""
    try:
        from kiln.persistence import get_db

        get_db().log_audit(
            tool_name="kiln consent window", safety_level="confirm", action=action, details=details,
        )
    except Exception:  # noqa: BLE001
        logger.debug("audit write failed for %s", action, exc_info=True)


def _machine_for_always(name: str) -> str:
    """The machine under *name*, or ``ValueError`` in the person's words
    when Kiln cannot tell which machine that is."""
    machine = machine_under(name)
    if not machine:
        raise ValueError(
            f"Kiln cannot tell which machine {name} is (no printer by that name is set up here, "
            "or it reports neither a serial number nor an address), so it could not notice a "
            "different one; always allow was not turned on"
        )
    return machine


def _bed_check_for(name: str, told_no_camera: bool) -> str:
    """What an entry records about the bed.  :data:`BED_NO_CAMERA` takes
    both halves: the door says the person read that Kiln cannot check this
    printer's bed, and Kiln itself knows of no camera on it and can get no
    picture (:func:`bed_camera`).  Anything else is :data:`BED_CAMERA`, the
    one that asks when it cannot see -- so a camera Kiln knows of is never
    recorded as absent because it was not answering that day."""
    return BED_NO_CAMERA if told_no_camera and bed_camera(name) != CAMERA_YES else BED_CAMERA


def _write_always(machines: dict[str, str], bed_checks: dict[str, str]) -> list[Window]:
    """Write one always-allow entry per ``{name: machine}``, all or none.
    The one writer; each door does its own guarding BEFORE calling this.
    An entry already on for a machine is closed in favour of the new one,
    so there is one per machine and it says who last turned it on."""
    now = _now()
    by = local_identity()
    entries = [
        Window(
            id=f"w_{secrets.token_hex(6)}", set_by=by, set_at=now, until=None,
            scope=(name,), source=SOURCE_TERMINAL, always=True, machine=machine,
            bed_check=bed_checks.get(name) or BED_CAMERA,
        )
        for name, machine in machines.items()
    ]
    with _lock:
        windows = _read()
        for i, w in enumerate(windows):
            # A copy of the account's record is the account's to close,
            # not this door's to replace.
            if w.always and w.revoked_at is None and not w.account_grant and w.machine in machines.values():
                windows[i] = _closed(w, now, REASON_REPLACED)
        windows.extend(entries)
        _write(windows)
    for w in entries:
        logger.info("always allow %s turned on by %s for %s (%s)", w.id, w.set_by, w.scope[0], w.machine)
        _audit(
            "always_allow_turned_on",
            {"window_id": w.id, "printer": w.scope[0], "machine": w.machine, "by": w.set_by,
             "at": w.set_at, "source": w.source, "together_with": len(entries) - 1,
             "bed_check": w.bed_check},
        )
    return entries


def open_always(*, printer_name: str, typed_name: str, told_no_camera: bool = False) -> Window:
    """The terminal door for always allow: a standing yes with no end, on
    the ONE printer named.

    *typed_name* is what the person typed when asked for the printer's
    name; it has to be that name.  Raises :class:`NotAPerson` off a
    terminal (and on the hosted server), ``ValueError`` when the name
    typed is another one, when no printer is named, or when Kiln cannot
    tell which machine the name is — a permission for a machine has to be
    able to notice a different one.

    *told_no_camera* is the door saying the screen the person read told
    them Kiln cannot check this printer's bed.  The entry records that
    only if a picture really cannot be had (:func:`_bed_check_for`).
    Below Kiln Pro, :class:`NotTheProTier`.
    """
    _require_person()
    name = str(printer_name or "").strip()
    if not name:
        raise ValueError("always allow is for one printer; name it")
    if not always_allow_is_yours():
        raise NotTheProTier(ALWAYS_ALLOW_NEEDS_PRO)
    if _norm(typed_name) != _norm(name):
        raise ValueError(f"that is not this printer's name ({name}); always allow was not turned on")
    machine = _machine_for_always(name)
    [entry] = _write_always({name: machine}, {name: _bed_check_for(name, told_no_camera)})
    return entry


def open_always_for_several(
    *, printer_names: list[str] | tuple[str, ...], typed_count: str,
    told_no_camera: list[str] | tuple[str, ...] = (),
) -> list[Window]:
    """The terminal door for always allow on several printers at once:
    one entry per printer, each exactly what :func:`open_always` writes,
    so each is for its own machine, turns itself off on its own, and is
    turned off on its own.

    The printers are named, every one; there is no "all of them".  The
    person confirms by typing how many they named (*typed_count*), which
    has to be that number.  Several machines at once is the tier that
    runs several machines at once (:class:`NotTheFleetTier` below it).
    Raises :class:`NotAPerson` off a terminal, ``ValueError`` for a count
    that is not the number named, a name given twice, two names for one
    machine, or any machine Kiln cannot tell apart — and then turns on
    none of them.  *told_no_camera* names the printers the screen said
    Kiln cannot check the bed of (see :func:`open_always`).
    """
    _require_person()
    names = [str(n or "").strip() for n in printer_names]
    names = [n for n in names if n]
    if len(names) < 2:
        raise ValueError("name at least two printers, or use the one-printer form")
    if len({_norm(n) for n in names}) != len(names):
        raise ValueError("a printer is named twice; name each one once")
    if not _fleet_tier_allows():
        raise NotTheFleetTier(
            "Always allow on several printers at once is a Business feature — running more than "
            "one printer at once is what that tier adds. Turn it on for one printer "
            "(--always --printer NAME), or see https://kiln3d.com/pricing."
        )
    if str(typed_count or "").strip() != str(len(names)):
        raise ValueError(f"that is not the number of printers named ({len(names)}); always allow was not turned on")
    machines = {name: _machine_for_always(name) for name in names}
    seen: dict[str, str] = {}
    for name, machine in machines.items():
        if machine in seen:
            raise ValueError(
                f"{seen[machine]} and {name} are the same machine; name it once. Always allow was not turned on"
            )
        seen[machine] = name
    told = {_norm(n) for n in told_no_camera}
    return _write_always(machines, {name: _bed_check_for(name, _norm(name) in told) for name in names})


def open_window_from_dialog(
    answer: DialogAnswer, *, printer_name: str, source: str = SOURCE_ELICITED,
) -> Window:
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
    :class:`NotAPerson` when there is none.  *source* names the door the
    answer came through: the host's dialog, or the code typed after a
    banner (:data:`SOURCE_CODE`) — the same answer, the same window.
    """
    if source not in SOURCE_DOORS:
        raise ValueError(f"a window from an answer names a known door, not {source!r}")
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
            seconds=check_window_length(seconds), scope=_checked_scope(scope), source=source,
        )
        logger.info("standing consent window %s opened by %s (%s) for %s, until %s",
                    w.id, w.set_by, w.source, describe_scope(w.scope), time.ctime(w.until))
        return w
    return _open_window(seconds=seconds, scope=scope, source=source)


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
            if w.always:
                raise ValueError(
                    f"{window_id} is always allow for {describe_scope(w.scope)}: it has no end to move. "
                    f"Turn it off with `kiln consent revoke {window_id}`."
                )
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


def _closed(w: Window, now: float, reason: str = "") -> Window:
    """*w* as a closed record.  One already closed keeps its time and reason."""
    if w.revoked_at is not None:
        return w
    return dataclasses.replace(
        w, revoked_at=now, revoked_reason=reason,
        # A copy of the account's record: its closing is owed to the account
        # until the account has been told (:func:`_tell_the_account`).
        account_owed=bool(w.account_grant) and reason != REASON_OFF_ON_ACCOUNT,
    )


def revoke_window(window_id: str, *, reason: str = "") -> Window:
    """Close a window now.  Anyone may close one: revoking is the safe
    direction, and a revoke nobody can perform is a window nobody can stop.
    On the hosted server the account's store closes it.  *reason* is kept
    on the record when something other than a person closed it."""
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
            closed = _closed(w, now, reason)
            windows[i] = closed
            _write(windows)
            break
        else:
            raise KeyError(f"no window {window_id}")
    if closed.account_owed:
        closed = _tell_the_account(closed)
    return closed


def _tell_the_account(closed: Window) -> Window:
    """Close, on the account, the record a closed copy stood for — so
    turning always allow off here turns it off everywhere, and the account
    page says why.  When the account cannot be told (offline, signed out)
    the copy stays marked as owed and :func:`sync_account_always` tries
    again; closed here is closed either way, because a start under a copy
    is confirmed with the account every time."""
    try:
        from kiln import bridge_client

        told = bridge_client.revoke_on_account(
            closed.account_grant,
            reason=REASON_MACHINE_CHANGED if closed.revoked_reason == REASON_MACHINE_CHANGED else _TOLD_TURNED_OFF_HERE,
        )
    except Exception:  # noqa: BLE001 — not told is owed, not lost
        told = False
    if not told:
        return closed
    settled = dataclasses.replace(closed, account_owed=False)
    with _lock:
        windows = _read()
        for i, w in enumerate(windows):
            if w.id == settled.id:
                windows[i] = settled
                _write(windows)
                break
    return settled


def mirror_account_always(*, grant_id: str, printer_name: str, set_by: str, set_at: float | None) -> Window | None:
    """The local copy of an always-allow record the signed-in account
    holds for this machine — made the first time the account answers with
    one, returned as it stands after that.

    Always allow turned on from the account page is the account's record;
    what it needs from this computer is what the terminal door's entry
    has: the machine found under the printer's name when it was first
    seen here, so a different one can be noticed, and an entry the status
    surfaces and the revoke paths already read.  The copy is that entry.
    It is never a yes by itself: a start under it is confirmed with the
    account each time (:func:`covering` leaves copies out for a start; the
    asker reads the account).  ``None`` when Kiln cannot tell which
    machine the name is, on the hosted server, or for no grant.  A copy
    already closed here comes back closed, and the account is told again.
    """
    grant = str(grant_id or "").strip()
    name = str(printer_name or "").strip()
    if not grant or not name or _hosted():
        return None
    with _lock:
        existing = next((w for w in _read() if w.account_grant == grant), None)
    if existing is not None:
        if existing.revoked_at is not None and existing.account_owed:
            return _tell_the_account(existing)
        return existing
    machine = machine_under(name)
    if not machine:
        return None
    entry = Window(
        id=f"w_{secrets.token_hex(6)}", set_by=str(set_by or "account"), set_at=float(set_at or _now()),
        until=None, scope=(name,), source=SOURCE_WEB, always=True, machine=machine, account_grant=grant,
        # What the copy says about the bed is left undecided here: a copy is
        # also made by a status read, which must not go asking about cameras.
        # It is decided at the copy's first start (:func:`settle_copy_bed_check`);
        # until then an undecided copy is one that looks, and asks when it
        # cannot see.
    )
    with _lock:
        windows = _read()
        windows.append(entry)
        _write(windows)
    logger.info("always allow %s for %s: copy of the account's record %s", entry.id, name, grant)
    _audit(
        "always_allow_turned_on",
        {"window_id": entry.id, "printer": name, "machine": machine, "by": entry.set_by,
         "at": entry.set_at, "source": entry.source, "account_grant": grant},
    )
    return entry


def settle_copy_bed_check(window_id: str, printer_name: str | None) -> Window | None:
    """Decide, once, what an account copy says about the bed -- at a start,
    where Kiln may look and may ask its catalogue.

    The account page told the person that without a camera Kiln cannot
    check the bed.  Here is where it is known which this printer is: no
    camera when its connection cannot carry a picture and none is
    registered beside it, or when Kiln's catalogue says the model has none
    and no picture comes back (:func:`bed_camera`).  Where Kiln is unsure
    there is nobody at this door to ask, so the copy is one that looks,
    and asks when it cannot see.  A copy already decided, a terminal
    entry, and a closed copy come back unchanged.  Never raises.
    """
    w = get_window(window_id)
    if w is None or not w.account_grant or w.bed_check or w.revoked_at is not None or _hosted():
        return w
    try:
        name = str(printer_name or (w.scope[0] if w.scope else ""))
        decided = BED_NO_CAMERA if bed_camera(name) == CAMERA_NO else BED_CAMERA
        settled = dataclasses.replace(w, bed_check=decided)
        with _lock:
            windows = _read()
            for i, each in enumerate(windows):
                if each.id == w.id and each.revoked_at is None and not each.bed_check:
                    windows[i] = settled
                    _write(windows)
                    return settled
    except (OSError, KeyError):
        logger.debug("account copy's bed check not settled", exc_info=True)
    return get_window(window_id)


def account_confirms(w: Window, printer_name: str | None) -> bool:
    """Whether the account still holds the record a copy stands for, asked
    now.  An account that cannot be reached has not confirmed.  Never
    raises."""
    if not w.account_grant:
        return False
    try:
        from kiln import bridge_client

        answer = bridge_client.read_always_allow(str(printer_name or (w.scope[0] if w.scope else "")))
    except Exception:  # noqa: BLE001 — unreachable is unconfirmed
        return False
    return bool(
        answer is not None and answer.allowed
        and answer.kind == bridge_client.KIND_MACHINE_ALWAYS and answer.id == w.account_grant
    )


def sync_account_always() -> None:
    """Bring the copies in line with the account, when it can be reached:
    make one for each always-allow record the account holds for this
    machine, close each copy the account no longer holds, and tell the
    account about any closing it is still owed.  What ``kiln consent
    status`` and the status tool call before they list, so a permission
    turned on or off on the account page shows here without waiting for a
    print.  With the account unreachable or signed out, nothing changes.
    Never raises."""
    if _hosted():
        return
    try:
        from kiln import bridge_client

        grants = bridge_client.always_allow_grants()
    except Exception:  # noqa: BLE001 — an account that cannot be read changes nothing
        grants = None
    if grants is None:
        return
    held = {g["id"]: g for g in grants}
    for w in all_windows():
        if not w.account_grant:
            continue
        if w.revoked_at is None and w.account_grant not in held:
            with contextlib.suppress(Exception):
                revoke_window(w.id, reason=REASON_OFF_ON_ACCOUNT)
        elif w.revoked_at is not None and w.account_owed:
            _tell_the_account(w)
    for grant in grants:
        grantor = grant["grantor"]
        mirror_account_always(
            grant_id=grant["id"], printer_name=grant["printer"],
            set_by=grantor if grantor.startswith("account:") or not grantor else f"account:{grantor}",
            set_at=grant["issued_at"],
        )


def revoke_all() -> list[Window]:
    closed: list[Window] = []
    for w in live_windows():
        closed.append(revoke_window(w.id))
    return closed


def revoke_covering(printer_name: str | None) -> list[Window]:
    """Close every live window that covers *printer_name* — a fleet window
    included, since it covers this printer too, and always allow for the
    machine this name reaches, whichever of its names it was turned on
    under.  The list closed, possibly empty.  Safe direction, so no
    guard."""
    closed: list[Window] = []
    machine = ""
    for w in live_windows():
        covers = w.covers(printer_name)
        if not covers and w.always:
            machine = machine or machine_under(printer_name)
            covers = bool(machine) and machine == w.machine
        if covers:
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


def is_live(window_id: str, now: float | None = None, *, printer_name: str | None = None) -> bool:
    """Whether a start may still rest on this window — the scheduler's
    question about a job queued under one.  For always allow that is the
    gate's question again: the machine, for the printer the job is about
    to be sent to (*printer_name*; the entry's own printer when not
    given)."""
    w = get_window(window_id)
    if w is None or not w.live(now):
        return False
    if w.always:
        return _always_stands(w, printer_name or w.scope[0])
    return True


def _always_stands(w: Window, printer_name: str | None) -> bool:
    """Whether always allow covers a start aimed at *printer_name*, now.

    Two machines are asked for, through the resolver a start uses: the one
    under the name the entry was turned on for, and the one the start is
    aimed at.  Both have to be the machine on the entry.  A different
    machine under the entry's name closes the entry, with the reason on
    record; a machine Kiln cannot identify is covered by nothing, and the
    person is asked the ordinary way.
    """
    if not w.account_grant and not always_allow_is_yours():
        # Pro, asked at every start: an entry from a terminal stops starting
        # prints unasked when the account leaves Pro.  Left on, not closed,
        # so it stands again if the account comes back.  The account's own
        # record is asked of the account, which draws the same line.
        return False
    return _covers_its_machine(w, printer_name)


def _covers_its_machine(w: Window, printer_name: str | None) -> bool:
    """:func:`_always_stands` past the plan: the machine half."""
    name = w.scope[0] if isinstance(w.scope, tuple) and w.scope else ""
    under_name = machine_under(name)
    if under_name and under_name != w.machine:
        _turn_off(w, under_name)
        return False
    if not under_name:
        return False
    aimed = under_name if _norm(printer_name) == _norm(name) else machine_under(printer_name)
    return aimed == w.machine


def _turn_off(w: Window, found_machine: str) -> None:
    """Close an always-allow entry whose name now reaches another machine."""
    try:
        revoke_window(w.id, reason=REASON_MACHINE_CHANGED)
    except KeyError:
        return  # gone since it was read: nothing left to close
    except OSError:
        # Unwritable now: the next read finds the same machine and tries
        # again.  Named faults only, so a refusal to serve this store at
        # all is never taken for one.
        logger.warning("always allow %s could not be closed after its machine changed", w.id, exc_info=True)
        return
    logger.warning(
        "always allow %s for %s turned itself off: the name now reaches %s, not %s",
        w.id, describe_scope(w.scope), found_machine, w.machine,
    )
    _audit(
        "always_allow_turned_off",
        {"window_id": w.id, "printer": describe_scope(w.scope), "reason": REASON_MACHINE_CHANGED,
         "machine": w.machine, "found": found_machine, "turned_on_by": w.set_by, "turned_on_at": w.set_at},
    )


def always_for(printer_name: str | None) -> Window | None:
    """The always-allow entry covering a start aimed at *printer_name*, or
    ``None``.  Local only: the hosted server keeps none."""
    if _hosted():
        return None
    for w in live_windows():
        if w.always and _always_stands(w, printer_name):
            return w
    return None


def always_waiting_on_pro(printer_name: str | None) -> Window | None:
    """The always-allow entry from a terminal that would cover a start
    aimed at *printer_name* but for the plan: on, for this machine, and not
    starting prints because this computer is not signed in on Kiln Pro now
    (left Pro, or signed out).  What a start says first when it asks
    anyway.  ``None`` otherwise.  Local only."""
    if _hosted() or always_allow_is_yours():
        return None
    for w in live_windows():
        if w.always and not w.account_grant and _covers_its_machine(w, printer_name):
            return w
    return None


def standing_now(now: float | None = None) -> list[Window]:
    """Every window a start could rest on right now — what a status
    surface lists.  The live timed windows, and always allow after its
    name has been checked against its machine, which closes an entry
    whose name now reaches a different one: status must not show as on
    what the next print would find off."""
    standing: list[Window] = []
    for w in live_windows(now):
        if w.always and not w.account_grant and not _hosted() and not always_allow_is_yours():
            continue
        if w.always and not _hosted():
            under_name = machine_under(w.scope[0])
            if under_name and under_name != w.machine:
                _turn_off(w, under_name)
                continue
        standing.append(w)
    return standing


def turned_off_recently(now: float | None = None) -> list[Window]:
    """Always-allow entries Kiln closed itself within
    :data:`TURNED_OFF_SAID_FOR_SECONDS`, newest first — less any whose
    printer has had always allow turned on again since.  What the refusal
    and the status surfaces read to say why prints there ask again."""
    if _hosted():
        return []
    now = _now() if now is None else now
    entries = [w for w in all_windows() if w.always]
    recent: list[Window] = []
    for w in entries:
        if w.revoked_reason != REASON_MACHINE_CHANGED or w.revoked_at is None:
            continue
        if now - w.revoked_at > TURNED_OFF_SAID_FOR_SECONDS:
            continue
        if any(o.set_at > w.set_at and _norm(o.scope[0]) == _norm(w.scope[0]) for o in entries):
            continue
        recent.append(w)
    return sorted(recent, key=lambda w: w.revoked_at or 0.0, reverse=True)


def turned_itself_off(printer_name: str | None, now: float | None = None) -> Window | None:
    """The entry from :func:`turned_off_recently` for *printer_name* — by
    that name, or by another name for the machine now under it — or
    ``None``."""
    aimed = ""
    for w in turned_off_recently(now):
        if _norm(printer_name) == _norm(w.scope[0]):
            return w
        aimed = aimed or machine_under(printer_name)
        if aimed and aimed == machine_under(w.scope[0]):
            return w
    return None


def covering(
    printer_name: str | None, now: float | None = None, *, for_a_start: bool = False, timed_only: bool = False,
) -> Window | None:
    """The live window that covers *printer_name*, or ``None``.  On the
    hosted server the file is nobody's, so the account's store answers,
    or nothing does.

    *for_a_start* is the gate's question — may a print start on this
    window's word alone?  A copy of the account's always allow may not:
    the account is asked each time, and the asker does that.  Everything
    that only REPORTS a window (the line on a result, a status surface)
    leaves it False and sees the copies too.

    *timed_only* leaves always allow out: the window with an end that
    covers this printer, if there is one.  Asked when always allow cannot
    be used for a print (the camera showed nothing), because a window a
    person opened for a while is their yes either way."""
    if _hosted():
        store = window_store()
        if store is None:
            return None
        try:
            w = store.covering(printer_name)
        except Exception:  # noqa: BLE001 — a store that fails has no window
            logger.debug("hosted window store could not answer", exc_info=True)
            return None
        # The account's store keeps timed windows only; an entry with no
        # end is never taken from it.
        timed = isinstance(w, Window) and not w.always
        return w if timed and w.live(now) and w.covers(printer_name) else None
    live = live_windows(now)
    # Always allow first: where both cover a start, the standing fact with
    # no end is the one the result and the audit line should name.
    for w in () if timed_only else live:
        if w.always and not (for_a_start and w.account_grant) and _always_stands(w, printer_name):
            return w
    for w in live:
        if not w.always and w.covers(printer_name):
            return w
    return None


def describe(w: Window, now: float | None = None) -> dict[str, Any]:
    """One window as a person reads it — the shape ``kiln consent status``,
    the status tool and the line on a print result all share, so the
    same window is described the same way at every door."""
    now = _now() if now is None else now
    facts: dict[str, Any] = {
        "id": w.id,
        "scope": describe_scope(w.scope),
        "set_by": w.set_by,
        "opened_via": _OPENED_VIA.get(w.source, "terminal"),
        "set_at": time.strftime("%Y-%m-%d %H:%M", time.localtime(w.set_at)),
        "always": w.always,
        "live": w.live(now),
        "revoked": w.revoked_at is not None,
        "extensions": len(w.extensions),
    }
    if w.always or w.until is None:
        # No end: the three time fields are present and empty, so a reader
        # that formats them has to notice rather than print a wrong date.
        facts.update({"until": None, "until_clock": None, "remaining_minutes": None})
    else:
        facts.update({
            "until": time.strftime("%Y-%m-%d %H:%M", time.localtime(w.until)),
            "until_clock": time.strftime("%H:%M", time.localtime(w.until)),
            "remaining_minutes": max(0, int((w.until - now) // 60)),
        })
    if w.revoked_reason:
        facts["revoked_reason"] = w.revoked_reason
    if w.always:
        facts["bed_check"] = w.bed_check or BED_CAMERA
    facts["summary"] = summary_line(facts)
    return facts


def summary_line(facts: dict[str, Any]) -> str:
    """One window in a few words, for every status line that lists them:
    ``garage until 2026-10-02 14:00`` or ``always allow on garage (since
    2026-10-02 09:14)``.  Takes :func:`describe`'s facts, so a surface
    that lists windows cannot format an entry with no end as a date."""
    if facts.get("always"):
        return f"always allow on {facts['scope']} (since {facts['set_at']})"
    return f"{facts['scope']} until {facts['until']}"


def _reset_for_tests() -> None:
    """Nothing is cached in memory but the hosted store hook; here so
    fixtures read the same way as the sibling ledgers'."""
    register_window_store(None)
