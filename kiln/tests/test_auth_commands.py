"""Tests for ``kiln.cli.auth_commands`` — the ``kiln login`` / ``logout``
/ ``whoami`` / ``pair`` device-code + pairing flow that ships in the
public CLI.

These tests cover the local-only surface of the commands:
registration, the logout/whoami code paths that don't hit the network,
and the 0600 permissions guarantee on the token file.  The server-side
device-flow endpoints and the LicenseManager's consumption of the
written tokens are covered by the kiln-pro test suite
(``tests/test_device_auth_flow.py``) since those components live there.
"""
from __future__ import annotations

import json
import os

import pytest


# =====================================================================
# CLI: register_auth_cli
# =====================================================================


class TestRegisterAuthCli:
    """The CLI registration hook must install the four device-flow /
    pairing commands at the top level, and must relocate any
    pre-existing top-level ``login`` command (a legacy
    identity-linking flow, if ever present) under
    ``kiln identity login`` so the OAuth device flow owns the
    first-run ``login`` name."""

    def test_installs_four_commands(self):
        import click
        from kiln.cli.auth_commands import register_auth_cli

        g = click.Group("kiln")
        register_auth_cli(g)
        assert set(g.commands.keys()) >= {"login", "logout", "whoami", "pair"}

    def test_imports_match_registered_names(self):
        """The four @click.command callables should be importable by
        name directly for external wiring (tests, docs, alt entry
        points)."""
        from kiln.cli.auth_commands import (
            auth_login,
            auth_logout,
            auth_pair,
            auth_whoami,
        )
        assert auth_login.name == "login"
        assert auth_logout.name == "logout"
        assert auth_whoami.name == "whoami"
        assert auth_pair.name == "pair"

    def test_relocates_legacy_login_to_identity(self):
        import click
        from kiln.cli.auth_commands import register_auth_cli

        # Simulate the state where a legacy top-level ``login`` + an
        # ``identity`` group already exist (kiln-pro's vcs_commands
        # registers both when installed).
        g = click.Group("kiln")

        @click.command("login")
        def legacy():
            pass

        g.add_command(legacy)
        identity_group = click.Group("identity")
        g.add_command(identity_group)

        register_auth_cli(g)

        # Our new login is at the top level.
        assert g.commands["login"] is not legacy
        # Legacy was moved under identity.
        assert "login" in identity_group.commands

    def test_gracefully_handles_missing_identity_group(self):
        # If the legacy login exists but there's no identity group
        # (future reorg), we should still install ours without crashing.
        import click
        from kiln.cli.auth_commands import register_auth_cli

        g = click.Group("kiln")

        @click.command("login")
        def legacy():
            pass

        g.add_command(legacy)
        # No identity group this time.
        register_auth_cli(g)
        # Our login wins, and we didn't blow up.
        assert "login" in g.commands


# =====================================================================
# CLI: logout / whoami (no network required)
# =====================================================================


@pytest.fixture
def auth_home(tmp_path, monkeypatch):
    """Redirect ~/.kiln/ to an isolated temp dir so tests don't touch
    the developer's real token file."""
    monkeypatch.setenv("KILN_AUTH_HOME", str(tmp_path))
    yield tmp_path


class TestLogoutAndWhoami:
    def test_logout_when_not_signed_in(self, auth_home):
        import click
        from click.testing import CliRunner
        from kiln.cli.auth_commands import register_auth_cli

        g = click.Group("kiln")
        register_auth_cli(g)
        runner = CliRunner()
        r = runner.invoke(g, ["logout"])
        assert r.exit_code == 0
        assert "weren't signed in" in r.output.lower()

    def test_logout_deletes_token_file(self, auth_home):
        import click
        from click.testing import CliRunner
        from kiln.cli.auth_commands import register_auth_cli

        # Drop a fake token file that logout should delete.
        (auth_home / ".kiln").mkdir(mode=0o700)
        token_path = auth_home / ".kiln" / "auth_tokens.json"
        token_path.write_text(json.dumps({"access_token": "x"}))

        g = click.Group("kiln")
        register_auth_cli(g)
        r = CliRunner().invoke(g, ["logout"])
        assert r.exit_code == 0
        assert not token_path.exists()

    def test_whoami_without_token_fails(self, auth_home):
        import click
        from click.testing import CliRunner
        from kiln.cli.auth_commands import register_auth_cli

        g = click.Group("kiln")
        register_auth_cli(g)
        r = CliRunner().invoke(g, ["whoami"])
        assert r.exit_code != 0
        assert "not signed in" in r.output.lower()

    def test_token_file_perms_are_0600(self, auth_home, monkeypatch):
        """Writing tokens lands at ~/.kiln/auth_tokens.json with 0600."""
        from kiln.cli import auth_commands

        auth_commands._write_tokens({"access_token": "abc"})
        path = auth_commands._tokens_path()
        assert path.exists()
        # Lower 9 bits of st_mode should be 600 (user r/w, no group/other).
        perms = os.stat(path).st_mode & 0o777
        assert perms == 0o600, f"expected 0600 perms, got 0o{perms:o}"
