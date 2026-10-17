"""Tier diagnostic tool — answer "what tier am I on and why".

Single MCP tool, optimized for agent discovery when a user asks tier-,
subscription-, or paywall-confusion questions in plain English ("why is
this asking me to pay", "what's my plan", "do I have Pro", etc.).

Walks the live tier-resolution chain (env var → license file → OAuth
session → cached entitlement → free fallback) and returns BOTH a
structured response (so the agent can branch on it) AND a plain-English
``agent_summary`` line the agent can paste straight to the user.

Lives in public Kiln so EVERY user — free or paid — can call it.
Lazy-imports the kiln-pro tier resolver when present; gracefully reports
"free (kiln-pro not installed)" when not.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

_logger = logging.getLogger(__name__)


_TIER_RANK = {"free": 0, "pro": 1, "business": 2, "enterprise": 3}


def _walk_resolution_chain() -> dict[str, Any]:
    """Walk the actual tier-resolution chain and report each step.

    Returns a dict with ``effective_tier``, ``resolution_chain`` (list of
    steps with whether each matched and what they contributed), and
    ``agent_summary`` (one-liner the agent can paste to the user).
    """
    chain: list[dict[str, Any]] = []
    effective_tier = "free"
    matched_source = "default"
    matched_detail = "no license, no OAuth session, no cached entitlement"

    # Try kiln-pro for the full resolution chain.  When kiln-pro isn't
    # installed, the user is necessarily on free tier (no paid features
    # available regardless of intent).
    try:
        from kiln_pro.enterprise.licensing import (
            _caller_tier_override,
            get_license_manager,
        )

        # Step 1: per-request override (only set inside the REST tool dispatcher)
        override = _caller_tier_override.get()
        if override is not None:
            tier_str = override.value if hasattr(override, "value") else str(override)
            chain.append({
                "source": "request_override",
                "matched": True,
                "tier": tier_str,
                "detail": "Set by REST tool dispatcher for this request only",
            })
            effective_tier = tier_str
            matched_source = "request_override"
            matched_detail = "REST request scope override"
            return _build_response(effective_tier, chain, matched_source, matched_detail)
        chain.append({
            "source": "request_override",
            "matched": False,
            "detail": "No per-request override (typical for CLI / MCP / local use)",
        })

        # Step 2: KILN_LICENSE_KEY env var
        env_key = (os.environ.get("KILN_LICENSE_KEY") or "").strip()
        if env_key:
            chain.append({
                "source": "license_key_env",
                "matched": True,
                "tier": "pending_validation",
                "detail": f"KILN_LICENSE_KEY env var is set ({len(env_key)} chars). Tier validated against signed key.",
            })
        else:
            chain.append({
                "source": "license_key_env",
                "matched": False,
                "detail": "KILN_LICENSE_KEY env var not set",
            })

        # Step 3: license file
        license_path = Path("~/.kiln/license").expanduser()
        if license_path.is_file():
            chain.append({
                "source": "license_key_file",
                "matched": True,
                "tier": "pending_validation",
                "detail": f"License file present at {license_path}",
            })
        else:
            chain.append({
                "source": "license_key_file",
                "matched": False,
                "detail": f"No license file at {license_path}",
            })

        # Step 4: OAuth session (kiln signin).  The file the session resolver
        # and the licence reader both read, and the resolver's verdict on it:
        # a session this machine can no longer renew changes what the tier
        # below is good for, and the answer has to say so.
        from kiln.auth_session import _tokens_path, resolve_session_bearer

        auth_path = _tokens_path()
        session = None
        if auth_path.is_file():
            try:
                session = resolve_session_bearer()
            except Exception:  # noqa: BLE001 — a diagnostic must not break
                session = None
            lapsed = session is not None and not session.token
            chain.append({
                "source": "oauth_session",
                "matched": True,
                "detail": (
                    f"OAuth session present at {auth_path} — bound to your kiln3d.com account"
                    + ("; it has expired and could not be renewed" if lapsed else "")
                ),
            })
        else:
            chain.append({
                "source": "oauth_session",
                "matched": False,
                # A chain entry states what IS, not what to type — the fix
                # travels once, in the response's agent-addressed field.
                "detail": f"No OAuth session at {auth_path}; this machine isn't bound to a kiln3d.com account",
            })

        # Step 5: ask the actual LicenseManager what the resolved tier is.
        # This is the canonical answer; everything above is just diagnostics
        # showing which inputs the manager had to work with.
        mgr = get_license_manager()
        try:
            resolved = mgr.get_tier()
            tier_str = resolved.value if hasattr(resolved, "value") else str(resolved)
            effective_tier = tier_str.lower()
        except Exception as exc:
            tier_str = "free"
            effective_tier = "free"
            chain.append({
                "source": "license_manager_resolve",
                "matched": False,
                "detail": f"LicenseManager.get_tier() raised: {exc}; falling back to free",
            })
        else:
            chain.append({
                "source": "license_manager_resolve",
                "matched": True,
                "tier": tier_str,
                "detail": "LicenseManager combined the inputs above into the effective tier",
            })
            matched_source = "license_manager_resolve"
            matched_detail = f"resolved by LicenseManager to {tier_str}"

        # The session speaks for any tier an operator's key did not supply:
        # the one it granted, or the free one it left behind when it ended.
        # A key needs no sign-in and reports exactly what it did before.
        try:
            from_key = mgr.get_info().source in ("env", "file")
        except Exception:  # noqa: BLE001
            from_key = False
        return _build_response(
            effective_tier, chain, matched_source, matched_detail,
            session=None if from_key else session,
        )

    except ImportError:
        # A plain install: the plan is the signed-in account's.
        return _account_plan_chain()


def _account_plan_chain() -> dict[str, Any]:
    """The answer on an install without kiln-pro: the plan of the Kiln
    account this machine is signed in to, asked of the account NOW so that
    "I just paid and it still says Free" is settled by this call."""
    from kiln.account_plan import refresh_plan
    from kiln.auth_session import _tokens_path, resolve_session_bearer

    chain: list[dict[str, Any]] = []
    auth_path = _tokens_path()
    session = None
    if not auth_path.is_file():
        chain.append({
            "source": "oauth_session",
            "matched": False,
            "detail": f"No sign-in at {auth_path}; this machine isn't connected to a Kiln account",
        })
        return _build_response(
            "free", chain,
            matched_source="default",
            matched_detail="not signed in to a Kiln account",
        )
    try:
        session = resolve_session_bearer()
    except Exception:  # noqa: BLE001 — a diagnostic must not break
        session = None
    lapsed = session is not None and not session.token
    chain.append({
        "source": "oauth_session",
        "matched": True,
        "detail": (
            f"Sign-in present at {auth_path}"
            + ("; it has expired and could not be renewed" if lapsed else "")
        ),
    })
    try:
        plan = refresh_plan().value
    except Exception as exc:  # noqa: BLE001
        plan = "free"
        chain.append({
            "source": "account_plan",
            "matched": False,
            "detail": f"The account's plan could not be read: {exc}",
        })
    else:
        chain.append({
            "source": "account_plan",
            "matched": True,
            "tier": plan,
            "detail": "The plan of the Kiln account this machine is signed in to",
        })
    return _build_response(
        plan, chain,
        matched_source="account_plan",
        matched_detail=f"the signed-in account is on {plan}",
        session=session,
    )


def _build_response(
    effective_tier: str,
    chain: list[dict[str, Any]],
    matched_source: str,
    matched_detail: str,
    *,
    session: Any = None,
) -> dict[str, Any]:
    """Produce the structured response + agent-friendly one-liner.

    *session* is the sign-in session's :class:`~kiln.auth_session.SessionBearer`
    when the tier came from it.  One that can no longer be renewed leads the
    summary, in the words ``license_status`` and ``get_started`` use for it.
    """
    tier_label = effective_tier.title() if effective_tier else "Free"
    rank = _TIER_RANK.get(effective_tier.lower(), 0)
    # Only the free branch is actionable by signing in — a resolved Pro or
    # Business seat has nothing to connect, and a hint offered there reads as
    # though something were still wrong.
    hint_fields: dict[str, Any] = {}

    if effective_tier.lower() == "free":
        from kiln.tiers_and_terms import (
            ALREADY_SUBSCRIBED_LINE,
            signin_hint_fields,
        )

        hint_fields = signin_hint_fields()
        # Written in second person, so this is the USER's half however it is
        # labelled: it says what's true and what it would take, and the
        # command travels in agent_hint alongside it.
        agent_summary = (
            f"You're on the Free tier. Why: {matched_detail}. "
            f"{ALREADY_SUBSCRIBED_LINE} "
            "Free-tier features still work via api.kiln3d.com once you're "
            "signed in. See what the paid tiers include at kiln3d.com/pricing"
        )
    elif effective_tier.lower() == "pro":
        agent_summary = (
            f"You're on the Pro tier. Source: {matched_detail}. "
            "All Pro+ features are unlocked, including cloud sync, design versioning, mid-print modification, "
            "and the texture engine. Pro stays at 1 printer — fleet starts at Business."
        )
    elif effective_tier.lower() == "business":
        agent_summary = (
            f"You're on the Business tier. Source: {matched_detail}. "
            "Pro features plus team collaboration (PRs, approval gates, cross-org transfer, and "
            "the fleet: 3 printers and 3 seats included, metered to caps of 50 and 10) are all unlocked."
        )
    elif effective_tier.lower() == "enterprise":
        agent_summary = (
            f"You're on the Enterprise tier. Source: {matched_detail}. "
            "Everything is unlocked: SSO/RBAC, audit-trail export, lockable safety profiles, "
            "unlimited printers, 99.9% uptime SLA."
        )
    else:
        agent_summary = (
            f"Effective tier: {tier_label}. Source: {matched_detail}."
        )

    extra: dict[str, Any] = {}
    if session is not None:
        extra["session_state"] = session.state
    if session is not None and not session.token:
        from kiln.tiers_and_terms import session_expired_message, signin_hint_fields

        extra["action_required"] = session.detail or session_expired_message()
        hint_fields = signin_hint_fields()
        if effective_tier.lower() != "free":
            agent_summary = (
                f"{extra['action_required']} Your {tier_label} plan is still on "
                "file: features that run on this machine keep working, but "
                "anything that goes through Kiln's servers (browser 3D links, "
                "the cloud library, hosted tools) needs you to sign back in first."
            )
        else:
            # A session that ended stops granting its plan here, so this
            # machine reads free.  Its owner hears what they had, and that
            # signing back in brings it back: not an invitation to upgrade.
            from kiln.auth_session import _read_tokens

            plan = str(_read_tokens().get("tier") or "").lower()
            if plan and plan != "free":
                agent_summary = (
                    f"{extra['action_required']} Until then this machine runs "
                    f"Kiln Free; your {plan.title()} plan comes back the moment "
                    "you sign back in."
                )

    return {
        "success": True,
        "effective_tier": effective_tier.lower(),
        "tier_label": tier_label,
        "tier_rank": rank,
        "resolution_chain": chain,
        "matched_source": matched_source,
        "agent_summary": agent_summary,
        "pricing_url": "https://kiln3d.com/pricing",
        **extra,
        **hint_fields,
    }


class _TierDiagnosticPlugin:
    """Tier diagnostic — answer 'what tier am I on, and why'.

    Tools:
        - check_my_tier
    """

    @property
    def name(self) -> str:
        return "tier_diagnostic_tools"

    @property
    def description(self) -> str:
        return "Self-service tier diagnostic — answer plan/subscription/paywall questions"

    def register(self, mcp: Any) -> None:
        """Register the tier-diagnostic tool with the MCP server."""

        @mcp.tool()
        def check_my_tier() -> dict:
            """Check the user's current Kiln subscription tier (Free / Pro / Business / Enterprise) and explain WHY they're on it.

            Use this whenever the user asks any tier / plan / subscription /
            paywall / access question — for example: "what tier am I on",
            "why does it say I need Pro", "do I have to pay for this",
            "what's my plan", "why isn't this Pro feature working", "did
            my subscription not activate", "what's the difference between
            Free and Pro", "I just paid but I'm still seeing free tier",
            "can I use the texture engine", "do I have access to fleet
            management", "what unlocks at Business", "how do I upgrade".

            Walks the live tier-resolution chain on the user's machine
            (KILN_LICENSE_KEY env var → ~/.kiln/license file → OAuth
            session at ~/.kiln/auth_tokens.json → cached entitlement →
            free-tier fallback) and reports:

              - effective_tier: one of 'free', 'pro', 'business', 'enterprise'
              - resolution_chain: list of every step with which matched
              - matched_source: which step actually determined the tier
              - agent_summary: a plain-English one-liner you can show
                the user verbatim
              - pricing_url: link to send the user if they want to upgrade

            No arguments.  Free-tier safe — does NOT require a license
            to call.  Available to every user.

            JUST PAIRED, AND THE TIER LOOKS STALE?  Try this order, and do
            it yourself rather than handing the user a checklist:

              1. Call ``restart_server``.  This server caches what it
                 resolved at startup, so a pairing that happened after it
                 launched may simply not be visible to it yet.  A restart
                 re-execs in place and is invisible to the user.
              2. Call this tool again.
              3. Only if the tier is STILL stale, ask the user to fully
                 quit and reopen their AI app.  That is the one step they
                 have to do by hand, so it is the last resort, not the
                 first suggestion — and MCP clients differ (some reconnect
                 to a restarted server on their own, some do not).

            Report the tier plainly whatever it turns out to be, Free
            included: pairing is not a purchase, and a free account that
            paired successfully must not be told it failed.

            Common interpretation:
              - effective_tier="free", matched_source="kiln_pro_install":
                kiln-pro not installed on this machine.  User can still
                use Pro features via api.kiln3d.com if signed in.
              - effective_tier="free", matched_source="default":
                kiln-pro installed but no auth — needs `kiln signin` or
                KILN_LICENSE_KEY.
              - effective_tier="pro" (or higher) with matched_source=
                "license_manager_resolve" and "oauth_session" matched=True
                in the chain: user is signed in via OAuth and the
                entitlement on file gives them this tier.

            Returns:
                dict with success/effective_tier/resolution_chain/
                matched_source/agent_summary/tier_rank/pricing_url.
            """
            try:
                return _walk_resolution_chain()
            except Exception as exc:
                _logger.exception("check_my_tier failed unexpectedly")
                return {
                    "success": False,
                    "effective_tier": "unknown",
                    "agent_summary": (
                        f"Couldn't resolve your tier — diagnostic crashed: {exc}. "
                        "This shouldn't happen; please report at "
                        "https://github.com/codeofaxel/Kiln/issues."
                    ),
                    "error": str(exc),
                }

        _logger.debug("Registered tier-diagnostic tools")


plugin = _TierDiagnosticPlugin()
