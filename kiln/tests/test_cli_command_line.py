"""A command Kiln tells a person to run has to run on their computer.

Plugins start Kiln through uvx (the Claude Code plugin, the Hermes package), so
no ``kiln`` is on the person's PATH and "run ``kiln accept-terms``" fails on the
first thing a new install asks of them.
"""

from __future__ import annotations

import shlex
import sys

from kiln import cli_command, server


def test_kiln_on_path_is_named_plainly(monkeypatch):
    monkeypatch.delenv(cli_command.CLI_ENV, raising=False)
    monkeypatch.setattr(cli_command.shutil, "which", lambda name: "/usr/local/bin/kiln")
    assert cli_command.kiln_command("accept-terms") == "kiln accept-terms"


def test_no_kiln_on_path_names_this_python(monkeypatch):
    monkeypatch.delenv(cli_command.CLI_ENV, raising=False)
    monkeypatch.setattr(cli_command.shutil, "which", lambda name: None)
    assert cli_command.kiln_command("accept-terms") == (
        f"{shlex.quote(sys.executable)} -m kiln accept-terms"
    )


def test_the_launcher_can_name_its_own_command(monkeypatch):
    monkeypatch.setenv(cli_command.CLI_ENV, "uvx --from kiln3d==9.9.9 kiln")
    monkeypatch.setattr(cli_command.shutil, "which", lambda name: "/usr/local/bin/kiln")
    assert cli_command.kiln_command("signin") == "uvx --from kiln3d==9.9.9 kiln signin"


def test_terms_gate_names_a_command_that_runs_here(monkeypatch):
    monkeypatch.setenv(cli_command.CLI_ENV, "uvx --from kiln3d==9.9.9 kiln")
    monkeypatch.setattr(server, "_mint_accept_link", lambda: None)
    msg = server._terms_consent_message()
    assert "uvx --from kiln3d==9.9.9 kiln accept-terms" in msg
