# Contributing to Kiln

Thanks for your interest in contributing to Kiln! Here's how to get started.

## Development Setup

```bash
# Clone the repo
git clone https://github.com/codeofaxel/Kiln.git
cd Kiln

# Install both packages in editable mode
pip install -e "./kiln[dev,bambu]"
pip install -e "./octoprint-cli[dev]"

# Install pre-commit hooks
pip install pre-commit
pre-commit install

# Run tests
cd kiln && pytest tests/ -q
cd ../octoprint-cli && pytest tests/ -q
```

## Pre-Commit Hooks

This project uses [pre-commit](https://pre-commit.com/) with [Ruff](https://docs.astral.sh/ruff/) for linting and formatting. Hooks run automatically on `git commit`:

- **Ruff lint** — catches common errors, import sorting, Python upgrades
- **Ruff format** — consistent code formatting
- **Trailing whitespace / end-of-file** — basic hygiene
- **Large file check** — blocks files > 500KB from being committed

To run hooks manually on all files:

```bash
pre-commit run --all-files
```

## Project Structure

- **`kiln/`** — MCP server package (`kiln3d` on PyPI)
- **`octoprint-cli/`** — Standalone OctoPrint CLI (`kiln3d-octoprint` on PyPI)
- **`docs/`** — Whitepaper, project docs, task tracking

This is a monorepo with two independent Python packages. Each has its own `pyproject.toml`, test suite, and entry point. PRs that touch both packages should have tests passing for both.

## Making Changes

1. **Fork and branch** — Create a feature branch from `main`.
2. **Write tests** — New features need tests. Bug fixes need regression tests.
3. **Run the full suite** — `cd kiln && pytest tests/ -q` (2,730+ tests should pass).
4. **Keep commits focused** — One logical change per commit.

## Code Style

- Type hints on all public functions.
- Docstrings on public classes and functions.
- No `# TODO` in critical paths (print submission, temperature control, G-code execution, auth).
- Use the existing patterns — look at how neighboring code does it before inventing something new.
- Ruff handles formatting and import sorting — don't fight the formatter.

## Safety-Critical Code

Kiln controls physical machines. Extra care is required when modifying:

- **Temperature control** — always validate against safety profiles before sending to hardware
- **G-code execution** — all commands must pass through the validator in `gcode.py`
- **Print submission** — pre-flight checks are mandatory and cannot be bypassed
- **Authentication** — no credentials in source code, ever

See `docs/LESSONS_LEARNED.md` for patterns and pitfalls discovered during development.

## Pull Requests

- Keep PRs small and focused. Big PRs take forever to review.
- Describe *why*, not just *what*.
- If your PR adds a CLI command or MCP tool, update the relevant docs.

## Reporting Bugs

Open a [GitHub issue](https://github.com/codeofaxel/Kiln/issues) with:
- Kiln version and Python version
- Printer type (OctoPrint / Moonraker / Bambu / Prusa Connect)
- Steps to reproduce
- Expected vs actual behavior

## Security Issues

Do **not** open a public issue for security vulnerabilities. See [SECURITY.md](SECURITY.md) for responsible disclosure instructions.
