"""Public-Kiln -> Kiln's servers printer check-in courier.

After a print ends on a printer, a signed-in install asks Kiln's servers
(https://api.kiln3d.com, ``POST /api/cloud/machines/checkin``) whether there
is one thing to ask the person about that printer.  This module carries the
printer's recent print records up and the question down, keeps the question
on this computer, shows it, and does what the person's answer says.  The
question, its sentences and its answers are the servers'; nothing here words
or changes them.

What goes up, and nothing else (:func:`request_body`): the machine as a
one-way code (:func:`kiln.outbound_guard.machine_code`), the name the person
gave the printer (left out when it holds one of this computer's printer
addresses, serials or codes), its repair mark (:mod:`kiln.machine_repairs`), the episodes
the person has answered, and per print record (:func:`record`) a one-way code
of the job id, the ending, who settled it, a failure word, two times, the
design id, one-way codes of the file's name and of its fingerprint, the
material word, the nozzle size, a one-way code of the sliced settings, the
spool as a one-way code, how far the print got and the firmware version when
it is a bare version.  Every value passes through
:func:`kiln.outbound_guard.scrub`.  No file name, path or fingerprint, no job
id, serial, address, note or settings.

When (:func:`ask_soon`, called by ``KilnDB`` after a row settles and after an
ending's own question is declined): once per ending per printer, a moment
later, on a background thread.  Never while the printer's newest print is
still open, and never while that ending still owes its own question
(:mod:`kiln.ending_question`).  The tool that reports an ending reads only
what is already kept here, from a record this process already has open, so it
never waits on the network or on opening the record.

A miss (offline, signed out, no answer, refused) asks nothing and says
nothing: what was kept is left as it was and the next ending asks again.

Shown (:func:`for_the_ending`, :func:`listed`): in the slot the ending's own
question uses (``ask_the_person``), once per ending and only when that ending
owes no question of its own, and in ``get_printer_insights``.  A question is
shown only while the ending it came back at is still the printer's newest
print.

Answered (:func:`answer`, the ``set_machine_repair`` tool's ``answer_to``):
each answer carries the servers' ``do`` word.  ``mark_needs_repair`` and
``mark_repaired`` write the person's mark through
:func:`kiln.machine_repairs.mark`; ``open_checkup`` points at this install's
setup checkup; ``remember`` does nothing more.  Every answer is kept on this
computer under its episode (``KilnDB.answer_printer_checkin``) and the
answered episodes go up with later check-ins.

Switched off with ``KILN_PRINTER_CHECKIN=0``; quiet under a test run or a
sandbox unless ``KILN_PRINTER_CHECKIN_IN_TESTS=1``; never on Kiln's servers.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

TOOL = "printer_checkin"
#: This module's name in :data:`kiln.outbound_guard.VERSION_FIELDS`.
SENDER = "kiln/_pro_printer_checkin_bridge.py"
PATH = "/api/cloud/machines/checkin"
SWITCH_ENV = "KILN_PRINTER_CHECKIN"
TEST_OPT_IN_ENV = "KILN_PRINTER_CHECKIN_IN_TESTS"
#: The most records and answered episodes one check-in carries.
MAX_PRINTS = 400
MAX_ANSWERED = 200
#: The longest printer name sent.
LABEL_MAX = 40
#: How long a burst of writes to one printer's rows is gathered (seconds).
_GATHER_S = 3.0
#: The longest one check-in waits on Kiln's servers (seconds).
WAIT_S = 6.0

#: What an answer may tell this computer to do.
DO_MARK_NEEDS_REPAIR = "mark_needs_repair"
DO_MARK_REPAIRED = "mark_repaired"
DO_OPEN_CHECKUP = "open_checkup"
DO_REMEMBER = "remember"
DOS = (DO_MARK_NEEDS_REPAIR, DO_MARK_REPAIRED, DO_OPEN_CHECKUP, DO_REMEMBER)


RULE = (
    "Say these sentences to the person as they are written, once, at a natural "
    "moment. Record what they say and never pick an answer for them. If they do "
    "not answer, leave it."
)

CODE_UNKNOWN = "PRINTER_QUESTION_UNKNOWN"
CODE_OTHER_PRINTER = "PRINTER_QUESTION_OTHER_PRINTER"
CODE_BAD_ANSWER = "PRINTER_ANSWER_UNKNOWN"

_EPISODE = re.compile(r"ep:[0-9a-f]{24}")
_WORD = re.compile(r"[a-z_]{1,40}")
_TOKEN = re.compile(r"[A-Za-z0-9._:@\-]{1,128}")
_SETTINGS_CODE = re.compile(r"[0-9a-f]{16,64}")
_SENTENCE_MAX = 600
_SAY_MAX = 120

_lock = threading.Lock()
_timers: dict[str, threading.Timer] = {}
#: ``printer name -> the state of its newest ending at the last check-in the
#: servers answered``: one check-in per ending as it stands.
_asked: dict[str, str] = {}


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


def _db(db: Any) -> Any:
    if db is not None:
        return db
    from kiln.persistence import get_db

    return get_db()


# ---------------------------------------------------------------------------
# Asking
# ---------------------------------------------------------------------------


def ask_soon(printer_name: Any, *, db: Any = None) -> None:
    """Check in on *printer_name* a moment from now, on a background thread.
    Returns at once; never raises."""
    try:
        name = str(printer_name or "").strip()
        if not name or not _enabled():
            return
        with _lock:
            held = _timers.get(name)
            if held is not None and held.is_alive():
                return
            timer = threading.Timer(_GATHER_S, _ask_quietly, args=(name, db))
            timer.daemon = True
            _timers[name] = timer
            timer.start()
    except Exception:  # noqa: BLE001 -- a check-in never fails the write that offered it
        logger.debug("printer check-in not started", exc_info=True)


def _ask_quietly(name: str, db: Any) -> None:
    try:
        ask_now(name, db=db)
    except Exception:  # noqa: BLE001 -- the next ending asks again
        logger.debug("printer check-in failed", exc_info=True)
    finally:
        with _lock:
            _timers.pop(name, None)


def _ending_state(row: dict[str, Any]) -> str:
    return "|".join(
        str(row.get(key) or "")
        for key in ("job_id", "outcome", "failure_mode", "determined_by", "stop_reason_asked")
    )


def ask_now(printer_name: str, *, db: Any = None, wait: float = WAIT_S) -> dict[str, Any]:
    """Check in on *printer_name* now and keep what came back.

    Returns ``{"asked": bool, "why": ...}``: ``why`` is ``kept`` (the servers
    answered; ``question`` says whether one is waiting), a reason nothing was
    sent (``local_only``, ``signed_out``, ``no_prints``, ``printing``,
    ``ending_question_first``, ``printer_not_identified``, ``already_asked``),
    or the cause of a miss (``offline``, ``signed_out``, ``unanswered``,
    ``refused``).  Never raises for the network; a miss changes nothing kept.
    """
    from kiln.runtime_env import is_hosted_multitenant

    if is_hosted_multitenant():
        return {"asked": False, "why": "local_only"}
    from kiln.auth_session import resolve_api_bearer

    if not resolve_api_bearer().token:
        return {"asked": False, "why": "signed_out"}
    store = _db(db)
    latest = store.latest_outcome_row(printer_name)
    if latest is None:
        return {"asked": False, "why": "no_prints"}
    if latest.get("outcome") == "pending":
        return {"asked": False, "why": "printing"}
    from kiln import ending_question

    if ending_question.for_row(latest) is not None:
        return {"asked": False, "why": "ending_question_first"}
    fingerprint = latest.get("machine_fingerprint")
    if not isinstance(fingerprint, str) or not fingerprint.strip():
        return {"asked": False, "why": "printer_not_identified"}
    state = _ending_state(latest)
    with _lock:
        if _asked.get(printer_name) == state:
            return {"asked": False, "why": "already_asked"}
    body = request_body(store, fingerprint, printer_name)
    if body is None:
        return {"asked": False, "why": "printer_not_identified"}

    from kiln.history_courier import _request
    from kiln.served_answer import Miss, classify_answer

    answer = _request(TOOL, "POST", PATH, body, timeout=wait)
    miss = classify_answer(answer)
    if miss is None and "question" not in answer:
        miss = Miss("unanswered", detail="no question field in the answer")
    if miss is not None:
        return {"asked": True, "why": miss.cause}
    question = _read_question(answer.get("question"))
    store.set_printer_checkin(fingerprint, printer_name, question, asked_job_id=str(latest.get("job_id") or ""))
    with _lock:
        _asked[printer_name] = state
    return {"asked": True, "why": "kept", "question": question is not None}


def forget() -> None:
    """Drop what this process remembers of the check-ins it made."""
    with _lock:
        _asked.clear()


def _number(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0:
        return float(value)
    return None


def record(row: dict[str, Any]) -> dict[str, Any] | None:
    """One print row as a check-in carries it: the named fields, nothing
    else.  ``None`` for a row with no job id, no start time, or an ending
    that is not a verdict (:func:`kiln.failure_vocabulary.ending_verdict`)."""
    from kiln.failure_vocabulary import ending_verdict
    from kiln.outbound_guard import file_code, file_hash_code, job_code, spool_code
    from kiln.print_evidence import wire_firmware

    if ending_verdict(row.get("outcome"), row.get("failure_mode"), row.get("determined_by")) is None:
        return None
    job = job_code(row.get("job_id"))
    started = _number(row.get("created_at"))
    if not job or started is None:
        return None
    out: dict[str, Any] = {"job_id": job, "outcome": row.get("outcome"), "created_at": started}
    for key in ("failure_mode", "determined_by"):
        value = row.get(key)
        if isinstance(value, str) and _WORD.fullmatch(value):
            out[key] = value
    ended = _number(row.get("ended_at"))
    if ended is not None:
        out["ended_at"] = ended
    design = row.get("design_id")
    if isinstance(design, str) and _TOKEN.fullmatch(design):
        out["design_id"] = design
    for key, code in (("file_code", file_code(row.get("file_name"))), ("file_hash", file_hash_code(row.get("file_hash")))):
        if code:
            out[key] = code
    material = row.get("material_type")
    if isinstance(material, str) and material.strip():
        out["material_type"] = material.strip()[:40]
    nozzle = _number(row.get("nozzle_diameter_mm"))
    if nozzle is not None:
        out["nozzle_diameter_mm"] = nozzle
    settings = row.get("settings_code")
    if isinstance(settings, str) and _SETTINGS_CODE.fullmatch(settings):
        out["settings_code"] = settings
    spool = spool_code(row.get("spool_id")) if isinstance(row.get("spool_id"), str) else None
    if spool:
        out["spool_code"] = spool
    firmware = wire_firmware(row.get("firmware_version"))
    if firmware:
        out["firmware"] = firmware
    share = row.get("progress_at_end")
    if isinstance(share, (int, float)) and not isinstance(share, bool) and 0.0 <= share <= 1.0:
        out["progress_at_end"] = float(share)
    return out


def _repair(store: Any, fingerprint: str) -> dict[str, Any] | None:
    """The machine's standing repair mark as a check-in carries it, or
    ``None`` for a machine with none."""
    condition = store.machine_condition(fingerprint)
    state = condition.get("state")
    if state == "needs_repair":
        out: dict[str, Any] = {"state": state, "since": condition.get("since")}
        standing = store.standing_repair_marks(condition.get("marks") or [])
        if standing:
            out["marked_at"] = standing[-1].get("created_at")
    elif state == "repaired":
        out = {"state": state, "repaired_at": condition.get("repaired_at")}
    else:
        return None
    if condition.get("kind"):
        out["kind"] = condition["kind"]
    return {key: value for key, value in out.items() if value is not None}


def _label(printer_name: Any) -> str | None:
    """The person's name for the printer as it may be sent: one line, cut to
    :data:`LABEL_MAX`.  ``None`` for a name that holds one of the addresses,
    serials or codes this computer keeps for its printers, or one that cannot
    be checked against them."""
    name = " ".join(str(printer_name or "").split())
    if not name:
        return None
    try:
        from kiln.outbound_guard import _own_printer_values

        own = _own_printer_values()
    except Exception:  # noqa: BLE001 -- a name that cannot be checked is not sent
        return None
    lowered = name.lower()
    for _phrase, held in own:
        mine = str(held).strip().lower()
        if mine and mine in lowered:
            return None
    return name[:LABEL_MAX]


def request_body(store: Any, machine_fingerprint: str, printer_name: str) -> dict[str, Any] | None:
    """The whole check-in for one machine, scrubbed, or ``None`` when the
    machine has no code."""
    from kiln.outbound_guard import VERSION_FIELDS, machine_code, scrub

    code = machine_code(machine_fingerprint)
    if not code:
        return None
    body: dict[str, Any] = {"machine_code": code}
    label = _label(printer_name)
    if label:
        body["printer_label"] = label
    prints = [record(row) for row in store.machine_print_rows(machine_fingerprint, MAX_PRINTS)]
    body["prints"] = [p for p in prints if p is not None]
    repair = _repair(store, machine_fingerprint)
    if repair:
        body["repair"] = repair
    answered = [
        {"key": row["episode"], **({"at": row["answered_at"]} if row.get("answered_at") else {})}
        for row in store.printer_checkins_answered(machine_fingerprint, MAX_ANSWERED)
        if isinstance(row.get("episode"), str) and _EPISODE.fullmatch(row["episode"])
    ]
    if answered:
        body["answered"] = answered
    return scrub(body, version_keys=VERSION_FIELDS[SENDER])


def _sentence(value: Any, limit: int = _SENTENCE_MAX) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text if text and len(text) <= limit and "\n" not in text else None


def _read_question(raw: Any) -> dict[str, Any] | None:
    """The servers' question as this computer keeps it, or ``None`` when
    there is none or it is not shaped like one.  Sentences are kept as
    written; a question with an answer this computer cannot carry out is
    not kept."""
    from kiln.machine_repairs import KINDS

    if not isinstance(raw, dict):
        return None
    episode, seen, ask = raw.get("episode"), _sentence(raw.get("seen")), _sentence(raw.get("ask"))
    if not isinstance(episode, str) or not _EPISODE.fullmatch(episode) or not seen or not ask:
        return None
    answers = raw.get("answers")
    if not isinstance(answers, list) or not 1 <= len(answers) <= 4:
        return None
    kept: list[dict[str, Any]] = []
    for item in answers:
        if not isinstance(item, dict):
            return None
        answer_id, say, do = item.get("id"), _sentence(item.get("say"), _SAY_MAX), item.get("do")
        if not isinstance(answer_id, str) or not _WORD.fullmatch(answer_id) or not say or do not in DOS:
            return None
        one: dict[str, Any] = {"id": answer_id, "say": say, "do": do}
        if item.get("kind") in KINDS:
            one["kind"] = item["kind"]
        since = _number(item.get("since"))
        if do == DO_MARK_NEEDS_REPAIR and since is not None:
            one["since"] = since
        kept.append(one)
    if len({a["id"] for a in kept}) != len(kept) or len({a["say"] for a in kept}) != len(kept):
        return None
    out: dict[str, Any] = {"episode": episode, "seen": seen, "ask": ask, "answers": kept}
    about = raw.get("about")
    if isinstance(about, str) and _WORD.fullmatch(about):
        out["about"] = about
    check = raw.get("check")
    if isinstance(check, dict) and _sentence(check.get("say")):
        out["check"] = {"say": _sentence(check.get("say"))}
        if check.get("kind") in KINDS:
            out["check"]["kind"] = check["kind"]
    return out


# ---------------------------------------------------------------------------
# Showing
# ---------------------------------------------------------------------------


def _block(row: dict[str, Any]) -> dict[str, Any] | None:
    """A kept question as an agent is shown it: the servers' sentences as
    written, and for each answer the call that records it."""
    try:
        question = json.loads(row["question"])
    except (TypeError, ValueError):
        return None
    if not isinstance(question, dict) or not question.get("answers"):
        return None
    name = str(row.get("printer_name") or "")
    episode = str(question.get("episode") or "")
    out: dict[str, Any] = {"about": "printer", "printer": name, "episode": episode, "seen": question.get("seen")}
    check = question.get("check")
    if isinstance(check, dict) and check.get("say"):
        out["check"] = check["say"]
        if check.get("kind"):
            out["check_part"] = check["kind"]
    out["question"] = question.get("ask")
    out["record"] = {
        answer["say"]: (
            f"set_machine_repair(machine_name={json.dumps(name)}, "
            f"answer_to={json.dumps(episode)}, answer={json.dumps(answer['id'])})"
        )
        for answer in question["answers"]
    }
    out["rule"] = RULE
    return out


def _waiting(printer_name: str, store: Any) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """``(the kept row, the printer's newest print row)`` for the question
    waiting on *printer_name*, or ``None``: none is kept, the printer's
    newest print is still open, that ending owes its own question, or the
    question came back at an earlier ending."""
    from kiln import ending_question

    latest = store.latest_outcome_row(printer_name)
    if latest is None or latest.get("outcome") == "pending":
        return None
    fingerprint = latest.get("machine_fingerprint")
    if not isinstance(fingerprint, str) or not fingerprint:
        return None
    if ending_question.for_row(latest) is not None:
        return None
    row = store.printer_checkin_waiting(fingerprint)
    if row is None or not row.get("asked_job_id") or row["asked_job_id"] != latest.get("job_id"):
        return None
    return row, latest


def for_the_ending(printer_name: str | None, *, db: Any = None) -> dict[str, Any] | None:
    """The printer question for the ``ask_the_person`` slot of the tool that
    reports *printer_name*'s ending, or ``None``.  Given once per ending:
    a question already shown at this ending is not given again.  Reads only
    this computer's record, and only when this process has it open
    (``kiln.persistence.db_if_open``): it never opens the record itself.
    Never raises."""
    try:
        if not printer_name:
            return None
        from kiln.runtime_env import is_hosted_multitenant

        if is_hosted_multitenant():
            return None
        store = db
        if store is None:
            from kiln.persistence import db_if_open

            store = db_if_open()
        if store is None:
            return None
        found = _waiting(printer_name, store)
        if found is None:
            return None
        row, latest = found
        if row.get("shown_job_id") == latest.get("job_id"):
            return None
        block = _block(row)
        if block is not None:
            store.mark_printer_checkin_shown(row["id"], str(latest.get("job_id") or ""))
        return block
    except Exception:  # noqa: BLE001 -- a question is never worth failing the tool that carries it
        logger.debug("printer question not read", exc_info=True)
        return None


def listed(printer_name: str | None, *, db: Any = None) -> dict[str, Any] | None:
    """The question waiting on *printer_name*, for the printer's own
    insights, or ``None``.  Listing it counts as showing it at this ending.
    Reads only this computer's record; never raises."""
    try:
        if not printer_name:
            return None
        from kiln.runtime_env import is_hosted_multitenant

        if is_hosted_multitenant():
            return None
        store = _db(db)
        found = _waiting(printer_name, store)
        if found is None:
            return None
        row, latest = found
        block = _block(row)
        if block is not None and row.get("shown_job_id") != latest.get("job_id"):
            store.mark_printer_checkin_shown(row["id"], str(latest.get("job_id") or ""))
        return block
    except Exception:  # noqa: BLE001
        logger.debug("printer question not listed", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Answering
# ---------------------------------------------------------------------------


def answer(machine_name: str, episode: str, answer_id: str, *, db: Any = None) -> dict[str, Any]:
    """Record the person's answer to the question *episode* about
    *machine_name*, and do what that answer's ``do`` word says.

    ``mark_needs_repair`` marks the printer needed, with the part and the
    date that came with the answer; a date :func:`kiln.machine_repairs.mark`
    will not take is replaced by the furthest back it takes, or by now, and
    ``dated`` says so.  ``mark_repaired`` marks it done, now.
    ``open_checkup`` returns ``checkup``, the command for this install's
    setup checkup.  ``remember`` writes nothing else.  The answer is then
    kept under its episode.

    Raises :class:`kiln.machine_repairs.RepairRefused` on Kiln's servers,
    where no such record is kept, for a question not on record here, one about another printer, an answer the question does not
    offer, and anything the mark itself refuses; nothing is kept then.  A
    question already answered is returned as it was answered, and nothing
    is written.
    """
    from kiln.machine_repairs import (
        BACKDATE_LIMIT_S,
        CODE_BAD_WHEN,
        CODE_LOCAL_ONLY,
        KINDS,
        RepairRefused,
        _fingerprint,
        _resolve,
        day_word,
        mark,
    )
    from kiln.runtime_env import is_hosted_multitenant

    if is_hosted_multitenant():
        raise RepairRefused(
            CODE_LOCAL_ONLY,
            "A question about a printer is kept and answered on the computer the printer is connected to, "
            "not on Kiln's servers (https://kiln3d.com/install).",
        )
    store = _db(db)
    key = str(episode or "").strip()
    row = store.get_printer_checkin(key) if _EPISODE.fullmatch(key) else None
    block = _block(row) if row is not None else None
    if row is None or block is None:
        raise RepairRefused(
            CODE_UNKNOWN,
            f"Kiln has no question {key!r} on record on this computer; nothing was recorded.",
        )
    adapter, name = _resolve(machine_name)
    if _fingerprint(adapter, name) != row["machine_fingerprint"]:
        raise RepairRefused(
            CODE_OTHER_PRINTER,
            f"That question is about {row['printer_name']}, not {name}; nothing was recorded.",
        )
    question = json.loads(row["question"])
    by_id = {a["id"]: a for a in question["answers"]}
    if row.get("answer"):
        earlier = by_id.get(row["answer"], {})
        return {
            "recorded": False, "already_answered": True, "printer": name, "episode": key,
            "answer": row["answer"], "said": earlier.get("say"),
            "next": "That question was already answered; nothing was changed.",
        }
    chosen = by_id.get(str(answer_id or "").strip())
    if chosen is None:
        raise RepairRefused(
            CODE_BAD_ANSWER,
            f"answer must be one of {', '.join(by_id)}; got {answer_id!r}. Nothing was recorded.",
        )
    do = chosen["do"]
    out: dict[str, Any] = {
        "recorded": True, "printer": name, "episode": key, "answer": chosen["id"], "said": chosen["say"], "did": do,
    }
    kind = chosen.get("kind") if chosen.get("kind") in KINDS else None
    if do == DO_MARK_NEEDS_REPAIR:
        since = chosen.get("since")
        try:
            marked = mark(name, "needed", kind=kind, when=since, db=store)
        except RepairRefused as refused:
            if refused.code != CODE_BAD_WHEN or since is None:
                raise
            now = time.time()
            if since < now:
                oldest = now - BACKDATE_LIMIT_S + 60.0
                marked = mark(name, "needed", kind=kind, when=oldest, db=store)
                out["dated"] = (
                    f"The first of those prints started more than {BACKDATE_LIMIT_S // 86400} days ago, "
                    f"further back than a mark can be dated; {name} is marked as of {day_word(oldest)}."
                )
            else:
                marked = mark(name, "needed", kind=kind, db=store)
                out["dated"] = (
                    f"The date that came with the question is ahead of this computer's clock; "
                    f"{name} is marked as of now."
                )
        out["mark"] = marked
        out["next"] = marked.get("next")
    elif do == DO_MARK_REPAIRED:
        marked = mark(name, "done", kind=kind or "other", db=store)
        out["mark"] = marked
        out["next"] = marked.get("next")
    elif do == DO_OPEN_CHECKUP:
        from kiln.cli_command import kiln_command

        # This install's setup checkup.
        command = kiln_command("doctor")
        out["checkup"] = {"run": command, "printer": name}
        out["next"] = f"The setup checkup for {name} is `{command}`, run on this computer."
    else:
        out["next"] = "Kept on this computer."
    store.answer_printer_checkin(row["id"], chosen["id"])
    return out


__all__ = [
    "DOS",
    "MAX_ANSWERED",
    "MAX_PRINTS",
    "PATH",
    "RULE",
    "SENDER",
    "TOOL",
    "WAIT_S",
    "answer",
    "ask_now",
    "ask_soon",
    "for_the_ending",
    "forget",
    "listed",
    "record",
    "request_body",
]
