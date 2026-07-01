"""Tests for ``kiln install-mcp`` / ``kiln uninstall-mcp``.

The installer is the command every Kiln user runs to wire their agent
(Claude Desktop, Claude Code, Codex) into Kiln.  Coverage here:

- registration: both commands attach to a CLI group.
- happy path: writes JSON / TOML configs with the selected executable.
- prefix args: ``python -m kiln`` launches preserve the ``-m kiln``
  prefix so MCP clients can reproduce the module launch.
- PATH warning: when the installer chooses a different binary than
  PATH would resolve, the user sees both paths.
- auth gate: ``--force`` skips the signed-in check; without it, an
  empty ``~/.kiln/auth_tokens.json`` blocks the install with a clear
  error.
- uninstall: removes only the ``kiln`` entry, leaving siblings + other
  top-level keys intact.
"""
from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

try:  # Python 3.11+ ships tomllib; 3.10 environments may use tomli.
    import tomllib as _toml_parser  # type: ignore[no-redef]
except ModuleNotFoundError:  # pragma: no cover - 3.10 fallback
    import tomli as _toml_parser  # type: ignore[no-redef]


def _load_toml(text: str) -> dict:
    """Parse TOML text into a dict.

    Used so assertions compare decoded values instead of serialized
    text: a filesystem path serialized into a TOML string has its
    backslashes escaped, which is correct but breaks raw substring
    checks on Windows.
    """
    return _toml_parser.loads(text)


def _extract_leading_json(output: str) -> dict:
    """Parse the JSON snippet at the start of ``install-mcp --print``
    output.  The snippet is followed by a blank line and explanatory
    prose; ``json.JSONDecoder.raw_decode`` stops at the end of the
    object so the trailing text is ignored.  Comparing the decoded
    object keeps the assertions OS-agnostic — a path serialized into a
    JSON string has its backslashes escaped on Windows."""
    return json.JSONDecoder().raw_decode(output.lstrip())[0]


def _write_session(home: Path) -> None:
    """Seed the ``~/.kiln/auth_tokens.json`` file so ``_is_signed_in`` reads
    True — used by tests that want the already-signed-in path (no sign-in
    nudge).  Install never requires it; this only exercises that branch."""
    tokens = home / ".kiln" / "auth_tokens.json"
    tokens.parent.mkdir(parents=True)
    tokens.write_text(json.dumps({"access_token": "token"}), encoding="utf-8")


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_registers_install_mcp_command() -> None:
    import click
    from kiln.cli.install_mcp import register_install_mcp_cli

    group = click.Group("kiln")
    register_install_mcp_cli(group)

    assert "install-mcp" in group.commands
    assert "uninstall-mcp" in group.commands


# ---------------------------------------------------------------------------
# install-mcp — JSON path (Claude Desktop / Claude Code)
# ---------------------------------------------------------------------------


def test_install_mcp_writes_current_executable_path(tmp_path: Path, monkeypatch) -> None:
    """The installer must not blindly write ``command: kiln``.

    A stale public Kiln binary on PATH can expose only public tools or
    pro stubs.  The config should preserve the executable selected by
    the user or by the current installer process.
    """
    from kiln.cli import install_mcp as mod

    home = tmp_path / "home"
    _write_session(home)
    config_path = tmp_path / "claude.json"
    kiln_bin = tmp_path / "venv" / "bin" / "kiln"
    kiln_bin.parent.mkdir(parents=True)
    kiln_bin.write_text("#!/bin/sh\n", encoding="utf-8")

    monkeypatch.setenv("KILN_AUTH_HOME", str(home))
    monkeypatch.setattr(
        mod,
        "_DEFAULT_CLIENTS",
        [mod.MCPClient("claude_code", "Claude Code", config_path)],
    )

    result = CliRunner().invoke(
        mod.install_mcp,
        ["--command", str(kiln_bin)],
    )

    assert result.exit_code == 0, result.output
    config = json.loads(config_path.read_text(encoding="utf-8"))
    assert config["mcpServers"]["kiln"] == {
        "command": str(kiln_bin),
        "args": ["serve"],
    }
    assert f"MCP command: {kiln_bin}" in result.output


def test_install_mcp_print_snippet_uses_selected_command(tmp_path: Path) -> None:
    from kiln.cli.install_mcp import install_mcp

    kiln_bin = tmp_path / "bin" / "kiln"
    kiln_bin.parent.mkdir(parents=True)
    kiln_bin.write_text("#!/bin/sh\n", encoding="utf-8")

    result = CliRunner().invoke(
        install_mcp,
        ["--print", "--command", str(kiln_bin)],
    )

    assert result.exit_code == 0, result.output
    snippet = _extract_leading_json(result.output)
    assert snippet["mcpServers"]["kiln"]["command"] == str(kiln_bin)
    assert "It points at the Kiln executable" in result.output


# ---------------------------------------------------------------------------
# PATH warning
# ---------------------------------------------------------------------------


def test_path_warning_names_both_binaries(tmp_path: Path, monkeypatch) -> None:
    """When PATH would resolve ``kiln`` to a different binary than the
    one the installer is using, the warning must include both paths so
    the user can see why their tool surface might differ from expectations."""
    from kiln.cli import install_mcp

    path_kiln = tmp_path / "path" / "kiln"
    selected = tmp_path / "selected" / "kiln"
    path_kiln.parent.mkdir(parents=True)
    selected.parent.mkdir(parents=True)
    path_kiln.write_text("#!/bin/sh\n", encoding="utf-8")
    selected.write_text("#!/bin/sh\n", encoding="utf-8")

    monkeypatch.setattr(install_mcp.shutil, "which", lambda name: str(path_kiln))

    warning = install_mcp._path_kiln_warning(str(selected))

    assert warning is not None
    assert str(path_kiln.resolve()) in warning
    assert str(selected.resolve()) in warning


# ---------------------------------------------------------------------------
# install-mcp — Codex TOML path
# ---------------------------------------------------------------------------


def test_install_mcp_writes_codex_toml_config(tmp_path: Path, monkeypatch) -> None:
    """Codex uses ``~/.codex/config.toml``, not Claude's JSON shape.

    The installer must surgically replace ``[mcp_servers.kiln]`` and
    its child tables without touching sibling tables (``[mcp_servers.docs]``,
    ``[features]``).  Stale child tables like ``[mcp_servers.kiln.env]``
    must be removed so old overrides don't survive a reinstall.
    """
    from kiln.cli import install_mcp as mod

    home = tmp_path / "home"
    _write_session(home)
    config_path = tmp_path / "codex" / "config.toml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        "\n".join(
            [
                'model = "gpt-5.5"',
                "",
                "[mcp_servers.docs]",
                'url = "https://developers.openai.com/mcp"',
                "",
                "[mcp_servers.kiln]",
                'command = "/old/kiln"',
                'args = ["serve"]',
                "",
                "[mcp_servers.kiln.env]",
                'STALE = "1"',
                "",
                "[features]",
                "memories = true",
                "",
            ]
        ),
        encoding="utf-8",
    )
    kiln_bin = tmp_path / "venv" / "bin" / "kiln"
    kiln_bin.parent.mkdir(parents=True)
    kiln_bin.write_text("#!/bin/sh\n", encoding="utf-8")

    monkeypatch.setenv("KILN_AUTH_HOME", str(home))
    monkeypatch.setattr(
        mod,
        "_DEFAULT_CLIENTS",
        [mod.MCPClient("codex", "Codex", config_path, "toml")],
    )

    result = CliRunner().invoke(
        mod.install_mcp,
        ["--command", str(kiln_bin)],
    )

    assert result.exit_code == 0, result.output
    text = config_path.read_text(encoding="utf-8")
    assert '[mcp_servers.docs]' in text
    assert '[features]' in text
    assert '[mcp_servers.kiln]' in text
    # Compare the decoded TOML rather than serialized text: on Windows
    # the path's backslashes are correctly escaped in the TOML string,
    # so a raw-string substring check would spuriously fail.
    parsed = _load_toml(text)
    assert parsed["mcp_servers"]["kiln"]["command"] == str(kiln_bin)
    assert parsed["mcp_servers"]["kiln"]["args"] == ["serve"]
    assert "env" not in parsed["mcp_servers"]["kiln"]
    assert "[mcp_servers.kiln.env]" not in text
    assert "Codex" in result.output
    assert "fresh Codex session" in result.output


# ---------------------------------------------------------------------------
# prefix_args — python -m kiln launch shape
# ---------------------------------------------------------------------------


def test_install_mcp_preserves_python_module_launch_for_codex(
    tmp_path: Path, monkeypatch,
) -> None:
    """When the installer is launched via ``python -m kiln`` (no entry-
    point binary on PATH), the MCP client can't run ``__main__.py``
    directly — it must invoke the same Python with ``-m kiln serve``.
    Verify the Codex TOML records ``args = ["-m", "kiln", "serve"]``."""
    from kiln.cli import install_mcp as mod

    home = tmp_path / "home"
    _write_session(home)
    config_path = tmp_path / "codex" / "config.toml"
    config_path.parent.mkdir(parents=True)
    module_main = tmp_path / "site-packages" / "kiln" / "__main__.py"
    module_main.parent.mkdir(parents=True)
    module_main.write_text("")
    python = tmp_path / "bin" / "python3"
    python.parent.mkdir()
    python.write_text("#!/bin/sh\n")

    monkeypatch.setenv("KILN_AUTH_HOME", str(home))
    monkeypatch.setattr(
        mod,
        "_DEFAULT_CLIENTS",
        [mod.MCPClient("codex", "Codex", config_path, "toml")],
    )
    monkeypatch.setattr(mod.sys, "argv", [str(module_main)])
    monkeypatch.setattr(mod.sys, "executable", str(python))
    monkeypatch.setattr(mod.shutil, "which", lambda name: None)

    result = CliRunner().invoke(mod.install_mcp, [])

    assert result.exit_code == 0, result.output
    text = config_path.read_text(encoding="utf-8")
    # Decode the TOML so the path comparison is OS-agnostic (Windows
    # escapes backslashes in the serialized string).
    kiln_entry = _load_toml(text)["mcp_servers"]["kiln"]
    assert kiln_entry["command"] == str(python)
    assert kiln_entry["args"] == ["-m", "kiln", "serve"]
    assert f"MCP command: {python} -m kiln serve" in result.output


def test_install_mcp_print_preserves_python_module_launch(
    tmp_path: Path, monkeypatch,
) -> None:
    """The ``--print`` snippet must also record the module-launch
    shape so users copy-pasting into Cursor / custom MCP clients get
    a config that actually starts kiln."""
    from kiln.cli import install_mcp as mod

    module_main = tmp_path / "site-packages" / "kiln" / "__main__.py"
    module_main.parent.mkdir(parents=True)
    module_main.write_text("")
    python = tmp_path / "bin" / "python3"
    python.parent.mkdir()
    python.write_text("#!/bin/sh\n")

    monkeypatch.setattr(mod.sys, "argv", [str(module_main)])
    monkeypatch.setattr(mod.sys, "executable", str(python))
    monkeypatch.setattr(mod.shutil, "which", lambda name: None)

    result = CliRunner().invoke(mod.install_mcp, ["--print"])

    assert result.exit_code == 0, result.output
    # The generic snippet is JSON; the prefix args show up inside the
    # ``args`` list together with ``serve``.  Compare the decoded
    # object so the path check is OS-agnostic.
    kiln_entry = _extract_leading_json(result.output)["mcpServers"]["kiln"]
    assert kiln_entry["command"] == str(python)
    assert kiln_entry["args"] == ["-m", "kiln", "serve"]


# ---------------------------------------------------------------------------
# Auth gate
# ---------------------------------------------------------------------------


def test_install_mcp_proceeds_without_signin_and_nudges(tmp_path: Path, monkeypatch) -> None:
    """No account required for local use: without a session on disk the
    installer still wires the client (exit 0) AND prints a one-line
    invitation to sign in — it never blocks."""
    from kiln.cli import install_mcp as mod

    home = tmp_path / "home"
    home.mkdir()  # no .kiln/auth_tokens.json inside
    config_path = tmp_path / "claude.json"
    kiln_bin = tmp_path / "bin" / "kiln"
    kiln_bin.parent.mkdir(parents=True)
    kiln_bin.write_text("#!/bin/sh\n", encoding="utf-8")

    monkeypatch.setenv("KILN_AUTH_HOME", str(home))
    monkeypatch.setattr(
        mod,
        "_DEFAULT_CLIENTS",
        [mod.MCPClient("claude_code", "Claude Code", config_path)],
    )

    result = CliRunner().invoke(
        mod.install_mcp,
        ["--command", str(kiln_bin)],
    )

    assert result.exit_code == 0, result.output          # never blocks
    assert "signin" in result.output.lower()             # the sign-in nudge
    config = json.loads(config_path.read_text(encoding="utf-8"))
    assert config["mcpServers"]["kiln"]["command"] == str(kiln_bin)  # actually wired


def test_install_mcp_force_still_accepted_and_silences_nudge(tmp_path: Path, monkeypatch) -> None:
    """``--force`` is a deprecated no-op kept for back-compat (the live
    install page + existing scripts still pass it).  Install already works
    without an account, so --force now only suppresses the sign-in nudge."""
    from kiln.cli import install_mcp as mod

    home = tmp_path / "home"
    home.mkdir()  # no session file
    config_path = tmp_path / "claude.json"
    kiln_bin = tmp_path / "bin" / "kiln"
    kiln_bin.parent.mkdir(parents=True)
    kiln_bin.write_text("#!/bin/sh\n", encoding="utf-8")

    monkeypatch.setenv("KILN_AUTH_HOME", str(home))
    monkeypatch.setattr(
        mod,
        "_DEFAULT_CLIENTS",
        [mod.MCPClient("claude_code", "Claude Code", config_path)],
    )

    result = CliRunner().invoke(
        mod.install_mcp,
        ["--force", "--command", str(kiln_bin)],
    )

    assert result.exit_code == 0, result.output
    assert "signin" not in result.output.lower()  # --force silences the nudge
    config = json.loads(config_path.read_text(encoding="utf-8"))
    assert config["mcpServers"]["kiln"]["command"] == str(kiln_bin)


# ---------------------------------------------------------------------------
# uninstall-mcp
# ---------------------------------------------------------------------------


def test_uninstall_mcp_removes_codex_config_block(tmp_path: Path, monkeypatch) -> None:
    """``kiln uninstall-mcp`` must remove the ``[mcp_servers.kiln]``
    block from Codex's TOML config without touching sibling MCP servers
    (``[mcp_servers.docs]``) or unrelated tables (``[features]``)."""
    from kiln.cli import install_mcp as mod

    config_path = tmp_path / "codex" / "config.toml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        "\n".join(
            [
                "[mcp_servers.docs]",
                'url = "https://developers.openai.com/mcp"',
                "",
                "[mcp_servers.kiln]",
                'command = "/old/kiln"',
                'args = ["serve"]',
                "",
                "[features]",
                "memories = true",
                "",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        mod,
        "_DEFAULT_CLIENTS",
        [mod.MCPClient("codex", "Codex", config_path, "toml")],
    )

    result = CliRunner().invoke(mod.uninstall_mcp)

    assert result.exit_code == 0, result.output
    text = config_path.read_text(encoding="utf-8")
    assert "[mcp_servers.kiln]" not in text
    assert "[mcp_servers.docs]" in text
    assert "[features]" in text


def test_uninstall_mcp_leaves_sibling_json_servers_alone(
    tmp_path: Path, monkeypatch,
) -> None:
    """A JSON config that has other MCP servers must lose only the
    ``kiln`` entry; siblings (``filesystem``, ``git``, etc.) stay."""
    from kiln.cli import install_mcp as mod

    config_path = tmp_path / "claude.json"
    config_path.write_text(
        json.dumps({
            "mcpServers": {
                "kiln": {"command": "/old/kiln", "args": ["serve"]},
                "filesystem": {"command": "/usr/bin/fs-mcp", "args": []},
            },
            "userPref": {"keep": True},
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        mod,
        "_DEFAULT_CLIENTS",
        [mod.MCPClient("claude_code", "Claude Code", config_path)],
    )

    result = CliRunner().invoke(mod.uninstall_mcp)

    assert result.exit_code == 0, result.output
    after = json.loads(config_path.read_text(encoding="utf-8"))
    assert "kiln" not in after["mcpServers"]
    assert after["mcpServers"]["filesystem"] == {
        "command": "/usr/bin/fs-mcp",
        "args": [],
    }
    assert after["userPref"] == {"keep": True}
