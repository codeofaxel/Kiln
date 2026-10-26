"""Carries a printer's repair marks to the person's Kiln account, and back.

A mark on a printer's repair record (:mod:`kiln.printer_repairs`) is made on
the computer the printer is connected to.  A person with two computers, or
one who marks a printer from the web, wants that mark to follow the printer
everywhere: when the person is signed in, this courier sends every mark made
here up to their account and brings down the marks made elsewhere for the
same printers, so routing, the start warning and ``set_printer_repair``'s
``condition`` read one record wherever the mark was made.

What goes up, and nothing else: for each mark, its own account id (random,
minted here once), the machine as a one-way code
(:func:`kiln.outbound_guard.machine_code`: the same code for the same machine
on this computer, meaningless anywhere else), the status, the kind, and when
it was made -- never the note (a note can name a serial or a place) and never
the printer's name.  Beside the marks, one announcement per printer this
computer has set up: its code, its model word, and, for a printer whose own
secret both computers already hold, a shared code
(:func:`kiln.printer_repairs.shared_code`) that lets the account tell two
computers are looking at one machine without either serial leaving.

What comes down: the account's marks for this computer's printers, resolved
through the account's links, absorbed into this computer's record under the
ids they already carry (so a mark is never taken twice, and this computer's
own marks come back as themselves); and the questions the account cannot
answer alone -- a printer marked from another computer that may be one of
this computer's unlinked printers of the same model.  Those are answered
through :func:`answer_question` (``set_printer_repair(same_as=...)`` /
``different_from=...``), or on the web, or in the host's own dialog.

Not sent: anything at all when nobody is signed in, and anything on Kiln's
own servers.  Switched off with ``KILN_REPAIR_COURIER=0``; quiet under a
test run or a sandbox unless ``KILN_REPAIR_COURIER_IN_TESTS=1``.
:func:`push_soon` is called by :func:`kiln.printer_repairs.mark` after every
mark, so every door that writes one is covered without knowing this module
exists; it never raises and never waits.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

TOOL = "machine_repair_courier"
SYNC_PATH = "/api/cloud/machines/repairs/sync"
ANSWER_PATH = "/api/cloud/machines/links/answer"
SWITCH_ENV = "KILN_REPAIR_COURIER"
TEST_OPT_IN_ENV = "KILN_REPAIR_COURIER_IN_TESTS"
#: The most marks one sync carries, matching what the server takes.
MAX_ROWS = 200
#: How long a burst of marks is gathered before it goes up (seconds).
_GATHER_S = 3.0
#: The longest one sync waits on Kiln's servers (seconds).
_WAIT_S = 15.0
#: How long a tool reply waits for the account before answering without it.
_ACCOUNT_REPLY_WAIT_S = 8.0

_SYNC_ID = re.compile(r"^[0-9a-f]{32}$")
_QUESTION_ID = re.compile(r"^q:[0-9a-f]{16,32}$")
_WORD = re.compile(r"^[a-z_]{1,40}$")

_lock = threading.Lock()
_timer: threading.Timer | None = None
#: The open questions the account last handed this computer, by the
#: machine code of the printer here they are about.  Read by the tool and
#: the host dialog; filled only by :func:`sync`.
_questions: dict[str, list[dict[str, Any]]] = {}


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
    """Send the owed marks a moment from now, on one background thread."""
    global _timer
    if not _enabled():
        return
    with _lock:
        if _timer is not None and _timer.is_alive():
            return
        _timer = threading.Timer(_GATHER_S, _sync_quietly)
        _timer.daemon = True
        _timer.start()


def _sync_quietly() -> None:
    global _timer
    try:
        sync()
    except Exception:  # noqa: BLE001 -- the next mark tries again
        logger.debug("printer repair courier sync failed", exc_info=True)
    finally:
        with _lock:
            _timer = None


def _cancel_pending() -> None:
    global _timer
    with _lock:
        if _timer is not None:
            _timer.cancel()
            _timer = None


def account_mark(row: dict[str, Any], sync_id: str) -> dict[str, Any]:
    """*row* as it goes to the account: the named fields, nothing else."""
    from kiln.outbound_guard import machine_code

    out: dict[str, Any] = {
        "source_id": sync_id,
        "status": row.get("status"),
        "marked_at": float(row.get("marked_at") or 0.0),
        "created_at": float(row.get("created_at") or 0.0),
    }
    code = machine_code(row.get("machine_fingerprint")) if row.get("machine_fingerprint") else None
    if code:
        out["machine_code"] = code
    kind = row.get("kind")
    if isinstance(kind, str) and _WORD.match(kind):
        out["kind"] = kind
    return out


def _model_of(adapter: Any) -> str | None:
    try:
        from kiln.community_autofire import resolve_adapter_model

        model = resolve_adapter_model(adapter)
    except Exception:  # noqa: BLE001 -- the model word is a label; the code is the identity
        model = getattr(adapter, "printer_model", None) or getattr(adapter, "_printer_model", None)
    text = " ".join(str(model or "").split())
    return text[:80] or None


def announcements(registry: Any = None) -> list[dict[str, Any]]:
    """One announcement per printer set up here that Kiln can tell apart:
    ``{"fingerprint", "name", "machine_code", "model", "shared_code"}``.
    The fingerprint and name never leave; they key what comes back."""
    from kiln.outbound_guard import machine_code
    from kiln.printer_repairs import shared_code
    from kiln.registry import stable_machine_fingerprint

    if registry is None:
        import kiln.server as _srv

        registry = _srv._get_registry()
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for name, adapter in registry.list_all().items():
        fp = stable_machine_fingerprint(adapter)
        if not fp or fp in seen:
            continue
        seen.add(fp)
        code = machine_code(fp)
        if not code:
            continue
        entry: dict[str, Any] = {"fingerprint": fp, "name": str(name), "machine_code": code}
        model = _model_of(adapter)
        if model:
            entry["model"] = model
        shared = shared_code(adapter)
        if shared:
            entry["shared_code"] = shared
        out.append(entry)
    return out


def _absorb(db: Any, printers: dict[str, Any], by_code: dict[str, dict[str, Any]]) -> int:
    """Keep the account's marks for this computer's printers here.  Returns
    how many were new."""
    taken = 0
    for code, block in (printers or {}).items():
        here = by_code.get(code)
        if here is None or not isinstance(block, dict):
            continue
        for mark in block.get("marks") or []:
            if not isinstance(mark, dict):
                continue
            source_id = str(mark.get("source_id") or "")
            status = str(mark.get("status") or "")
            if not _SYNC_ID.match(source_id) or status not in ("needed", "done", "clear"):
                continue
            kind = mark.get("kind")
            kind = kind if isinstance(kind, str) and _WORD.match(kind) else None
            try:
                marked_at = float(mark.get("marked_at"))
                created_at = float(mark.get("created_at") or marked_at)
            except (TypeError, ValueError):
                continue
            if db.absorb_account_repair(
                here["fingerprint"], here["name"], account_source_id=source_id,
                status=status, kind=kind, marked_at=marked_at, created_at=created_at,
            ):
                taken += 1
    return taken


def _keep_questions(questions: Any, by_code: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    kept: list[dict[str, Any]] = []
    for q in questions or []:
        if not isinstance(q, dict) or not _QUESTION_ID.match(str(q.get("id") or "")):
            continue
        code = str(q.get("this_code") or "")
        here = by_code.get(code)
        if here is None:
            continue
        entry = {
            "id": q["id"],
            "printer": here["name"],
            "words": str(q.get("words") or ""),
            "other": {k: q.get(k) for k in ("other_label", "other_model", "other_state", "other_since") if q.get(k)},
        }
        kept.append(entry)
    with _lock:
        _questions.clear()
        for code, here in by_code.items():
            own = [e for e in kept if e["printer"] == here["name"]]
            if own:
                _questions[code] = own
    return kept


def open_questions(fingerprint_or_adapter: Any) -> list[dict[str, Any]]:
    """The questions the account last asked about one printer here, from
    the most recent sync.  Empty when there are none or nothing synced."""
    from kiln.outbound_guard import machine_code

    fp = fingerprint_or_adapter
    if not isinstance(fp, str):
        from kiln.registry import stable_machine_fingerprint

        fp = stable_machine_fingerprint(fp)
    if not fp:
        return []
    code = machine_code(fp)
    with _lock:
        return [dict(q) for q in _questions.get(code or "", [])]


def sync(*, db: Any = None, registry: Any = None, limit: int = MAX_ROWS,
         timeout: float | None = None) -> dict[str, Any]:
    """Send every mark the account has not taken, announce this computer's
    printers, and bring down the account's marks and open questions for
    them.  Returns what happened, in words a caller can show; never raises
    for the network."""
    from kiln.auth_session import resolve_api_bearer

    _cancel_pending()
    if not resolve_api_bearer().token:
        return {"synced": False, "why": "signed_out"}
    if db is None:
        from kiln.persistence import get_db

        db = get_db()
    try:
        printers = announcements(registry)
    except Exception:  # noqa: BLE001 -- a registry that cannot be read announces nothing
        logger.debug("printer repair courier: printers not announced", exc_info=True)
        printers = []
    by_code = {p["machine_code"]: p for p in printers}
    owed = db.repairs_owed_to_account(limit=limit)

    from kiln.history_courier import _request
    from kiln.outbound_guard import scrub

    marks: list[dict[str, Any]] = []
    sent_ids: dict[str, int] = {}
    for row in owed:
        sync_id = db.claim_repair_sync_id(row["id"])
        if not _SYNC_ID.match(sync_id):
            continue
        marks.append(account_mark(row, sync_id))
        sent_ids[sync_id] = row["id"]
    body = {
        "marks": marks,
        "machines": [{k: v for k, v in p.items() if k in ("machine_code", "model", "shared_code")} for p in printers],
    }
    answer = _request(TOOL, "POST", SYNC_PATH, scrub(body), timeout=timeout or _WAIT_S)
    if answer.get("success") is False or answer.get("status") == "error":
        return {"synced": False, "why": "not_taken", "code": answer.get("code"),
                "error": answer.get("error"), "owed": len(marks)}
    # Taken and refused (a mark the account cannot keep) both mean the
    # account has seen it; nothing is owed twice.
    for sync_id, row_id in sent_ids.items():
        db.mark_repair_pushed(row_id)
    absorbed = _absorb(db, answer.get("machines") or {}, by_code)
    questions = _keep_questions(answer.get("questions"), by_code)
    return {
        "synced": True,
        "sent": len(marks),
        "kept": len(answer.get("kept") or []),
        "absorbed": absorbed,
        "questions": questions,
        "at": time.time(),
    }


def answer_question(question_id: str, *, same: bool, db: Any = None, registry: Any = None,
                    timeout: float | None = None) -> dict[str, Any]:
    """Answer one of the account's identity questions: the printer it asks
    about IS (*same*) or is NOT the one marked elsewhere.  A yes links the
    two records on the account; a no is remembered so the pair is never
    asked again.  Then the account's marks are brought down, so a yes shows
    at once in the printer's condition here."""
    from kiln.auth_session import resolve_api_bearer
    from kiln.history_courier import _request

    qid = str(question_id or "").strip()
    if not _QUESTION_ID.match(qid):
        return {"status": "error", "success": False, "code": "QUESTION_UNKNOWN",
                "error": "That is not one of the questions your account asked; set_printer_repair lists them."}
    if not resolve_api_bearer().token:
        return {"status": "error", "success": False, "code": "KILN_ACCOUNT_NOT_PAIRED", "why": "signed_out",
                "error": "Answering needs this computer signed in to your Kiln account."}
    answer = _request(TOOL, "POST", ANSWER_PATH, {"question_id": qid, "answer": "same" if same else "different"},
                      timeout=timeout or _WAIT_S)
    if answer.get("success") is False or answer.get("status") == "error":
        return answer
    after = sync(db=db, registry=registry, timeout=timeout)
    return {"status": "success", "success": True, "question_id": qid, "answer": "same" if same else "different",
            "linked": bool(answer.get("linked")), "account": after}


__all__ = [
    "ANSWER_PATH",
    "MAX_ROWS",
    "SYNC_PATH",
    "TOOL",
    "account_mark",
    "announcements",
    "answer_question",
    "open_questions",
    "push_soon",
    "sync",
]
