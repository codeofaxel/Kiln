# /swarm-feature — Build a Feature with Parallel Teammates

A team-based skill for implementing multi-file features. The lead breaks down the work, spawns specialists with file ownership boundaries, and coordinates integration.

## When to Use
- Feature touches 3+ files across logic, interface, and/or integration layers
- Work can be split by file ownership without teammates needing to edit the same files
- Feature is well-defined enough to break into independent tasks

For single-file changes or tightly coupled work, skip the swarm and just implement directly.

## Phase 0: Analyze & Plan (Lead does this)
1. Run `git branch --show-current` — confirm branch
2. Read the feature request and identify all affected files/layers
3. Break the feature into tasks with clear file ownership boundaries
4. Verify NO two teammates will edit the same file — if they would, assign those edits to one teammate
5. Decide which teammates to spawn (not all are always needed)

## Phase 1: Spawn Teammates

Require plan approval from each teammate before they implement.

### Teammate: "logic" (always spawned)
**Prompt**: You are implementing the logic/data layer for [FEATURE].

Your file ownership:
- WRITE: `kiln/src/kiln/printers/**` (adapters, base interface, dataclasses)
- WRITE: `kiln/src/kiln/server.py` (MCP tool handlers — only the business logic portions)
- FORBIDDEN: `octoprint-cli/src/octoprint_cli/**` — if you need CLI changes, message the "interface" teammate

Key constraints:
- All printer operations must go through the PrinterAdapter abstract interface.
- New adapters MUST implement ALL abstract methods from `base.py`.
- Normalize external API data into strict internal dataclass types. Never pass raw dicts.
- Network calls always wrapped in try/except. Printers go offline — handle it.
- No `# TODO` in critical paths (print submission, upload, temperature, G-code). Implement fully or error-stub.
- Check `docs/LESSONS_LEARNED.md` if you hit unfamiliar patterns.

Verify when done (`python -m py_compile`). Report what you implemented and what the "interface" teammate needs to wire to.

### Teammate: "interface" (always spawned)
**Prompt**: You are implementing the interface layer for [FEATURE].

Your file ownership:
- WRITE: `octoprint-cli/src/octoprint_cli/**` (CLI commands, output formatting, config)
- WRITE: `kiln/src/kiln/server.py` (MCP tool definitions — only the tool decorator and parameter annotations)
- FORBIDDEN: `kiln/src/kiln/printers/**` — if you need adapter changes, message the "logic" teammate

Key constraints:
- CLI commands must support `--json` output mode for agent consumption.
- Every CLI command must use proper exit codes from `exit_codes.py`.
- MCP tools must return the standard response format: `{"status": ..., "data": ...}` or `{"error": ...}`.
- Use `click.option`/`click.argument` with proper type annotations and help text.
- Wire commands to the logic layer's adapter methods. Don't create business logic in CLI/MCP layer.

Verify when done. Report what you need from the "logic" teammate if anything.

### Teammate: "integration" (spawn only if feature touches new external APIs or adapters)
**Prompt**: You are implementing the integration layer for [FEATURE].

Your file ownership:
- WRITE: `kiln/src/kiln/printers/<new_adapter>.py` (new adapter implementations)
- WRITE: `kiln/src/kiln/printers/__init__.py` (adapter registry)
- FORBIDDEN: `octoprint-cli/src/octoprint_cli/**`

Key constraints:
- Normalize external data into strict internal types. Never pass raw API responses to the logic layer.
- Wrap all network calls in error handling. Network failures are expected.
- Map ALL backend printer states to the `PrinterStatus` enum. No gaps.
- Document the external API: base URL, auth method, rate limits, response format.
- Handle HTTP retries for transient failures (502, 503, 504).

Verify when done. Report the API contract (method signatures, return types) for the "logic" teammate.

### Teammate: "qa" (spawn after others finish, or in parallel for test-first)
**Prompt**: You are writing tests and validating [FEATURE].

Your file ownership:
- WRITE: `kiln/tests/**`, `octoprint-cli/tests/**`
- READ: everything else (for verification)

Key constraints:
- Check environment before blaming code (clean install, dependency versions) — 80% of "bugs" are stale state.
- Write pytest tests for the new logic. Mock HTTP calls with `responses` library.
- Test edge cases: printer offline, empty responses, invalid inputs, state transitions.
- Run the test suite and report results.

## Phase 2: Coordinate (Lead monitors)
- Monitor teammate progress via task list
- If a teammate needs something from another, they message each other directly
- Resolve any file ownership conflicts (reassign if needed)
- Wait for all teammates to complete before proceeding

## Phase 3: Integrate & Verify (Lead does this)
1. Run `python -m py_compile` on all edited files
2. Verify no merge conflicts from parallel work
3. Run tests if they exist (`pytest`)
4. Report: what was built, what works, any remaining issues

## Rules
- File ownership boundaries are HARD constraints in spawn prompts, not suggestions.
- If a feature is too tightly coupled to split (e.g., all changes in one file), don't swarm.
- The lead does NOT implement code — it coordinates. Use delegate mode if available.
- Maximum 4 teammates. More than that adds coordination overhead without benefit.
