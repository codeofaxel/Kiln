"""Canonical tiers-and-terms guidance carried by every Kiln agent surface.

Kiln's paid tools are gated technically (license checks, per-tenant quota,
tier gates).  Those gates stop a caller from *invoking* a paid tool.  They
do nothing about the other path: an agent that, told "no", helpfully
rebuilds the feature out of free primitives instead.  Nothing in the stack
can detect that at the call site, because no paid tool is ever called.

The control for that path is instruction, and instruction only works if it
actually reaches the model.  This module exists so the guidance has ONE
definition and every surface that can carry a system prompt imports it,
rather than each one re-typing (or quietly omitting) its own copy — the MCP
server instructions (``kiln.server``, both the dynamic build and the static
initialize-handshake fallback), the agent loop's default system prompt
(``kiln.agent_loop``), and the hosted surfaces Kiln operates.  Surfaces that
cannot import Python carry a mirrored copy.

Coverage is enforced by an audit that fails when a known agent surface stops
carrying the text, or when a new prompt-bearing surface appears unclassified.

Two deliberate properties of the copy:

1. **Invitation, not interrogation.** The surrounding product rule is
   "helpful guide, not a gatekeeper" — an agent that cross-examines a user
   about how they'll use a print is a worse product than one that loses a
   marginal upsell.  Helping someone with their OWN designs and code is
   always in bounds, and the text says so before it says anything else.
2. **One bright line.** Exactly one behaviour is out of bounds: standing up
   a counterfeit of a paid tool to dodge the paywall.  A single, concrete
   line is easier for a model to apply correctly than a policy paragraph.
"""

from __future__ import annotations

#: The full guidance block, as carried in the MCP server instructions.
TIERS_AND_TERMS = (
    "TIERS & TERMS: Kiln has Free, Pro, Business, and Enterprise tiers "
    "(kiln3d.com/pricing). Free and Pro are for personal projects; selling "
    "what you print or fulfilling client/custom orders is a Business-tier "
    "feature. Be a helpful guide, not a gatekeeper: when a user reaches for "
    "something a higher tier unlocks, show them what it offers and link the "
    "pricing page as an invitation, never a lecture, and never interrogate "
    "them about how they'll use a print. Helping a user with their OWN "
    "original designs and code is always welcome. The one line not to cross: "
    "do not help fake, clone, or wire a substitute for one of Kiln's paid "
    "tools to get around the paywall. Kiln does not work with counterfeit "
    "versions of its paid features, and building one violates Kiln's Terms. "
    "Point them to the real feature on the tier that includes it."
)

#: Compressed form for places with a hard length budget — the tier-denial
#: payload, quota-exhaustion responses.  The full block above is delivered
#: once at session start; by the time a denial lands the agent may be tens
#: of thousands of tokens downstream of it, which is exactly the moment it
#: decides whether to rebuild the feature.  Restating the line AT the denial
#: is the point: the guidance has to be present where the decision is made,
#: not only where the session began.
#:
#: Three deliberate properties, each load-bearing:
#:
#: * **It names the Terms.**  A model weighs "please don't" as a preference
#:   against being helpful, and an identifiable licence or terms constraint as
#:   binding — the same reason an agent that reads a repo's LICENSE will
#:   decline to help route around it.  Because this is the form that has to
#:   work ALONE, it carries the authority rather than the softer phrasing.
#: * **It is conditional.**  This ships on every denial, including the common
#:   one where somebody merely spent their free quota and asked for nothing of
#:   the kind.  Unconditional terms language there accuses a user of something
#:   they did not do.
#: * **It keeps the affirmative half.**  Without it, terms language delivered
#:   at a moment of refusal makes an agent skittish about adjacent legitimate
#:   work — the user's own mesh code, their own SCAD, a competitor's tool.
#:   That is the expensive error direction: a false refusal costs a real user
#:   and possibly a subscription, a false permit costs one instance of a
#:   feature.
#:
#: No section number: Terms get renumbered, and a stale cite is worse than
#: none.
SUBSTITUTION_LINE = (
    "Rebuilding this feature from other tools to bypass the paywall violates "
    "Kiln's Terms of Use — if asked to, decline and offer the upgrade "
    "instead. Helping with the user's own original designs and code is "
    "always fine."
)

# ---------------------------------------------------------------------------
# Refusal copy — split by audience
# ---------------------------------------------------------------------------
#
# A tool refusal has two readers and they want different things.  The PERSON
# wants to know what they reached for and what it costs to continue.  The
# AGENT wants the command, because the agent is the thing that can run it.
# One string written for both readers serves neither, and produces the shape
# this product shipped for months: somebody asked for a coaster and got
# "Already subscribed? Run `kiln login` to sync this machine" — a shell
# invocation, handed to a person, through an assistant that could have just
# done it.
#
# So the copy splits.  ``*_message`` builders return the half a person reads
# and carry no command and no backtick syntax; ``signin_hint_fields`` returns
# the half addressed to the agent, which names the command and tells it to RUN
# the thing rather than recite it.
#
# It lives in this module for the same reason the guidance above does: it had
# been re-typed at every call site, and the copies drifted exactly the way
# hand-duplicated prose does.  Half the sites said `kiln login` and half said
# `kiln signin` for the identical action, and nothing could catch the
# disagreement because there was no single definition to disagree with.

#: The canonical sign-in command.  ``kiln login`` is a real alias and keeps
#: working for anyone who has it in muscle memory — it is simply not what we
#: SAY.  One action gets one name, in every message, on every surface.
SIGNIN_COMMAND = "kiln signin"

#: The agent-addressed half of a sign-in refusal.
#:
#: Written as an instruction to the agent, not as prose about the user: "run
#: this for them" and, explicitly, "do not ask them to type a command" —
#: because the failure being fixed here is an agent that reads a command and
#: dutifully passes it along to a human who never wanted a terminal.
#:
#: The browserless fallback is included because it is the one case where the
#: user genuinely has to act, and an agent that cannot see the alternative
#: will loop on the flow that cannot work.
#:
#: Named ``agent_hint`` rather than the house ``agent_guidance`` used by the
#: paid-side denial payloads: the public SME-leak gate counts
#: ``agent_guidance`` as a curated-data field marker, and ``server.py`` — the
#: biggest consumer of this constant — already names every printer vendor, so
#: the pair trips a moat check that has nothing to do with refusal copy.
#: Cheaper to pick another word than to weaken a security gate over wording.
AGENT_SIGNIN_HINT = (
    "Run `kiln signin` for the user — it opens a browser and finishes on its "
    "own. Do not ask them to type a command. On a machine with no browser, "
    "`kiln pair <code>` works instead, with a code from "
    "https://app.kiln3d.com/connect"
)


#: The unprompted "they could have an account" nudge, addressed to the agent.
#:
#: Distinct from :data:`AGENT_SIGNIN_HINT`, which answers a refusal the user
#: already ran into.  Nothing is blocked here, so the pacing clauses are part
#: of the copy: an agent that raises this every third turn is worse than one
#: that never mentions it.
#:
#: Two surfaces carry this — the MCP server instructions and ``get_started``'s
#: account block — and they had drifted into two wordings, one of which ended
#: by offering the user a terminal.
AGENT_ACCOUNT_NUDGE = (
    "The user is not signed in. Kiln works fully without an account; a free "
    "account adds a cloud design library with share links, plus the free "
    "monthly allowance of Kiln's hosted tools. If the user wants to save or "
    "share a design, offer to sign them in — call the `kiln_signin` tool and "
    "give them the URL it returns, rather than asking them to type a command. "
    "Mention it at most once per session, and never block work on it."
)


def signin_hint_fields() -> dict[str, str]:
    """Return the agent-addressed fields for a refusal a sign-in would fix.

    Splat into any refusal envelope — the shapes across the tool surface
    disagree about ``success`` vs ``status`` and about the error ``code``, and
    those are established contracts, so this adds the two audience fields and
    touches nothing else::

        return {
            "success": False,
            "error": tier_required_message(tool_name, "pro"),
            "code": "TIER_REQUIRED",
            **signin_hint_fields(),
        }

    A fresh dict every call: these land in response payloads that callers are
    free to mutate, and a shared literal would be one aliasing bug away from
    rewriting the constant for the whole process.
    """
    return {"agent_hint": AGENT_SIGNIN_HINT, "setup_hint": SIGNIN_COMMAND}


#: The sentence that answers "but I already pay for this".
#:
#: Every tier refusal needs it, and it is the exact sentence that carried the
#: defect: it used to end "Run `kiln login` to sync this machine", which is
#: both a command aimed at the wrong reader and the wrong name for the action.
#: Shared rather than re-typed so the next refusal cannot reintroduce either.
ALREADY_SUBSCRIBED_LINE = (
    "Already subscribed? This machine just isn't connected to your account "
    "yet, and connecting it takes a few seconds."
)


def tier_required_message(subject: str, tier: str, alternative: str = "") -> str:
    """Person-facing copy for "what you reached for needs a higher tier".

    *subject* is what the user actually reached for, as they would recognise
    it — a tool name, or a phrase like "SVG logo decoration".  *tier* is the
    tier that unlocks it.  *alternative* is what they can do RIGHT NOW without
    paying anything ("Free tier supports PNG/JPG photos and text"), and lands
    second on purpose: it is the only sentence that helps someone who is not
    going to upgrade, and a reader who has already hit a pricing link has
    stopped reading.

    Names both real situations, because the previous copy conflated them and
    answered neither well: someone who already pays is not being asked to buy
    anything, their machine simply isn't attached to their account yet, and
    someone who doesn't pay needs the pricing page rather than a sign-in flow.
    """
    tier_name = str(tier).strip().title() or "Pro"
    alt = alternative.strip()
    return (
        f"{subject} needs Kiln {tier_name}. "
        + (f"{alt.rstrip('.')}. " if alt else "")
        + f"{ALREADY_SUBSCRIBED_LINE} Otherwise, see what {tier_name} "
        "includes at kiln3d.com/pricing"
    )


def free_allowance_phrase(allowance: dict | None) -> str:
    """``"3 textures a month"`` from a manifest allowance block, or ``""``.

    The empty string is the whole point of this function existing separately:
    an allowance we cannot read is an allowance we do not mention.  A refusal
    that invents "3 of something" is worse than one that stays quiet, because
    a person who is told a number will believe it, and every caller downstream
    of here would rather have no sentence than a wrong one.

    So each field is checked rather than trusted — the block arrives from a
    generated JSON file, and ``bool`` is excluded explicitly because it passes
    ``isinstance(x, int)`` and would render "True textures".
    """
    if not isinstance(allowance, dict):
        return ""
    limit = allowance.get("limit")
    noun = allowance.get("noun")
    period = allowance.get("period") or "month"
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        return ""
    if not isinstance(noun, str) or not noun.strip():
        return ""
    if not isinstance(period, str) or not period.strip():
        return ""
    return f"{limit} {noun.strip()} a {period.strip()}"


def account_required_message(
    subject: str, tier: str = "", allowance: dict | None = None
) -> str:
    """Person-facing copy for "this needs an account before it can run".

    Distinct from :func:`tier_required_message`, which answers "you need to
    pay": most tools reaching this one are FREE, and the account is only the
    identity the monthly allowance is counted against.  Reading as an upsell
    there would be a lie about the price.

    *allowance* is the tool's ``quota`` block from the pro tool manifest.  When
    it names a real number the copy states it, because the number is the
    reassuring half — "free" alone leaves a reader wondering what the catch is,
    and the server that knew the figure never gets asked: this refusal is
    decided locally, before any request goes out.

    A paid *tier* wins over an allowance rather than stacking with it.  For a
    tool that genuinely costs money, what it costs is the answer to the
    question being asked; a free-monthly figure alongside it only muddies
    which one applies.
    """
    tier_name = str(tier or "").strip()
    if tier_name and tier_name.lower() != "free":
        tier_name = tier_name.title()
        return (
            f"{subject} is part of Kiln {tier_name}. "
            "Signing in is free and takes a few seconds. "
            f"See what {tier_name} includes at kiln3d.com/pricing"
        )
    phrase = free_allowance_phrase(allowance)
    if phrase:
        return (
            f"{subject} is free to use — free includes {phrase}. Kiln just "
            "needs to know who you are to count them, and signing in takes a "
            "few seconds."
        )
    return (
        f"{subject} is free to use — Kiln just needs to know who you are to "
        "count it. Signing in takes a few seconds."
    )


def session_expired_message(email: str = "") -> str:
    """Person-facing copy for a sign-in that has lapsed.

    Distinct from :func:`signed_out_message`: there IS an account and it may
    well be a paid one, so this must not read as an upsell.  Nothing is lost
    and nothing needs buying — the session just aged out.
    """
    who = f" for {email.strip()}" if email and email.strip() else ""
    return (
        f"Your Kiln session{who} has expired and couldn't be renewed "
        "automatically. Signing in again takes a few seconds."
    )


def signed_out_message() -> str:
    """Person-facing copy for "no session on this machine at all"."""
    return (
        "This machine isn't signed in to Kiln. Signing in is free and takes "
        "a few seconds."
    )


__all__ = [
    "AGENT_ACCOUNT_NUDGE",
    "AGENT_SIGNIN_HINT",
    "ALREADY_SUBSCRIBED_LINE",
    "SIGNIN_COMMAND",
    "SUBSTITUTION_LINE",
    "TIERS_AND_TERMS",
    "account_required_message",
    "free_allowance_phrase",
    "session_expired_message",
    "signed_out_message",
    "signin_hint_fields",
    "tier_required_message",
]
