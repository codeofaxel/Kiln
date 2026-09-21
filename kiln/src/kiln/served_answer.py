"""One voice for every answer Kiln's servers did not give.

Public Kiln asks Kiln's servers for a handful of things it cannot work out
on its own machine: a tool from the paid-tool manifest, a printer's
head-motion plan, whether a filament-cutter blade is due.  When no answer
comes back, every door used to word the gap its own way -- a raw exception
string, "check the network", a code -- and none of them said which of four
different things had happened.  This module is the one place that decides
WHY there is no answer and the one place that words it, so a person reads
the same sentence whichever door they came through::

    <what is on the line>. Kiln can't <do the thing> right now because
    <cause>, so it <what it won't do>. <safe remedy>, or <fix> and try again.

Four causes, each with its own fix, and nothing else:

``offline``
    This computer has no route to Kiln's servers (no DNS, no network, no
    host).  Fix: reconnect to the internet.
``signed_out``
    No sign-in on this install, or one Kiln's servers no longer accept.
    Fix: sign in.
``unanswered``
    The request went out and nothing usable came back: a timeout on a live
    link, a gateway page, a server that itself said "try again shortly".
    Fix: wait a minute.
``refused``
    Kiln's servers answered, and the answer was no.  The server's own
    sentence is the fix, so it rides whole and nothing is added to it.

The sentence never carries a code and never a system word; the code and
the transport detail ride beside it (:func:`fields`) for anything that
branches on them.  A safety floor that leans on a served answer -- a head
motion, a placement check -- fails CLOSED whichever of the four it was,
and words its refusal through :func:`sentence` so the person learns which
of the four things to do.  Nothing here decides a tier, and nothing here
opens a door: it only says why one stayed shut.

**What is on the line for a manifest tool** is decided by its KIND
(:func:`kind_of_tool`): a verdict that was not given must never read as a
yes, a record that was not written must be told again, a copy that did
not happen leaves the local copy as it was.  The private side's manifest
generator resolves every served tool to a kind at ship time and refuses
to generate one it cannot place; the bundled manifest carries the kind,
and this side derives it the same way when a bundle predates the block.

**Every door to Kiln's servers** in this package is on :data:`HOSTED_DOORS`
with how it words a miss; a test pins the roster against the source, so a
new door cannot ship without deciding what an offline person is told.
"""

from __future__ import annotations

import errno
import http.client
import json
import re
import socket
import ssl
from dataclasses import dataclass
from typing import Any

CAUSES = ("offline", "signed_out", "unanswered", "refused")

#: What each cause reads as, and what fixes it.  ``{feature}`` is the
#: served thing in the person's words ("servers", "placement check").
_CAUSE_CLAUSE = {
    "offline": "this computer is offline",
    "signed_out": "Kiln is signed out",
    "unanswered": "Kiln's {feature} didn't answer",
    "refused": "Kiln's {feature} said no",
}
_FIX = {
    "offline": "reconnect to the internet",
    "signed_out": "sign in",
    "unanswered": "wait a minute",
    "refused": "",  # the server's own words carry the fix
}

#: Wire codes that mean "no sign-in the servers accept" -- this install's
#: own wall, an expired session, and the servers' own answer to a bearer
#: they could not turn into an account.
SIGNED_OUT_CODES = frozenset({
    "KILN_ACCOUNT_NOT_PAIRED", "KILN_SESSION_EXPIRED", "ACCOUNT_REQUIRED", "KILN_AUTH_REJECTED",
})
#: Wire codes that are not a ruling on anything: the transport failed, the
#: route was wrong, or the server said in so many words to try again
#: shortly.  A cached answer that was true before one of these is still
#: true after it.  A server that marks its own answer ``retryable`` needs
#: no entry here; the list covers answers that predate that mark.
UNANSWERED_CODES = frozenset({
    "SERVER_UNREACHABLE", "KILN_API_HTTP_ERROR", "NOT_SERVED_HERE", "MACHINE_UNVERIFIABLE", "CAP_UNAVAILABLE",
})

_OFFLINE_ERRNOS = frozenset({
    errno.ENETUNREACH, errno.ENETDOWN, errno.EHOSTUNREACH, errno.EHOSTDOWN, errno.EADDRNOTAVAIL,
})
_UNANSWERED_EXCEPTIONS = (
    ConnectionRefusedError, ConnectionResetError, ConnectionAbortedError, BrokenPipeError,
    http.client.RemoteDisconnected, http.client.BadStatusLine, http.client.IncompleteRead,
    json.JSONDecodeError, ssl.SSLError,
)
_TIMEOUTS = (TimeoutError, socket.timeout)
_DETAIL_LIMIT = 400
_PROBE_TIMEOUT_S = 2.0


@dataclass(frozen=True)
class Miss:
    """Why there is no answer: one of :data:`CAUSES`, the wire code that
    said so (beside the sentence, never in it), and the server's own words
    when it gave any."""

    cause: str
    code: str = ""
    detail: str = ""

    def __post_init__(self) -> None:
        if self.cause not in CAUSES:
            raise ValueError(f"not a cause: {self.cause!r}")


# ---------------------------------------------------------------------------
# what a manifest tool puts on the line
# ---------------------------------------------------------------------------

#: The kinds a served tool can be, each with what it puts on the line and
#: what "nothing happened" means for it.  ``{tool}`` is the tool's name.
#: The wording is the floor for that kind: a verdict not given is never a
#: yes; a record not written must be told again; a copy that did not run
#: leaves the local copy as it was.
KINDS: dict[str, tuple[str, str]] = {
    "verdict": ("The {tool} tool asks Kiln's servers for a verdict",
                "gave none (treat the answer as unknown, never as a yes)"),
    "made": ("The {tool} tool makes something on Kiln's servers, not on this computer",
             "made nothing"),
    "record": ("The {tool} tool records something on Kiln's servers",
               "recorded nothing (tell Kiln again once it can reach them)"),
    "read": ("The {tool} tool reads from Kiln's servers",
             "has no answer"),
    "sync": ("The {tool} tool copies between this computer and Kiln's servers",
             "copied nothing, and your local copy is as it was"),
    "action": ("The {tool} tool acts through Kiln's servers",
               "sent nothing to any printer and changed nothing"),
    "other": ("The {tool} tool runs on Kiln's servers, not on this computer",
              "did nothing"),
}

#: Name rules, first match wins.  A tool the rules do not place resolves
#: to ``""`` here and to ``other`` at run time; the private side's manifest
#: gate refuses to ship such a tool until someone places it (by name, in
#: its override table), so the run-time fallback is for a bundle that
#: predates the block, never a way to skip the question.
_KIND_RULES: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (kind, re.compile(rx))
    for kind, rx in (
        ("sync", r"^(cloud_|pull_reflog|push_reflog|push_palettes|sync_)"),
        ("verdict", r"^(check|verify|assess|may_i|predict|validate|classify|evaluate|analy[sz]e|explain|compare|"
                    r"diff_|detect|review|audit|food_safety|nozzle_drift|tolerance|estimate|compute|forecast|"
                    r"advise|recommend|suggest|answer|lookup|design_for|design_hole|describe|resolve_template|"
                    r"propose_sourcing|drying_advisor|.*_advisor$|"
                    r"get_(print_confidence|material_warnings|maintenance_prediction|recovery_recommendations|"
                    r"optimal_settings|auto_tuned_settings|calibration_freshness|calibration_health))"),
        ("made", r"^(generate|make|apply|smart_|batch_|auto_|build|render|preview|embed|export|regenerate|split|"
                 r"separate|paint|segment|keep_|recover_texture|derive|prepopulate|hollow|"
                 r"merge_(design|feature|decoration)|rebase|cherry_pick|iterate|rollback|undo|restore|promote|"
                 r"attach|import|add_|deboss|decorate|source_merge|wrap_gcode|change_|design_session|"
                 r"propose_design)"),
        ("record", r"^(record|set_|save|register|create|delete|rename|archive|unarchive|retire|annotate|reply|"
                   r"resolve_comment|reopen|dismiss|sign_|ingest|log_|submit|intake|manage|enable|bisect|remove|"
                   r"revoke|grant|assign|unassign|transfer|backfill|recategorize|prune|cancel_(speed|auto)|"
                   r"confirm_auto|report_issue|billing_delete|refund|reissue|start_subscription|open_billing|"
                   r"kiln_spend_caps|apply_role|setup_team|configure|update)"),
        ("read", r"^(list|get|find|search|show|inspect|read|visualize|visual_diff|fingerprint|tail|summarize|query|"
                 r"usage_summary|billing_|license_status|check_payment|preview_overage|cutter_wear_status|"
                 r"nozzle_wear_status|maintenance_due|abrasive_escalation|decoration_history|"
                 r"design_version_health|github_|best_|cross_|resolve_handle)"),
        ("action", r"^(fleet_|pause|resume|cancel|revert|plan_|request_|auto_recover|recover_power|"
                   r"resume_interrupted|emit_test|add_feature_during|decorate_during|deboss_during|"
                   r"apply_mid_print|plan_mid_print|preview_mid_print|revert_mid_print|start_|stop_|trigger|"
                   r"home|park|wipe|purge|load|unload|set_speed|set_nozzle|set_telemetry|set_operator|set_hooks|"
                   r"set_event|set_approval|remove_approval|ams_)"),
    )
)


def kind_of_tool(name: str, category: str | None = None) -> str:
    """The kind *name* resolves to by rule, or ``""`` when no rule places it.

    Category first where a category is one kind through and through
    (``cloud_sync``), then the name rules in :data:`_KIND_RULES` order --
    a ``check_*`` is a verdict before it is anything else.
    """
    name = str(name or "").strip()
    if not name:
        return ""
    if category == "cloud_sync":
        return "sync"
    for kind, rx in _KIND_RULES:
        if rx.match(name):
            return kind
    return ""


def story_for_tool(name: str, kind: str | None = None, category: str | None = None) -> tuple[str, str]:
    """``(on_the_line, wont)`` for a manifest tool: the manifest's kind when
    the bundle carries one, else the kind derived by name, else ``other``."""
    resolved = kind if kind in KINDS else (kind_of_tool(name, category) or "other")
    on_the_line, wont = KINDS[resolved]
    return on_the_line.format(tool=name), wont


# ---------------------------------------------------------------------------
# deciding why
# ---------------------------------------------------------------------------


def classify_answer(answer: Any) -> Miss | None:
    """The :class:`Miss` an envelope from Kiln's servers amounts to, or
    ``None`` when it is an answer.

    Reads the envelope shapes the served door produces and passes through:
    ``{"status": "error", "code", "error"}``, ``{"success": False, "error":
    {"code", "message"}}``, FastAPI's bare ``{"detail": ...}``, and a
    ``why`` already decided upstream.  An error with no code is not a
    ruling; neither is one the server marked ``retryable``.
    """
    if not isinstance(answer, dict):
        return Miss("unanswered", detail="not an answer")
    error = answer.get("error")
    code = str(answer.get("code") or "")
    text = ""
    if isinstance(error, dict):
        code = code or str(error.get("code") or "")
        text = str(error.get("message") or error.get("error") or "")
    elif error:
        text = str(error)
    if not text:
        text = str(answer.get("message") or answer.get("detail") or "")
    bare_detail = "detail" in answer and not any(k in answer for k in ("status", "success", "error", "code", "plan"))
    is_error = answer.get("status") == "error" or answer.get("success") is False or bool(error) or bare_detail
    if not is_error:
        return None
    text = text.strip()[:_DETAIL_LIMIT]
    why = answer.get("why")
    if why in CAUSES:
        return Miss(why, code, text)
    if code in SIGNED_OUT_CODES:
        return Miss("signed_out", code, text)
    if bare_detail and not code:
        return Miss("refused", code, text)  # the server spoke, in FastAPI's own shape, and said no
    if not code or code in UNANSWERED_CODES or answer.get("retryable") is True:
        return Miss("unanswered", code, text)
    return Miss("refused", code, text)


def classify_transport_error(exc: BaseException, *, host: str | None = None, probe: bool = True) -> Miss:
    """Why a request never got an HTTP answer: ``offline`` or ``unanswered``.

    Walks the exception and its ``reason`` / cause chain.  No DNS and no
    route are offline; a refused, reset or half-finished connection is a
    server that did not answer; a timeout is the one ambiguous case, so it
    asks whether this computer can reach *host* at all (:func:`_route_to`)
    before choosing -- unless *probe* is off, when a door that must never
    open a second socket (a preview link) settles for unanswered.  Anything
    unrecognised reads as unanswered -- the claim that asks the person for
    the least.
    """
    cause = _cause_of(exc)
    if cause is None:
        cause = "unanswered" if (not probe or _route_to(host)) else "offline"
    return Miss(cause, code="SERVER_UNREACHABLE", detail=str(exc)[:_DETAIL_LIMIT])


def _cause_of(exc: BaseException | None) -> str | None:
    """``offline`` / ``unanswered`` when the chain says so; ``None`` when a
    timeout leaves it open."""
    timeout_seen = False
    hops = 0
    while isinstance(exc, BaseException) and hops < 8:
        if isinstance(exc, socket.gaierror) or getattr(exc, "errno", None) in _OFFLINE_ERRNOS:
            return "offline"
        if isinstance(exc, _UNANSWERED_EXCEPTIONS):
            return "unanswered"
        if isinstance(exc, _TIMEOUTS):
            timeout_seen = True
        nxt = getattr(exc, "reason", None)
        if not isinstance(nxt, BaseException):
            nxt = exc.__cause__ or exc.__context__
        exc = nxt
        hops += 1
    return None if timeout_seen else "unanswered"


def _route_to(host: str | None) -> bool:
    """Can this computer resolve *host* and open a socket to it?  Asked only
    after a timeout, to tell a slow server from a dead link; the same host
    and port the request just went to, never a third party."""
    name, port = _host_and_port(host)
    if not name:
        return True
    try:
        with socket.create_connection((name, port), timeout=_PROBE_TIMEOUT_S):
            return True
    except OSError:
        return False


def _host_and_port(host: str | None) -> tuple[str, int]:
    if not host:
        return "", 443
    from urllib.parse import urlsplit

    text = str(host).strip()
    if "://" not in text:
        text = f"https://{text}"
    try:
        parts = urlsplit(text)
        name = parts.hostname or ""
        port = parts.port or (80 if parts.scheme == "http" else 443)
    except ValueError:
        return "", 443
    return name, int(port)


# ---------------------------------------------------------------------------
# wording it
# ---------------------------------------------------------------------------


def sentence(
    miss: Miss,
    *,
    feature: str,
    on_the_line: str,
    cannot: str,
    wont: str,
    safe_remedy: str = "",
    then: str = "try again",
) -> str:
    """The one sentence shape, filled in for *miss*.

    *on_the_line* is what is at stake in the person's words ("Homing moves
    bambu_a1's head and presses the nozzle onto the plate"); *cannot* what
    Kiln can't do ("get bambu_a1's own homing sequence"); *wont* what it
    therefore won't do ("won't home bambu_a1"); *safe_remedy* the thing to
    do instead, when there is one; *then* what the fix leads to ("try
    again", "run the pre-flight again").  A refusal appends the server's
    own words whole and offers no fix of its own, because the server named
    it.
    """
    cause = _CAUSE_CLAUSE[miss.cause].format(feature=feature)
    fix = _FIX[miss.cause]
    if miss.cause == "refused" and not miss.detail:
        cause += " and gave no reason"
        fix = "wait a minute"
    out = f"{_cap(on_the_line).rstrip('. ')}. Kiln can't {cannot.strip()} right now because {cause}, so it {wont.strip().rstrip('.')}."
    if miss.cause == "refused" and miss.detail:
        out += f" {_sentence_of(miss.detail)}"
    remedies = [r for r in (safe_remedy.strip().rstrip("."), f"{fix} and {then.strip().rstrip('.')}" if fix else "") if r]
    if remedies:
        out += f" {_cap(', or '.join(remedies))}."
    return out


def clause(miss: Miss, *, feature: str, cannot: str, then: str = "try again") -> str:
    """The same content as a clause that follows "because": no capital, no
    full stop, the cause in brackets and the fix after a semicolon.  For a
    door whose answer already has a sentence and only needs the why."""
    cause = _CAUSE_CLAUSE[miss.cause].format(feature=feature)
    fix = _FIX[miss.cause]
    if miss.cause == "refused":
        cause = f"{cause}: {miss.detail.rstrip('. ')}" if miss.detail else f"{cause} and gave no reason"
        fix = "" if miss.detail else "wait a minute"
    out = f"Kiln can't {cannot.strip()} right now ({cause})"
    if fix:
        out += f"; {fix} and {then.strip().rstrip('.')}"
    return out


def fields(miss: Miss | None) -> dict[str, str]:
    """The machine-readable twin of the sentence, to splat beside it."""
    if miss is None:
        return {}
    return {"why": miss.cause, "why_code": miss.code, "why_detail": miss.detail}


# ---------------------------------------------------------------------------
# the served door's own envelopes
# ---------------------------------------------------------------------------


def envelope_for_transport(
    tool_name: str, exc: BaseException, *, host: str | None = None, kind: str | None = None,
) -> dict[str, Any]:
    """The error envelope for a request that got no HTTP answer at all."""
    miss = classify_transport_error(exc, host=host)
    return {
        "status": "error",
        "success": False,
        "error": _tool_sentence(tool_name, miss, kind),
        "code": "SERVER_UNREACHABLE",
        "tool": tool_name,
        **fields(miss),
        "transport": str(exc)[:_DETAIL_LIMIT],
    }


def envelope_for_http(tool_name: str, status: int, body: Any, *, kind: str | None = None) -> dict[str, Any]:
    """The envelope for an HTTP error answer.

    A body that already is an error envelope (a code and a sentence of the
    server's own) comes back untouched: the server built it for this call
    and this side has no better words.  FastAPI's bare ``{"detail": ...}``
    -- which used to come back as-is, with no field that marked it an
    error -- and a body that is not JSON at all are worded here.
    """
    if isinstance(body, dict) and any(k in body for k in ("status", "success", "error", "code")):
        return body
    detail = ""
    if isinstance(body, dict):
        raw = body.get("detail")
        detail = raw if isinstance(raw, str) else (json.dumps(raw) if raw is not None else "")
    if status == 401:
        miss = Miss("signed_out", "KILN_AUTH_REJECTED", detail)
    elif status >= 500:
        miss = Miss("unanswered", "KILN_API_HTTP_ERROR", detail)
    elif status == 429:
        miss = Miss("refused", "ALLOWANCE_USED_UP", detail)
    else:
        miss = Miss("refused", "KILN_API_HTTP_ERROR", detail)
    out: dict[str, Any] = {
        "status": "error",
        "success": False,
        "error": _tool_sentence(tool_name, miss, kind),
        "code": miss.code,
        "tool": tool_name,
        "http_status": int(status),
        **fields(miss),
    }
    if miss.cause == "signed_out":
        # The same agent-addressed fields every other sign-in refusal carries.
        from kiln.tiers_and_terms import signin_hint_fields

        out.update(signin_hint_fields())
    return out


def _tool_sentence(tool_name: str, miss: Miss, kind: str | None = None) -> str:
    on_the_line, wont = story_for_tool(tool_name, kind)
    return sentence(miss, feature="servers", on_the_line=on_the_line, cannot="run it", wont=wont)


# ---------------------------------------------------------------------------
# the roster of doors to Kiln's servers
# ---------------------------------------------------------------------------

#: Every module in this package that reaches Kiln's servers, and how it
#: words a miss.  ``served_answer``: through :func:`sentence` / :func:`clause`
#: (a feature a person asked for).  ``own_vocabulary``: a door whose reasons
#: are its own sentences already, reviewed on their own (the sign-in doors
#: themselves; the browser stage link, whose fallback is the local stage).
#: ``infrastructure``: best-effort mirrors with a local record as the floor,
#: where no person is waiting on the answer.  A module that reaches the
#: servers and is not here fails ``tests/test_hosted_doors_roster.py`` --
#: the question "what is an offline person told?" is answered at the
#: moment the door is added, not found later.
HOSTED_DOORS: dict[str, tuple[str, str]] = {
    "kiln.server": ("served_answer", "the paid-tool manifest stubs and the served door every bridge uses"),
    "kiln._pro_motion_bridge": ("served_answer", "head-motion plans; a miss is worded by the Bambu doors"),
    "kiln._pro_cutter_bridge": ("served_answer", "blade status for the pre-flight; cut reports are fire-and-forget"),
    "kiln._pro_nozzle_bridge": ("served_answer", "the pre-print nozzle-life verdict for the pre-flight, a start and a slice"),
    "kiln._pro_placement_bridge": ("served_answer", "the plate-clearance verdict for slicing beside a part left on the plate"),
    "kiln.stage_link": ("served_answer", "browser stage links; the four served causes word through here, the local stage is the floor"),
    "kiln.stage_cache": ("infrastructure", "the stage document upload behind stage_link; its refusals surface through stage_link"),
    "kiln.monitor_twin": ("infrastructure", "print twins pushed best-effort; the local monitor is the floor"),
    "kiln.bridge_client": ("own_vocabulary", "opt-in web control relay; reconnects and reports its state file"),
    "kiln.community_sync": ("infrastructure", "community aggregates; a generation never claims them, local knowledge is the floor"),
    "kiln.terms": ("infrastructure", "terms acceptance mirrored to the account; the local record is the only gate"),
    "kiln.usage_ledger": ("infrastructure", "usage counts flushed later; nothing waits on it"),
    "kiln.heartbeat": ("infrastructure", "the daily install heartbeat; never user-facing"),
    "kiln.auth_session": ("own_vocabulary", "token refresh; its states word the sign-in doors"),
    "kiln.cli.auth_commands": ("own_vocabulary", "kiln signin / pair / signout: the sign-in doors themselves"),
    "kiln.cli.main": ("own_vocabulary", "kiln register: a sign-in door"),
    "kiln.cli.spend_caps_commands": ("own_vocabulary", "spend caps CLI: a terminal door with its own errors"),
    "kiln.plugins.tier_diagnostic_tools": ("infrastructure", "names the host in prose only; reads local state"),
    "kiln.runtime_env": ("infrastructure", "names the host in prose only"),
    "kiln.errors": ("infrastructure", "names the host in prose only"),
    "kiln.api_device": ("infrastructure", "device headers for the served door; sends nothing itself"),
}


# ---------------------------------------------------------------------------
# small text helpers
# ---------------------------------------------------------------------------

_IDENTIFIER_FIRST = re.compile(r"^[a-z0-9]+_[a-z0-9_]*\b")


def _cap(text: str) -> str:
    """Capitalise the first letter, unless the sentence opens with an
    identifier (``printer_id must ...``), which keeps its spelling."""
    text = text.strip()
    if not text or _IDENTIFIER_FIRST.match(text):
        return text
    return text[:1].upper() + text[1:]


def _sentence_of(text: str) -> str:
    text = _cap(text)
    return text if text.endswith((".", "!", "?")) else f"{text}."


__all__ = [
    "CAUSES",
    "HOSTED_DOORS",
    "KINDS",
    "Miss",
    "SIGNED_OUT_CODES",
    "UNANSWERED_CODES",
    "classify_answer",
    "classify_transport_error",
    "clause",
    "envelope_for_http",
    "envelope_for_transport",
    "fields",
    "kind_of_tool",
    "sentence",
    "story_for_tool",
]
