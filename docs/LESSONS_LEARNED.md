# Kiln — Lessons Learned

Hard-won technical patterns and bug fixes. Consulted when hitting unfamiliar issues. **Append new entries under the relevant section when you learn something new.**

---

## Printer Adapter Patterns
<!-- Patterns related to PrinterAdapter interface, state mapping, data normalization -->

## OctoPrint API Quirks
<!-- Non-obvious behaviors of the OctoPrint REST API -->

## MCP Server Patterns
<!-- FastMCP tool registration, response formatting, error propagation -->

## CLI / Output Formatting
<!-- Click CLI patterns, JSON output, exit codes, config management -->

## Python / Build System
<!-- pyproject.toml, setuptools, import resolution, packaging -->

## Testing Patterns
<!-- pytest, mocking HTTP calls, mocking hardware, test isolation -->

## Configuration & Environment
<!-- Config precedence, environment variables, credential handling -->

### Bambu access_code vs api_key env vars
Bambu printers use an `access_code` (not the same as an API key). When building env-var config fast paths, don't reuse the same env var (`KILN_PRINTER_API_KEY`) for both `api_key` and `access_code` fields. Use `KILN_PRINTER_ACCESS_CODE` for Bambu access codes, with fallback to `KILN_PRINTER_API_KEY` for backward compat.

## Hardware / Safety
<!-- Physical printer safety, G-code validation, temperature limits, destructive operations -->

### Preflight checks must be enforced, not optional
If `start_print()` doesn't call `preflight_check()` internally, agents WILL skip it. Safety-critical validation should be enforced by default with an explicit opt-out (`skip_preflight=True`), not left as a "remember to call this first" contract.
