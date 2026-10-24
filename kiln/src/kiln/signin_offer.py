"""The one place a signed-out install is offered a free Kiln account.

An install that was never signed in meets moments where a free account would
have done more for the thing on the table: a printer held to generic limits,
a design made on this computer alone, a history tool that needs the account.
Each of those doors used to word, pace and count its own offer, or say
nothing at all.  This module owns all three, so every door only supplies the
one sentence that is true for its moment.

**Who is offered.**  Only an install that holds no session at all and no
licence key.  A lapsed session is a returning owner and is told to sign back
in by the door that met it (:mod:`kiln.served_answer`), never offered an
account; a signed-in install, Free or paid, is never offered one.  Nothing is
offered on Kiln's own hosted server, where every caller already has an
account.

**How often.**  Once per kind of value per install, kept on this computer in
``~/.kiln/signin_offers.json``.  A kind may be offered once more when the
same moment comes round again at least :data:`REOFFER_AFTER_S` later, and
never more than :data:`MAX_OFFERS_PER_KIND` times.  A person who said no on
the first day has a reason to hear it again only when a new instance of the
value arrives, not on a timer.  The printer kind is kept per printer model: a
second model is a new reason, a second machine of the same model is not.

**What it says.**  The door's sentence (what Kiln did, and what a free account
would do for this printer or design) and one instruction for the agent: call
the ``kiln_signin`` tool, which works in any chat, and drop it if the person
declines.  No tier, no price, no pricing link: signing in is free.

**Counted** as the kind's ``offered_*`` stage of the sign-in funnel
(:func:`kiln.daily_stats.record_signin_offer`), which also leaves the marker a
sign-in that finishes within a day reads (``completed_*``).

Nothing here gates anything, and nothing that prevents damage ever depends on
it: an offer rides beside an answer that is complete without it.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Each kind of value, and the funnel stage its offer is counted as.  The
#: printer kind keeps the word the setup nudge has always been counted under.
KINDS: dict[str, str] = {
    "printer": "offered_limits",
    "design": "offered_design",
    "history": "offered_history",
    "outcome": "offered_outcome",
}

#: A kind may be offered again only this long after its last offer.
REOFFER_AFTER_S = 14 * 24 * 3600
#: The most times one kind (one printer model, for the printer kind) is offered.
MAX_OFFERS_PER_KIND = 2

#: Where the answer carries an offer.
FIELD = "signin_offer"

#: The agent-addressed half of every offer.  Names the tool first because an
#: agent in a chat app has no terminal: in the thirty days to 2026-10-24 not
#: one agent started a sign-in, across every offer that named only the
#: terminal command.
AGENT_OFFER_HINT = (
    "Tell the user this once, in your own words, and offer to sign them in: "
    "call the `kiln_signin` tool and give them the link it returns (in a "
    "terminal, `kiln signin` does the same). Signing in is free; never call "
    "it a paid step. If they say no, drop it and carry on."
)

#: Tests point this at a file of their own; ``None`` is the person's own
#: ``~/.kiln``, resolved at call time so a moved HOME is honoured.
_LEDGER_PATH: Path | None = None
_LOCK = threading.Lock()


def _ledger_path() -> Path:
    return _LEDGER_PATH or Path.home() / ".kiln" / "signin_offers.json"


def _suppressed() -> bool:
    """True where an offer must neither be made nor written down: Kiln's own
    server (every caller there has an account, and one ledger would speak
    for every tenant), and a test, CI or container run against the real
    ledger.  Unsure means no offer."""
    try:
        from kiln.heartbeat import _is_ephemeral_runner, _is_hosted_multitenant

        if _is_hosted_multitenant():
            return True
        if _LEDGER_PATH is not None:
            return False
        if os.environ.get("PYTEST_CURRENT_TEST") or "pytest" in sys.modules:
            return True
        return _is_ephemeral_runner()
    except Exception:  # noqa: BLE001 -- unsure means no offer, never a crash
        return True


def never_signed_in() -> bool:
    """True when this install holds no session and no licence key.

    A stored refresh token with a dead access token is a lapsed session, not
    a stranger, so either token on disk answers False."""
    if os.environ.get("KILN_LICENSE_KEY", "").strip():
        return False
    try:
        from kiln.auth_session import _read_tokens

        held = _read_tokens()
    except Exception:  # noqa: BLE001 -- an unreadable session is not "no account"
        return False
    return not (held.get("access_token") or held.get("refresh_token"))


def _key(kind: str, subject: str) -> str:
    subject = str(subject or "").strip().lower()
    return f"{kind}:{subject}" if subject else kind


def _read_ledger() -> dict[str, Any]:
    try:
        raw = json.loads(_ledger_path().read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception:  # noqa: BLE001 -- a missing or broken ledger is an empty one
        return {}


def _write_ledger(data: dict[str, Any]) -> None:
    path = _ledger_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".tmp{os.getpid()}")
        tmp.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        logger.debug("sign-in offer ledger not written: %s", exc)


def _now() -> float:
    return time.time()


def _due(times: list[float], now: float) -> bool:
    if len(times) >= MAX_OFFERS_PER_KIND:
        return False
    return not times or now - max(times) >= REOFFER_AFTER_S


def claim(kind: str, subject: str = "") -> bool:
    """True when *kind* (for *subject*) may be offered now, and records it.

    False for an unknown kind, where offers are suppressed, on an install
    that holds any session, and when the pacing above says not yet.  Taking
    the claim is making the offer: the caller must say it.  Never raises.
    """
    if kind not in KINDS or _suppressed() or not never_signed_in():
        return False
    key = _key(kind, subject)
    now = _now()
    with _LOCK:
        ledger = _read_ledger()
        entry = ledger.get(key)
        times = [float(t) for t in (entry.get("times") or []) if isinstance(t, (int, float))] if isinstance(entry, dict) else []
        if not _due(times, now):
            return False
        ledger[key] = {"times": [*times, now]}
        _write_ledger(ledger)
    return True


def offered_kinds() -> list[str]:
    """The kinds this install has already been offered, for the agent's
    onboarding block, so it does not repeat one.  ``[]`` where offers are
    suppressed."""
    if _suppressed():
        return []
    return sorted({str(key).split(":", 1)[0] for key in _read_ledger() if str(key).split(":", 1)[0] in KINDS})


def offer(kind: str, message: str, subject: str = "") -> dict[str, Any] | None:
    """The offer block for this moment, or ``None`` when none is due.

    *message* is the door's own sentence for the person: what Kiln did, and
    what a free account would do for this printer or design.  Counted as the
    kind's ``offered_*`` stage.  Never raises.
    """
    text = str(message or "").strip()
    if not text or not claim(kind, subject):
        return None
    with contextlib.suppress(Exception):
        from kiln.daily_stats import record_signin_offer

        record_signin_offer(KINDS[kind], subject)
    return {
        "kind": kind,
        "message": text,
        "tool": "kiln_signin",
        "agent_hint": AGENT_OFFER_HINT,
    }


#: What a free account does for design history, in the pricing page's words
#: for the free band ("on the designs you keep").
HISTORY_OFFER = (
    "A free Kiln account keeps your designs' full history: branch, merge, "
    "cherry-pick and undo, on the designs you keep in your Kiln account."
)


def history_offer(tool: str) -> dict[str, Any] | None:
    """The one-time history offer for *tool*'s sign-in refusal, or ``None``.

    Only for a history tool a free account runs: the manifest's tier for it
    is free.  A paid one keeps the refusal that names its plan, so an answer
    never offers the account and a plan at once."""
    server = sys.modules.get("kiln.server")
    tiers = getattr(server, "_PRO_TOOL_TIERS", {}) if server is not None else {}
    if str(tiers.get(tool) or "free").strip().lower() != "free":
        return None
    return offer("history", HISTORY_OFFER)


def reset_for_tests(path: Path | None = None) -> None:
    """Point the ledger at *path* (``None``: the real one)."""
    global _LEDGER_PATH
    _LEDGER_PATH = path


__all__ = [
    "AGENT_OFFER_HINT",
    "FIELD",
    "HISTORY_OFFER",
    "KINDS",
    "MAX_OFFERS_PER_KIND",
    "REOFFER_AFTER_S",
    "claim",
    "history_offer",
    "never_signed_in",
    "offer",
    "offered_kinds",
]
