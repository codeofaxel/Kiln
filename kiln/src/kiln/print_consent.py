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
That is why the gate needs BOTH facts — a preview token (a door recorded
that the file was shown) and a consent (a person said go) — and neither
alone starts a print.

Where a yes can come from, graded by who is holding the pen:

* **Grade A** — the host drew the dialog (``user_elicited``), or the
  hosted server's account approval (``hosted_account_approval``, a hook
  kiln-pro fills; public Kiln ships no implementation).
* **Grade B** — a person at a terminal typed yes (``user_terminal``), or
  a person at a terminal opened a standing window (``standing_window``,
  see :mod:`kiln.consent_windows`).  A terminal is a fact about the
  process, not about a person, so on the hosted multi-tenant server
  grade B is not accepted.

A yes is for ONE print, on the printer it was aimed at.  A person may
name a wider scope — a list of printers, or the fleet — and ``matches()``
honours it; the default is the one printer.  A yes never becomes a
window: a window is a separate record a person opens on purpose.

Identity is recorded as what it is: ``os_user:<name>`` locally, the
hook's account on the hosted server, and nothing where nothing is known.
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
#: A person at a terminal was shown the preview (or, when nothing could be
#: rendered, the description of the job) and typed yes.
SOURCE_TERMINAL = "user_terminal"
#: A plate built from an approved input — copies of one approved mesh, one
#: object cut out of an approved plate — starts under the input's approval.
SOURCE_DERIVED = "derived"
#: A person at a terminal opened a standing window covering this printer.
SOURCE_WINDOW = "standing_window"
#: The hosted server's account approved this file (the hook below).
SOURCE_HOSTED_APPROVAL = "hosted_account_approval"

GRADE_A = "A"
GRADE_B = "B"

#: A consent that covers every printer.
SCOPE_FLEET = "fleet"
#: A consent that covers any file (a window is about printers, not files).
ANY_FILE = "*"


def grade_of(source: str) -> str | None:
    """``"A"`` when the host or the account held the pen, ``"B"`` when a
    terminal did, ``None`` for a source that is not a person's yes of its
    own (a derived plate rides its input's; a bypass is not a yes)."""
    if source in (SOURCE_ELICITED, SOURCE_HOSTED_APPROVAL):
        return GRADE_A
    if source in (SOURCE_TERMINAL, SOURCE_WINDOW):
        return GRADE_B
    return None


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
    """One person's yes, to one print, on the machine(s) they named."""

    tool: str
    file_name: str
    printer_name: str | None
    granted_at: float = field(default_factory=time.time)
    source: str = SOURCE_ELICITED
    #: ``None`` — the one printer the print is aimed at (``printer_name``);
    #: a tuple of names; or :data:`SCOPE_FLEET`.  A person names anything
    #: wider than the default.
    scope: tuple[str, ...] | str | None = None
    #: A yes with an end.  Only a window has one; a plain yes lives as
    #: long as its call and no longer.
    expires_at: float | None = None
    #: Who said it, labelled: ``os_user:adam``, ``account:acct_…``, or
    #: ``""`` when nothing is known.  Never guessed.
    identity: str = ""
    #: The door the person looked through, when the door that took the
    #: yes also judged the preview (the terminal path: ``url``/``png``
    #: from the token judge, or ``described`` when nothing could be
    #: drawn and the person was told so).  Empty for a dialog or a
    #: window, which cannot show anything; those need a token.
    door: str = ""
    #: The standing window this yes rests on, for the audit line.
    window_id: str = ""

    def scope_covers(self, printer_name: str | None) -> bool:
        if self.scope is None:
            # The default: the printer the print was aimed at.  A question
            # that never named a machine cannot vouch for a particular
            # one, so it is consent to the file wherever the tool was
            # already aimed, rather than silently authorising a second
            # printer.
            return not (self.printer_name and _norm(printer_name) != _norm(self.printer_name))
        if self.scope == SCOPE_FLEET:
            return True
        return _norm(printer_name) in {_norm(s) for s in self.scope}

    def matches(self, *, file_name: str, printer_name: str | None) -> bool:
        """True when this consent covers the print now being started."""
        if self.expires_at is not None and time.time() >= self.expires_at:
            return False
        if self.file_name != ANY_FILE and _norm(file_name) != _norm(self.file_name):
            return False
        return self.scope_covers(printer_name)

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "file_name": self.file_name,
            "printer_name": self.printer_name,
            "granted_at": self.granted_at,
            "source": self.source,
            "scope": list(self.scope) if isinstance(self.scope, tuple) else self.scope,
            "expires_at": self.expires_at,
            "identity": self.identity,
            "door": self.door,
            "window_id": self.window_id,
        }


# ---------------------------------------------------------------------------
# The hosted account approval — a hook, not an implementation
# ---------------------------------------------------------------------------

#: ``hook(*, file_name, file_hash, printer_name) -> PrintConsent | None``.
#: The hosted server registers one that asks its own store whether the
#: signed-in account approved these bytes; public Kiln ships none.  The
#: hook resolves the account itself from the request it is serving —
#: the gate does not know accounts, and must not guess one.
_hosted_hook = None


def register_hosted_approval_hook(hook) -> None:
    """Install (or, with ``None``, remove) the hosted account-approval hook."""
    global _hosted_hook  # noqa: PLW0603
    _hosted_hook = hook


def hosted_approval_hook():
    return _hosted_hook


def _ask_hosted_hook(*, file_name: str, printer_name: str | None) -> PrintConsent | None:
    hook = _hosted_hook
    if hook is None:
        return None
    try:
        file_hash = None
        try:
            from kiln.preview_gate import hash_file

            digest = hash_file(str(file_name))
            file_hash = None if digest.startswith("NO_FILE:") else digest
        except Exception:  # noqa: BLE001
            file_hash = None
        answer = hook(file_name=file_name, file_hash=file_hash, printer_name=printer_name)
    except Exception:  # noqa: BLE001 — a hook that fails has not said yes
        return None
    if not isinstance(answer, PrintConsent) or answer.source != SOURCE_HOSTED_APPROVAL:
        return None
    return answer


#: Consent for the tool call currently being served.  Two writers — the
#: tool-call wrapper in ``kiln.server`` (a person's yes) and
#: ``_covered_by_approval`` there (a plate derived from an approved input)
#: — and one reader (the preview gate); anything else reading this would be
#: a second opinion on a question that has one answer.
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


def consent_for(
    *, file_name: str, printer_name: str | None, aimed_at: str | None = None,
) -> PrintConsent | None:
    """The live consent covering this print, or ``None``.

    Three places a yes can be, asked in order: the answer recorded for
    this call (a dialog, a terminal, a derived plate); the hosted
    account-approval hook; a standing window a person opened at a
    terminal.  ``None`` is the honest answer for "nobody said go" and for
    "somebody said go to a different print" alike.

    *aimed_at* is the printer an unnamed call resolves to, for matching a
    window; the consent recorded for the call is matched on the name the
    call used, as before.
    """
    consent = _current.get()
    if consent is not None:
        # A yes was given for THIS call.  If it is about another print,
        # that is a mismatch to refuse, not a gap to fill from elsewhere.
        return consent if consent.matches(file_name=file_name, printer_name=printer_name) else None
    hosted = _ask_hosted_hook(file_name=file_name, printer_name=printer_name)
    if hosted is not None:
        return hosted if hosted.matches(file_name=file_name, printer_name=printer_name) else None
    try:
        from kiln import consent_windows

        window = consent_windows.covering(aimed_at or printer_name)
    except Exception:  # noqa: BLE001 — an unreadable store is no window
        window = None
    if window is None:
        return None
    return PrintConsent(
        tool="kiln consent window",
        file_name=file_name,
        printer_name=printer_name,
        granted_at=window.set_at,
        source=SOURCE_WINDOW,
        scope=window.scope,
        expires_at=window.until,
        identity=window.set_by,
        window_id=window.id,
    )


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


def _reset_for_tests() -> None:
    """Drop any consent left in this context.  The CLI's terminal yes is
    set for the life of the command and dropped with the process; a test
    runner that hosts many commands in one process needs this."""
    _current.set(None)


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
