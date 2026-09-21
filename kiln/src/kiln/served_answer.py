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
"""

from __future__ import annotations

import errno
import http.client
import json
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
#: shortly (its heartbeat table or its counter was down).  A cached answer
#: that was true before one of these is still true after it.
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


def classify_transport_error(exc: BaseException, *, host: str | None = None) -> Miss:
    """Why a request never got an HTTP answer: ``offline`` or ``unanswered``.

    Walks the exception and its ``reason`` / cause chain.  No DNS and no
    route are offline; a refused, reset or half-finished connection is a
    server that did not answer; a timeout is the one ambiguous case, so it
    asks whether this computer can reach *host* at all (:func:`_route_to`)
    before choosing.  Anything unrecognised reads as unanswered -- the
    claim that asks the person for the least.
    """
    cause = _cause_of(exc)
    if cause is None:
        cause = "unanswered" if _route_to(host) else "offline"
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
    the request just went to, never a third party."""
    name = _hostname(host)
    if not name:
        return True
    try:
        with socket.create_connection((name, 443), timeout=_PROBE_TIMEOUT_S):
            return True
    except OSError:
        return False


def _hostname(host: str | None) -> str:
    if not host:
        return ""
    from urllib.parse import urlsplit

    text = str(host).strip()
    if "://" not in text:
        text = f"https://{text}"
    try:
        return urlsplit(text).hostname or ""
    except ValueError:
        return ""


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


def envelope_for_transport(tool_name: str, exc: BaseException, *, host: str | None = None) -> dict[str, Any]:
    """The error envelope for a request that got no HTTP answer at all."""
    miss = classify_transport_error(exc, host=host)
    return {
        "status": "error",
        "success": False,
        "error": _tool_sentence(tool_name, miss),
        "code": "SERVER_UNREACHABLE",
        "tool": tool_name,
        **fields(miss),
        "transport": str(exc)[:_DETAIL_LIMIT],
    }


def envelope_for_http(tool_name: str, status: int, body: Any) -> dict[str, Any]:
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
    return {
        "status": "error",
        "success": False,
        "error": _tool_sentence(tool_name, miss),
        "code": miss.code,
        "tool": tool_name,
        "http_status": int(status),
        **fields(miss),
    }


def _tool_sentence(tool_name: str, miss: Miss) -> str:
    return sentence(
        miss,
        feature="servers",
        on_the_line=f"The {tool_name} tool runs on Kiln's servers, not on this computer",
        cannot="run it",
        wont="did nothing",
    )


# ---------------------------------------------------------------------------
# small text helpers
# ---------------------------------------------------------------------------


def _cap(text: str) -> str:
    text = text.strip()
    return text[:1].upper() + text[1:] if text else text


def _sentence_of(text: str) -> str:
    text = _cap(text)
    return text if text.endswith((".", "!", "?")) else f"{text}."


__all__ = [
    "CAUSES",
    "Miss",
    "SIGNED_OUT_CODES",
    "UNANSWERED_CODES",
    "classify_answer",
    "classify_transport_error",
    "clause",
    "envelope_for_http",
    "envelope_for_transport",
    "fields",
    "sentence",
]
