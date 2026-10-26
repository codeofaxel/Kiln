"""A printer's condition, as the person says it: needs repair, or repaired.

Kiln judges a machine by its own prints.  A person who fixes a printer
tells Kiln here, and from that moment the machine is judged by what it does
next, not by what it did before: nothing is deleted, the old prints stay in
the record, they just stop counting against the machine.  A person taking
a printer out of service says so here too, and routing stops choosing it
until they say it is repaired.

Two states, one record, keyed by the machine's own identity (the same
fingerprint its print rows carry, :func:`kiln.registry.stable_machine_fingerprint`)::

    ok --[needed]--> needs_repair --[done]--> ok (older prints stop counting)

Every door -- the ``set_printer_repair`` tool, ``kiln machine repair``, the
routing candidates, the start-print warning -- reads and writes through the
two functions here, :func:`mark` and :func:`condition`.  Kiln never sets a
state itself: the record is the person's own claim (``user_reported``),
about their own printer, and the worst it can do is mislead their own
routing.

A ``done`` may be backdated up to :data:`BACKDATE_LIMIT_S` (the person fixed
it last week and forgot); nothing may be dated in the future.  ``clear``
retracts the latest mark still standing, as a row of its own.

When the person is signed in, a mark made here follows the printer to their
account and to every other computer of theirs
(:mod:`kiln.machine_repair_courier`), and marks made elsewhere -- another
computer, the web -- come down into this record.  The account tells two
computers' records of one machine apart by a shared code
(:func:`shared_code`, a printer whose own secret both computers hold) or by
asking the person once; a question is answered with
:func:`answer_identity`.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import threading
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

#: What a person may say about a printer.
STATUSES = ("needed", "done", "clear")
#: What was repaired.  A label for the person; Kiln never reasons from it.
KINDS = ("nozzle", "hotend", "extruder", "bed", "belts", "other")
#: How far back a repair may be dated.
BACKDATE_LIMIT_S = 30 * 86400
#: The longest note kept.
NOTE_MAX = 500

#: Refusal codes, so a door can branch and a person can read.
CODE_BAD_STATUS = "REPAIR_STATUS_UNKNOWN"
CODE_BAD_KIND = "REPAIR_KIND_UNKNOWN"
CODE_KIND_REQUIRED = "REPAIR_KIND_REQUIRED"
CODE_BAD_WHEN = "REPAIR_TIME_INVALID"
CODE_BAD_NOTE = "REPAIR_NOTE_INVALID"
CODE_NO_IDENTITY = "PRINTER_IDENTITY_UNKNOWN"
CODE_NOTHING_TO_CLEAR = "REPAIR_NOTHING_TO_CLEAR"
CODE_LOCAL_ONLY = "LOCAL_ONLY"
CODE_NOT_FOUND = "NOT_FOUND"


class RepairRefused(ValueError):
    """A mark Kiln will not write, with the sentence that says why."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat(timespec="seconds")


def day_word(ts: float | None) -> str:
    """``Oct 20`` for a sentence; the year too when it is not this year."""
    if ts is None:
        return "an unknown date"
    when = datetime.fromtimestamp(float(ts), tz=timezone.utc)
    if when.year != datetime.now(tz=timezone.utc).year:
        return f"{when.strftime('%b')} {when.day}, {when.year}"
    return f"{when.strftime('%b')} {when.day}"


def _parse_when(when: Any, status: str, now: float) -> float:
    """The moment a mark is for: now by default; a ``done`` may be dated
    back to :data:`BACKDATE_LIMIT_S` ago; nothing is dated ahead."""
    if when is None or (isinstance(when, str) and not when.strip()):
        return now
    if isinstance(when, bool) or not isinstance(when, (int, float, str)):
        raise RepairRefused(CODE_BAD_WHEN, "when must be a date and time (ISO 8601, like 2026-10-20T14:00), or left out for now.")
    if isinstance(when, (int, float)):
        ts = float(when)
    else:
        text = when.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            try:
                ts = float(text)
            except ValueError:
                raise RepairRefused(
                    CODE_BAD_WHEN, f"when={when!r} is not a date Kiln can read; write it as ISO 8601, like 2026-10-20T14:00.",
                ) from None
        else:
            if parsed.tzinfo is None:
                parsed = parsed.astimezone()  # the person's own clock
            ts = parsed.timestamp()
    if ts > now + 60:
        raise RepairRefused(CODE_BAD_WHEN, "when is in the future; a repair is recorded when it has happened, not before.")
    if status == "done" and ts < now - BACKDATE_LIMIT_S:
        raise RepairRefused(
            CODE_BAD_WHEN,
            f"when is more than {BACKDATE_LIMIT_S // 86400} days ago; a repair can be dated at most that far back. "
            "Prints it has run since then already stand for the repaired machine.",
        )
    if status != "done" and ts < now - BACKDATE_LIMIT_S:
        raise RepairRefused(CODE_BAD_WHEN, f"when is more than {BACKDATE_LIMIT_S // 86400} days ago; mark it as of today.")
    return ts


def _resolve(adapter_or_name: Any) -> tuple[Any, str]:
    """``(adapter, name)`` for a registered printer, by adapter or by name."""
    if isinstance(adapter_or_name, str):
        name = adapter_or_name.strip()
        if not name:
            raise RepairRefused(CODE_NOT_FOUND, "printer_name is required.")
        import kiln.server as _srv

        try:
            adapter = _srv._get_registry().get(name)
        except Exception:  # noqa: BLE001 -- the registry's own error names the name; this says it plainly
            raise RepairRefused(CODE_NOT_FOUND, f"No printer named {name!r} is set up here.") from None
        return adapter, name
    adapter = adapter_or_name
    name = getattr(adapter, "_kiln_registered_name", None) or getattr(adapter, "name", None) or "this printer"
    return adapter, str(name)


def _fingerprint(adapter: Any, name: str) -> str:
    from kiln.registry import stable_machine_fingerprint

    fp = stable_machine_fingerprint(adapter)
    if not fp:
        raise RepairRefused(
            CODE_NO_IDENTITY,
            f"Kiln cannot tell {name} apart from another machine (it has no serial number or address on "
            "record), so it cannot keep a repair record for it. Set the printer up with its address or "
            "serial and try again.",
        )
    return fp


def _db(db: Any):
    if db is not None:
        return db
    from kiln.persistence import get_db

    return get_db()


def mark(
    adapter_or_name: Any,
    status: str,
    *,
    kind: str | None = None,
    note: str | None = None,
    when: Any = None,
    db: Any = None,
) -> dict[str, Any]:
    """Write one mark on a printer's repair record and say what it changed.

    *status* is ``needed`` (the printer leaves routing until repaired; a
    start on it is still allowed, with a warning), ``done`` (the repair
    happened at *when*; prints before it stop counting against the
    machine; *kind* required) or ``clear`` (retract the latest mark still
    standing).  Refuses, by name, a status or kind outside the lists, a
    ``done`` with no kind, a time in the future or too far back, a machine
    with no stable identity, a ``clear`` with nothing standing, and the
    hosted server, where this record is not kept.

    Returns ``{"recorded": True, "printer", "status", "kind", "marked_at",
    "condition", "next"}`` plus ``reset`` (``prints_before``,
    ``versions_affected``) for a ``done`` and ``cleared`` for a ``clear``.
    """
    from kiln.runtime_env import is_hosted_multitenant

    if is_hosted_multitenant():
        raise RepairRefused(
            CODE_LOCAL_ONLY,
            "A printer's repair record is kept on the computer your printer is connected to, so this runs "
            "there, not on Kiln's servers (https://kiln3d.com/install). On the web, the fleet view's buttons "
            "mark a printer on your account, and a signed-in computer picks the mark up.",
        )
    status_word = str(status or "").strip().lower()
    if status_word not in STATUSES:
        raise RepairRefused(CODE_BAD_STATUS, f"status must be one of {', '.join(STATUSES)}; got {status!r}.")
    kind_word = str(kind).strip().lower() if kind is not None else None
    if kind_word == "":
        kind_word = None
    if kind_word is not None and kind_word not in KINDS:
        raise RepairRefused(CODE_BAD_KIND, f"kind must be one of {', '.join(KINDS)}; got {kind!r}.")
    if status_word == "done" and kind_word is None:
        raise RepairRefused(CODE_KIND_REQUIRED, f"A repair says what was done: kind is one of {', '.join(KINDS)}.")
    if note is not None and not isinstance(note, str):
        raise RepairRefused(CODE_BAD_NOTE, "note must be text.")
    note_text = (note or "").strip()[:NOTE_MAX] or None

    adapter, name = _resolve(adapter_or_name)
    fingerprint = _fingerprint(adapter, name)
    store = _db(db)
    import time as _time

    now = _time.time()
    before = store.printer_condition(fingerprint)
    out: dict[str, Any] = {"recorded": True, "printer": name, "status": status_word}

    if status_word == "clear":
        standing = store.standing_repair_marks(before["marks"])
        if not standing:
            raise RepairRefused(CODE_NOTHING_TO_CLEAR, f"{name} has no repair mark to clear.")
        last = standing[-1]
        store.add_printer_repair(fingerprint, name, "clear", marked_at=now)
        out["cleared"] = {"status": last["status"], "kind": last["kind"], "marked_at": _iso(last["marked_at"])}
        out["marked_at"] = _iso(now)
    else:
        ts = _parse_when(when, status_word, now)
        store.add_printer_repair(fingerprint, name, status_word, kind=kind_word, note=note_text, marked_at=ts)
        out["kind"] = kind_word
        out["marked_at"] = _iso(ts)
        if status_word == "done":
            out["reset"] = store.prints_before_repair(fingerprint, ts)

    after = store.printer_condition(fingerprint)
    out["condition"] = condition_summary(after)
    out["next"] = _next_sentence(name, status_word, out)
    try:
        from kiln.machine_repair_courier import push_soon

        push_soon()
    except Exception:  # noqa: BLE001 -- the record is written; the account is told when it can be
        logger.debug("printer repair courier not started", exc_info=True)
    return out


#: The shared-code recipe's version word, part of its salt.
SHARED_CODE_SALT = "kiln-printer-code-v1"
#: PBKDF2 rounds for the shared code: slow enough that a code never helps
#: anyone guess an access code, cached so each printer pays it once.
SHARED_CODE_ROUNDS = 200_000
_shared_codes: dict[str, str] = {}
_shared_lock = threading.Lock()


def shared_code(adapter: Any, *, account_id: str | None = None) -> str | None:
    """One code for one physical printer, the same on every computer of the
    signed-in person's that holds the printer's own secret, and nothing the
    account could turn back into a serial or an access code.

    Today that is a printer set up with a serial and a LAN access code (a
    Bambu printer): ``"pc:"`` and 24 hex digits of
    ``HMAC(PBKDF2-SHA256(access_code, salt), "machine")`` where the salt
    is ``kiln-printer-code-v1|<account id>|<serial>``.  The account id is in
    the salt so two accounts that share a printer never produce the same
    code; the access code is the password, so a changed code is a new
    printer to the account (the person is then asked once, and links the
    two).  ``None`` for a printer with no such secret, or when nobody is
    signed in -- never a code made from something weaker.
    """
    serial = str(getattr(adapter, "serial", None) or getattr(adapter, "_serial", None) or "").strip().lower()
    access = str(getattr(adapter, "access_code", None) or getattr(adapter, "_access_code", None) or "").strip()
    if not serial or not access:
        return None
    if account_id is None:
        from kiln.auth_session import signed_in_account_id

        account_id = signed_in_account_id()
    account = str(account_id or "").strip()
    if not account:
        return None
    cache_key = hashlib.sha256(f"{account}|{serial}|{access}".encode("utf-8")).hexdigest()
    with _shared_lock:
        cached = _shared_codes.get(cache_key)
    if cached:
        return cached
    salt = f"{SHARED_CODE_SALT}|{account}|{serial}".encode("utf-8")
    key = hashlib.pbkdf2_hmac("sha256", access.encode("utf-8"), salt, SHARED_CODE_ROUNDS)
    code = "pc:" + hmac.new(key, b"machine", hashlib.sha256).hexdigest()[:24]
    with _shared_lock:
        _shared_codes[cache_key] = code
    return code


def answer_identity(adapter_or_name: Any, question_id: str, *, same: bool, db: Any = None) -> dict[str, Any]:
    """Answer the account's question whether a printer here is the one
    marked from another computer: *same* links the two records on the
    account (the mark made there now stands here too), a no is remembered
    so the pair is never asked again.  Refuses the hosted server like
    :func:`mark`, and a printer Kiln cannot tell apart."""
    from kiln.machine_repair_courier import answer_question
    from kiln.runtime_env import is_hosted_multitenant

    if is_hosted_multitenant():
        raise RepairRefused(
            CODE_LOCAL_ONLY,
            "Which computer's printer this is can only be answered from that computer, or on the web "
            "(https://kiln3d.com/install).",
        )
    adapter, name = _resolve(adapter_or_name)
    _fingerprint(adapter, name)
    answer = answer_question(question_id, same=same, db=db)
    if answer.get("success") is False or answer.get("status") == "error":
        raise RepairRefused(str(answer.get("code") or "ACCOUNT_NOT_ANSWERED"), str(answer.get("error") or ""))
    out = {"recorded": True, "printer": name, "question_id": answer.get("question_id"),
           "answer": answer.get("answer"), "linked": answer.get("linked"), "account": answer.get("account")}
    out["condition"] = condition(adapter, db=db)
    out["next"] = (
        f"{name} and the printer marked from the other computer are one record now; its marks stand here too."
        if same else f"{name} keeps its own record; you will not be asked about that pair again."
    )
    return out


def _next_sentence(name: str, status: str, out: dict[str, Any]) -> str:
    if status == "needed":
        return f"{name} is out of routing until you mark it repaired; a print you start on it yourself still runs, with a warning."
    if status == "done":
        reset = out.get("reset") or {}
        n = int(reset.get("prints_before") or 0)
        set_aside = (f"{n} earlier print{'s' if n != 1 else ''} no longer count{'' if n != 1 else 's'} against it; ") if n else ""
        return f"{set_aside}its next prints decide its standing."
    state = out["condition"]["state"]
    if state == "needs_repair":
        return f"{name} is marked needs repair again, as it was before the cleared mark."
    return f"{name} is judged by its whole record again."


def condition(adapter_or_name: Any, *, db: Any = None) -> dict[str, Any]:
    """A printer's condition for a reader: ``{"state": "ok" | "needs_repair"
    | "repaired", "since", "repaired_at", "kind"}`` with ISO times, or
    ``None`` fields when nothing is on record.  Never raises: a machine with
    no stable identity, or a store that cannot be read, reads as ``ok`` with
    ``known: False``, so a blank never becomes a verdict."""
    try:
        adapter, name = _resolve(adapter_or_name)
        from kiln.registry import stable_machine_fingerprint

        fp = stable_machine_fingerprint(adapter)
        if not fp:
            return {"state": "ok", "since": None, "repaired_at": None, "kind": None, "known": False}
        return condition_summary(_db(db).printer_condition(fp))
    except Exception:  # noqa: BLE001 -- a condition that cannot be read is unknown, never a mark
        logger.debug("printer condition not read", exc_info=True)
        return {"state": "ok", "since": None, "repaired_at": None, "kind": None, "known": False}


def condition_summary(raw: dict[str, Any]) -> dict[str, Any]:
    """The store's condition with its times as ISO strings and no marks."""
    return {
        "state": raw["state"],
        "since": _iso(raw.get("since")),
        "repaired_at": _iso(raw.get("repaired_at")),
        "kind": raw.get("kind"),
        "known": True,
    }


def routing_block(summary: dict[str, Any]) -> dict[str, Any] | None:
    """The ``repair`` block a routing candidate carries, from
    :func:`condition_summary`: ``{"status": "needed", "since"}`` for a machine
    out of service, ``{"status": "done", "at", "kind"}`` for a repaired one,
    ``None`` for a machine with nothing on record.  Carries no name and no
    note: the note may say where a machine stands or what it is called."""
    if summary.get("state") == "needs_repair":
        return {"status": "needed", "since": summary.get("since")}
    if summary.get("state") == "repaired" and summary.get("repaired_at"):
        return {"status": "done", "at": summary["repaired_at"], "kind": summary.get("kind") or "other"}
    return None


def start_warning(adapter: Any, *, db: Any = None) -> str | None:
    """One sentence for a start on a printer marked needs repair; ``None``
    otherwise.  A warning, never a refusal: the person's hands, the person's
    call."""
    summary = condition(adapter, db=db)
    if summary.get("state") != "needs_repair":
        return None
    name = getattr(adapter, "_kiln_registered_name", None) or getattr(adapter, "name", None) or "This printer"
    since = summary.get("since")
    try:
        since_ts = datetime.fromisoformat(since).timestamp() if since else None
    except ValueError:
        since_ts = None
    return (
        f"{name} is marked needs repair since {day_word(since_ts)}; it was started as asked, and Kiln will "
        "judge it by how this print goes. Mark it repaired with set_printer_repair once it is fixed."
    )


__all__ = [
    "BACKDATE_LIMIT_S",
    "KINDS",
    "STATUSES",
    "RepairRefused",
    "answer_identity",
    "condition",
    "condition_summary",
    "day_word",
    "mark",
    "routing_block",
    "shared_code",
    "start_warning",
]
