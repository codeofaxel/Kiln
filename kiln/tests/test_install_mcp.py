"""Tests for ``kiln install-mcp`` first-run MCP setup."""
from __future__ import annotations

import json


def test_registers_install_mcp_command() -> None:
    import click
    from kiln.cli.install_mcp import register_install_mcp_cli

    group = click.Group("kiln")
    register_install_mcp_cli(group)

    assert "install-mcp" in group.commands


def test_install_mcp_writes_selected_executable(tmp_path, monkeypatch) -> None:
    from click.testing import CliRunner
    from kiln.cli import install_mcp

    config_path = tmp_path / "claude_desktop_config.json"
    executable = tmp_path / "bin" / "kiln"
    executable.parent.mkdir()
    executable.write_text("#!/bin/sh\n")

    monkeypatch.setattr(
        install_mcp,
        "_claude_desktop_config_path",
        lambda: config_path,
    )

    result = CliRunner().invoke(
        install_mcp.install_mcp,
        [
            "--client",
            "claude-desktop",
            "--command",
            str(executable),
        ],
    )

    assert result.exit_code == 0, result.output
    data = json.loads(config_path.read_text())
    assert data["mcpServers"]["kiln"] == {
        "command": str(executable),
        "args": ["serve"],
    }


def test_install_mcp_print_uses_selected_executable(tmp_path) -> None:
    from click.testing import CliRunner
    from kiln.cli.install_mcp import install_mcp

    executable = tmp_path / "kiln"
    executable.write_text("#!/bin/sh\n")

    result = CliRunner().invoke(
        install_mcp,
        ["--print", "--command", str(executable)],
    )

    assert result.exit_code == 0, result.output
    assert f'"command": "{executable}"' in result.output
    assert '"args": [' in result.output
    assert '"serve"' in result.output


def test_path_warning_names_selected_and_path_binaries(tmp_path, monkeypatch) -> None:
    from kiln.cli import install_mcp

    selected = tmp_path / "venv" / "kiln"
    path_kiln = tmp_path / "global" / "kiln"
    selected.parent.mkdir()
    path_kiln.parent.mkdir()
    selected.write_text("")
    path_kiln.write_text("")
    monkeypatch.setattr(install_mcp.shutil, "which", lambda name: str(path_kiln))

    warning = install_mcp._path_kiln_warning(str(selected))

    assert warning is not None
    assert str(selected.resolve()) in warning
    assert str(path_kiln.resolve()) in warning


def test_install_mcp_writes_codex_toml_config(tmp_path, monkeypatch) -> None:
    from click.testing import CliRunner
    from kiln.cli import install_mcp

    config_path = tmp_path / "codex" / "config.toml"
    config_path.parent.mkdir()
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
        )
    )
    executable = tmp_path / "bin" / "kiln"
    executable.parent.mkdir()
    executable.write_text("#!/bin/sh\n")

    monkeypatch.setattr(install_mcp, "_codex_config_path", lambda: config_path)

    result = CliRunner().invoke(
        install_mcp.install_mcp,
        [
            "--client",
            "codex",
            "--command",
            str(executable),
        ],
    )

    assert result.exit_code == 0, result.output
    text = config_path.read_text()
    assert "[mcp_servers.docs]" in text
    assert "[features]" in text
    assert "[mcp_servers.kiln]" in text
    assert f'command = "{executable}"' in text
    assert 'args = ["serve"]' in text
    assert "[mcp_servers.kiln.env]" not in text
    assert "Codex" in result.output


def test_install_mcp_print_codex_snippet_is_toml(tmp_path) -> None:
    from click.testing import CliRunner
    from kiln.cli.install_mcp import install_mcp

    executable = tmp_path / "kiln"
    executable.write_text("#!/bin/sh\n")

    result = CliRunner().invoke(
        install_mcp,
        ["--client", "codex", "--print", "--command", str(executable)],
    )

    assert result.exit_code == 0, result.output
    assert "[mcp_servers.kiln]" in result.output
    assert f'command = "{executable}"' in result.output
    assert 'args = ["serve"]' in result.output
