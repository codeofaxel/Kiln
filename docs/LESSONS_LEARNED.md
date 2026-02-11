# Kiln — Lessons Learned

Hard-won technical patterns and bug fixes. Consulted when hitting unfamiliar issues. **Append new entries under the relevant section when you learn something new.**

---

## Printer Adapter Patterns
<!-- Patterns related to PrinterAdapter interface, state mapping, data normalization -->

### Temperature validation belongs in the base class
Don't rely on each adapter individually validating temperature bounds — they won't. Put a shared `_validate_temp()` in the abstract `PrinterAdapter` base class and call it from every concrete `set_tool_temp()`/`set_bed_temp()`. The MCP tool layer should ALSO validate, giving defense-in-depth. Same principle applies to any safety-critical operation: validate at every layer, not just one.

### Negative temperatures bypass `temp > limit` checks
The G-code validator originally only checked `temp > MAX_TEMP`. Negative temperatures (e.g., `M104 S-50`) passed right through. Always check `temp < 0` explicitly. This is the kind of bug that's invisible in happy-path testing but catastrophic in adversarial scenarios.

## OctoPrint API Quirks
<!-- Non-obvious behaviors of the OctoPrint REST API -->

## MCP Server Patterns
<!-- FastMCP tool registration, response formatting, error propagation -->

### Never pass `**body` from HTTP requests to tool functions
`func(**body)` lets callers inject arbitrary keyword arguments. Use `inspect.signature()` to filter to only valid parameters and reject unknowns with a 400. This prevents parameter pollution attacks where extra keys override internal defaults.

### Sanitize tool results before feeding to LLM agents
Tool results from MCP tools are untrusted data — printer names, filenames, and API error messages can all contain prompt injection payloads. Always sanitize before passing to the LLM. Add a system prompt warning about untrusted tool results.

## CLI / Output Formatting
<!-- Click CLI patterns, JSON output, exit codes, config management -->

## Python / Build System
<!-- pyproject.toml, setuptools, import resolution, packaging -->

## Testing Patterns
<!-- pytest, mocking HTTP calls, mocking hardware, test isolation -->

## Configuration & Environment
<!-- Config precedence, environment variables, credential handling -->

### Bambu access_code vs api_key env vars
Bambu printers use an `access_code` (not the same as an API key). When building env-var config fast paths, don't reuse the same env var (`KILN_PRINTER_API_KEY`) for both `api_key` and `access_code` fields. Use `KILN_PRINTER_ACCESS_CODE` for Bambu access codes. DO NOT fall back to `KILN_PRINTER_API_KEY` — these are semantically different credential types and cross-contamination can cause auth failures or send wrong credentials to wrong backends.

## Hardware / Safety
<!-- Physical printer safety, G-code validation, temperature limits, destructive operations -->

### Preflight checks must be enforced, not optional
If `start_print()` doesn't call `preflight_check()` internally, agents WILL skip it. Safety-critical validation must be mandatory with NO opt-out. The original `skip_preflight=True` parameter was removed entirely — even an "advanced user" bypass is a security hole because agents will discover and use it.

### Path traversal in save/write operations
Any function that accepts a file path from an agent/user and writes to disk is a path traversal risk. Always resolve to absolute path (`os.path.realpath()`), then check it starts with an allowed prefix (home dir, temp dir). Use `tempfile.gettempdir()` resolved through `os.path.realpath()` for cross-platform temp dir detection — macOS `/tmp` resolves to `/private/tmp` and pytest fixtures use `/private/var/folders/`.

### Lock ordering prevents deadlocks
Never emit events (which trigger callbacks) while holding a lock. Callbacks may try to acquire the same lock → deadlock. Pattern: collect event data inside the lock, release the lock, THEN publish events. Applied to `materials.py:deduct_usage()` where `_emit_spool_warnings()` was called inside `with self._lock`.

### Bambu A-series sends UPPERCASE state values
A1/A1 mini printers send `gcode_state` as "RUNNING", "IDLE", "PAUSE" (all caps), unlike X1C/P1S which send lowercase. Always `.lower()` normalize `gcode_state` before matching. Also applies to MQTT `command` field — use case-insensitive comparison for all Bambu string enums.

### Bambu A-series uses implicit FTPS (port 990), not STARTTLS
A1/A1 mini requires implicit TLS on port 990 — the socket must be wrapped in TLS immediately on connect, before the FTP greeting. Standard `ftplib.FTP_TLS` uses explicit STARTTLS (connect plain, then upgrade). Requires a custom `_ImplicitFTP_TLS` subclass that wraps the socket in `connect()` and reuses the TLS session on data channels via `ntransfercmd()`.

### Never auto-print generated or unverified models
3D printers are delicate hardware. Misconfigured or malformed models (especially AI-generated ones) can cause physical damage — jammed nozzles, broken beds, stripped gears. Default to uploading only, require explicit `start_print` call. Provide opt-in toggles (`KILN_AUTO_PRINT_MARKETPLACE`, `KILN_AUTO_PRINT_GENERATED`) rather than opt-out. Surface these settings early in setup so users make a conscious decision.

### Print confirmation requires MQTT polling, not just command success
Sending a print command to Bambu via MQTT succeeds even if the printer doesn't actually start (e.g., wrong file path, lid open). Must poll `gcode_state` via MQTT to confirm the printer transitions to an active state. Without this, `start_print()` returns success while the printer sits idle.
