# /debug — Thorough Debug & Whitehat Audit

A comprehensive debugging skill that investigates issues systematically, then tries to break things like a whitehat tester to find hidden problems.

## Phase 0: Context Lock (NEVER SKIP)
1. Run `git branch --show-current` — confirm and display the active branch
2. Run `git status` — check for uncommitted changes
3. Run `git log --oneline -10` — show recent commits for context
4. Read any files modified in the last 3 commits to understand recent changes
5. Report this context summary before proceeding

## Phase 1: Bug Investigation
If the user describes a specific bug:
1. **Reproduce understanding** — Restate the bug in one sentence to confirm understanding
2. **Trace end-to-end** — Follow the data/control flow through all layers:
   - MCP tool call → `server.py` handler → `PrinterAdapter` method → HTTP request → Printer API response
   - For CLI bugs: Click command → `client.py` → HTTP → API → `output.py` formatting
3. **Identify root cause** — Pinpoint the exact line(s) causing the issue
4. **Check for siblings** — Search for the same pattern elsewhere in the codebase (the same bug likely exists in similar code)
5. **Fix and verify** — Implement the fix, then verify it compiles (`python -m py_compile`)

If no specific bug is mentioned, skip to Phase 2.

## Phase 2: Whitehat Stress Test (the "try to break it" phase)
After fixing the reported bug (or if no bug was specified), proactively hunt for problems in the affected area:

### 2a. Data Flow Integrity
- **API response audit**: Compare what the printer API actually returns to what the adapter parses — flag any fields accessed but not present
- **Type coercion**: Find places where raw API values are cast without validation (e.g., `int(response["temp"])` without checking for None/string)
- **Dataclass completeness**: Verify all fields in `PrinterState`, `JobProgress`, `PrinterFile` etc. are populated — no silent `None` defaults hiding missing data
- **State mapping coverage**: Check that `_map_state()` handles ALL known printer state combinations, not just common ones

### 2b. Edge Cases & Race Conditions
- **Printer offline**: What happens when the printer is unreachable during every operation? Does every HTTP call handle `ConnectionError`?
- **Mid-print operations**: What if you call `upload_file()` while printing? `start_print()` while already printing?
- **Empty responses**: What happens with 0 files? Empty job progress? Printer just booted with no state?
- **Concurrent tool calls**: Can two MCP tool calls hit the printer simultaneously and cause conflicts?
- **Timeout exhaustion**: What happens when all HTTP retries fail? Is the error message useful?

### 2c. Security Audit
- **Credential exposure**: Are API keys logged, printed, or included in error responses sent to agents?
- **Path traversal**: Can a malicious filename in `upload_file()` escape the intended directory?
- **G-code injection**: Can raw G-code commands be used to damage the printer (unrestricted temperature, axis beyond bounds)?
- **Config file security**: Are config files with API keys created with restrictive permissions?
- **Input validation**: Check all user/agent-supplied inputs for injection or overflow potential

### 2d. Adapter Contract Compliance
- **Abstract method coverage**: Does the adapter implement ALL methods from `PrinterAdapter`?
- **Return type correctness**: Does every method return the exact dataclass type declared in the abstract interface?
- **Error handling consistency**: Does every method handle connection failures the same way (return OFFLINE, don't raise)?
- **State enum exhaustiveness**: Are all `PrinterStatus` values reachable? Any dead states?

### 2e. MCP Layer
- **Tool registration**: Are all tools properly registered with correct type annotations?
- **Response format**: Do all tools return the standard `{"status": ..., "data": ...}` or `{"error": ...}` format?
- **Error propagation**: Do adapter exceptions bubble up as clean error responses, or as stack traces?
- **Lazy init safety**: What happens if `get_adapter()` fails? Is the error reported clearly?

## Phase 3: Report
Summarize findings in a concise format:

```
Branch: [branch-name]
Bug Fix: [one-line summary of fix, or "N/A"]
Verified: [py_compile passes / tests pass / fails]

Issues Found:
- [CRITICAL] Description (crash/data loss/printer safety risk)
- [HIGH] Description (functional bug)
- [MEDIUM] Description (edge case / degraded behavior)
- [LOW] Description (code smell / minor)

No Issues In: [list areas that checked out clean]
```

Then fix CRITICAL and HIGH issues automatically (verify after each fix). For MEDIUM/LOW, list them and ask if the user wants them fixed.

## Rules
- Do NOT ask for permission during investigation — just investigate
- Do NOT stop to propose a plan — execute the phases
- Use `grep` patterns to find similar bugs across the codebase
- If you find a bug during the whitehat phase, fix it inline and note it in the report
- Keep all output concise — findings, not narration
