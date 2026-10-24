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
ending, who settled it, a failure-vocabulary word and the start and end times.
No printer name, serial, address, file name, settings, notes or readings.

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
    for key, src in (("started_at", "created_at"), ("ended_at", "ended_at")):
        when = row.get(src)
        if isinstance(when, (int, float)) and not isinstance(when, bool) and when > 0:
            out[key] = float(when)
    return out


def _locked(design_ids: set[str]) -> set[str]:
    """The designs among *design_ids* that are locked to this computer."""
    if not design_ids:
        return set()
    try:
        from kiln.history_vault import HistoryVault
    except Exception:  # noqa: BLE001 -- no vault, nothing can be locked
        return set()
    vault = HistoryVault()
    try:
        return {d for d in design_ids if vault.is_locked(d)}
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
    locked = _locked({r["design_id"] for r in owed if r.get("design_id")})
    # A locked design's prints are settled here and stay here: marked as
    # taken so they are not offered again, and never sent.
    for row in owed:
        if row.get("design_id") in locked:
            db.mark_pushed_to_account(row["id"], db.account_push_state(row))
    sendable = [r for r in owed if r.get("design_id") not in locked]
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
        payload.append(account_row(row, sync_id))
        states.append((row["id"], db.account_push_state(row)))
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
