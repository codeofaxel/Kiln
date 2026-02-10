# Kiln — Claude Code Guidelines

## Git Workflow (MANDATORY)
- **Always run `git branch --show-current` before investigating bugs or making changes.** Do not skip this.
- Verify you're on the correct branch before reading code, grepping, or proposing fixes.
- If the user mentions a branch name, switch to it first.
- When editing documentation or changelogs, **append** new content rather than replacing existing content unless explicitly told to replace.

## Communication Style
- **Be direct. Execute, don't narrate.** Show findings concisely. Don't over-explain reasoning.
- When asked to investigate or explain, provide findings directly **without asking for approval**. Only ask for approval before making destructive or irreversible changes (deleting files, force pushing, dropping tables).
- Don't propose plans for simple tasks. Just do them.
- For complex multi-file changes, briefly state your approach (2-3 sentences max) then execute.
- Never say "shall I proceed?" or "would you like me to?" for investigation, reading, or analysis tasks.

## Code Discipline
- **Root causes only.** Never apply band-aid fixes. Trace to the actual source of the problem.
- **Minimal blast radius.** Only touch what's necessary. Don't refactor adjacent code "while you're in there" unless asked.
- **Simplicity first.** Prefer the simplest correct solution. Don't over-engineer.
- **Challenge your own work.** Before presenting a fix, ask: "Is there a simpler way? Did I introduce new issues? Would a staff engineer approve this?"

## Subagent Strategy
- **Offload research** to subagents to keep the main context window clean. Use subagents for grepping across the codebase, reading multiple files to answer a question, or exploring unfamiliar code.
- **One task per subagent.** Give each subagent a focused, specific job. Don't ask a subagent to "investigate and fix" — ask it to "find all usages of X" and process the results yourself.
- **Use subagents for parallel exploration.** When multiple hypotheses exist, spin up subagents to investigate each one simultaneously.
- **Don't subagent trivial tasks.** Reading one file or running one grep doesn't need a subagent. Use them when the work would eat significant context window.

## Build & Test
- Two Python packages in this monorepo:
  - **kiln** (MCP server): `kiln/` — entry point `python -m kiln` or `kiln`
  - **octoprint-cli** (CLI tool): `octoprint-cli/` — entry point `octoprint-cli`
- Build system: `pyproject.toml` + setuptools for both packages
- Tests: `kiln/tests/` and `octoprint-cli/tests/` (pytest)
- After making Python edits, verify with: `cd kiln && python -m py_compile src/kiln/<file>.py` or run `pytest`
- Install for development: `pip install -e ./kiln` and `pip install -e ./octoprint-cli`

## Debugging Approach
- Trace bugs end-to-end: MCP tool call → server.py handler → PrinterAdapter method → HTTP request → OctoPrint API
- For adapter bugs, check the abstract interface in `base.py` matches the concrete implementation in `octoprint.py`
- For CLI bugs, trace: Click command → client.py → HTTP → OctoPrint API → output.py formatting
- Check that printer state mapping covers all edge cases (OctoPrint flags → PrinterStatus enum)
- Use structured JSON output from the CLI for debugging response formats

## Autonomous Fix Loops
When asked to fix bugs or failing tests, work autonomously:
1. Identify the failure (import error, test failure, runtime bug)
2. Trace the root cause
3. Implement the fix
4. Verify the code compiles (`python -m py_compile`)
5. If tests exist, run them and iterate until passing
6. Report results only when done or truly blocked

Do NOT stop after step 2 to ask permission. Complete the full loop.

## Project Structure Quick Reference
```
kiln/                           — MCP Server package
  src/kiln/
    __init__.py
    __main__.py                 — Entry point (python -m kiln)
    server.py                   — FastMCP server, 30+ MCP tools
    slicer.py                   — PrusaSlicer/OrcaSlicer integration
    registry.py                 — Fleet printer registry
    queue.py                    — Priority job queue
    scheduler.py                — Background job dispatcher
    events.py                   — Pub/sub event bus
    persistence.py              — SQLite storage
    webhooks.py                 — Webhook delivery with HMAC
    auth.py                     — API key authentication
    billing.py                  — Fee tracking
    gcode.py                    — G-code safety validator
    printers/
      base.py                   — Abstract PrinterAdapter, enums, dataclasses
      octoprint.py              — OctoPrint REST adapter
      moonraker.py              — Moonraker REST adapter
      bambu.py                  — Bambu Lab MQTT adapter
    marketplaces/
      base.py                   — Marketplace adapter interface
      thingiverse.py            — Thingiverse API client
      myminifactory.py          — MyMiniFactory API client
      cults3d.py                — Cults3D API client
    cli/
      main.py                   — Click CLI (20+ commands)
      config.py                 — Config management (YAML/env/flags)
      discovery.py              — mDNS printer scanning
      output.py                 — JSON/text output formatting
  tests/                        — pytest tests (1165)
  pyproject.toml

octoprint-cli/                  — CLI Tool package
  src/octoprint_cli/
    cli.py                      — Click CLI entry point
    client.py                   — OctoPrint REST client
    config.py                   — Config management
    output.py                   — JSON/text output formatting
    safety.py                   — Pre-flight checks
    exit_codes.py               — Standard exit codes
  tests/                        — pytest tests (239)
  pyproject.toml

docs/                           — Documentation
  WHITEPAPER.md                 — Technical whitepaper
  PROJECT_DOCS.md               — Full project documentation
  COMPLETED_TASKS.md            — Shipped features log
  TASKS.md                      — Open backlog
  LESSONS_LEARNED.md            — Hard-won patterns (auto-updated)
  PRINT_FLOW.md                 — End-to-end flow diagram
  roles/                        — Swarm teammate role references
  SWARM_GUIDE.md                — System guide
```

## Common Bug Patterns
- **State mapping gaps**: OctoPrint returns flag combinations not covered by `_map_state()` → defaults to UNKNOWN
- **Nested dict access**: OctoPrint API responses have deeply nested optional fields — use safe access helpers or `.get()` chains
- **File path handling**: Upload paths differ between local filesystem and OctoPrint's virtual filesystem
- **Retry logic masking errors**: HTTP retry on 502/503/504 can mask persistent backend failures — check retry exhaustion paths
- **Config precedence confusion**: CLI flags → env vars → config file — bugs often come from the wrong layer winning

## Hard Laws (crash/data-loss prevention — never violate these)

### 1. Printer Safety First
Before any print operation:
- **Pre-flight check is mandatory**: Never bypass `preflight_check()`. Temperature, file existence, and printer state MUST be validated.
- **Confirm before destructive ops**: `cancel_print()`, `start_print()`, raw G-code commands always require explicit confirmation context.
- **Never send raw G-code without validation**: G-code commands that home axes, set temperatures, or move steppers can cause physical damage. Validate command safety.

### 2. Adapter Interface Contract
Every new printer adapter MUST:
- Implement ALL abstract methods from `PrinterAdapter` in `base.py`
- Return the correct dataclass types (never raw dicts)
- Map all backend states to `PrinterStatus` enum (no silent fallthrough)
- Handle connection failures gracefully (return OFFLINE, don't raise)

### 3. Error Boundary Discipline
- **Network calls always fail**: Every HTTP request to a printer MUST be wrapped in try/except. Printers go offline, networks drop, APIs timeout.
- **Structured error responses**: Never return raw exception messages to agents. Always wrap in the standard `{"error": ..., "status": ...}` format.
- **No silent failures**: If an operation fails, the agent MUST know. Never swallow exceptions.

### 4. Configuration Safety
- **Never hardcode credentials**: API keys, host URLs, and secrets come from environment variables or config files. Never in source code.
- **Validate config on load**: Missing or malformed config should fail fast with a clear error, not silently use defaults that hit production printers.
- **Config file permissions**: Warn if config files containing API keys are world-readable.

### 5. No-TODO Critical Paths
No `// TODO` or `# TODO` in: print job submission, file upload, temperature control, G-code execution, or authentication flows. Code must be fully implemented or error-stubbed with user-visible feedback.

### 6. Type Safety at Boundaries
- **Normalize external data**: OctoPrint/Moonraker/Bambu APIs all return different JSON shapes. Adapters MUST normalize to the internal dataclass types.
- **Validate before forwarding**: Never pass raw API responses through to the MCP layer. Parse, validate, type-check.
- **Enum exhaustiveness**: When adding new printer states or capabilities, update ALL switch/match statements across the codebase.

## When to Swarm vs Solo
- **Solo**: Single bug, single file, quick edit, test verification, focused investigation. Use `/debug` for targeted single-area audits.
- **Swarm**: Multi-file feature, broad audit (whole subsystem), 3+ independent bugs, release pipeline. Use `/swarm-*` skills when work can be split by file ownership without dependencies.
- **Rule of thumb**: If teammates would need to edit the same file, don't swarm — use solo with sequential edits instead.

## Learning Reflex (Self-Improvement Loop)
When the user corrects you, points out a mistake, or you discover a non-obvious fix:
1. **Immediately** append the pattern to `docs/LESSONS_LEARNED.md` under the relevant section
2. Write it as a reusable rule: what went wrong, why, and the correct pattern
3. Keep entries concise (3-5 lines max)
4. This is NOT optional — every correction becomes institutional knowledge

**Triggers:** User says "no, that's wrong", "actually you should...", "that's not how X works", a fix takes 2+ attempts, a test/validation fails for a non-obvious reason. When in doubt, file the lesson.

## Self-Challenge Gate (Mandatory Before Presenting Work)
Before reporting ANY non-trivial work as complete, run this checklist. If ANY answer is "no," **iterate silently until it's "yes."** Do not present work that fails a check — fix it first.

1. **Code valid?** (imports resolve, no syntax errors, type hints consistent)
2. **Root cause addressed?** Not a band-aid. The actual underlying issue is fixed.
3. **Blast radius minimal?** Only the necessary files were changed. No drive-by refactors.
4. **Edge cases handled?** None, empty, error states, offline printer, timeout — not just the happy path.
5. **Simpler solution exists?** Re-read the code. Is there a 5-line version of your 20-line fix? Use it.
6. **Staff engineer test:** Would a senior infrastructure engineer at a top company approve this on first review? If "probably not" or "maybe" — iterate. Only present when the answer is "yes, confidently."

**The rule:** Do not present output you wouldn't ship to production. If your internal confidence is below "I'd bet money this is correct and clean," keep working. When in doubt, iterate one more time — the cost of one extra pass is always less than the cost of a sloppy delivery.

## Documentation Auto-Update Triggers

Kiln maintains three living documents that must stay in sync with the codebase:
- `README.md` — Project overview, quick start, feature summary
- `docs/WHITEPAPER.md` — Technical whitepaper (architecture, protocol, safety model)
- `docs/PROJECT_DOCS.md` — Full project documentation (CLI reference, MCP tools, adapter details)

**When to update these documents:**

1. **New CLI command added** → Update README command table + PROJECT_DOCS CLI Reference section.
2. **New MCP tool added** → Update README MCP Tools table + PROJECT_DOCS Tool Catalog section.
3. **New printer adapter added** → Update README Supported Printers table + PROJECT_DOCS Printer Adapters section + WHITEPAPER adapter list.
4. **New marketplace adapter added** → Update README Model Marketplaces table + PROJECT_DOCS Project Structure.
5. **New module created** → Update README Modules table + PROJECT_DOCS Project Structure.
6. **Test count changes significantly (±50)** → Update README Development section test counts.
7. **Safety system changes** → Update WHITEPAPER safety section + PROJECT_DOCS Safety Systems section.
8. **Architecture changes** (new subsystem, protocol change) → Update WHITEPAPER architecture section.

**When NOT to update:**
- Bug fixes, refactors, or internal changes that don't add user-facing features.
- Test additions without new features.
- Documentation-only changes (avoid circular updates).

**How to update:** Append or edit the specific section — don't rewrite the entire document. Keep the whitepaper formal and the guide reference-dense.

## Reference Docs
- `README.md` — Project overview and quick start. Keep concise.
- `docs/WHITEPAPER.md` — Technical whitepaper in academic style. Covers architecture, safety, protocol design.
- `docs/PROJECT_DOCS.md` — Full project documentation (Gitbook-style). CLI reference, MCP tool catalog, adapter details, configuration.
- `docs/COMPLETED_TASKS.md` — Record of shipped features. Append after each feature lands.
- `docs/TASKS.md` — Open task backlog.
- `docs/LESSONS_LEARNED.md` — Hard-won technical patterns and bug fixes. Consult when hitting unfamiliar issues. **Append to this file when you learn something new.**
- `docs/roles/` — Slim role references (LOGIC.md, INTERFACE.md, QA.md, INTEGRATION.md) used for swarm teammate spawn prompts.
- `docs/SWARM_GUIDE.md` — Full guide to the agent swarm system.
