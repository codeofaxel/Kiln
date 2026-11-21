"""How a print is kept: in the person's Kiln account, on this computer only, or not at all.

One value per print, one of three:

* ``kept`` -- the print joins the design's history on this computer and,
  for a signed-in person, on their Kiln account (today's behaviour).
* ``local`` -- the person chose "this computer only" for their account.  The
  print's record and what Kiln learns from it stay on this computer; nothing
  about it is sent to the account or to Kiln's servers, and it is never
  pooled.  The choice is the account's (``print_history`` on the person's
  Kiln account), with this computer's last reading of it kept beside the
  person's other sharing answers (:mod:`kiln.sharing_consent`).
* ``none`` -- the person asked for this one print not to be kept.  Only on
  their request, never suggested.  While it prints, only what the running
  print needs to stay safe lives on this computer: its start record
  (:mod:`kiln.monitor_twin`, which the mid-print checks and a power-loss
  resume read) and its hardware plan.  When it ends both are deleted, and no
  record of the print is written anywhere, on this computer or on the
  account.  Every safety check runs in full.

The one decision is :func:`decide`, made once by the start every print
passes through (:meth:`kiln.printers.base.PrinterAdapter.start_print`) and
written into the running print's start record.  The one read is
:func:`level_of`: every writer of print history asks it, and every sender
asks :func:`may_leave` before anything about a print leaves this computer.

A team's rule.  A team owner can require that the team's designs printed on
the team's printers are kept.  The rules of the person's teams are read from
their account and kept on this computer, so applying one sends nothing about
the print.  When a rule applies to a print the person asked not to keep, the
start is held before anything reaches the printer and says so
(``TEAM_KEEPS_PRINTS``); the print starts only when it is asked for again
as a kept print.  A print the person would keep on this computer only is
held the same way the first time a team's rule applies, and told in the
start's answer after that.  A personal design follows the person's own
choice on any printer.
"""

from __future__ import annotations

import contextvars
import functools
import json
import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

KEPT = "kept"
LOCAL = "local"
NONE = "none"
LEVELS = (KEPT, LOCAL, NONE)

#: The account's two words for where print history is kept.
ACCOUNT = "account"
COMPUTER = "computer"
ACCOUNT_CHOICES = (ACCOUNT, COMPUTER)

_NOTHING_SENT = (
    "Nothing about your prints is sent to your Kiln account or Kiln's servers, "
    "and none is pooled."
)
#: What the person sees at a terminal for each choice: they sit at the computer.
SAID_AT_THIS_COMPUTER: dict[str, str] = {
    ACCOUNT: "Print history: kept in your Kiln account.",
    COMPUTER: f"Print history: kept on this computer only. {_NOTHING_SENT}",
}
#: What the account says of the choice wherever the person is not at the
#: computer: to an agent after it sets the choice, and on the web account page.
SAID_BY_THE_ACCOUNT: dict[str, str] = {
    ACCOUNT: "Print history: kept in your Kiln account.",
    COMPUTER: f"Print history: kept only on the computer that printed it. {_NOTHING_SENT}",
}
#: Said under the choice when the account could not be asked.
LAST_SEEN = "(Kiln couldn't check your account just now; this is the last setting it saw.)"

#: The start's answer for a print the person asked not to keep, said once.
NOT_KEPT_SAID = (
    "This print won't be kept. While it prints, Kiln keeps only what it needs "
    "to stay safe, then deletes it."
)

#: The code a start held for a team's rule answers with.
TEAM_HOLD_CODE = "TEAM_KEEPS_PRINTS"

#: The one sentence for a print asked not to be kept of a team's design on
#: the team's printer, when the team's rule could not be read just now.
TEAM_RULE_UNREAD_SAID = (
    "Kiln can't check this team's print rule right now; this design belongs to the team. "
    "Try again when you're online, or print it as kept."
)

#: The account route the choice and the team rules are read from.
PREFS_PATH = "/api/cloud/user/prefs"
TEAM_RULES_PATH = "/api/team/print-rules"

#: How long a reading of the account's choice or the team rules stands.
_FRESH_S = 60.0
_TEAM_FRESH_S = 600.0
_READ_TIMEOUT_S = 5.0

#: Keys in this computer's sharing answers (``~/.kiln/sharing.json``).
_CHOICE_KEY = "print_history"
_CHOICE_READ_KEY = "print_history_read_at"
_TEAM_RULES_KEY = "team_print_rules"
_TEAM_RULES_READ_KEY = "team_print_rules_read_at"
_TEAM_TOLD_KEY = "team_print_rules_told"

# A print not kept that this process started or saw end, per printer: the
# file's base name.  In memory only, so a writer still knows when the start
# record could not be written or was already deleted; cleared by the next
# start on that printer.
_ended_not_kept: dict[str, str] = {}
# Pictures of the plate taken while a printer's print is one not kept (the
# camera's frames handed to eyes), per printer: deleted when it ends, or
# when the next print starts on that printer.  In memory only.
_pictures: dict[str, list[str]] = {}
_lock = threading.Lock()


@dataclass(frozen=True)
class Decision:
    """How the print about to start is kept.

    ``held``: the start must not go ahead; ``said`` is why.  ``team``: the
    team whose rule keeps this print.  ``said`` on a start that goes ahead is
    the one line its answer carries.
    """

    level: str
    held: bool = False
    team: str | None = None
    said: str | None = None


# ---------------------------------------------------------------------------
# The person's choices
# ---------------------------------------------------------------------------


def _sharing() -> Any:
    from kiln import sharing_consent

    return sharing_consent


def account_choice() -> str:
    """The account's choice as this computer last read it: ``account`` (the
    default, and the answer before any reading) or ``computer``.  A stored
    answer that cannot be read reads as ``computer``: nothing leaves on a
    choice nobody can see."""
    try:
        path = _sharing().store_path()
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ACCOUNT
        data = json.loads(raw)
        if not isinstance(data, dict):
            return COMPUTER
        value = data.get(_CHOICE_KEY)
    except Exception:  # noqa: BLE001 -- unreadable: keep it here
        return COMPUTER
    return COMPUTER if value == COMPUTER else ACCOUNT


def remember_account_choice(choice: str) -> None:
    """Keep *choice* (``account`` / ``computer``) as this computer's reading."""
    if choice not in ACCOUNT_CHOICES:
        raise ValueError(f"print history is kept in the account or on this computer: {ACCOUNT_CHOICES}")
    sharing = _sharing()
    data = sharing._read()
    data.update({_CHOICE_KEY: choice, _CHOICE_READ_KEY: time.time()})
    sharing._write(data)


def refresh_account_choice(*, fresh: bool = False) -> str | None:
    """Read the account's choice when the last reading is older than a
    minute (or *fresh*), keep it, and return it.  ``None`` when it could not
    be read just now (signed out, offline, no answer); the last reading is
    left as it was."""
    try:
        read_at = float(_sharing()._read().get(_CHOICE_READ_KEY) or 0.0)
    except Exception:  # noqa: BLE001
        read_at = 0.0
    if not fresh and 0.0 <= time.time() - read_at < _FRESH_S:
        return account_choice()
    from kiln.history_courier import _request

    answer = _request("print_history", "GET", PREFS_PATH, timeout=_READ_TIMEOUT_S)
    if answer.get("status") == "error":
        return None
    choice = answer.get("print_history")
    if choice not in ACCOUNT_CHOICES:
        choice = ACCOUNT
    remember_account_choice(choice)
    return choice


def set_account_choice(choice: str) -> dict[str, Any]:
    """Set where the account keeps print history (``account`` /
    ``computer``), on the account, then keep it on this computer."""
    if choice not in ACCOUNT_CHOICES:
        return {"status": "error", "success": False, "code": "VALIDATION_ERROR",
                "error": f"choice must be one of {', '.join(ACCOUNT_CHOICES)}"}
    from kiln.history_courier import _request

    answer = _request("print_history", "PUT", PREFS_PATH, {"print_history": choice},
                      timeout=_READ_TIMEOUT_S)
    if answer.get("status") == "error":
        return answer
    remember_account_choice(choice)
    return {"status": "success", "success": True, "print_history": choice,
            "summary": SAID_BY_THE_ACCOUNT[choice]}


# ---------------------------------------------------------------------------
# The team rules
# ---------------------------------------------------------------------------


def team_rules(*, refresh: bool = True) -> list[dict[str, Any]]:
    """The print rules of the person's teams: each ``{"org_id", "name",
    "printers", "design_ids"}`` for a team whose owner requires its prints
    kept.  Read from the account at most every ten minutes when *refresh*;
    the last reading otherwise.  ``[]`` when none was ever read."""
    sharing = _sharing()
    try:
        data = sharing._read()
    except Exception:  # noqa: BLE001
        data = {}
    read_at = float(data.get(_TEAM_RULES_READ_KEY) or 0.0)
    if refresh and not 0.0 <= time.time() - read_at < _TEAM_FRESH_S:
        try:
            from kiln.history_courier import _request

            answer = _request("print_history", "GET", TEAM_RULES_PATH, timeout=_READ_TIMEOUT_S)
        except Exception:  # noqa: BLE001 -- the last reading stands
            answer = {"status": "error"}
        if answer.get("status") != "error" and isinstance(answer.get("rules"), list):
            rules = [_clean_rule(r) for r in answer["rules"]]
            data = sharing._read()
            data.update({_TEAM_RULES_KEY: [r for r in rules if r], _TEAM_RULES_READ_KEY: time.time()})
            sharing._write(data)
    rules = data.get(_TEAM_RULES_KEY)
    return [r for r in rules if isinstance(r, dict)] if isinstance(rules, list) else []


def _clean_rule(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict) or not raw.get("org_id"):
        return None
    return {
        "org_id": str(raw["org_id"]),
        "name": str(raw.get("name") or ""),
        "printers": [str(p) for p in raw.get("printers") or [] if isinstance(p, str) and p.strip()],
        "design_ids": [str(d) for d in raw.get("design_ids") or [] if isinstance(d, str) and d.strip()],
    }


def team_rules_fresh() -> bool:
    """Whether this computer's reading of the team rules is current (read
    from the account within the last ten minutes)."""
    try:
        read_at = float(_sharing()._read().get(_TEAM_RULES_READ_KEY) or 0.0)
    except Exception:  # noqa: BLE001
        return False
    return 0.0 <= time.time() - read_at < _TEAM_FRESH_S


def team_rule_for(printer_name: str | None, design_id: str | None, *, refresh: bool = True) -> dict[str, Any] | None:
    """The team rule that keeps a print of *design_id* on *printer_name*, or
    ``None``.  A rule applies only to its own team's designs on the printers
    its owner named."""
    if not printer_name or not design_id:
        return None
    wanted = printer_name.strip().casefold()
    for rule in team_rules(refresh=refresh):
        printers = {p.strip().casefold() for p in rule.get("printers") or []}
        if wanted in printers and design_id in (rule.get("design_ids") or []):
            return rule
    return None


def _told(org_id: str) -> bool:
    try:
        told = _sharing()._read().get(_TEAM_TOLD_KEY)
    except Exception:  # noqa: BLE001
        return False
    return isinstance(told, list) and org_id in told


def _mark_told(org_id: str) -> None:
    sharing = _sharing()
    data = sharing._read()
    told = data.get(_TEAM_TOLD_KEY)
    told = [t for t in told if isinstance(t, str)] if isinstance(told, list) else []
    if org_id not in told:
        told.append(org_id)
    data[_TEAM_TOLD_KEY] = told
    sharing._write(data)


def team_sentence(rule: dict[str, Any], *, held: bool) -> str:
    name = str(rule.get("name") or "").strip()
    team = f"The team {name}" if name else "This team"
    said = f"{team} keeps its prints of the team's designs on this printer."
    if held:
        said += " Nothing was sent to the printer. Start it again as a kept print to print it."
    return said


# ---------------------------------------------------------------------------
# The decision, at the start
# ---------------------------------------------------------------------------


def requested(value: Any) -> bool | None:
    """A print door's ``keep_record`` argument as the start takes it:
    ``False`` (do not keep this print), ``True`` (keep it), ``None``
    (follow the account's choice)."""
    return value if isinstance(value, bool) else None


def decide(adapter: Any, file_name: str, kwargs: dict[str, Any], keep_record: bool | None) -> Decision:
    """How the print about to start on *adapter* is kept.  Called once, by
    the start, before anything reaches the printer.  Never raises: a
    decision that cannot be made keeps the print the account's way."""
    try:
        _refresh_soon()
        if keep_record is True:
            return Decision(KEPT)
        wanted = NONE if keep_record is False else (LOCAL if account_choice() == COMPUTER else KEPT)
        if wanted == KEPT:
            return Decision(KEPT)
        rule = _team_rule_at_start(adapter, file_name, kwargs)
        if rule is not None:
            if wanted == NONE and not team_rules_fresh():
                # The design is the team's on the team's printer, by the last
                # reading of its rule, and the rule cannot be read now: this
                # one print waits until it can, or is printed as kept.
                return Decision(NONE, held=True, team=rule.get("name") or rule["org_id"],
                                said=TEAM_RULE_UNREAD_SAID)
            if wanted == NONE or not _told(rule["org_id"]):
                _mark_told(rule["org_id"])
                return Decision(wanted, held=True, team=rule.get("name") or rule["org_id"],
                                said=team_sentence(rule, held=True))
            return Decision(KEPT, team=rule.get("name") or rule["org_id"], said=team_sentence(rule, held=False))
        return Decision(wanted, said=NOT_KEPT_SAID if wanted == NONE else None)
    except Exception:  # noqa: BLE001 -- the most private way asked for, never a refusal
        logger.debug("print keeping decision failed", exc_info=True)
        if keep_record is False:
            return Decision(NONE, said=NOT_KEPT_SAID)
        if keep_record is True:
            return Decision(KEPT)
        return Decision(LOCAL if account_choice() == COMPUTER else KEPT)


def _refresh_soon() -> None:
    """Read the account's choice again on a thread of its own, so a choice
    made on another computer or the web reaches this one without a start
    ever waiting on the network.  Signed out, it does nothing."""
    try:
        from kiln.auth_session import resolve_api_bearer

        if not resolve_api_bearer().token:
            return
        threading.Thread(target=_refresh_quietly, name="kiln-print-keeping", daemon=True).start()
    except Exception:  # noqa: BLE001
        logger.debug("account choice not refreshed", exc_info=True)


def _refresh_quietly() -> None:
    try:
        refresh_account_choice()
    except Exception:  # noqa: BLE001 -- the last reading stands
        logger.debug("account choice not read", exc_info=True)


def _team_rule_at_start(adapter: Any, file_name: str, kwargs: dict[str, Any]) -> dict[str, Any] | None:
    from kiln.printers.base import outcome_printer_name

    design_id = kwargs.get("design_id")
    if not design_id:
        try:
            from kiln.auto_record_hook import print_identity_at_start

            design_id = (print_identity_at_start(adapter, kwargs, file_name) or {}).get("design_id")
        except Exception:  # noqa: BLE001
            design_id = None
    return team_rule_for(outcome_printer_name(adapter), str(design_id) if design_id else None)


# ---------------------------------------------------------------------------
# The one read, and the running print's record
# ---------------------------------------------------------------------------


def _stem(name: Any) -> str:
    from kiln.persistence import _file_stem_token

    return _file_stem_token(os.path.basename(str(name or ""))) or ""


def _slug(printer_name: str | None) -> str:
    from kiln.monitor_twin import _slug as twin_slug

    return twin_slug(printer_name or "default")


def _running(printer_name: str | None) -> dict[str, Any] | None:
    from kiln.monitor_twin import active_twin

    if not printer_name:
        return None
    try:
        return active_twin(printer_name)
    except Exception:  # noqa: BLE001
        return None


def level_of(printer_name: str | None, file_name: str | None = None) -> str:
    """How the print of *file_name* on *printer_name* is kept: ``none`` when
    it is a print the person asked not to keep (running, or seen ending by
    this process), else ``local`` or ``kept`` by the account's choice.  With
    no *file_name*, the printer's current print.  Never raises."""
    try:
        wanted = _stem(file_name) if file_name else ""
        record = _running(printer_name)
        if isinstance(record, dict) and record.get("kept") == NONE:
            if not wanted or _stem(record.get("file_name")) == wanted:
                return NONE
        with _lock:
            ended = _ended_not_kept.get(_slug(printer_name)) if printer_name else None
        if ended is not None and (not wanted or _stem(ended) == wanted):
            return NONE
    except Exception:  # noqa: BLE001 -- unreadable: nothing is written
        logger.debug("print keeping level unreadable", exc_info=True)
        return NONE
    return LOCAL if account_choice() == COMPUTER else KEPT


def keeps(printer_name: str | None, file_name: str | None = None) -> bool:
    """Whether a record of this print may be written at all."""
    return level_of(printer_name, file_name) != NONE


def may_leave(row: dict[str, Any] | None = None, *, printer_name: str | None = None,
              file_name: str | None = None) -> bool:
    """Whether anything about a print (a row of :mod:`kiln.persistence`'s
    ``print_outcomes``, or the print on *printer_name*) may leave this
    computer: never for a print kept on this computer only or not kept,
    and never while the account's choice is this computer only."""
    if account_choice() == COMPUTER:
        return False
    if isinstance(row, dict):
        if row.get("kept") in (LOCAL, NONE):
            return False
        printer_name = printer_name or row.get("printer_name")
        file_name = file_name or row.get("file_name")
    if printer_name and level_of(printer_name, file_name) != KEPT:
        return False
    return True


def _hosted() -> bool:
    """Kiln's shared server runs no printer: nothing here is kept there.  A
    posture that cannot be read counts as a person's own computer, so a
    print not kept is never recorded for want of an answer."""
    try:
        from kiln.runtime_env import is_hosted_multitenant

        return bool(is_hosted_multitenant())
    except Exception:  # noqa: BLE001
        return False


def note_picture(printer_name: str | None, path: str | None) -> bool:
    """A picture of *printer_name*'s plate was saved at *path*.  While that
    printer's print is one not kept, it is remembered so it is deleted when
    the print ends, and the caller keeps it nowhere and attaches it to
    nothing: returns ``True``.  ``False`` otherwise.  Never raises."""
    try:
        if not printer_name or not path or _hosted() or keeps(printer_name):
            return False
        with _lock:
            _pictures.setdefault(_slug(printer_name), []).append(str(path))
        return True
    except Exception:  # noqa: BLE001 -- unreadable: treat it as not kept
        return True


#: The name Kiln gives every plate picture it saves (kiln.plate_state.save_frame).
PLATE_PICTURE_PREFIX = "kiln_plate_"


def remove_kilns_own(path: str | None, roots: list[str] | tuple[str, ...], *, prefix: str = "") -> bool:
    """Delete *path* only when it is a file Kiln made: it resolves inside one
    of *roots* (Kiln's own folders) and, with *prefix*, its name starts with
    the name Kiln gives such files.  A person's own file is never touched.
    Returns whether it was deleted.  Never raises."""
    try:
        if not path:
            return False
        real = os.path.realpath(str(path))
        if prefix and not os.path.basename(real).startswith(prefix):
            return False
        for root in roots:
            base = os.path.realpath(str(root))
            if root and real.startswith(base.rstrip(os.sep) + os.sep) and os.path.isfile(real):
                os.unlink(real)
                return True
    except OSError:
        logger.debug("a file Kiln made was not deleted", exc_info=True)
    return False


def picture_roots() -> tuple[str, ...]:
    """The folders Kiln saves plate pictures in."""
    from kiln.output_paths import kiln_temp_dir

    import tempfile

    return (kiln_temp_dir(), tempfile.gettempdir())


def _drop_pictures(printer_name: str) -> None:
    with _lock:
        paths = _pictures.pop(_slug(printer_name), [])
    for path in paths:
        remove_kilns_own(path, picture_roots(), prefix=PLATE_PICTURE_PREFIX)


def print_started(printer_name: str, file_name: str, decision: Decision) -> None:
    """A print started: this process remembers, in memory, a print the
    person asked not to keep (so no writer records it even when its start
    record could not be written), and forgets any earlier one."""
    if _hosted():
        return
    _drop_pictures(printer_name)
    with _lock:
        if decision.level == NONE:
            _ended_not_kept[_slug(printer_name)] = os.path.basename(str(file_name or ""))
        else:
            _ended_not_kept.pop(_slug(printer_name), None)


def print_ended(printer_name: str | None) -> bool:
    """The print on *printer_name* ended.  When it was a print not kept, its
    start record and the files kept beside it, its hardware plan and its
    entries in the slice ledger are deleted, and this process remembers (in
    memory only) that it was not kept.  Returns whether anything was
    deleted.  Never raises."""
    if not printer_name or _hosted():
        return False
    try:
        try:
            record = _running(printer_name)
        except Exception:  # noqa: BLE001 -- the memory below still answers
            record = None
        recorded = isinstance(record, dict) and record.get("kept") == NONE
        with _lock:
            remembered = _ended_not_kept.get(_slug(printer_name))
        if not recorded and remembered is None:
            return False
        file_name = str(record.get("file_name") or "") if recorded else remembered
        with _lock:
            _ended_not_kept[_slug(printer_name)] = file_name or ""
        names = _names_of_the_print(file_name)
        if recorded:
            from kiln.monitor_twin import forget_print

            forget_print(printer_name)
        _drop_pictures(printer_name)
        for forget in (_forget_slice_records, _unname_plate):
            try:
                forget(names)
            except Exception:  # noqa: BLE001
                logger.debug("a record of a print not kept was not forgotten", exc_info=True)
        try:
            from kiln.hardware_stops import forget_plans_for_file

            forget_plans_for_file(file_name)
        except Exception:  # noqa: BLE001
            logger.debug("hardware plan of a print not kept not deleted", exc_info=True)
        from kiln.spool_usage import forget_charge

        forget_charge(printer_name)
        return True
    except Exception:  # noqa: BLE001
        logger.debug("print-not-kept cleanup failed", exc_info=True)
        return False


def _names_of_the_print(file_name: str | None) -> set[str]:
    """The base names this print went by: the file the printer started and
    the slice and package the start record joined it to (never a match by
    likeness: :func:`kiln.monitor_twin.sliced_entry_for` is the exact join
    the start made)."""
    names = {os.path.basename(str(file_name or ""))} - {""}
    try:
        from kiln.monitor_twin import sliced_entry_for

        entry = sliced_entry_for(file_name or "") if file_name else None
        for key in ("output", "wrapped"):
            if isinstance(entry, dict) and entry.get(key):
                names.add(os.path.basename(str(entry[key])))
    except Exception:  # noqa: BLE001
        logger.debug("slice join not read", exc_info=True)
    return names


def _forget_slice_records(names: set[str]) -> None:
    from kiln.print_lineage import forget_files

    forget_files(names)


def _unname_plate(names: set[str]) -> None:
    from kiln.plate_state import forget_file

    forget_file(names)


def reply_fields(result: Any) -> dict[str, Any]:
    """What a print door's answer carries about how the print is kept, read
    off the start's own result: ``print_kept`` (the level), ``kept_note``
    (the one line to show, when there is one) and, for a start a team's
    rule held, ``code``."""
    out: dict[str, Any] = {}
    level = getattr(result, "print_kept", None)
    if level in LEVELS:
        out["print_kept"] = level
    said = getattr(result, "print_kept_said", None)
    if isinstance(said, str) and said:
        out["kept_note"] = said
    if getattr(result, "code", "") == TEAM_HOLD_CODE:
        out["code"] = TEAM_HOLD_CODE
    return out


# ---------------------------------------------------------------------------
# A print door's call about a print not kept
# ---------------------------------------------------------------------------

_not_kept_call: contextvars.ContextVar[bool] = contextvars.ContextVar("kiln_print_not_kept_call", default=False)

#: Detail keys of Kiln's action log that name a print.
_NAMING_KEYS = frozenset({"file", "file_name", "file_path", "files", "version_id", "design_id",
                          "branch_id", "gcode", "model"})


def call(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Mark a print door: for the length of a call with ``keep_record=False``,
    Kiln's action log keeps the action and nothing that names the print
    (:func:`audit_details`)."""
    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        token = _not_kept_call.set(kwargs.get("keep_record") is False)
        try:
            return fn(*args, **kwargs)
        finally:
            _not_kept_call.reset(token)

    return wrapper


def event_about_a_print_not_kept(event_type: str, data: Any) -> bool:
    """Whether an event is about a print the person asked not to keep, so
    nothing that carries it (a webhook, the event log) sends or keeps it:
    an event raised inside such a print's own call; a queued job not kept;
    a print event naming a printer and file not kept; or a print event about
    a printer whose current print is not kept.  Never raises."""
    try:
        if _not_kept_call.get():
            return True
        if not isinstance(data, dict):
            return False
        job_id = data.get("job_id")
        if job_id:
            try:
                from kiln import server as _srv

                job = _srv._get_queue().get_job(str(job_id))
                if (getattr(job, "metadata", None) or {}).get("keep_record") is False:
                    return True
            except Exception:  # noqa: BLE001 -- not a queued job
                pass
        printer = data.get("printer_name") or data.get("printer")
        if not printer:
            return False
        file_name = data.get("file_name") or data.get("file") or data.get("job_name")
        if file_name:
            return level_of(str(printer), str(file_name)) == NONE
        return str(event_type or "").startswith(("print.", "job.")) and level_of(str(printer)) == NONE
    except Exception:  # noqa: BLE001 -- unreadable: carried nowhere
        return True


def without_names(data: Any) -> Any:
    """*data* with every key that names a print taken out: what a safety
    event about a print not kept still carries (the safety word reaches the
    person; the print's name does not)."""
    if not isinstance(data, dict):
        return data
    return {k: v for k, v in data.items() if k not in _NAMING_KEYS and k not in ("job_id", "job_name", "file_hash")}


def mark_call_not_kept() -> None:
    """For a one-shot process (the ``kiln print --dont-keep`` command): the
    rest of this call is about a print not kept (:func:`audit_details`)."""
    _not_kept_call.set(True)


def audit_details(details: dict[str, Any] | None) -> dict[str, Any] | None:
    """*details* of an action-log entry as it is kept: without what names
    the print, inside a print door's call for a print not kept."""
    if not details or not _not_kept_call.get():
        return details
    kept = {
        k: ({kk: vv for kk, vv in v.items() if kk not in ("frame", "frame_at")} if isinstance(v, dict) else v)
        for k, v in details.items() if k not in _NAMING_KEYS
    }
    kept["print_kept"] = NONE
    return kept
