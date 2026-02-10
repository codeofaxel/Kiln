# /swarm-debug — Parallel Whitehat Audit

A team-based debugging skill that spawns parallel teammates to stress-test different dimensions of the codebase simultaneously.

## When to Use
- Broad audit scope (entire subsystem, multiple files)
- After major refactors or before releases
- When you want thorough coverage across data flow, edge cases, security, adapter compliance, and MCP layer

For focused single-file or single-bug investigation, use `/debug` instead.

## Phase 0: Context Lock (Lead does this — NEVER SKIP)
1. Run `git branch --show-current` — confirm active branch
2. Run `git status` — check for uncommitted changes
3. Run `git log --oneline -5` — recent commits for context
4. Identify the scope (which files/subsystems to audit)
5. Report context summary before spawning teammates

## Phase 1: Bug Investigation (Lead does this if a specific bug is mentioned)
1. Restate the bug in one sentence
2. Trace end-to-end: MCP tool → server.py → PrinterAdapter → HTTP → Printer API
3. Identify root cause and fix it
4. Then proceed to Phase 2 to audit the surrounding area

## Phase 2: Spawn Parallel Audit Teammates

Create an agent team with the following teammates. Each gets a clear scope and the Hard Laws from CLAUDE.md.

### Teammate: "data-flow"
**Prompt**: You are auditing data flow integrity for [SCOPE]. Check:
- API response audit: Compare what the printer API returns to what the adapter parses — flag fields accessed but not present
- Type coercion: Find places where raw API values are cast without validation
- Dataclass completeness: Verify all fields in PrinterState, JobProgress, PrinterFile etc. are populated
- State mapping coverage: Check that _map_state() handles ALL known printer state combinations
Report findings with severity: CRITICAL / HIGH / MEDIUM / LOW.

### Teammate: "edge-cases"
**Prompt**: You are auditing edge cases and race conditions for [SCOPE]. Check:
- Printer offline: Does every HTTP call handle ConnectionError/Timeout?
- Mid-operation conflicts: What if operations are called during incompatible states (upload while printing, start while already printing)?
- Empty responses: 0 files, empty job progress, freshly booted printer with no state?
- Concurrent tool calls: Can two MCP calls hit the printer simultaneously and conflict?
- Timeout exhaustion: What happens when all HTTP retries fail?
Report findings with severity: CRITICAL / HIGH / MEDIUM / LOW.

### Teammate: "security"
**Prompt**: You are running a security audit for [SCOPE]. Check:
- Credential exposure: Are API keys logged, printed, or included in error responses?
- Path traversal: Can malicious filenames in upload_file() escape intended directories?
- G-code safety: Can raw G-code commands cause physical damage (unrestricted temp, axis beyond bounds)?
- Config file permissions: Are config files with API keys world-readable?
- Input validation: Check all agent-supplied inputs for injection or overflow
Report findings with severity: CRITICAL / HIGH / MEDIUM / LOW.

### Teammate: "adapter-contract" (spawn if scope touches printer adapters)
**Prompt**: You are auditing adapter interface compliance for [SCOPE]. Check:
- Abstract method coverage: Does each adapter implement ALL methods from PrinterAdapter?
- Return type correctness: Does every method return the exact dataclass type from the interface?
- Error handling consistency: Does every method handle connection failures the same way?
- State enum exhaustiveness: Are all PrinterStatus values reachable? Any dead states?
- New adapter template: If a new adapter were added, would the interface be sufficient?
Report findings with severity: CRITICAL / HIGH / MEDIUM / LOW.

### Teammate: "mcp-layer" (spawn if scope touches MCP server)
**Prompt**: You are auditing the MCP server layer for [SCOPE]. Check:
- Tool registration: Are all tools properly registered with correct type annotations?
- Response format consistency: Do all tools return the standard response format?
- Error propagation: Do adapter exceptions bubble up as clean error responses or stack traces?
- Lazy init safety: What happens if get_adapter() fails at various points?
- Input validation: Are tool parameters validated before reaching the adapter?
Report findings with severity: CRITICAL / HIGH / MEDIUM / LOW.

## Phase 3: Synthesize (Lead does this after all teammates report)

Collect all teammate findings and produce a unified report:

```
Branch: [branch-name]
Bug Fix: [one-line summary, or "N/A"]
Verified: [compiles / tests pass / fails]

CRITICAL:
- [finding] (source: [teammate])

HIGH:
- [finding] (source: [teammate])

MEDIUM:
- [finding] (source: [teammate])

LOW:
- [finding] (source: [teammate])

Clean Areas: [list areas that checked out fine]
```

Then fix CRITICAL and HIGH issues automatically (verify after each). List MEDIUM/LOW and ask user if they want them fixed.

## Rules
- Lead does Phase 0, 1, and 3. Teammates do Phase 2 in parallel.
- Each teammate gets read access to the full codebase but should focus only on their assigned dimension.
- If fewer than 3 areas need auditing, skip swarming and use solo `/debug` instead.
