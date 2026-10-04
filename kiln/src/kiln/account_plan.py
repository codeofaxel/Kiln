"""The plan the person on this machine holds, read from their Kiln account.

A Kiln plan belongs to an account, and ``kiln signin`` connects a machine to
one.  This module answers "which plan is that?" for every gate in this
package: it reads the plan saved with the sign-in
(``~/.kiln/auth_tokens.json``) and keeps it current by asking the Kiln API
(``GET /api/auth/whoami``), so a person who upgrades is on their new plan
without signing in again and a plan that ended stops being honoured.

``kiln.licensing`` re-exports everything here, and that is the name gates
import.  Nothing in this module decides what a plan includes beyond the
printer caps below; it only reports the plan.

When the account is asked
-------------------------
* A saved plan older than :data:`PAID_PLAN_FRESH_FOR_S` (a paid plan) or
  :data:`FREE_PLAN_FRESH_FOR_S` (Free) is re-read on the next ask.
* A gate about to refuse asks again first (:func:`check_tier`,
  :func:`requires_tier`), at most once every
  :data:`RECHECK_BEFORE_REFUSAL_MIN_INTERVAL_S`, so someone who has just
  upgraded is never told to upgrade.

What an unanswered ask means
----------------------------
The saved plan stands.  A network fault is never a reason to take a paid
plan away, nor to grant one: the last answer the account gave holds for
:data:`PAID_PLAN_OFFLINE_GRACE_S`, and after that a paid plan reads as Free
until the account answers again.  A sign-in the server has refused grants
no paid plan at all (``kiln.auth_session.session_rejected``).
"""

from __future__ import annotations

import enum
import functools
import logging
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
    "BUSINESS_TIER_MAX_PRINTERS",
    "FREE_TIER_MAX_PRINTERS",
    "PRO_TIER_MAX_PRINTERS",
    "LicenseTier",
    "check_tier",
    "get_tier",
    "max_printers_for_tier",
    "refresh_plan",
    "requires_tier",
]


class LicenseTier(enum.Enum):
    """The plans Kiln sells, lowest first."""

    FREE = "free"
    PRO = "pro"
    BUSINESS = "business"
    ENTERPRISE = "enterprise"

    def _rank(self) -> int:
        return _TIER_ORDER[self]

    def __ge__(self, other: LicenseTier) -> bool:
        return self._rank() >= other._rank()

    def __gt__(self, other: LicenseTier) -> bool:
        return self._rank() > other._rank()

    def __le__(self, other: LicenseTier) -> bool:
        return self._rank() <= other._rank()

    def __lt__(self, other: LicenseTier) -> bool:
        return self._rank() < other._rank()


_TIER_ORDER: dict[LicenseTier, int] = {
    LicenseTier.FREE: 0,
    LicenseTier.PRO: 1,
    LicenseTier.BUSINESS: 2,
    LicenseTier.ENTERPRISE: 3,
}

#: How many printers a plan runs at once.  Enterprise has no cap.
FREE_TIER_MAX_PRINTERS = 1
PRO_TIER_MAX_PRINTERS = 1
BUSINESS_TIER_MAX_PRINTERS = 50

#: A saved paid plan is trusted this long before the account is asked again.
PAID_PLAN_FRESH_FOR_S = 24 * 3600.0
#: A saved Free plan is asked about sooner: the change worth noticing
#: quickly is an upgrade.
FREE_PLAN_FRESH_FOR_S = 30 * 60.0
#: A gate about to refuse asks the account first, no more often than this.
RECHECK_BEFORE_REFUSAL_MIN_INTERVAL_S = 60.0
#: With the account unreachable, the last paid answer holds this long.
PAID_PLAN_OFFLINE_GRACE_S = 7 * 24 * 3600.0
#: After an ask that got no answer, the next one waits this long, so an
#: offline machine does not pay a timeout on every gate.
_UNANSWERED_RETRY_AFTER_S = 10 * 60.0
_WHOAMI_ROUTE = "/api/auth/whoami"
_HTTP_TIMEOUT_S = 6.0

_lock = threading.Lock()
#: When this process last asked the account, and whether it answered.
_last_ask_at = 0.0
_last_ask_answered = True


def max_printers_for_tier(tier: object) -> int | None:
    """How many printers *tier* runs at once; ``None`` for no cap."""
    value = str(getattr(tier, "value", tier) or "").strip().lower()
    if value == "free":
        return FREE_TIER_MAX_PRINTERS
    if value == "pro":
        return PRO_TIER_MAX_PRINTERS
    if value == "business":
        return BUSINESS_TIER_MAX_PRINTERS
    if value == "enterprise":
        return None
    # A plan this release does not know reads as the lowest cap.
    return FREE_TIER_MAX_PRINTERS


def _as_tier(value: object) -> LicenseTier:
    """*value* as a plan; anything unrecognised is Free."""
    try:
        return LicenseTier(str(getattr(value, "value", value) or "").strip().lower())
    except ValueError:
        return LicenseTier.FREE


def _checked_at(stored: dict) -> float:
    """When the account last told this machine its plan (unix time)."""
    for key in ("plan_checked_at", "signed_in_at"):
        try:
            stamp = float(stored.get(key) or 0)
        except (TypeError, ValueError):
            stamp = 0.0
        if stamp > 0:
            return stamp
    return 0.0


def _saved_plan(stored: dict, now: float) -> LicenseTier:
    """The plan the saved sign-in grants right now, with no network."""
    from kiln.auth_session import session_rejected

    if not str(stored.get("access_token") or "").strip():
        return LicenseTier.FREE
    if session_rejected(stored):
        return LicenseTier.FREE
    plan = _as_tier(stored.get("tier"))
    if plan is LicenseTier.FREE:
        return plan
    # A sign-in that carries no stamp cannot be timed, so it is asked about
    # (see ``get_tier``) but not expired: only a known age runs out.
    checked = _checked_at(stored)
    if checked and now - checked > PAID_PLAN_OFFLINE_GRACE_S:
        return LicenseTier.FREE
    return plan


def _ask_account() -> dict | None:
    """The account's own answer about its plan, or ``None`` when there is none.

    ``None`` covers every way of not getting an answer: signed out, the
    network, a server fault, the server unable to read the plan.  A refused
    session is the session resolver's to record, not this function's.
    """
    from kiln.auth_session import _api_base, resolve_session_bearer

    session = resolve_session_bearer()
    if not session.token:
        return None
    try:
        import requests

        from kiln import __version__

        resp = requests.get(
            f"{_api_base()}{_WHOAMI_ROUTE}",
            headers={
                "Authorization": f"Bearer {session.token}",
                "User-Agent": f"kiln/{__version__}",
            },
            timeout=_HTTP_TIMEOUT_S,
        )
        if resp.status_code != 200:
            return None
        body = resp.json()
    except Exception:  # noqa: BLE001 — no answer is an answer this handles
        logger.debug("account plan: the account did not answer", exc_info=True)
        return None
    if not isinstance(body, dict) or not body.get("success"):
        return None
    if not str(body.get("tier") or "").strip():
        return None
    return body


def refresh_plan(*, min_interval_s: float = 0.0) -> LicenseTier:
    """Ask the account for its plan now, save the answer, and return the plan.

    Skipped (the saved plan is returned) when this process asked within
    *min_interval_s*, or asked recently and got no answer.  Never raises.
    """
    global _last_ask_at, _last_ask_answered

    from kiln.auth_session import _read_tokens, _write_tokens

    now = time.time()
    with _lock:
        since = now - _last_ask_at
        wait = min_interval_s if _last_ask_answered else _UNANSWERED_RETRY_AFTER_S
        if _last_ask_at and since < wait:
            return _saved_plan(_read_tokens(), now)
        _last_ask_at = now
        try:
            answer = _ask_account()
        except Exception:  # noqa: BLE001 — a plan read must never break a tool
            logger.debug("account plan: ask failed", exc_info=True)
            answer = None
        _last_ask_answered = answer is not None
        if answer is None:
            return _saved_plan(_read_tokens(), now)
        try:
            # Re-read: the session resolver may have rotated the tokens.
            stored = _read_tokens()
            if str(stored.get("access_token") or "").strip():
                stamp = int(time.time())
                stored["tier"] = _as_tier(answer.get("tier")).value
                stored["has_entitlement"] = bool(answer.get("has_entitlement"))
                stored["plan_checked_at"] = stamp
                # The stamp other readers of this file treat as "the plan
                # was current as of".
                stored["signed_in_at"] = stamp
                _write_tokens(stored)
        except Exception:  # noqa: BLE001 — unsaved is still answered
            logger.debug("account plan: could not save the answer", exc_info=True)
        return _as_tier(answer.get("tier"))


def get_tier() -> LicenseTier:
    """The plan the person on this machine holds.  Never raises.

    Reads the saved sign-in, and asks the account when that answer is old
    (see the module docstring).  Signed out, or on a sign-in the server
    refused, the plan is Free.
    """
    try:
        from kiln.auth_session import _read_tokens

        now = time.time()
        stored = _read_tokens()
        if not str(stored.get("access_token") or "").strip():
            return LicenseTier.FREE
        plan = _saved_plan(stored, now)
        saved_tier = _as_tier(stored.get("tier"))
        fresh_for = (
            FREE_PLAN_FRESH_FOR_S
            if saved_tier is LicenseTier.FREE
            else PAID_PLAN_FRESH_FOR_S
        )
        if now - _checked_at(stored) > fresh_for:
            return refresh_plan(min_interval_s=fresh_for)
        return plan
    except Exception:  # noqa: BLE001 — an unreadable plan is Free
        logger.debug("account plan: read failed", exc_info=True)
        return LicenseTier.FREE


def _covers(required: object) -> bool:
    """Whether the person's plan covers *required*, asking the account
    again before answering no."""
    needed = _as_tier(required)
    if get_tier() >= needed:
        return True
    return refresh_plan(min_interval_s=RECHECK_BEFORE_REFUSAL_MIN_INTERVAL_S) >= needed


def _refusal(subject: str, required: object) -> dict[str, Any]:
    """Why *subject* is refused, said for the state this machine is in.

    Four states, four different things to tell a person:

    * signed out: a subscriber connects this machine; anyone else sees the
      plans (the sign-in fields are for the agent);
    * a sign-in the server refused: sign back in.  Never an upsell, since
      the account may well hold the plan;
    * a paid plan Kiln has not confirmed for too long: get online;
    * signed in on a plan that does not include it: which account and plan
      Kiln sees, and where the plan that includes it is described.
    """
    from kiln.auth_session import _read_tokens, _rejected_verdict
    from kiln.tiers_and_terms import (
        plan_does_not_include_message,
        plan_unconfirmed_message,
        signin_hint_fields,
        tier_required_message,
        upgrade_link,
    )

    needed = _as_tier(required).value
    try:
        stored = _read_tokens()
    except Exception:  # noqa: BLE001
        stored = {}
    if not str(stored.get("access_token") or "").strip():
        return {
            "error": tier_required_message(subject, needed),
            "code": "TIER_REQUIRED",
            "upgrade_url": upgrade_link(subject if subject.isidentifier() else ""),
            **signin_hint_fields(),
        }
    rejected = _rejected_verdict(stored)
    if rejected is not None:
        return {
            "error": f"{subject} needs Kiln {needed.title()}. {rejected.detail}",
            "code": "SIGN_IN_AGAIN",
            **signin_hint_fields(),
        }
    saved = _as_tier(stored.get("tier"))
    if saved >= _as_tier(required):
        # The saved plan covers it and was refused all the same: the
        # account has not confirmed it within the grace period.
        return {
            "error": plan_unconfirmed_message(subject, saved.value),
            "code": "PLAN_UNCONFIRMED",
            "retryable": True,
        }
    return {
        "error": plan_does_not_include_message(
            subject, needed, saved.value, str(stored.get("email") or "")
        ),
        "code": "TIER_REQUIRED",
        "current_tier": saved.value,
        "upgrade_url": upgrade_link(subject if subject.isidentifier() else ""),
    }


def check_tier(required: object, *_args: Any, **_kwargs: Any) -> tuple[bool, str | None]:
    """``(True, None)`` when the person's plan covers *required*; otherwise
    ``(False, message)`` with the sentence to show them."""
    if _covers(required):
        return True, None
    return False, _refusal("This feature", required)["error"]


def requires_tier(tier: object):
    """Gate a tool on the person's plan covering *tier*.

    Below it, the tool is not run and the caller gets the refusal envelope:
    what was reached for, the plan that includes it, and how a subscriber
    connects this machine to their account.
    """
    needed = _as_tier(tier)

    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            if _covers(needed):
                return fn(*args, **kwargs)
            tool_name = fn.__name__
            # Counted so the heartbeat can say which tools people reach
            # for and are refused.  Never blocks the refusal.
            try:
                from kiln.daily_stats import record_tier_denial

                record_tier_denial(tool_name)
            except Exception:  # noqa: BLE001
                pass
            return {
                "success": False,
                "required_tier": needed.value,
                "tool": tool_name,
                "retryable": False,
                **_refusal(tool_name, needed),
            }

        return wrapper

    return decorator
