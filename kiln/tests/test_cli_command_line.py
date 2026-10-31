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


# --- defects a lap found (2026-10-28; each fails at dedaa065) ---


def test_windows_fallback_is_quoted_the_windows_way(monkeypatch):
    """The fallback quoted this interpreter's path with POSIX single quotes,
    which Windows' cmd does not read, so a Windows user with a space in their
    path and no kiln on PATH was told a command that could not run."""
    monkeypatch.delenv(cli_command.CLI_ENV, raising=False)
    monkeypatch.setattr(cli_command.shutil, "which", lambda name: None)
    monkeypatch.setattr(cli_command, "_ON_WINDOWS", True)
    monkeypatch.setattr(cli_command.sys, "executable", r"C:\Users\Ada Lovelace\uv\python.exe")
    assert cli_command.kiln_command("accept-terms") == (
        r'"C:\Users\Ada Lovelace\uv\python.exe" -m kiln accept-terms'
    )


# --- security review on the push (2026-10-28; fails at abbf1e4b) ---


def test_a_value_cmd_would_run_is_never_put_in_a_windows_line(monkeypatch):
    """A printer or file name is chosen by a person or a printer, and the line
    is meant to be pasted.  Windows' cmd runs ``&``, ``|`` and friends even
    inside double quotes' reach (``%VAR%`` expands, ``^`` escapes), so such a
    value is shown as a placeholder instead of being quoted into the line."""
    monkeypatch.delenv(cli_command.CLI_ENV, raising=False)
    monkeypatch.setattr(cli_command.shutil, "which", lambda name: "C:\\kiln.exe")
    monkeypatch.setattr(cli_command, "_ON_WINDOWS", True)
    for hostile in ('A1" & calc & "', "A1 | evil", "%PATH%", "A1^", "a<b", "a>b", "a!b", "line\nbreak",
                    "$env:X", "a`b", "a;b", "(a)", "{a}", "a'b"):
        line = cli_command.kiln_command("status", hostile)
        assert line == "kiln status <name>", (hostile, line)
    assert cli_command.kiln_command("status", "Garage A1") == 'kiln status "Garage A1"'


def test_a_control_character_never_reaches_a_line_on_any_system(monkeypatch):
    monkeypatch.delenv(cli_command.CLI_ENV, raising=False)
    monkeypatch.setattr(cli_command.shutil, "which", lambda name: "/usr/bin/kiln")
    monkeypatch.setattr(cli_command, "_ON_WINDOWS", False)
    assert cli_command.kiln_command("status", "A1\x1b[2J") == "kiln status <name>"
    for hostile in ("a'b; rm -rf ~", "$(evil)", "a`b`", "a;b", "--force", "-x", "a\u202eb", "caf\u00e9"):
        assert cli_command.kiln_command("status", hostile) == "kiln status <name>", hostile
    assert cli_command.kiln_command("status", "Garage A1") == "kiln status 'Garage A1'"
