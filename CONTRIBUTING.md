# Contributing to Kiln

Thanks for your interest in contributing to Kiln! Here's how to get started.

## Development Setup

```bash
# Clone the repo
git clone https://github.com/codeofaxel/Kiln.git
cd Kiln

# Create a virtualenv (recommended)
python3 -m venv .venv && source .venv/bin/activate

# Install both packages in editable mode with dev extras
pip3 install -e "./kiln[dev,bambu]"
pip3 install -e "./octoprint-cli[dev]"

# Install pre-commit hooks
pip3 install pre-commit
pre-commit install
```

## Running Tests

```bash
# Kiln (5,004 tests)
cd kiln && python3 -m pytest tests/ -q

# OctoPrint CLI (223 tests)
cd octoprint-cli && python3 -m pytest tests/ -q
```

## Linting & Formatting

Ruff handles both linting and formatting. Pre-commit hooks run automatically on `git commit`, but you can run them manually:

```bash
# Check linting
ruff check kiln/ octoprint-cli/

# Auto-format
ruff format kiln/ octoprint-cli/

# Run all pre-commit hooks
pre-commit run --all-files
```

## Code Style

- Follow existing patterns -- look at neighboring code before inventing something new.
- Type hints on all public functions.
- Ruff handles formatting and import sorting. Don't fight the formatter.
- No `# TODO` in critical paths (print submission, temperature control, G-code execution, auth).

## PR Process

1. **Fork** the repo and create a feature branch from `main`.
2. **Write tests.** New features need tests. Bug fixes need regression tests.
3. **Run the full suite** before opening a PR.
4. **Keep PRs small and focused.** One logical change per PR.
5. **Open a PR against `main`** with a description of *why*, not just *what*.
6. If your PR adds a CLI command or MCP tool, update the relevant docs.

## Project Structure

This is a monorepo with two independent Python packages:

- **`kiln/`** -- MCP server + CLI (`kiln3d` on PyPI)
- **`octoprint-cli/`** -- Standalone OctoPrint CLI (`kiln3d-octoprint` on PyPI)
- **`docs/`** -- Whitepaper, project docs, task tracking

Each package has its own `pyproject.toml`, test suite, and entry point. PRs that touch both packages should have tests passing for both.

## Safety Rules (Hard Laws)

Kiln controls physical machines. These rules exist to prevent hardware damage and are non-negotiable:

1. **Pre-flight checks are mandatory.** Never bypass `preflight_check()`. Temperature, file existence, and printer state must be validated before every print.
2. **G-code must be validated.** All G-code commands pass through the validator in `gcode.py` before reaching hardware. Commands that home axes, set temperatures, or move steppers can cause physical damage if unchecked.
3. **Temperature limits are enforced.** Validate against the printer's safety profile before sending temperature commands. Never hardcode temperature limits -- use the safety profiles database.
4. **Network calls always fail.** Every HTTP/MQTT request to a printer must be wrapped in try/except. Printers go offline, networks drop, APIs timeout. Return structured errors, never raw exceptions.
5. **No credentials in source code.** API keys, host URLs, and secrets come from environment variables or config files. Never committed to the repo.
6. **Confirmation required for destructive ops.** Cancel, raw G-code, firmware updates -- these require explicit confirmation context.
7. **Normalize external data.** Printer APIs return different JSON shapes. Adapters must convert to internal dataclass types. Never pass raw API responses through to the MCP layer.

## Reporting Bugs

Open a [GitHub issue](https://github.com/codeofaxel/Kiln/issues) using the bug report template.

## Security Issues

Do **not** open a public issue for security vulnerabilities. See [SECURITY.md](SECURITY.md) for responsible disclosure instructions.
