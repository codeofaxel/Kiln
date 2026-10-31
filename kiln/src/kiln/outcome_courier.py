"""Carries how each print of a kept version ended to the person's Kiln account.

Every print Kiln starts is one row on this computer (``print_outcomes`` in
``kiln.persistence``): which version, which machine, which nozzle, how it
ended and who said so.  When the person is signed in, a row of a kept version
is copied to their account once it settles, so the workshop's version row can
show the mark and they can answer "did it come out?" from a phone.  The
account keeps the same rule this computer does: a weaker source never changes
an ending a stronger one settled.

What goes up, and nothing else: the row's own account id (random, minted here
once; never the job id, which is often a file's name), the version, design and
branch ids, the machine as a one-way code (:func:`kiln.outbound_guard.machine_code`,
the same code for the same machine on this computer, meaningless anywhere
else), the printer's model word, the material word, the nozzle size, the
ending, who settled it, a failure-vocabulary word, the start and end times,
and a note the person or their agent wrote on how it came out, labelled with
who wrote it (:mod:`kiln.outcome_note`), sent only when it changed here (a
note deleted here goes up as a delete).  No printer name, serial, address,
file name, settings or readings; a system string in ``notes`` with no label
(a hook's "auto-recorded ...") stays here.

Not sent: a print of a file that is no kept design (no version to mark), a
print still ``pending``, a design locked to this computer, anything at all when
nobody is signed in, and anything on Kiln's own servers.  Switched off with
``KILN_OUTCOME_COURIER=0``; quiet under a test run or a sandbox unless
``KILN_OUTCOME_COURIER_IN_TESTS=1``.

:func:`push_soon` is called by ``KilnDB.save_print_outcome`` after every
settled row, so every door that records an ending is covered without knowing
this module exists.  It never raises and never waits: the push runs on one
background thread, a moment later, so a burst of endings goes up together.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

TOOL = "outcome_courier"
PUSH_PATH = "/api/cloud/outcomes/push"
SWITCH_ENV = "KILN_OUTCOME_COURIER"
TEST_OPT_IN_ENV = "KILN_OUTCOME_COURIER_IN_TESTS"
#: The most rows one push carries, matching what the server takes.
MAX_ROWS = 200
#: How long a burst of endings is gathered before it goes up (seconds).
_GATHER_S = 3.0
#: The longest one push waits on Kiln's servers (seconds).
_WAIT_S = 20.0

_SYNC_ID = re.compile(r"^[0-9a-f]{32}$")
_WORD = re.compile(r"^[a-z_]{1,40}$")

_lock = threading.Lock()
_timer: threading.Timer | None = None


def _enabled() -> bool:
    if os.environ.get(SWITCH_ENV, "").strip() == "0":
        return False
    try:
        from kiln.runtime_env import is_hosted_multitenant

        if is_hosted_multitenant():
            return False
    except Exception:  # noqa: BLE001 -- an unknown posture sends nothing
        return False
    try:
        from kiln.dev_home_guard import SANDBOX_ENV, _pytest_running

        if (_pytest_running() or os.environ.get(SANDBOX_ENV, "").strip()) and not os.environ.get(
            TEST_OPT_IN_ENV, ""
        ).strip():
            return False
    except Exception:  # noqa: BLE001
        return False
    return True


def push_soon() -> None:
    """Send the owed rows a moment from now, on one background thread."""
    global _timer
    if not _enabled():
        return
    with _lock:
        if _timer is not None and _timer.is_alive():
            return
        _timer = threading.Timer(_GATHER_S, _push_quietly)
        _timer.daemon = True
        _timer.start()


def _push_quietly() -> None:
    global _timer
    try:
        push_owed()
    except Exception:  # noqa: BLE001 -- the next ending tries again
        logger.debug("outcome courier push failed", exc_info=True)
    finally:
        with _lock:
            _timer = None


def account_row(row: dict[str, Any], sync_id: str) -> dict[str, Any]:
    """*row* as it goes to the account: the named fields, nothing else."""
    from kiln.outbound_guard import machine_code
    from kiln.outcome_note import NOTE_BY, clean_note

    out: dict[str, Any] = {
        "source_job_id": sync_id,
        "version_id": row.get("version_id"),
        "ending": row.get("outcome"),
    }
    for key in ("design_id", "branch_id", "printer_model", "determined_by"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            out[key] = value.strip()
    code = machine_code(row.get("machine_fingerprint")) if row.get("machine_fingerprint") else None
    if code:
        out["machine_code"] = code
    material = row.get("material_type")
    if isinstance(material, str) and material.strip():
        out["material"] = material.strip()
    nozzle = row.get("nozzle_diameter_mm")
    if isinstance(nozzle, (int, float)) and not isinstance(nozzle, bool) and nozzle > 0:
        out["nozzle_diameter_mm"] = float(nozzle)
    failure = row.get("failure_mode")
    if isinstance(failure, str) and _WORD.match(failure):
        out["failure_mode"] = failure
    note_by = row.get("note_by")
    if note_by in NOTE_BY:
        note = clean_note(row.get("notes"))
        if note:
            out["note"] = note
            out["note_by"] = note_by
    for key, src in (("started_at", "created_at"), ("ended_at", "ended_at")):
        when = row.get(src)
        if isinstance(when, (int, float)) and not isinstance(when, bool) and when > 0:
            out[key] = float(when)
    return out


def _note_part(state: str | None) -> str:
    """The note half of a pushed state (``outcome|by|note_by|text``); empty
    for a state with no labelled note."""
    parts = (state or "").split("|", 2)
    return parts[2] if len(parts) == 3 else ""


def note_change(row_out: dict[str, Any], pushed_state: str | None, state: str) -> dict[str, Any]:
    """*row_out* with its note as the account should take it: sent only when
    the note changed on this computer since the account last took the row,
    and a note deleted here sent as a delete.  An unchanged note is left out,
    so a later ending never sends this computer's stale copy over a note the
    person changed or deleted on the web."""
    before, now = _note_part(pushed_state), _note_part(state)
    out = dict(row_out)
    if before == now:
        out.pop("note", None)
        out.pop("note_by", None)
    elif not now:
        out.pop("note", None)
        out["note_clear"] = True
        out["note_by"] = "person"
    return out


def _kept_here(rows: list[dict[str, Any]]) -> set[int]:
    """The ids of *rows* whose design is locked to this computer.

    A row's design is every design it names: its own ``design_id``, and the
    designs this computer's records say its version and branch belong to, so a
    row that carries only a version id is judged by its design too.  Records
    that exist but cannot be read hold every row back, as
    :func:`kiln.history_vault.design_locked` does for a push: the courier waits
    rather than send a print of a design the person may have kept here.
    """
    import sqlite3

    from kiln.history_vault import HistoryVault, _kiln_home

    if not (_kiln_home() / "history_vault.db").is_file():
        return set()
    try:
        vault = HistoryVault()
    except sqlite3.Error:
        logger.warning("outcome courier: this computer's records could not be read; nothing was sent")
        return {r["id"] for r in rows}
    try:
        held: set[int] = set()
        for row in rows:
            designs = {row.get("design_id")}
            if row.get("version_id"):
                designs.add(vault.design_of_version(row["version_id"]))
            if row.get("branch_id"):
                designs.add(vault.design_of_branch(row["branch_id"]))
            if any(d and vault.is_locked(d) for d in designs):
                held.add(row["id"])
        return held
    except sqlite3.Error:
        logger.warning("outcome courier: this computer's records could not be read; nothing was sent")
        return {r["id"] for r in rows}
    finally:
        vault.close()


def push_owed(*, db: Any = None, limit: int = MAX_ROWS) -> dict[str, Any]:
    """Send every settled row the account has not taken yet.  Returns what
    happened, in words a caller can show; never raises for the network."""
    from kiln.auth_session import resolve_api_bearer

    if not resolve_api_bearer().token:
        return {"sent": 0, "why": "signed_out"}
    if db is None:
        from kiln.persistence import get_db

        db = get_db()
    owed = db.outcomes_owed_to_account(limit=limit)
    if not owed:
        return {"sent": 0, "why": "nothing_owed"}
    # A locked design's prints stay here.  Not marked as sent: unlocking the
    # design later lets them go up, and a vault that could not be read now
    # may be readable on the next ending.
    held = _kept_here(owed)
    sendable = [r for r in owed if r["id"] not in held]
    if not sendable:
        return {"sent": 0, "why": "locked_to_this_computer", "kept_here": len(owed)}

    from kiln.history_courier import _request
    from kiln.outbound_guard import scrub

    payload: list[dict[str, Any]] = []
    states: list[tuple[int, str]] = []
    for row in sendable:
        sync_id = db.claim_account_sync_id(row["id"])
        if not _SYNC_ID.match(sync_id):
            continue
        state = db.account_push_state(row)
        payload.append(note_change(account_row(row, sync_id), row.get("account_pushed_state"), state))
        states.append((row["id"], state))
    answer = _request(TOOL, "POST", PUSH_PATH, {"rows": scrub(payload)}, timeout=_WAIT_S)
    if answer.get("success") is False or answer.get("status") == "error":
        return {"sent": 0, "why": "not_taken", "code": answer.get("code"), "owed": len(payload)}
    # Taken, held (a surer answer is already there) and refused (a row the
    # account cannot keep) all mean "the account has seen this state".
    for row_id, state in states:
        db.mark_pushed_to_account(row_id, state)
    return {
        "sent": len(payload),
        "kept": len(answer.get("kept") or []),
        "held": len(answer.get("held") or []),
        "refused": int(answer.get("refused") or 0),
        "at": time.time(),
    }
