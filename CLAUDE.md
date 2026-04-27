# Kiln — Claude Code Guidelines

Sections are tagged with `[SCOPE]` hints so Opus can weight them by task type. Tags: `[ALWAYS]` applies every turn, `[BUG-FIX]` / `[NEW-FEATURE]` / `[RELEASE]` / `[BRAINSTORM]` apply for matching tasks, `[SAFETY]` is non-negotiable regardless of task.

**Playbook skills** — invoke these; do not re-derive them:
- `/ship-gate` — pre-present gate (compile, test, lint, root-cause, docs). Runs before any non-trivial work is shown to the user.
- `/judges-panel` — four-judge review (Jobs, Ive, antirez, Andreessen). Runs after ship-gate for non-trivial work.
- `/chris-audit` — Supabase/RLS security sweep. Runs on any RLS/auth-path change in kiln-pro.
- `/kiln-stats` — authoritative MCP / CLI / test counts. The skill lives in kiln-pro at `.claude/skills/kiln-stats/SKILL.md` and points at `scripts/check_doc_counts.py`. Use it any time you need to quote a count or check whether docs are stale; do not grep `@mcp.tool()` and do not invent a methodology — past sessions have re-derived these counts five different wrong ways.

## [ALWAYS] Git Workflow
- **Always run `git branch --show-current` before investigating bugs or making changes.** Do not skip this.
- Verify you're on the correct branch before reading code, grepping, or proposing fixes.
- If the user mentions a branch name, switch to it first.
- When editing documentation or changelogs, **append** new content rather than replacing existing content unless explicitly told to replace.

### Commit and push as separate Bash calls — never chained
The Claude Code permission gate that protects pushes to default
branches reads `git commit && git push origin main` as a single
"push to main" action and blocks the entire chain — even though
the commit alone would have been allowed.  Workaround:

1. Run the bare `git commit` in one Bash call.
2. Run the bare `git push` in a separate Bash call.

Same advice if the chain includes `git add`: split add+commit
from push, or run all three as separate Bash invocations.  Avoid
the compound `git commit && git push` form entirely — it costs
a round-trip and surfaces a confusing denial.

### Never reference internal product-thinking processes in committed artifacts
Personas like "Jobs", "Ive", "antirez", "Andreessen", and phrases
like "judges panel", "war room", "round 4", "Steve says", "per
Jony's note" are an **internal** thinking tool to sharpen taste.
They are **not** co-authors and they MUST NOT appear in:

- Commit messages (this repo is public on GitHub — every commit ships)
- Code comments and docstrings
- File names or branch names
- PR / issue descriptions
- Any artifact that gets committed

Describe the *change* and the *reason* neutrally, without citing
the deliberation process that produced them.

Bad:  `site(landing): receipts promoted after Jobs/Ive war-room round 2`
Good: `site(landing): promote receipts to second screen for higher proof density`

Bad code comment: `// Heading sized smaller per Jony's "visual hierarchy" note`
Good code comment: `// Heading sized smaller so the .tagline above stays the visual anchor`

When you catch a slip-up in existing committed code (e.g. an old
"war room" comment in `docs/site/src/pages/index.astro`), sanitize
it in the next natural content commit on that file — don't open a
dedicated cleanup PR for it.

### Never namedrop third-party trademarks as shorthand
Trademarks like "IKEA", "LEGO", "Apple", "Tesla", "Bambu" (when
referring to anyone other than the actual Bambu printers we
integrate with), etc., are NOT shorthand for a category. Don't use
them as flavor in:

- Commit messages (this repo is public — every commit ships)
- Code comments and docstrings
- File names, branch names, function names, variable names
- PR / issue descriptions
- ANY artifact that gets committed

The 2026-04-27 incident: a kiln-pro session-shorthand habit ("IKEA-
style assembly manual") leaked into the public Kiln `slice_and_print`
docstring AND into 2 public-Kiln commit messages. Source code was
scrubbed in a follow-up commit but the commit-message leaks remain
on pushed history because rewriting shared history is destructive.

The rule: describe what the code DOES in neutral category language.

Bad:  `IKEA-style assembly manual is generated`
Good: `flat-pack-style PDF assembly manual is generated`
Best: `a multi-page PDF assembly manual is generated`

If you reach for a trademark to convey the vibe, that's a signal
your prose is leaning on someone else's brand instead of describing
your own product. Rewrite around the actual mechanic.

### Public Kiln describes interfaces — name kiln-pro, skip the strategy
Public Kiln docstrings, comments, and READMEs MAY name kiln-pro
and MAY link `https://kiln3d.com` (or `/pricing`) — that's a
funnel, not a leak — and MAY note in one short sentence which
tier a feature requires when public-side metadata surfaces it
(e.g. "Multi-language and co-brand are kiln-pro Business+
features"). What they MUST NOT contain:

  * Internal product-thinking codenames or session shorthand:
    "velvet-rope upsell", "iPhone-moment", "war-room",
    "judges-panel", "Steve says", "per Jony's note",
    "round 4 of the panel". None of these are co-authors and
    none of them ship to a public package.
  * Tier-by-tier behavior breakdowns ("Free → returns X,
    Pro+ → returns Y, Business+ → returns Z"). That belongs in
    kiln-pro's own docstring; public Kiln describes the
    **interface contract**: "when the plugin is installed, the
    response may include an `assembly_manual` field with this
    shape."
  * Vivid marketing prose. Linking the pricing page is the
    funnel; rephrasing the pricing page in the docstring is not.

Bad public-Kiln docstring:
```
When the caller passes ``metadata["assembly_json"]`` and the user
is on Pro+, an IKEA-style multilingual manual is generated with
the iPhone-moment auto-trigger; Free-tier callers see a one-time
velvet-rope upsell.
```

Good public-Kiln docstring:
```
When ``metadata["assembly_json"]`` is provided and kiln-pro
(https://kiln3d.com) is installed, the response includes an
``assembly_manual`` field with the cached or expected PDF path.
Multi-language and co-brand are kiln-pro Business+ features
(https://kiln3d.com/pricing).
```

Why: any user can `pip show kiln3d` and read the public docstring.
Naming kiln-pro and linking the pricing page is a funnel; leaking
codenames or pricing logic is not. The 2026-04-27 incident
overcorrected and stripped the kiln-pro name entirely, killing
the funnel; the rule above is the corrected version.

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

## [ALWAYS] Communication Style
- **Be direct. Execute, don't narrate.** Show findings concisely. Don't over-explain reasoning.
- When asked to investigate or explain, provide findings directly **without asking for approval**. Only ask for approval before making destructive or irreversible changes (deleting files, force pushing, dropping tables).
- Don't propose plans for simple tasks. Just do them.
- For complex multi-file changes, briefly state your approach (2-3 sentences max) then execute.
- Never say "shall I proceed?" or "would you like me to?" for investigation, reading, or analysis tasks.
- **[BRAINSTORM] mode**: When the user says "brainstorm", "discuss", "think through", "talk about", "let's explore", or ends with "thoughts?" / "wdyt" — stay in **conversation mode**. Do NOT jump into auditing code, proposing implementation plans, or executing changes. Ask questions, explore tradeoffs, riff collaboratively. Only shift to execution when the user explicitly says to build/implement/fix.
- **Skip human-only tasks by default**: When given a task list, silently skip items requiring human action (account creation, App Store submissions, manual device testing, credential entry) unless explicitly told to include them. Focus on what you can execute autonomously.

## [SAFETY] Relationship to kiln-pro (read before moving code)
- A **private** paid-tier companion repo (`kiln-pro`) adds premium features (textures, device intelligence, billing, fleet, etc.).
- Kiln discovers pro features via `try: from kiln_pro.bridge import pro_features` with `except ImportError` fallback.
- **Free users access pro tools via kiln-pro's REST API server** (`POST /api/tools/{tool_name}`), which runs server-side with all tools loaded. Free users never install kiln-pro locally.
- **NEVER move proprietary code from kiln-pro into this repo to "make it available to free users."** The REST API proxy pattern keeps IP private while serving tools to any tier. To open a pro tool to free users, change its gate in kiln-pro (e.g., `check_pro()` → quota check), don't move the code here.
- Quota enforcement (`decoration_quota.py`) lives here. Tier resolution calls `kiln.licensing` which kiln-pro provides; without kiln-pro, tier defaults to `"free"`.

## [ALWAYS] Project Identity
- **Kiln is infrastructure, not software.** Never describe it as "automation software", "an API", or use cloud/SaaS framing. Kiln is local stdio-based infrastructure that AI agents use to control physical printers. It is not cloud-hosted.
- **Correct framing**: "3D printing infrastructure for AI agents", "printer control layer", "MCP infrastructure"
- **Wrong framing**: "automation software", "API platform", "cloud service", "printer management app"

## [ALWAYS] Code Discipline
- **Root causes only.** Never apply band-aid fixes. Trace to the actual source of the problem.
- **Minimal blast radius.** Only touch what's necessary. Don't refactor adjacent code "while you're in there" unless asked.
- **Simplicity first.** Prefer the simplest correct solution. Don't over-engineer.
- **Challenge your own work.** Before presenting, run `/ship-gate`. For non-trivial work, follow with `/judges-panel`.

### Critical Contrasts — the five patterns that have bitten us repeatedly

These are lived-in lessons. Each ❌ is something that actually shipped and hurt; each ✅ is the pattern that would have prevented it.

**1. Root cause vs band-aid** — see `/ship-gate` phase 2.1

```
❌ Bug: calibration fails when printer is warming up.
   Fix: add sleep(5) before calibration call.
   Problem: any timing skew re-triggers the bug; the true precondition is hidden.

✅ Bug: calibration fails when printer is warming up.
   Fix: poll printer.get_state() until status == READY, timeout with clear error.
   Why: the precondition ("printer must be ready") is now explicit and enforced.
```

**2. Kiln adapter vs raw protocol** — see Hard Law 0

```
❌ Need a Bambu camera frame, adapter's get_snapshot() is timing out.
   Fix: drop to raw ffmpeg with the RTSP URL.
   Problem: raw path misses TLS quirks, auth retry, and error mapping the adapter handles.
   When it fails in 2 weeks, nobody knows why.

✅ Need a Bambu camera frame, adapter's get_snapshot() is timing out.
   Fix: diagnose the adapter (increase timeout, handle new firmware response shape),
   land the fix in the adapter, ALL callers benefit. If the feature doesn't exist
   in any adapter, build the MCP tool before shipping the workaround.
```

**3. REST API proxy vs moving pro code to public** — see "Relationship to kiln-pro"

```
❌ Free users want access to texture engine (kiln-pro).
   Fix: copy texture_engine.py into Kiln, gate with check_tier("free").
   Problem: IP leaks to public repo; kiln-pro's value proposition erodes.

✅ Free users want access to texture engine (kiln-pro).
   Fix: in kiln-pro, change check_pro() → _check_texture_quota() (3/month free).
   Free users hit the REST API at api.kiln3d.com; tool runs server-side;
   source code never leaves kiln-pro; IP stays protected.
```

**4. Push discipline** — approval is explicit or it didn't happen

```
❌ Finished the feature. CI green. Push to origin main.
   Problem: Adam didn't say "push it." Past violations caused irreversible damage
   (design_styles pushed to public main without permission, April 2026).

✅ Finished the feature. CI green. Show the diff to Adam, wait for "push it" / "merge it".
   The cost of waiting 2 minutes for approval << the cost of a rollback.
```

**5. Minimal diff vs scope creep** — one fix per commit

```
❌ "Fix a typo in the status message."
   Diff: typo + rename 3 vars + reformat 2 files + extract helper function.
   Problem: reviewer can't cleanly approve "the typo fix"; rollback is ugly.

✅ "Fix a typo in the status message."
   Diff: the typo, one character changed.
   The rename and extraction (if genuinely good ideas) ship in separate commits
   with their own justification.
```

## [ALWAYS] Design Preferences & Taste

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

## [NEW-FEATURE] Reference Implementations (Copy These Patterns)

When adding new code, find and follow the closest existing pattern. Don't invent new conventions.

| Adding...                  | ✅ Copy the pattern in...                                           | ❌ Don't mimic                                                      |
|----------------------------|----------------------------------------------------------------------|----------------------------------------------------------------------|
| New MCP tool               | `plugins/design_tools.py` or any plugin file (decorator, error handling, return format). | `server.py` — it has 120+ legacy tools. All new tools go in `plugins/`. |
| New printer adapter        | `printers/octoprint.py` (method order, retry logic, error wrapping, dataclass returns) | Raw `requests` or `httpx` calls sprinkled in the adapter body — wrap in `_request()`. |
| New CLI command            | `cli/main.py` → `status` command (Click decorators, context, `--json` flag, error handling) | Commands that print without `--json` support, or that raise raw exceptions to the terminal. |
| New marketplace adapter    | `marketplaces/thingiverse.py` (API client pattern, auth, response normalization) | Returning raw API JSON — always normalize to internal dataclass types first. |
| New test file              | `tests/test_octoprint_adapter.py` (class organization, `responses` mocking, fixture usage) | `unittest.TestCase` + `self.assertEqual` — native pytest is the standard. |
| New dataclass/enum         | `printers/base.py` (string enum values, `to_dict()`, Optional fields) | Integer enums or enums without `.value` strings (breaks JSON serialization). |
| New config option          | `cli/config.py` → `load_printer_config()` (env var fast path, validation, YAML fallback) | Hardcoded defaults with no env-var override path. |
| New safety check           | `gcode.py` → temperature validation (per-printer limits, clear error messages) | Generic "value out of range" errors with no context. |
| New JSON data file         | `data/safety_profiles.json` (keyed by printer model, validated on load) | Free-form JSON without a `_meta` header or schema validation. |
| New output formatter       | `cli/output.py` → `format_status()` (JSON envelope, Rich + plain-text fallback) | Print statements scattered through business logic — formatters go in one place. |

**The rule:** Before writing new code, `grep` for the closest existing example and match its structure exactly. If no reference exists, propose the pattern before implementing.

## [ALWAYS] Use Existing Tools — Never Reinvent

Before writing ad-hoc scripts for printer operations, **check if an MCP tool already exists**. Kiln has 728+ MCP tools. The answer is almost always yes.

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

## [ALWAYS] Working Alone vs Delegating

One unified policy for "do I handle this, delegate to a subagent, or ask the user?" Consult this decision tree any time you're unsure.

### Default: solo

Most tasks don't need subagents. Most tasks don't need to ask permission. Do it inline unless one of the escalation conditions below is met.

### When to spawn a subagent

Subagent IS justified when:
- Search requires 5+ rounds of exploration, or touches 5+ files
- The task would significantly bloat the main context (e.g., reading a 2000-line log file for one number)
- You're running genuinely independent parallel branches of work (each branch is substantial — not "one file each")

Subagent is NOT justified when:
- Task is 2–3 greps or file reads
- "Subtasks" are one-line edits
- You'd spawn parallel subagents for work that's faster done sequentially (2 greps sequentially < spawning 2 subagents)

Model choice: haiku for research (grep, read, analyze). sonnet/opus only when the subagent must write non-trivial code.

### When to swarm (multiple coordinated subagents)

Swarm only when the task has 4+ genuinely independent substantial components. Don't swarm:
- Sequential dependencies ("step 1, then step 2, …")
- Small tasks dressed up with fancy coordination
- Anything a single session could do in under 30 minutes

**When in doubt, solo.** The coordination overhead of a swarm is never justified for small or medium work.

### When to ask the user (Decision Priority)

If you're unsure how to proceed, walk this list before interrupting the user:

1. **Check `.dev/LESSONS_LEARNED.md`** — has this problem been solved before?
2. **Check reference implementations** — is there an existing pattern to follow?
3. **Check tests** — do existing tests document the expected behavior?
4. **Grep the codebase** — how does similar code elsewhere handle this case?
5. **Follow the simplest approach** consistent with existing code patterns.
6. **Only ask the user** if:
   - The decision is **irreversible** (schema change, data migration, API contract change, push to main)
   - The decision involves a **new architectural pattern** not seen anywhere in the codebase
   - There are **2+ equally valid approaches** with meaningfully different tradeoffs
   - The requirement is **genuinely ambiguous** — not just unfamiliar

**Default bias: act, don't ask.** It's faster for Adam to review a finished implementation than to answer a question about a hypothetical one.

## [ALWAYS] Build & Test
- **Use `python3` and `pip3`** (not `python`/`pip`) — on macOS, `python` may not exist or may point to a system Python 2.
- Two Python packages in this monorepo:
  - **kiln** (MCP server): `kiln/` — entry point `python3 -m kiln` or `kiln`
  - **octoprint-cli** (CLI tool): `octoprint-cli/` — entry point `octoprint-cli`
- Build system: `pyproject.toml` + setuptools for both packages
- Tests: `kiln/tests/` and `octoprint-cli/tests/` (pytest)
- After making Python edits, verify with `/ship-gate` phase 1 (compile + pytest + ruff).
- Install for development: `pip3 install -e "./kiln[dev,bambu]"` and `pip3 install -e "./octoprint-cli[dev]"`
- **Linting**: Ruff is configured in both `pyproject.toml` files. Pre-commit hooks run Ruff lint + format automatically.
- **MANDATORY: Run Ruff before every push.** `cd kiln && python3 -m ruff check src/kiln/`. CI fails on any violation.
- **CI verification after push**: `gh run list --limit 1`. If red, fix immediately.

## [BUG-FIX] Debugging Approach
- Trace bugs end-to-end: MCP tool call → server.py handler → PrinterAdapter method → HTTP request → OctoPrint API
- For adapter bugs, check the abstract interface in `base.py` matches the concrete implementation in `octoprint.py`
- For CLI bugs, trace: Click command → client.py → HTTP → OctoPrint API → output.py formatting
- Check that printer state mapping covers all edge cases (OctoPrint flags → PrinterStatus enum)
- Use structured JSON output from the CLI for debugging response formats
- After fixing: `/ship-gate` (all three phases). Skip the judges panel for pure bug fixes unless they touch user-facing surfaces.

## Autonomous Work Loops

### [BUG-FIX] Bug Fix Loop
1. Identify the failure (import error, test failure, runtime bug)
2. Trace the root cause (not just the symptom)
3. Implement the fix
4. `/ship-gate` phase 1 (compile + tests)
5. If tests exist, run them and iterate until passing
6. Report results only when done or truly blocked

Do NOT stop after step 2 to ask permission. Complete the full loop.

### [NEW-FEATURE] Feature Implementation Loop
1. Find the **reference implementation** (see Reference Implementations table above)
2. Read the reference file to internalize the pattern
3. **Collision check**: `grep "^def \|^async def " src/kiln/server.py` (or target file) to verify new function names don't clash
4. Implement following that pattern exactly — same structure, same error handling, same naming
5. Add tests following the test reference pattern (`test_octoprint_adapter.py` style)
6. `/ship-gate` (all three phases)
7. `/judges-panel` (four-judge review including Andreessen)
8. Update docs per Documentation Auto-Update Triggers if applicable
9. Report results only when done or truly blocked

Do NOT stop after step 1 to propose the plan. If a reference implementation exists, follow it and deliver the finished work.

### Refactoring Loop
1. Read all affected files first — understand the full dependency graph
2. Run the test suite BEFORE making changes (establish baseline)
3. Make changes incrementally, verifying compilation after each file
4. `/ship-gate` phase 1 AFTER all changes
5. If tests fail, fix them — refactoring must be behavior-preserving unless told otherwise
6. Report the diff summary when done

## [ALWAYS] File Lookup Rule
- **Internal working docs live in `.dev/`.** When the user references a file by name (e.g., "longterm_vision_tasks", "tasks", "lessons learned", "completed tasks", "swarm guide"), look in `.dev/` first — never glob the entire repo.
- **Key directories:** `kiln/src/kiln/` (MCP server), `kiln/src/kiln/cli/` (CLI), `kiln/src/kiln/printers/` (adapters), `kiln/src/kiln/data/` (JSON data files), `kiln/tests/` (pytest), `octoprint-cli/` (CLI tool package), `docs/` (public docs), `.dev/` (internal working docs)

## [RELEASE] Desktop App (Kiln-pro/kiln-desktop)

The canonical native macOS SwiftUI desktop app lives in the **kiln-pro** repo at `kiln-desktop/` (`/Users/adamarreola/Kiln-pro/kiln-desktop/`). It wraps Kiln + kiln-pro features in a premium desktop experience — Kiln is the brain, the app is the interface.

**Note**: There's also a `kiln-desktop/` folder in `forge-internal/` — this is a separate experimental workspace, NOT the canonical app. Any desktop app changes referenced in version bumps or release notes point to the Kiln-pro location.

When pushing new Kiln version releases:
- **KilnToolRegistry.swift / KilnAPIClient.swift** may need updating to reflect new or changed MCP tools
- **NativeMonitor.swift** polls Kiln tools (`monitor_print`, `check_print_health`, `printer_trend_analysis`, etc.) — verify these still match after tool signature changes
- **AlertInjector.swift** maps Kiln's safety/monitoring responses to native macOS notifications — update when new alert types or safety features are added
- New materials, design patterns, or safety profiles should be reflected in the **DesignVisualizer**'s material system and the Models tab's Design Library
- Build and test: `cd /Users/adamarreola/Kiln-pro/kiln-desktop && swift build -c release`

The `Kiln-pro/CLAUDE.md` § "Desktop App" has the full architecture reference (API client wiring, tier system, install commands).

## [RELEASE] Release Process (Version Bumps)
When bumping the version for a new release:
1. **Update `kiln/pyproject.toml`** — bump `version = "X.Y.Z"`
2. **Update `server.json`** — bump both top-level `version` and `packages[0].version` to match (the CI workflow auto-syncs this from the git tag, but keep it in sync in the repo too)
3. **Update `docs/site/src/layouts/BaseLayout.astro`** — bump `softwareVersion` in the JSON-LD structured data
4. **Update the Kiln Desktop App** in Kiln-pro (see "Desktop App" section above) — hardcoded version references, new MCP tools surfaced via `KilnToolRegistry`, new safety/material features surfaced via `NativeMonitor`/`AlertInjector`/`DesignVisualizer`.
5. Commit, then Adam creates a GitHub Release (`gh release create vX.Y.Z`) which auto-triggers:
   - PyPI publish (trusted publishing, no token needed)
   - MCP Registry publish (OIDC, no token needed)
6. Both PyPI and MCP Registry are **fully automated** on release — no manual publish commands needed.

## [SAFETY] Hard Laws (crash/data-loss prevention — never violate these)

### 0. Always Use Kiln Tools — Never Go Raw
- **Never use raw MQTT (`paho-mqtt`), FTPS (`ftplib`), ffmpeg, or direct socket connections** for printer operations. Always use `BambuAdapter` / `OctoPrintAdapter` / `MoonrakerAdapter` methods (e.g., `upload_file()`, `start_print()`, `get_snapshot()`, `get_state()`, `list_files()`, `delete_file()`).
- **If a Kiln tool fails, fix the tool** — don't bypass it with raw commands. Raw paths miss protocol details Kiln already handles (auth packets, TLS quirks, path detection, error mapping).
- **If no Kiln tool exists for the operation, that's a gap** — build the MCP tool first, then use it. Don't ship a workaround.
- See Critical Contrasts #2 above for the concrete pattern.

### 1. Printer Safety First
- **Pre-flight check is mandatory**: Never bypass `preflight_check()`. Temperature, file existence, and printer state MUST be validated.
- **Confirm before destructive ops**: `cancel_print()`, `start_print()`, raw G-code commands always require explicit confirmation context.
- **Never send raw G-code without validation**: commands that home axes, set temperatures, or move steppers can cause physical damage. Validate command safety.

### 2. Adapter Interface Contract
Every new printer adapter MUST:
- Implement ALL abstract methods from `PrinterAdapter` in `base.py`
- Return the correct dataclass types (never raw dicts)
- Map all backend states to `PrinterStatus` enum (no silent fallthrough)
- Handle connection failures gracefully (return OFFLINE, don't raise)

### 3. Error Boundary Discipline
- **Network calls always fail**: Every HTTP request to a printer MUST be wrapped in try/except. Printers go offline, networks drop, APIs timeout.
- **Structured error responses**: Never return raw exception messages to agents. Always wrap in `{"error": ..., "status": ...}` format.
- **No silent failures**: If an operation fails, the agent MUST know. Never swallow exceptions.

### 4. Configuration Safety
- **Never hardcode credentials**: API keys, host URLs, and secrets come from environment variables or config files. Never in source code.
- **Validate config on load**: Missing or malformed config should fail fast with a clear error, not silently use defaults that hit production printers.
- **Config file permissions**: Warn if config files containing API keys are world-readable.

### 5. No-TODO Critical Paths
No `// TODO` or `# TODO` in: print job submission, file upload, temperature control, G-code execution, or authentication flows. Code must be fully implemented or error-stubbed with user-visible feedback.

### 6. Type Safety at Boundaries
- **Normalize external data**: OctoPrint/Moonraker/Bambu APIs all return different JSON shapes. Adapters MUST normalize to internal dataclass types.
- **Validate before forwarding**: Never pass raw API responses through to the MCP layer. Parse, validate, type-check.
- **Enum exhaustiveness**: When adding new printer states or capabilities, update ALL switch/match statements across the codebase.

## [ALWAYS] Correction Reflex (Self-Improvement Loop)

Every correction from Adam becomes a permanent rule. **Goal: never get the same correction twice.**

**Triggers** — file a lesson when:
- User says "no, that's wrong", "actually you should...", "that's not how X works"
- A fix takes 2+ attempts
- A test/validation fails for a non-obvious reason
- You discover a non-obvious pattern

**Where the rule lands** depends on what was corrected:
- Code style / naming → add to "Design Preferences & Taste"
- Architecture / patterns → add to "Reference Implementations"
- Approach / workflow → add to "Working Alone vs Delegating" or "Code Discipline"
- Non-obvious bug fix → append to `.dev/LESSONS_LEARNED.md` (3-5 lines max)
- "Always do X" / "never do Y" → add to the most relevant section (often Hard Laws if safety-related, or Design Preferences)

**After adding the rule**, briefly confirm: "Added to CLAUDE.md: [one-line summary]" so Adam knows it's captured and can correct it immediately if the rule is wrong.

## [ALWAYS] Pre-Present Gates (Ship Gate + Judges Panel)

**Every non-trivial work item passes through `/ship-gate` before presenting.** For new features / MCP tools / CLI commands / adapters / safety changes, follow with `/judges-panel`.

Skip `/judges-panel` only for pure bug fixes and trivial edits. Never skip `/ship-gate`.

If `/ship-gate` fails, iterate silently — don't present broken work. If `/judges-panel` has any judge below 95, iterate on their specific concern. See the skill files for full checklists.

**Andreessen is always the 4th judge.** His role is to stress-test business implications and debate craft-vs-business tradeoffs with Jobs, Ive, and antirez. When he disagrees with the others, surface the debate in the output — don't silently resolve it.

## [RELEASE] Documentation Auto-Update Triggers

Kiln maintains three living documents that must stay in sync with the codebase:
- `README.md` — Project overview, quick start, feature summary
- `docs/WHITEPAPER.md` — Technical whitepaper (architecture, protocol, safety model)
- `docs/PROJECT_DOCS.md` — Full project documentation (CLI reference, MCP tools, adapter details)

**When to update:**

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

### [SAFETY] Hard rule: NEVER edit historical metrics in old blog posts

Old blog posts are **point-in-time snapshots**. The MCP tool count, CLI command count, test count, printer-model count, and any other numeric metric stated in a published post is part of the historical record of what Kiln was on the date of that post. **NEVER update these numbers when doing a doc count sync.**

- Files this applies to: every `*.astro` file under `docs/site/src/pages/blog/`, the per-post card descriptions in `docs/site/src/pages/blog.astro`, and any future `docs/site/src/pages/blog/**` post or social-card SVG (e.g. `docs/site/public/blog/*.svg`).
- This includes the title, body, bullet lists, "What ships today" sections, and meta `description=` props of those posts.
- Editing a Feb 17 launch post to claim 559 MCP tools (when Feb 17 had 197) is a credibility-destroying revision of history. It happened repeatedly in past sessions (commits `5c05a86`, `f3ccb82`, `ad1945f`, `1644602b`, `454f20fa` all touched historical post numbers) and must stop.
- When grepping for stale counts to update, **explicitly exclude `docs/site/src/pages/blog/` and any blog-post asset files** before running batch edits.
- If a published post contains a count that's actually wrong for its post date, restore it to the count that was correct on the post's date — not to the current count, and not to whatever someone else last edited it to.
- The doc-count sync flow only updates **currently-true marketing surfaces** (README, server.json, SKILL.md, whitepaper, litepaper, PROJECT_DOCS, THREAT_MODEL, llms.txt, the live site shell — Hero/FeatureGrid/pricing/install/integrations/use-cases/faq, the desktop app strings, plus any other surface that asserts a current claim).

## [ALWAYS] Session Continuity
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
