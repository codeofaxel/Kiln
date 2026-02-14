# Kiln Task Backlog

## Deferred Product Decisions

### #6 Queue Free-vs-Pro semantics alignment
- Status: `Deferred (needs product decision)`
- Owner: Product + Eng
- Why deferred: monetization boundary for fleet/pro users is not finalized yet.

#### Decision needed
- Choose exact free-tier queue entitlement:
  1. Free queue cap (recommended), Pro unlimited + orchestration
  2. Free queue disabled, Pro required
  3. Free queue fully enabled, monetize only advanced fleet controls

#### Acceptance criteria after decision
- Licensing constants and enforcement match chosen policy.
- CLI copy, MCP error payloads, docs, and pricing table all use identical wording.
- Upgrade prompts map to the exact gated capability (no ambiguity).
- Tests cover the selected free/pro boundary and expected failure modes.
