# Contributing to Kiln

Thanks for your interest in contributing to Kiln! Here's how to get started.

## Development Setup

```bash
# Clone the repo
git clone https://github.com/codeofaxel/Kiln.git
cd Kiln

# Install both packages in editable mode
pip install -e ./kiln[dev]
pip install -e ./octoprint-cli[dev]

# Run tests
cd kiln && pytest tests/ -q
cd ../octoprint-cli && pytest tests/ -q
```

## Project Structure

- **`kiln/`** — MCP server package (`kiln3d` on PyPI)
- **`octoprint-cli/`** — Standalone OctoPrint CLI (`kiln3d-octoprint` on PyPI)
- **`docs/`** — Whitepaper, project docs, task tracking

## Making Changes

1. **Fork and branch** — Create a feature branch from `main`.
2. **Write tests** — New features need tests. Bug fixes need regression tests.
3. **Run the full suite** — `cd kiln && pytest tests/ -q` (2,400+ tests should pass).
4. **Keep commits focused** — One logical change per commit.

## Code Style

- Type hints on all public functions.
- Docstrings on public classes and functions.
- No `# TODO` in critical paths (print submission, temperature control, G-code execution, auth).
- Use the existing patterns — look at how neighboring code does it before inventing something new.

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
