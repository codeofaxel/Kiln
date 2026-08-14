"""Who approved this print, and for what.

Kiln has always had a consent gate on printing, and it has always been the
same shape: the server hands the agent a token and trusts the agent to have
shown a human something first.  ``issue_preview_token`` proves a preview was
RENDERED for a file.  It cannot prove anyone saw it.  The agent is both the
one who asks and the one who reports the answer, so an agent that renders a
preview, pockets the token and shows nobody sails straight through.  The
fulfillment path has the same shape with two tokens instead of one.

MCP elicitation moves the asking to the client: the server poses the
question, the host draws it, the person answers, and the agent is not
holding the pen.  That is the half the token could never cover.

This module is the record of such an answer.  Three rules shape it:

* **A consent is about one print.**  A record that says only "approved"
  authorises the next print too.  It carries the machine and the file, and
  ``matches()`` refuses anything else — the same discipline the fulfillment
  tokens use when they bind to a quote and a shipping option.
* **It lives exactly as long as the call it belongs to.**  A ContextVar set
  by the tool-call wrapper and reset in its ``finally``, so consent cannot
  outlive the request that obtained it or leak into a later one.
* **"Not asked" is never "approved".**  Every path that fails to reach a
  human — no session, a host that cannot elicit, a transport error — leaves
  no record, and the caller falls back to the token gate it would have used
  before this existed.

What this deliberately does NOT claim: form-mode elicitation carries text
and a flat schema, so the question names the file, it does not show the
model.  The honest reading of an approval here is "a person was asked about
this file and said yes", not "a person looked at the geometry".  Showing the
render needs URL-mode elicitation, which is a different rung and not built.
"""

from __future__ import annotations

import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

#: How the approval was obtained, recorded on every print it authorises so
#: the audit trail says which rung of the ladder was actually used.
SOURCE_ELICITED = "user_elicited"
SOURCE_PREVIEW_TOKEN = "preview_token"
SOURCE_STANDING_OPT_IN = "standing_opt_in"
SOURCE_CI_BYPASS = "ci_bypass"


def _norm(value: str | None) -> str:
    """File names and printer names compare case- and path-insensitively.

    A tool may be handed ``/tmp/Benchy.3MF`` and hand the printer back
    ``benchy.3mf``; those are the same print, and a consent that refused
    the second would train users to approve twice.
    """
    if not value:
        return ""
    text = str(value).strip().replace("\\", "/")
    return (text.rsplit("/", 1)[-1] or text).lower()


@dataclass(frozen=True)
class PrintConsent:
    """One person's yes, to one print, on one machine."""

    tool: str
    file_name: str
    printer_name: str | None
    granted_at: float = field(default_factory=time.time)
    source: str = SOURCE_ELICITED

    def matches(self, *, file_name: str, printer_name: str | None) -> bool:
        """True when this consent covers the print now being started.

        The printer is compared only when the consent recorded one: a
        question that never named a machine cannot vouch for a particular
        one, so it is treated as consent to the file wherever the tool was
        already aimed, rather than silently authorising a second printer.
        """
        if _norm(file_name) != _norm(self.file_name):
            return False
        return not (
            self.printer_name and _norm(printer_name) != _norm(self.printer_name)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "file_name": self.file_name,
            "printer_name": self.printer_name,
            "granted_at": self.granted_at,
            "source": self.source,
        }


#: Consent for the tool call currently being served.  One writer (the
#: tool-call wrapper in ``kiln.server``) and one reader (the preview gate);
#: anything else reading this would be a second opinion on a question that
#: has one answer.
_current: ContextVar[PrintConsent | None] = ContextVar(
    "kiln_current_print_consent", default=None,
)


def set_consent(consent: PrintConsent | None):
    """Record the answer for this call.  Returns a token to reset with."""
    return _current.set(consent)


def reset_consent(token) -> None:
    """Drop the answer when its call ends.  Always in a ``finally``."""
    with _suppress():
        _current.reset(token)


def consent_for(*, file_name: str, printer_name: str | None) -> PrintConsent | None:
    """The live consent covering this print, or ``None``.

    ``None`` is the honest answer for both "nobody was asked" and "somebody
    was asked about a different print"; callers fall back to the token gate
    either way.
    """
    consent = _current.get()
    if consent is None:
        return None
    if not consent.matches(file_name=file_name, printer_name=printer_name):
        return None
    return consent


def describe_print_request(
    tool: str,
    *,
    file_name: str,
    printer_name: str | None,
    extra: dict[str, Any] | None = None,
) -> str:
    """The question a person is actually asked, in their words.

    Written to be answerable on its own: a tool name and a token tell a
    reader nothing about what their printer is about to do.  It names the
    file, the machine, and whatever facts the caller could supply, and it
    is explicit that Kiln is describing the job rather than showing it —
    the alternative is a dialog that implies a preview it cannot render.
    """
    where = f" on {printer_name}" if printer_name else " on the default printer"
    lines = [f"Start printing {file_name or 'this file'}{where}?"]
    for key, value in (extra or {}).items():
        if value in (None, "", []):
            continue
        label = str(key).replace("_", " ").strip().capitalize()
        lines.append(f"  {label}: {value}")
    lines.append("")
    lines.append(
        f"Requested by the {tool} tool. Kiln is describing this job, not "
        "showing it — approve only if you know what this file is."
    )
    return "\n".join(lines)


class _suppress:
    """Tiny local contextlib.suppress(Exception).

    Resetting a ContextVar token from a different context raises, and a
    cleanup path that can raise is a cleanup path that leaks the consent
    it was meant to clear.
    """

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type, exc, tb) -> bool:
        return exc_type is not None
