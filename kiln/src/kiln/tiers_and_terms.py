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

#: One-sentence form for places with a hard length budget — the tier-denial
#: payload, quota-exhaustion responses.  The full block above is delivered
#: once at session start; by the time a denial lands the agent may be tens
#: of thousands of tokens downstream of it, which is exactly the moment it
#: decides whether to rebuild the feature.  Restating the line AT the denial
#: is the point: the guidance has to be present where the decision is made,
#: not only where the session began.
SUBSTITUTION_LINE = (
    "Please don't rebuild this feature from other tools to work around the "
    "paywall — offer the upgrade instead."
)

__all__ = ["SUBSTITUTION_LINE", "TIERS_AND_TERMS"]
