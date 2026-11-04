"""``kiln register`` is gone (decided 2026-10-11: deleted in the first release
after 1.5.0).

Until 2026-10-11 the command traded an email for a free licence key with no
account behind it; from then to 1.5.0 it only pointed at ``kiln signin``.
1.5.1 deletes it.  A free account is made by signing in.  The hosted API's
answer to the old route stays: installs that never upgrade still call it.
"""

from __future__ import annotations

from click.testing import CliRunner

from kiln.cli import main as cli_main


def test_the_command_is_deleted():
    assert "register" not in cli_main.cli.commands


def test_signin_is_the_way_in():
    result = CliRunner().invoke(cli_main.cli, ["--help"])
    assert result.exit_code == 0
    listed = {line.split()[0] for line in result.output.splitlines() if line.startswith("  ") and line.split()}
    assert "signin" in listed and "register" not in listed
