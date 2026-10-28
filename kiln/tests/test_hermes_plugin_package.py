"""The Hermes Agent package at plugins/hermes stays tied to the release it runs.

A catalog listing pins one commit of this repo, and Hermes keeps only the
package folder, so the code it runs is the kiln3d release named in mcp.json.
That pin, the package version and the skill all drift silently unless
something ties them to the repo: this file is the tie.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "plugins" / "hermes"


def _pyproject_version() -> str:
    text = (ROOT / "kiln" / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'(?m)^\s*version\s*=\s*"([^"]+)"\s*$', text)
    assert match, "could not find version in kiln/pyproject.toml"
    return match.group(1)


def _server() -> dict:
    config = json.loads((PACKAGE / "mcp.json").read_text(encoding="utf-8"))
    assert config["$schema"] == "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json"
    assert list(config["mcpServers"]) == ["kiln"]
    return config["mcpServers"]["kiln"]


def test_package_version_matches_the_release() -> None:
    manifest = json.loads((PACKAGE / "plugin.json").read_text(encoding="utf-8"))
    assert manifest["name"] == "kiln"
    assert manifest["version"] == _pyproject_version(), (
        "plugins/hermes/plugin.json drifted from kiln/pyproject.toml; bump it in "
        "the same commit as the release"
    )


def test_server_runs_exactly_this_release() -> None:
    server = _server()
    assert server["type"] == "stdio"
    assert server["command"] == "uvx"
    assert server["args"] == ["--from", f"kiln3d=={_pyproject_version()}", "kiln", "serve"]


def test_kiln_never_updates_itself_inside_hermes() -> None:
    env = _server().get("env", {})
    assert env.get("KILN_UPDATE_COMMAND") == "hermes plugins update kiln"


def test_no_daily_usage_report_from_a_hermes_install() -> None:
    """The Hermes catalog requires usage reporting to be opt-in.  Off here,
    for this package only; every other install keeps its default."""
    env = _server().get("env", {})
    assert env.get("KILN_TELEMETRY") == "false"


def test_commands_kiln_tells_the_person_to_run_work_here() -> None:
    """No ``kiln`` is on PATH for a package install; the first-run terms step
    tells the person a command, and it has to be this release's."""
    env = _server().get("env", {})
    assert env.get("KILN_CLI") == f"uvx --from kiln3d=={_pyproject_version()} kiln"


def test_hermes_runs_kiln_at_its_normal_trust() -> None:
    """Kiln asks before every print itself; Hermes's untrusted mode would also
    ask before every tool that does not declare itself read-only, which today is
    most of them.  Revisit once Kiln's tools carry read-only hints."""
    manifest = json.loads((PACKAGE / "plugin.json").read_text(encoding="utf-8"))
    assert "extensions" not in manifest


def test_the_skill_is_the_repo_skill() -> None:
    packaged = (PACKAGE / "skills" / "kiln" / "SKILL.md").read_text(encoding="utf-8")
    assert packaged == (ROOT / "SKILL.md").read_text(encoding="utf-8"), (
        "plugins/hermes/skills/kiln/SKILL.md drifted from SKILL.md; copy it over "
        "(a symlink would leave the package folder, which Hermes refuses)"
    )


def test_package_ships_no_binary() -> None:
    magic = (b"\x7fELF", b"MZ", b"\xca\xfe\xba\xbe", b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\x00asm")
    for path in PACKAGE.rglob("*"):
        if path.is_file():
            head = path.read_bytes()[:4]
            assert not any(head.startswith(m) for m in magic), f"{path} is a compiled binary"
