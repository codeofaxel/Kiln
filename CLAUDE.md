# Kiln — Claude Code Guidelines

## Git Workflow (MANDATORY)
- **Always run `git branch --show-current` before investigating bugs or making changes.** Do not skip this.
- Verify you're on the correct branch before reading code, grepping, or proposing fixes.
- If the user mentions a branch name, switch to it first.
- When editing documentation or changelogs, **append** new content rather than replacing existing content unless explicitly told to replace.

### Files Never to Commit
- `.env`, credentials, API keys, access tokens
- `tasks.md` / `task.md` (gitignored — private task tracking)
- **Session summaries, work notes, scratch markdown** — files like
  `OVERNIGHT_WORK_SUMMARY.md`, `WORK_LOG.md`, `NOTES.md`, `SUMMARY.md`,
  `*_NOTES.md`, `*_SUMMARY.md`, `SESSION_*.md`, `TODO.md`.  These are
  ephemeral working-memory artifacts and MUST NOT be committed to the
  repo.  Put them in `/tmp/`, outside the repo, or in a gitignored
  local-only directory.  If one slips in, remove it before merging —
  they bloat the repo and leak internal context to future contributors.
  The `.gitignore` has patterns for these as a safety net.

## Communication Style
- **Be direct. Execute, don't narrate.** Show findings concisely. Don't over-explain reasoning.
- When asked to investigate or explain, provide findings directly **without asking for approval**. Only ask for approval before making destructive or irreversible changes (deleting files, force pushing, dropping tables).
- Don't propose plans for simple tasks. Just do them.
- For complex multi-file changes, briefly state your approach (2-3 sentences max) then execute.
- Never say "shall I proceed?" or "would you like me to?" for investigation, reading, or analysis tasks.
- **Brainstorm mode**: When the user says "brainstorm", "discuss", "think through", "talk about", or "let's explore" — stay in **conversation mode**. Do NOT jump into auditing code, proposing implementation plans, or executing changes. Ask questions, explore tradeoffs, and riff on ideas collaboratively. Only shift to execution when the user explicitly says to build/implement/fix something.
- **Skip human-only tasks by default**: When given a task list, silently skip items requiring human action (account creation, App Store submissions, manual device testing, credential entry) unless explicitly told to include them. Focus on what you can execute autonomously.

## Relationship to kiln-pro (IMPORTANT — read before moving code)
- A **private** paid-tier companion repo (`kiln-pro`) adds premium features (textures, device intelligence, billing, fleet, etc.).
- Kiln discovers pro features via `try: from kiln_pro.bridge import pro_features` with `except ImportError` fallback.
- **Free users access pro tools via kiln-pro's REST API server** (`POST /api/tools/{tool_name}`), which runs server-side with all tools loaded. Free users never install kiln-pro locally.
- **NEVER move proprietary code from kiln-pro into this repo to "make it available to free users."** The REST API proxy pattern keeps IP private while serving tools to any tier. To open a pro tool to free users, change its gate in kiln-pro (e.g., `check_pro()` → quota check), don't move the code here.
- Quota enforcement (`decoration_quota.py`) lives here. Tier resolution calls `kiln.licensing` which kiln-pro provides; without kiln-pro, tier defaults to `"free"`.

## Project Identity
- **Kiln is infrastructure, not software.** Never describe it as "automation software", "an API", or use cloud/SaaS framing. Kiln is local stdio-based infrastructure that AI agents use to control physical printers. It is not cloud-hosted.
- **Correct framing**: "3D printing infrastructure for AI agents", "printer control layer", "MCP infrastructure"
- **Wrong framing**: "automation software", "API platform", "cloud service", "printer management app"
- **Never compare Kiln to other companies.** No "X for 3D printing", no "like Uber/Waze/Shopify but for...", no analogies to other products in code, docs, README, blog posts, or conversation. Kiln stands on its own. Describe what it does, not what it's "like".

## Code Discipline
- **Root causes only.** Never apply band-aid fixes. Trace to the actual source of the problem.
- **Minimal blast radius.** Only touch what's necessary. Don't refactor adjacent code "while you're in there" unless asked.
- **Simplicity first.** Prefer the simplest correct solution. Don't over-engineer.
- **Challenge your own work.** Before presenting a fix, ask: "Is there a simpler way? Did I introduce new issues? Would a staff engineer approve this?"

## Design Preferences & Taste

These encode how Adam wants code to look and feel. Follow these even when not explicitly stated — they're the difference between "done" and "done well."

### Architecture Preferences
- **Extend existing files over creating new ones.** If functionality fits naturally in an existing module, put it there. Only create a new file when it represents a genuinely new concern.
- **Flat module structure.** Don't nest directories unless there are 5+ files that share a clear sub-domain (e.g., `printers/`, `marketplaces/`).
- **Lazy-loaded singletons** for expensive resources (adapters, DB connections). Initialize on first access, not at import time.
- **Config via env vars > config file > hardcoded defaults.** Never add a new config option without an env var fast path.
- **Private by default.** Prefix internal functions/vars with `_`. Only expose what the public API needs.

### Code Style Preferences
- **`from __future__ import annotations`** at the top of every Python file.
- **Type hints everywhere.** Use `Optional[T]` for nullable, `dict[str, Any]` for flexible dicts, never bare `dict`.
- **Dataclasses for return types**, never raw dicts from adapters or tools. Include `.to_dict()` method with enum → string conversion.
- **Enums use string values** for JSON serialization (`PrinterStatus.IDLE = "idle"`).
- **Docstrings**: ReST format when documenting Args/Returns/Raises. Brief one-liner if the function name is self-explanatory.
- **No class inheritance unless it's the adapter pattern.** Prefer composition and plain functions.
- **Keyword-only args** for optional parameters: `def foo(required, *, optional=None)`.

### Error Handling Style
- **Specific exception types** with `cause=` parameter to preserve the chain. Never bare `except:`.
- **`PrinterError` for all adapter failures** — never let `requests.RequestException` leak to callers.
- **Structured error format** at MCP/REST boundary: `{"error": "message", "status": "error"}`. Human-readable string internally.
- **Validate at boundaries, trust internally.** Validate user/agent input at the MCP tool level and CLI entry point. Internal function-to-function calls can trust their inputs.

### Testing Style
- **One test class per logical unit** (e.g., `TestOctoPrintAdapterConstructor`, `TestGetState`).
- **Test names**: `test_<scenario>` — descriptive enough to understand without reading the body (e.g., `test_empty_host_raises`, `test_cancelling_maps_to_cancelling`).
- **`responses` library** for HTTP mocking, `unittest.mock.patch` for everything else.
- **Native pytest assertions**, not `self.assert*`. Use `pytest.raises(ExcType, match="regex")` for exception tests.
- **Test edge cases explicitly**: empty input, None, missing keys, offline printer, timeout, all enum values covered.
- **No test docstrings on individual methods** — the name should be enough. Class-level docstring listing coverage areas is fine.

### Output & UX Style
- **Dual-mode output**: Every CLI command supports `--json` for machines and rich text for humans. JSON uses the standard envelope: `{"status": "success|error", "data": {...}, "error": {...}}`.
- **Rich library with plain-text fallback.** Guard all Rich usage behind `if RICH_AVAILABLE:`.
- **Error messages include context**: what failed, why, and what to try next. Not just "operation failed."
- **Progress and status use emojis sparingly** in human mode. Never in JSON mode.

### Naming Conventions
- **Files**: lowercase_with_underscores (e.g., `slicer_profiles.py`)
- **Classes**: PascalCase (e.g., `OctoPrintAdapter`, `PrinterState`)
- **Functions/methods**: snake_case (e.g., `get_state`, `_map_flags_to_status`)
- **Constants**: UPPER_SNAKE_CASE (e.g., `_RETRYABLE_STATUS_CODES`)
- **CLI commands**: lowercase with hyphens in Click, underscores in Python (e.g., `start-print` → `start_print`)
- **Env vars**: `KILN_` prefix always (e.g., `KILN_PRINTER_HOST`)
- **MCP tools**: snake_case matching the function name (e.g., `get_printer_status`)

## Reference Implementations (Copy These Patterns)

When adding new code, find and follow the closest existing pattern. Don't invent new conventions.

| Adding...                  | Copy the pattern in...                                              |
|----------------------------|---------------------------------------------------------------------|
| New MCP tool               | `plugins/design_tools.py` or any plugin file (decorator, error handling, return format). **NEVER add new tools to server.py** — it has 120 tools; all new tools go in `plugins/`. |
| New printer adapter        | `printers/octoprint.py` (method order, retry logic, error wrapping, dataclass returns) |
| New CLI command            | `cli/main.py` → `status` command (Click decorators, context, `--json` flag, error handling) |
| New marketplace adapter    | `marketplaces/thingiverse.py` (API client pattern, auth, response normalization) |
| New test file              | `tests/test_octoprint_adapter.py` (class organization, `responses` mocking, fixture usage) |
| New dataclass/enum         | `printers/base.py` (string enum values, `to_dict()`, Optional fields) |
| New config option          | `cli/config.py` → `load_printer_config()` (env var fast path, validation, YAML fallback) |
| New safety check           | `gcode.py` → temperature validation (per-printer limits, clear error messages) |
| New JSON data file         | `data/safety_profiles.json` (keyed by printer model, validated on load) |
| New output formatter       | `cli/output.py` → `format_status()` (JSON envelope, Rich + plain-text fallback) |

**The rule:** Before writing new code, `grep` for the closest existing example and match its structure exactly. If no reference exists, propose the pattern before implementing.

## Use Existing Tools — Never Reinvent (MANDATORY)
Before writing ad-hoc scripts for printer operations, **check if an MCP tool already exists**. Kiln has 702 MCP tools. The answer is almost always yes.

| Operation                  | Use this tool — don't write a script                                |
|----------------------------|---------------------------------------------------------------------|
| Monitor a print            | `monitor_print()` in `server.py` — standardized format with progress, temps, layers, speed, errors, snapshot, health commentary. **Always use this for print monitoring.** |
| Get printer status         | `get_printer_status()` — state, temps, flags                       |
| Get job info               | `get_print_job()` — file, progress, layers, time                   |
| Get AMS/material info      | `ams_status()` — tray colors, types, amounts                       |
| Take a camera snapshot     | `camera_snapshot()` — saves to temp file, returns path              |
| Upload and print a file    | `upload_file()` + `start_print()` — handles FTPS/REST per adapter  |
| Print multiple copies      | `multi_copy_print()` — prints N copies of a model sequentially with monitoring |
| Re-slice with overrides    | `reslice_with_overrides()` — re-slice a model with custom slicer settings |
| Re-slice and print         | `run_reslice_and_print()` — re-slice + upload + print in one step  |
| Extract model from 3MF     | `extract_model_from_3mf()` — pull embedded mesh from .3mf/.gcode.3mf → STL |

**The rule:** `grep "^def \|^async def " src/kiln/server.py | grep -i "<keyword>"` before writing any printer interaction script. If a tool exists, call it. If it doesn't, build it as a proper MCP tool first — don't ship a one-off script.

**Why this matters:** Context window rotations lose knowledge of what tools exist. This table survives rotations because CLAUDE.md is loaded at session start.

## Subagent Strategy
- **Solo by default.** Most tasks don't need subagents. Do it inline unless there's a clear reason not to.
- **Inline first, subagent only if necessary.** Try a direct Grep/Glob/Read first. Only escalate to a subagent if: the search requires 5+ rounds of exploration, touches 5+ files, or would significantly bloat the main context.
- **One task per subagent.** Give each subagent a focused, specific job. Don't ask a subagent to "investigate and fix" — ask it to "find all usages of X" and process the results yourself.
- **Always use haiku-model subagents** for research tasks (grepping, file reading, analysis). Only use sonnet/opus subagents when the subagent itself must write non-trivial code or do complex multi-step reasoning.
- **Never spawn parallel subagents for tasks that could be done sequentially in under 5 minutes.** Two sequential greps are cheaper than two parallel subagent spawns. The overhead of spawning is real — only parallelize when each branch would take significant work independently.

## Build & Test
- **Use `python3` and `pip3`** (not `python`/`pip`) — on macOS, `python` may not exist or may point to a system Python 2.
- Two Python packages in this monorepo:
  - **kiln** (MCP server): `kiln/` — entry point `python3 -m kiln` or `kiln`
  - **octoprint-cli** (CLI tool): `octoprint-cli/` — entry point `octoprint-cli`
- Build system: `pyproject.toml` + setuptools for both packages
- Tests: `kiln/tests/` and `octoprint-cli/tests/` (pytest)
- After making Python edits, verify with: `cd kiln && python3 -m py_compile src/kiln/<file>.py` or run `pytest`
- Install for development: `pip3 install -e "./kiln[dev,bambu]"` and `pip3 install -e "./octoprint-cli[dev]"`
- **Linting**: Ruff is configured in both `pyproject.toml` files. Pre-commit hooks run Ruff lint + format automatically.
- **MANDATORY: Run Ruff before every push.** After any Python edits, run `cd kiln && python3 -m ruff check src/kiln/` (and/or the octoprint-cli equivalent). CI runs Ruff lint and will fail on any violation. Never push without verifying lint passes locally first.
- **CI verification after push**: After pushing, check CI status with `gh run list --limit 1`. If it fails, fix immediately — don't leave CI red.

## Release Process (Version Bumps)
When bumping the version for a new release:
1. **Update `kiln/pyproject.toml`** — bump `version = "X.Y.Z"`
2. **Update `server.json`** — bump both top-level `version` and `packages[0].version` to match (the CI workflow auto-syncs this from the git tag, but keep it in sync in the repo too)
3. **Update `docs/site/src/layouts/BaseLayout.astro`** — bump `softwareVersion` in the JSON-LD structured data
4. **Update the Kiln Desktop App** — the native macOS SwiftUI app in the private `forge-internal` repo (`kiln-desktop/`) wraps Kiln's MCP tools. On version bumps:
   - Update any hardcoded Kiln version references in the desktop app
   - If new MCP tools were added, ensure the desktop app's `KilnToolRegistry` and `KilnToolExecutor` can surface them
   - If new safety features, alert types, or material profiles were added, update `NativeMonitor` and `AlertInjector` to leverage them
   - If new design intelligence was added (materials, patterns), update the `DesignVisualizer` material system
   - The desktop app lives at: `github.com/codeofaxel/forge-internal/kiln-desktop/`
5. Commit, then Adam creates a GitHub Release (`gh release create vX.Y.Z`) which auto-triggers:
   - PyPI publish (trusted publishing, no token needed)
   - MCP Registry publish (OIDC, no token needed)
6. Both PyPI and MCP Registry are **fully automated** on release — no manual publish commands needed.

## Debugging Approach
- Trace bugs end-to-end: MCP tool call → server.py handler → PrinterAdapter method → HTTP request → OctoPrint API
- For adapter bugs, check the abstract interface in `base.py` matches the concrete implementation in `octoprint.py`
- For CLI bugs, trace: Click command → client.py → HTTP → OctoPrint API → output.py formatting
- Check that printer state mapping covers all edge cases (OctoPrint flags → PrinterStatus enum)
- Use structured JSON output from the CLI for debugging response formats

## Autonomous Work Loops

### Bug Fix Loop
When asked to fix bugs or failing tests, work autonomously:
1. Identify the failure (import error, test failure, runtime bug)
2. Trace the root cause
3. Implement the fix
4. Verify the code compiles (`python3 -m py_compile`)
5. If tests exist, run them and iterate until passing
6. Report results only when done or truly blocked

Do NOT stop after step 2 to ask permission. Complete the full loop.

### Feature Implementation Loop
When asked to implement a feature, work autonomously through the full lifecycle:
1. Find the **reference implementation** (see Reference Implementations table above)
2. Read the reference file to internalize the pattern
3. **Collision check**: Before writing any new functions, grep target files for existing function names to avoid collisions. For `server.py`, run `grep "^def \|^async def " src/kiln/server.py` and verify your new names don't clash.
4. Implement following that pattern exactly — same structure, same error handling, same naming
5. Add tests following the test reference pattern (`test_octoprint_adapter.py` style)
6. Run the full test suite for the affected package: `cd kiln && python3 -m pytest tests/ -x -q` or `cd octoprint-cli && python3 -m pytest tests/ -x -q`
7. Run `python3 -m py_compile` on all changed files
8. Self-challenge gate (see below)
9. Update docs per Documentation Auto-Update Triggers if applicable
10. Report results only when done or truly blocked

Do NOT stop after step 1 to propose the plan. If a reference implementation exists, follow it and deliver the finished work.

### Refactoring Loop
When asked to refactor:
1. Read all affected files first — understand the full dependency graph
2. Run the test suite BEFORE making changes (establish baseline)
3. Make changes incrementally, verifying compilation after each file
4. Run the full test suite AFTER all changes
5. If tests fail, fix them — refactoring must be behavior-preserving unless told otherwise
6. Report the diff summary when done

### When Stuck — Decision Priority
If you're unsure how to proceed, work through this list before asking the user:
1. **Check `.dev/LESSONS_LEARNED.md`** — has this problem been solved before?
2. **Check reference implementations** — is there an existing pattern to follow?
3. **Check tests** — do existing tests document the expected behavior?
4. **Grep the codebase** — how does similar code elsewhere handle this case?
5. **Follow the simplest approach** consistent with existing code patterns
6. **Only ask the user** if:
   - The decision is **irreversible** (schema change, data migration, API contract change)
   - The decision involves a **new architectural pattern** not seen anywhere in the codebase
   - There are **2+ equally valid approaches** with meaningfully different tradeoffs
   - The requirement is **genuinely ambiguous** — not just unfamiliar

**Default bias: act, don't ask.** It's faster for Adam to review a finished implementation than to answer a question about a hypothetical one.

## File Lookup Rule (MANDATORY)
- **Internal working docs live in `.dev/`.** When the user references a file by name (e.g., "longterm_vision_tasks", "tasks", "lessons learned", "completed tasks", "swarm guide"), look in `.dev/` first — never glob the entire repo.
- **Key directories:** `kiln/src/kiln/` (MCP server), `kiln/src/kiln/cli/` (CLI), `kiln/src/kiln/printers/` (adapters), `kiln/src/kiln/data/` (JSON data files), `kiln/tests/` (pytest), `octoprint-cli/` (CLI tool package), `docs/` (public docs), `.dev/` (internal working docs)

## Desktop App (forge-internal/kiln-desktop)

A native macOS SwiftUI desktop app lives in the private `forge-internal` repo at `kiln-desktop/`. It wraps Kiln's MCP tools in a premium desktop experience — Kiln is the brain, the app is the interface.

When pushing new Kiln version releases:
- **KilnToolRegistry.swift / KilnAPIClient.swift** may need updating to reflect new or changed MCP tools
- **NativeMonitor.swift** polls Kiln tools (`monitor_print`, `check_print_health`, `printer_trend_analysis`, etc.) — verify these still match after tool signature changes
- **AlertInjector.swift** maps Kiln's safety/monitoring responses to native macOS notifications — update when new alert types or safety features are added
- New materials, design patterns, or safety profiles should be reflected in the **DesignVisualizer**'s material system and the Models tab's Design Library
- Build and test: `cd forge-internal/kiln-desktop && swift build`

The `forge-internal/CLAUDE.md` has the full desktop app architecture reference (key files, tier system, build instructions).

## Hard Laws (crash/data-loss prevention — never violate these)

### 0. Always Use Kiln Tools — Never Go Raw
- **Never use raw MQTT (`paho-mqtt`), FTPS (`ftplib`), ffmpeg, or direct socket connections** for printer operations. Always use `BambuAdapter` / `OctoPrintAdapter` / `MoonrakerAdapter` methods (e.g., `upload_file()`, `start_print()`, `get_snapshot()`, `get_state()`, `list_files()`, `delete_file()`).
- **If a Kiln tool fails, fix the tool** — don't bypass it with raw commands. The raw approach will fail in different ways because it misses protocol details Kiln already handles (auth packets, TLS quirks, path detection, error mapping).
- **If no Kiln tool exists for the operation, that's a gap** — build the MCP tool first, then use it. Don't ship a workaround.
- **This applies to ALL printer interactions**: camera snapshots, file uploads, print commands, status queries, temperature control, calibration. Kiln's adapter layer exists precisely so agents don't have to guess at protocol details.

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
- **Solo is the default for most work.** Single-file edits, 2-file changes, sequential dependencies, bug fixes, small features — all solo. Don't swarm unless the task is genuinely large.
- **Swarm only when it clearly saves significant time**: Multi-file features with 4+ independent components that each require substantial work (not one-line edits), or auditing 4+ unrelated subsystems simultaneously.
- **Don't swarm small tasks.** If each "subtask" is just a few lines of code or a single file edit, do them sequentially. The coordination overhead of a swarm is never justified for small work.
- **When in doubt, solo.** The cost of a swarm that wasn't needed is much higher than doing sequential work that could have been parallelized.

## Learning Reflex (Self-Improvement Loop)
When the user corrects you, points out a mistake, or you discover a non-obvious fix:
1. **Immediately** append the pattern to `.dev/LESSONS_LEARNED.md` under the relevant section
2. Write it as a reusable rule: what went wrong, why, and the correct pattern
3. Keep entries concise (3-5 lines max)
4. This is NOT optional — every correction becomes institutional knowledge

**Triggers:** User says "no, that's wrong", "actually you should...", "that's not how X works", a fix takes 2+ attempts, a test/validation fails for a non-obvious reason. When in doubt, file the lesson.

### Correction → Rule Flywheel
Every time Adam edits or corrects AI output, that correction should become a permanent rule. The goal is to **never get the same correction twice.**

- If Adam changes code style → add to "Design Preferences & Taste"
- If Adam changes architecture → add to "Reference Implementations" or "Design Preferences"
- If Adam rejects an approach → add to "When Stuck" or "Design Preferences"
- If a bug fix was non-obvious → add to "Lessons Learned" (already covered)
- If Adam says "always do X" or "never do Y" → add as a rule in the most relevant section

**After adding the rule**, briefly confirm: "Added to CLAUDE.md: [one-line summary]" so Adam knows it's captured and can correct it immediately if the rule is wrong.

## Self-Challenge Gate (Mandatory Before Presenting Work)
Before reporting ANY non-trivial work as complete, run this checklist. If ANY answer is "no," **iterate silently until it's "yes."** Do not present work that fails a check — fix it first.

1. **Code valid?** (imports resolve, no syntax errors, type hints consistent)
2. **Root cause addressed?** Not a band-aid. The actual underlying issue is fixed.
3. **Blast radius minimal?** Only the necessary files were changed. No drive-by refactors.
4. **Edge cases handled?** None, empty, error states, offline printer, timeout — not just the happy path.
5. **Simpler solution exists?** Re-read the code. Is there a 5-line version of your 20-line fix? Use it.
6. **Staff engineer test:** Would a senior infrastructure engineer at a top company approve this on first review? If "probably not" or "maybe" — iterate. Only present when the answer is "yes, confidently."

**The rule:** Do not present output you wouldn't ship to production. If your internal confidence is below "I'd bet money this is correct and clean," keep working. When in doubt, iterate one more time — the cost of one extra pass is always less than the cost of a sloppy delivery.

## Judges Panel (Mandatory for Non-Trivial Work)

After the self-challenge gate passes, run the work through three harsh judges. Each scores 0-100. **The work is not done until all three would score it ≥95.** If any judge is below 95, iterate on their specific feedback before presenting.

Write the scorecards in first person as each judge. Be genuinely harsh — these judges have zero patience for "good enough."

### Steve Jobs — Product & User Experience
*"Does this just work? Would a user pay for this? Is the experience seamless or does it leak implementation details? If I have to explain how it works, it's broken. Ship things people love, not things engineers admire."*
- Does it solve a real user need without requiring the user to think?
- Is the output good enough that users trust it immediately?
- Does the error case guide the user, or just fail?
- Would you demo this on stage with confidence?

### Jony Ive — Design & Craft
*"Is every detail intentional? Does the surface betray the implementation? Good design is honest — it doesn't pretend to be something it isn't, and it doesn't expose complexity the user didn't ask for."*
- Does the visual/API output feel intentional and polished?
- Are there artifacts, seams, or rough edges that break the illusion?
- Is there unnecessary ornamentation, or is every element earning its place?
- Does it feel like one coherent system, not a collection of parts?

### antirez (Salvatore Sanfilippo) — Code Quality & Elegance
*"Is every line earning its place? Code should read like prose — clear intent, no wasted words. Every abstraction must justify its existence. Every magic number is a decision you didn't document."*
- Could any function be shorter without losing clarity?
- Are there unnecessary abstractions, indirections, or layers?
- Are magic numbers named and explained?
- Does the code read top-to-bottom without needing to jump around?
- Is the data flow obvious? Would a stranger understand it in one pass?

**Iteration protocol:** When a judge scores below 95, fix their specific concerns silently, then re-score. Don't ask for permission to iterate — just do it and present the improved version. The user should only see work that all three judges approve.

## Definition of Done

A feature/fix is "done" — not "done enough" — when ALL of these are true:

1. **Code works** — compiles, passes all tests, handles edge cases
2. **Pattern-consistent** — matches the reference implementation's structure, naming, error handling
3. **Tests exist** — new behavior has tests; modified behavior has updated tests. No untested MCP tools, CLI commands, or adapter methods.
4. **Docs updated** — if Documentation Auto-Update Triggers apply, the docs are already updated in the same work session. Don't leave this as a follow-up.
5. **Lint clean** — Ruff passes locally (`python3 -m ruff check src/kiln/`). Don't leave format violations for pre-commit or CI to catch.
6. **CI green** — After pushing, verify CI passes (`gh run list --limit 1`). If red, fix immediately before reporting work as done.
7. **Self-challenge gate passed** — the 6-point checklist above all answered "yes"

**NOT done if:** tests are skipped with a comment, docs say "TODO: update", error handling says `pass`, CI is red, or you need to come back and "clean up later." Deliver complete work or flag what's blocking completion.

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
9. **Version bump / release** → Republish to ClawHub: `clawhub publish .dev --slug kiln --name "Kiln" --version X.Y.Z --tags "3d-printing,manufacturing,printer,mcp,octoprint,bambu,moonraker,klipper,prusa,ai-agent" --changelog "summary"`. Also update MCP tool counts across docs if they changed (grep for the old count).

**When NOT to update:**
- Bug fixes, refactors, or internal changes that don't add user-facing features.
- Test additions without new features.
- Documentation-only changes (avoid circular updates).

**How to update:** Append or edit the specific section — don't rewrite the entire document. Keep the whitepaper formal and the guide reference-dense.

### Hard rule: NEVER edit historical metrics in old blog posts (MANDATORY)

Old blog posts are **point-in-time snapshots**. The MCP tool count, CLI command count, test count, printer-model count, and any other numeric metric stated in a published post is part of the historical record of what Kiln was on the date of that post. **NEVER update these numbers when doing a doc count sync.**

- Files this applies to: every `*.astro` file under `docs/site/src/pages/blog/`, the per-post card descriptions in `docs/site/src/pages/blog.astro`, and any future `docs/site/src/pages/blog/**` post or social-card SVG (e.g. `docs/site/public/blog/*.svg`).
- This includes the title, body, bullet lists, "What ships today" sections, and meta `description=` props of those posts.
- Editing a Feb 17 launch post to claim 559 MCP tools (when Feb 17 had 197) is a credibility-destroying revision of history. It happened repeatedly in past sessions (commits `5c05a86`, `f3ccb82`, `ad1945f`, `1644602b`, `454f20fa` all touched historical post numbers) and must stop.
- When grepping for stale counts to update, **explicitly exclude `docs/site/src/pages/blog/` and any blog-post asset files** before running batch edits.
- If a published post contains a count that's actually wrong for its post date, restore it to the count that was correct on the post's date — not to the current count, and not to whatever someone else last edited it to.
- The doc-count sync flow only updates **currently-true marketing surfaces** (README, server.json, SKILL.md, whitepaper, litepaper, PROJECT_DOCS, THREAT_MODEL, llms.txt, the live site shell — Hero/FeatureGrid/pricing/install/integrations/use-cases/faq, the desktop app strings, plus any other surface that asserts a current claim).

## Session Continuity
For long or multi-session work, maintain a lightweight state file so crashed or context-exhausted sessions can be resumed cleanly.

- **File**: `.dev/SESSION_STATE.md` (gitignored — local working state only)
- **When to update**: At natural milestones during long sessions (after each major task completes), and always before ending a session with pending work.
- **What to capture**:
  1. Current branch and last commit
  2. Completed tasks this session
  3. In-progress task with specific details on what's done and what remains
  4. Key decisions or context that would be lost between sessions
  5. Known blockers or issues
- **On session start**: If `.dev/SESSION_STATE.md` exists, read it first to pick up where the last session left off. Don't re-do completed work.
- **Keep it under 2000 tokens** — enough to resume, not a full transcript.

## Reference Docs
- `README.md` — Project overview and quick start. Keep concise.
- `docs/WHITEPAPER.md` — Technical whitepaper in academic style. Covers architecture, safety, protocol design.
- `docs/PROJECT_DOCS.md` — Full project documentation (Gitbook-style). CLI reference, MCP tool catalog, adapter details, configuration.
- `.dev/COMPLETED_TASKS.md` — Record of shipped features. Append after each feature lands.
- `.dev/TASKS.md` — Open task backlog. **When completing a task from TASKS.md, always move it to COMPLETED_TASKS.md** (append a new entry at the top with today's date). Remove the completed item from TASKS.md. This is mandatory — never leave a shipped feature only in the backlog.
- `.dev/LESSONS_LEARNED.md` — Hard-won technical patterns and bug fixes. Consult when hitting unfamiliar issues. **Append to this file when you learn something new.**
- `.dev/roles/` — Slim role references (LOGIC.md, INTERFACE.md, QA.md, INTEGRATION.md) used for swarm teammate spawn prompts.
- `.dev/SWARM_GUIDE.md` — Full guide to the agent swarm system.
