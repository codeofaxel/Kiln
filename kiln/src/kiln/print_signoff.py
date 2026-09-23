"""The one clearance every print start carries, and the one place it is checked.

``_preview_gate_error`` in :mod:`kiln.server` is the gate the MCP doors
call.  It was a rule with a list of callers, and the list was short of the
truth: ``run_quick_print``, ``run_reslice_and_print``, the job queue, and
every CLI start path reached ``adapter.start_print`` with no gate at all
— which is how a print started unseen on 2026-09-16 through ``kiln
print``.  Adding the missing callers closes the doors that exist today.
This module is for the door added next year.

Two halves:

* **A clearance.**  When a gate passes — a token verified, a person's
  elicited yes, the CI bypass, a standing opt-in the operator armed, a
  queued job that was cleared at submit — it records WHAT was cleared: the
  file, the machine, the door the preview came through, the source of the
  yes.  A ContextVar, so it lives exactly as long as the call that earned
  it and is consumed by the start it covers.

* **A backstop.**  :meth:`PrinterAdapter.start_print` is the template
  method no entry point can bypass.  An adapter handed out by a printer
  registry or the CLI is marked as requiring sign-off, and the template
  refuses to start a file no clearance covers.  Adapters built bare (unit
  tests, embedding hosts that never registered) are untouched.

"Covers" is by name or by lineage: the gate sees the model the person
previewed, the printer sees the slice made from it, and the slice ledger
in :mod:`kiln.monitor_twin` is what joins the two.  A clearance for
``jar.stl`` starts ``jar.gcode`` sliced from it and nothing else.
"""

from __future__ import annotations

import contextlib
import logging
import os
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from kiln.print_consent import (
    SCOPE_FLEET,
    SOURCE_CI_BYPASS,
    SOURCE_ELICITED,
    SOURCE_PREVIEW_TOKEN,
    SOURCE_STANDING_OPT_IN,
)

logger = logging.getLogger(__name__)

__all__ = [
    "SCOPE_FLEET",
    "SOURCE_CI_BYPASS",
    "SOURCE_ELICITED",
    "SOURCE_PREVIEW_TOKEN",
    "SOURCE_STANDING_OPT_IN",
    "SOURCE_QUEUED",
    "SOURCE_PRIOR_APPROVAL",
    "SOURCE_RESUME",
    "Clearance",
    "Verdict",
    "grant",
    "grant_from_record",
    "record_for",
    "record_refusal",
    "current",
    "clear",
    "take",
    "require_signoff",
    "signoff_required",
    "token_verdict",
    "adapter_verdict",
]

#: A job cleared when it entered the queue; the scheduler dispatches on that.
SOURCE_QUEUED = "queued_job"
#: A retry of the same object with new print settings leans on the approval
#: the first print got.
SOURCE_PRIOR_APPROVAL = "prior_approval"
#: A resume 3MF continues a print that already had its yes.
SOURCE_RESUME = "resume"

CODE_NOT_CONFIRMED = "PREVIEW_NOT_CONFIRMED"
CODE_TOKEN_INVALID = "PREVIEW_TOKEN_INVALID"

#: The attribute a registry or the CLI sets on an adapter it hands out.
_REQUIRED_ATTR = "_kiln_signoff_required"

_BYPASS_VALUES = ("1", "true", "yes")


def _norm(value: str | None) -> str:
    if not value:
        return ""
    text = str(value).strip().replace("\\", "/")
    return (text.rsplit("/", 1)[-1] or text).lower()


def _bypassed() -> bool:
    return os.environ.get("KILN_SKIP_PREVIEW_GATE", "").strip() in _BYPASS_VALUES


def _is_resume(file_name: str, kwargs: dict[str, Any] | None = None) -> bool:
    if kwargs and kwargs.get("resume_from_paused"):
        return True
    try:
        from kiln.printers.base import is_resume_mode_3mf

        return is_resume_mode_3mf(file_name)
    except Exception:  # noqa: BLE001
        name = (file_name or "").lower()
        return "_resume_" in name


@dataclass(frozen=True)
class Clearance:
    """One print, cleared: what, where, how the yes was obtained."""

    tool: str
    file_name: str
    printer_name: str | None
    source: str
    door: str = ""
    granted_at: float = field(default_factory=time.time)
    #: ``None`` — the printer it was aimed at; a tuple of names; or
    #: ``"fleet"``.  The scope the person gave, carried to the machine.
    scope: tuple[str, ...] | str | None = None
    #: The standing window the yes rests on, if any — audited by id.
    window_id: str = ""
    #: Who said yes, as the consent recorded it (``os_user:…``, an account, or "").
    identity: str = ""

    def covers_printer(self, printer_name: str | None) -> bool:
        return _scope_covers(self.scope, self.printer_name, printer_name)

    def covers(self, *, file_name: str, printer_name: str | None) -> bool:
        if printer_name and not self.covers_printer(printer_name):
            return False
        mine, theirs = _norm(self.file_name), _norm(file_name)
        if mine == theirs:
            return True
        return _norm(_sliced_from(file_name)) == mine


def _scope_covers(scope: Any, aimed: str | None, printer_name: str | None) -> bool:
    """The one rule for "does this clearance reach that printer": the
    fleet reaches every printer, a list reaches the ones it names, and no
    scope reaches the printer the clearance was aimed at (or any, when it
    was aimed at none)."""
    if scope == SCOPE_FLEET:
        return True
    if isinstance(scope, (tuple, list)) and scope:
        return _norm(printer_name) in {_norm(s) for s in scope}
    return not (aimed and printer_name and _norm(printer_name) != _norm(aimed))


def _scope_from_record(value: Any) -> tuple[str, ...] | str | None:
    if value == SCOPE_FLEET:
        return SCOPE_FLEET
    if isinstance(value, (list, tuple)):
        names = tuple(str(s) for s in value if str(s).strip())
        return names or None
    return None


def _sliced_from(file_name: str) -> str | None:
    """The mesh this machine sliced *file_name* from, by the slice ledger."""
    try:
        from kiln.monitor_twin import sliced_entry_for

        entry = sliced_entry_for(os.path.basename(str(file_name or "")))
        return str(entry.get("input") or "") or None if entry else None
    except Exception:  # noqa: BLE001
        return None


_current: ContextVar[Clearance | None] = ContextVar("kiln_print_clearance", default=None)


def grant(
    tool: str,
    file_name: str,
    printer_name: str | None,
    *,
    source: str,
    door: str = "",
    scope: tuple[str, ...] | str | None = None,
    window_id: str = "",
    identity: str = "",
) -> Clearance:
    """Record that this call may start *file_name* on *printer_name*."""
    clearance = Clearance(
        tool=tool, file_name=str(file_name or ""), printer_name=printer_name, source=source, door=door,
        scope=scope, window_id=window_id or "", identity=identity or "",
    )
    _current.set(clearance)
    return clearance


def current() -> Clearance | None:
    return _current.get()


def clear() -> None:
    _current.set(None)


def take(file_name: str, printer_name: str | None) -> Clearance | None:
    """The clearance covering this start, consumed.  ``None`` otherwise."""
    clearance = _current.get()
    if clearance is None or not clearance.covers(file_name=file_name, printer_name=printer_name):
        return None
    _current.set(None)
    return clearance


def record_for(clearance: Clearance | None) -> dict[str, Any] | None:
    """A clearance as data, for a job record or a paused pipeline to carry."""
    if clearance is None:
        return None
    return {
        "tool": clearance.tool,
        "file_name": clearance.file_name,
        "printer_name": clearance.printer_name,
        "source": clearance.source,
        "door": clearance.door,
        "at": clearance.granted_at,
        "scope": list(clearance.scope) if isinstance(clearance.scope, tuple) else clearance.scope,
        "window_id": clearance.window_id,
        "identity": clearance.identity,
    }


def record_refusal(record: dict[str, Any] | None, printer_name: str | None) -> str | None:
    """Why a stored clearance may NOT start on *printer_name* now — or
    ``None`` when it may.

    Read by the scheduler before it dispatches a queued job: the yes the
    job carries was for a scope, and a printer outside it gets a reason,
    not a print.  A yes that rested on a standing window is re-checked
    against the window at dispatch, so a revoked or run-out window stops
    the jobs queued under it.  A job with no record at all is not judged
    here — the queue's doors are its gate.
    """
    if not isinstance(record, dict) or not record:
        return None
    scope = _scope_from_record(record.get("scope"))
    aimed = record.get("printer_name") or None
    if not _scope_covers(scope, aimed, printer_name):
        if scope == SCOPE_FLEET:
            covered = "the fleet"
        elif isinstance(scope, tuple):
            covered = ", ".join(scope)
        else:
            covered = str(aimed)
        return (
            f"not started on {printer_name}: the yes this job carries covers {covered}, "
            f"not {printer_name}. Queue it again aimed at a printer the person named, or "
            "have them name a wider scope."
        )
    window_id = str(record.get("window_id") or "")
    if window_id:
        try:
            from kiln.consent_windows import is_live

            live = is_live(window_id)
        except Exception:  # noqa: BLE001 — an unreadable store is no window
            live = False
        if not live:
            return (
                f"not started on {printer_name}: standing window {window_id} that this job "
                "was queued under has been revoked or has run out. A person can open a new "
                "one in the approval dialog of the next print, or with `kiln consent window`."
            )
    return None


def grant_from_record(
    record: dict[str, Any] | None, *, tool: str, file_name: str, printer_name: str | None,
) -> Clearance | None:
    """Re-grant a clearance a door stored earlier — a queued job, a paused
    pipeline — for the start that door is about to make now.  The record
    was written by a gate that passed; it is trusted for what it says and
    aimed at the file actually being started."""
    if not isinstance(record, dict):
        return None
    return grant(
        tool, file_name, printer_name,
        source=str(record.get("source") or SOURCE_QUEUED),
        door=str(record.get("door") or ""),
        scope=_scope_from_record(record.get("scope")),
        window_id=str(record.get("window_id") or ""),
        identity=str(record.get("identity") or ""),
    )


# ---------------------------------------------------------------------------
# Marking adapters
# ---------------------------------------------------------------------------


def require_signoff(adapter: Any) -> None:
    """Mark *adapter* as one whose starts must be cleared.  Never raises."""
    with contextlib.suppress(Exception):  # a slotted fake is not worth a failed registration
        setattr(adapter, _REQUIRED_ATTR, True)


def signoff_required(adapter: Any) -> bool:
    return bool(getattr(adapter, _REQUIRED_ATTR, False))


# ---------------------------------------------------------------------------
# The token half of the gate
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Verdict:
    """What the token gate decided, for the caller to word and audit."""

    ok: bool
    code: str = ""
    message: str = ""
    source: str = ""
    door: str = ""
    audit: str = ""


def not_confirmed_message(tool: str) -> str:
    """The refusal for a start with no preview on record: what to show,
    through which door, and how to hand the token in."""
    return (
        f"{tool} refuses to proceed without a preview confirmation. Show the user the "
        "print first — the inline 3D stage if this host draws one (show_on_stage(file_path) "
        "opens it on the file), else a viewer link "
        "(visualize_model(file_path, share_link=True) gives a viewer_url), else the PNG "
        "renders — then call issue_preview_token(file_path, door=<stage|url|png>) and "
        "pass the token as preview_token=<token>. To bypass (advanced / CI only), set "
        "KILN_SKIP_PREVIEW_GATE=1."
    )


def exemption_verdict(
    tool: str, file_name: str, printer_name: str | None, *, is_resume: bool = False,
) -> Verdict | None:
    """The two starts that need neither fact — the audited CI bypass and a
    resume continuation of a print that already had its yes — granted
    here, or ``None`` when this start is not one of them.  Decided before
    anything touches the token, so a refusal for want of a yes does not
    spend a token that is still good for the call that carries one."""
    if _bypassed():
        if not is_resume:
            logger.warning(
                "KILN_SKIP_PREVIEW_GATE is set — skipping mandatory preview confirmation for %s(%s).  "
                "Only do this in CI.",
                tool, file_name,
            )
        grant(tool, file_name, printer_name, source=SOURCE_CI_BYPASS)
        return Verdict(ok=True, source=SOURCE_CI_BYPASS, audit="" if is_resume else "preview_gate_skipped")
    if is_resume:
        grant(tool, file_name, printer_name, source=SOURCE_RESUME)
        return Verdict(ok=True, source=SOURCE_RESUME)
    return None


def token_verdict(
    tool: str,
    file_name: str,
    preview_token: str | None,
    *,
    printer_name: str | None = None,
    printer_id: str | None = None,
    is_resume: bool = False,
) -> Verdict:
    """Decide a start on its preview token, and grant the clearance on a yes.

    The order is the one ``_preview_gate_error`` has always had: the CI
    bypass, then a resume continuation, then the token.  This is the SAW
    half only; the gate in ``kiln.server`` pairs it with a person's yes.
    """
    exempt = exemption_verdict(tool, file_name, printer_name, is_resume=is_resume)
    if exempt is not None:
        return exempt
    if not preview_token:
        return Verdict(ok=False, code=CODE_NOT_CONFIRMED, message=not_confirmed_message(tool))
    door = ""
    try:
        from kiln.preview_gate import get_preview_gate

        ok, reason, token = get_preview_gate().validate_detail(
            preview_token, file_name, printer_id=printer_id or None,
        )
        if not ok:
            return Verdict(
                ok=False,
                code=CODE_TOKEN_INVALID,
                message=f"Preview token rejected: {reason}. Re-render the preview and issue a fresh token.",
            )
        door = getattr(token, "door", "") or ""
    except Exception as exc:  # noqa: BLE001 — a broken gate must not brick printing
        logger.warning("Preview gate validation failed: %s", exc)
    grant(tool, file_name, printer_name, source=SOURCE_PREVIEW_TOKEN, door=door)
    return Verdict(ok=True, source=SOURCE_PREVIEW_TOKEN, door=door, audit="preview_gate_satisfied")


# ---------------------------------------------------------------------------
# The backstop
# ---------------------------------------------------------------------------


def adapter_verdict(adapter: Any, file_name: str, kwargs: dict[str, Any] | None = None) -> dict[str, str] | None:
    """Called from ``PrinterAdapter.start_print``.  ``None`` to allow.

    Refuses only an adapter marked as requiring sign-off, for a file no
    clearance covers, that is not a resume continuation, when the CI
    bypass is off.  Everything unreadable allows: a backstop that could
    strand a legitimate print over its own bookkeeping is worse than none.
    """
    try:
        if not signoff_required(adapter):
            return None
        if _bypassed() or _is_resume(file_name, kwargs):
            return None
        printer_name = getattr(adapter, "_kiln_registered_name", None) or None
        if take(file_name, printer_name) is not None:
            return None
    except Exception:  # noqa: BLE001
        logger.debug("sign-off backstop could not decide; allowing", exc_info=True)
        return None
    name = os.path.basename(str(file_name or "")) or "this file"
    return {
        "reason": (
            f"{name} was not started: no preview sign-off reached the printer. Every door that "
            "starts a print clears it first — pass preview_token from issue_preview_token "
            "(CLI: --preview-token) so the person sees the stage, the link, or the renders "
            "before the machine moves."
        ),
        "code": CODE_NOT_CONFIRMED,
    }


def _reset_for_tests() -> None:
    _current.set(None)
