"""Tests for ``kiln.cli.auth_commands`` — the ``kiln signin`` / ``signout``
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

    def test_installs_five_commands(self):
        import click
        from kiln.cli.auth_commands import register_auth_cli

        g = click.Group("kiln")
        register_auth_cli(g)
        assert set(g.commands.keys()) >= {
            "login",
            "logout",
            "whoami",
            "pair",
            "invite",
        }

    def test_imports_match_registered_names(self):
        """The five @click.command callables should be importable by
        name directly for external wiring (tests, docs, alt entry
        points)."""
        from kiln.cli.auth_commands import (
            auth_invite,
            auth_login,
            auth_logout,
            auth_pair,
            auth_whoami,
        )
        # Canonical click names (match the web workshop + MCP tools).
        # `login` / `logout` stay reachable as aliases via register_auth_cli.
        assert auth_login.name == "signin"
        assert auth_logout.name == "signout"
        assert auth_whoami.name == "whoami"
        assert auth_pair.name == "pair"
        assert auth_invite.name == "invite"

    def test_login_alias_points_to_auth_signin(self):
        # `kiln login` must be the backcompat alias for our OAuth
        # signin command — NOT kiln-pro's identity-linking command
        # (which now lives at `kiln identity link`).  If this invariant
        # breaks, users typing `kiln login` will suddenly hit a
        # completely different flow.
        import click
        from kiln.cli.auth_commands import register_auth_cli, auth_login

        g = click.Group("kiln")
        register_auth_cli(g)

        assert "login" in g.commands, "backcompat `kiln login` alias missing"
        assert g.commands["login"] is auth_login, (
            "`kiln login` must alias auth_login (OAuth signin), not some "
            "other command"
        )
        assert "signin" in g.commands
        assert g.commands["signin"] is auth_login

    def test_does_not_touch_identity_group(self):
        # Regression guard: the old register_auth_cli popped a
        # top-level `login` and relocated it into the `identity` group
        # to clear the name for OAuth.  kiln-pro now registers its
        # identity-linking command natively as `kiln identity link`,
        # so register_auth_cli must NOT mutate the identity group.
        import click
        from kiln.cli.auth_commands import register_auth_cli

        g = click.Group("kiln")
        identity_group = click.Group("identity")

        @identity_group.command("link")
        def pro_identity_link():
            pass

        g.add_command(identity_group)
        register_auth_cli(g)

        # identity group is intact.
        assert "link" in identity_group.commands
        assert identity_group.commands["link"] is pro_identity_link
        # No stray `login` got relocated in.
        assert "login" not in identity_group.commands


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


# =====================================================================
# CLI: invite (no network required for the happy-path stubbing)
# =====================================================================


class TestInvite:
    """``kiln invite`` — CLI-initiated pairing.  The happy-path hits
    the network, but the "not signed in" failure mode is purely local
    and worth pinning: ships a clear message pointing at ``kiln
    login`` instead of a cryptic 401."""

    def test_invite_without_saved_session_fails_clearly(self, auth_home):
        import click
        from click.testing import CliRunner
        from kiln.cli.auth_commands import register_auth_cli

        g = click.Group("kiln")
        register_auth_cli(g)
        r = CliRunner().invoke(g, ["invite"])
        assert r.exit_code != 0
        # The error message must guide the user back to `kiln signin`
        # (and NOT mention `kiln pair`, which would confuse someone
        # whose first step is just signing in).
        out = r.output.lower()
        assert "not signed in" in out
        assert "kiln signin" in out

    def test_invite_with_empty_access_token_fails_clearly(self, auth_home):
        """A token file that exists but has an empty access_token
        (seen on 2026-04-23 as a bad pairing that wrote partial
        tokens) should be treated the same as "no session" — fail
        before trying to POST the empty bearer to the API."""
        import json as _json
        import click
        from click.testing import CliRunner
        from kiln.cli.auth_commands import register_auth_cli

        (auth_home / ".kiln").mkdir(mode=0o700, exist_ok=True)
        (auth_home / ".kiln" / "auth_tokens.json").write_text(
            _json.dumps({"access_token": ""})
        )

        g = click.Group("kiln")
        register_auth_cli(g)
        r = CliRunner().invoke(g, ["invite"])
        assert r.exit_code != 0
        assert "not signed in" in r.output.lower()

    def test_invite_success_prints_code_and_url(self, auth_home, monkeypatch):
        """Happy path: with a valid saved session and a mocked server
        response, `kiln invite` prints the code + verification URL so
        the user can type it into a browser tab.  The code is the
        hero of the output."""
        import json as _json
        import click
        from click.testing import CliRunner
        from kiln.cli import auth_commands
        from kiln.cli.auth_commands import register_auth_cli

        # Seed a valid saved session.
        (auth_home / ".kiln").mkdir(mode=0o700, exist_ok=True)
        (auth_home / ".kiln" / "auth_tokens.json").write_text(
            _json.dumps({
                "access_token": "fake-access-token",
                "refresh_token": "fake-refresh-token",
            })
        )

        # Mock the server response.  The invite endpoint returns a
        # short code + absolute expiration + the verify URL the
        # browser should visit.
        captured: dict = {}

        def fake_post(path, body, *, bearer=None, timeout=15.0):
            captured["path"] = path
            captured["body"] = body
            captured["bearer"] = bearer
            return {
                "success": True,
                "code": "KLN-ABCD-EFGH",
                "expires_at": "2099-01-01T00:10:00Z",
                "verify_url": "https://app.kiln3d.com/settings/agent",
            }

        monkeypatch.setattr(auth_commands, "_http_post", fake_post)

        g = click.Group("kiln")
        register_auth_cli(g)
        r = CliRunner().invoke(g, ["invite"])
        assert r.exit_code == 0, r.output

        # The bearer MUST be forwarded so the server can resolve the
        # user; the refresh_token is in the body so the browser-side
        # claim can outlive the short-lived access_token.
        assert captured["path"] == "/api/auth/pairing/invite"
        assert captured["bearer"] == "fake-access-token"
        # ``client_name`` is always present (post-migration-028); the
        # auto-detector fills it when no --client flag is supplied.
        # The test harness' env may carry CLAUDE_CODE_* or similar
        # signals, so we assert shape + refresh_token, not an exact
        # value for client_name.
        assert captured["body"].get("refresh_token") == "fake-refresh-token"
        assert "client_name" in captured["body"]

        out = r.output
        assert "KLN-ABCD-EFGH" in out
        assert "app.kiln3d.com/settings/agent" in out

    def test_invite_json_mode_emits_raw_server_body(self, auth_home, monkeypatch):
        """`kiln invite --json` pipes the raw server body for scripts,
        so an automation can parse the code without screen-scraping
        the human-facing output."""
        import json as _json
        import click
        from click.testing import CliRunner
        from kiln.cli import auth_commands
        from kiln.cli.auth_commands import register_auth_cli

        (auth_home / ".kiln").mkdir(mode=0o700, exist_ok=True)
        (auth_home / ".kiln" / "auth_tokens.json").write_text(
            _json.dumps({"access_token": "tok", "refresh_token": "ref"})
        )

        body = {
            "success": True,
            "code": "KLN-WXYZ-1234",
            "expires_at": "2099-01-01T00:10:00Z",
            "verify_url": "https://app.kiln3d.com/settings/agent",
        }
        monkeypatch.setattr(
            auth_commands, "_http_post",
            lambda path, payload, *, bearer=None, timeout=15.0: body,
        )

        g = click.Group("kiln")
        register_auth_cli(g)
        r = CliRunner().invoke(g, ["invite", "--json"])
        assert r.exit_code == 0, r.output
        # Click 8.2+ keeps stdout and stderr separate in CliRunner
        # results — ``r.stdout`` alone is parseable JSON.  That
        # mirrors the real `kiln invite --json | jq ...` pipe, where
        # stderr status lines must not contaminate the payload.
        parsed = _json.loads(r.stdout)
        assert parsed == body


# =====================================================================
# CLI: _detect_client_name / _resolve_client_name + --client flag wiring
# =====================================================================
#
# Two paired machines on the same laptop both labelled "Adams-MBP" was
# the bug these tests pin.  The CLI now sends a ``client_name`` body
# field on every /claim and /invite call, captured by (in priority
# order) the explicit ``--client`` flag, env-var sniff, parent-process
# name, or an interactive TTY prompt.
#
# Each test isolates one rung of that ladder so a regression shows up
# on the exact rung that broke.


@pytest.fixture
def clear_client_env(monkeypatch):
    """Strip every env var the client-name detector reads, so tests
    start from a known-empty baseline.  Individual tests re-add the
    one signal they're exercising."""
    from kiln.cli import auth_commands

    for var, _label in auth_commands._CLIENT_NAME_ENV_SIGNALS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("VSCODE_PID", raising=False)
    # Defensive: nuke any stray CLAUDE_CODE_* prefix-match vars too.
    for key in list(os.environ.keys()):
        if key.startswith("CLAUDE_CODE_"):
            monkeypatch.delenv(key, raising=False)
    yield


class TestDetectClientName:
    """``_detect_client_name`` walks env → parent-process → fallback.

    We stub parent-process name resolution (``_sniff_client_from_parent_process``)
    to empty so env-var tests aren't contaminated by whichever binary
    ran the pytest process (zsh, bash, sh on CI, etc.).
    """

    def test_env_claude_desktop_wins(self, monkeypatch, clear_client_env):
        from kiln.cli import auth_commands

        monkeypatch.setenv("CLAUDE_DESKTOP_SESSION", "sess-abc")
        monkeypatch.setattr(
            auth_commands, "_sniff_client_from_parent_process", lambda: "",
        )
        assert auth_commands._detect_client_name() == "Claude Desktop"

    def test_env_cursor_signal(self, monkeypatch, clear_client_env):
        from kiln.cli import auth_commands

        monkeypatch.setenv("CURSOR_EDITOR", "1")
        monkeypatch.setattr(
            auth_commands, "_sniff_client_from_parent_process", lambda: "",
        )
        assert auth_commands._detect_client_name() == "Cursor"

    def test_env_claude_code_prefix_match(self, monkeypatch, clear_client_env):
        """Any ``CLAUDE_CODE_*`` env var with a non-empty value counts
        as Claude Code — new session-variable names ship faster than
        this ladder can be hand-updated."""
        from kiln.cli import auth_commands

        monkeypatch.setenv("CLAUDE_CODE_SOMETHING_NEW", "yes")
        monkeypatch.setattr(
            auth_commands, "_sniff_client_from_parent_process", lambda: "",
        )
        assert auth_commands._detect_client_name() == "Claude Code"

    def test_env_claude_desktop_beats_claude_code(
        self, monkeypatch, clear_client_env,
    ):
        """Order matters.  A Claude Desktop session that happens to
        also set a CLAUDE_CODE_* env var is still semantically
        Claude Desktop."""
        from kiln.cli import auth_commands

        monkeypatch.setenv("CLAUDE_DESKTOP_SESSION", "sess")
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "also-set")
        monkeypatch.setattr(
            auth_commands, "_sniff_client_from_parent_process", lambda: "",
        )
        assert auth_commands._detect_client_name() == "Claude Desktop"

    def test_vscode_without_cursor(self, monkeypatch, clear_client_env):
        from kiln.cli import auth_commands

        monkeypatch.setenv("VSCODE_PID", "12345")
        monkeypatch.setattr(
            auth_commands, "_sniff_client_from_parent_process", lambda: "",
        )
        assert auth_commands._detect_client_name() == "VS Code"

    def test_headless_empty_returns_unspecified_mcp(
        self, monkeypatch, clear_client_env,
    ):
        """stdin-not-a-TTY + no env + no proc hit → the MCP-subprocess
        default label."""
        from kiln.cli import auth_commands

        monkeypatch.setattr(
            auth_commands, "_sniff_client_from_parent_process", lambda: "",
        )
        monkeypatch.setattr(auth_commands, "_is_stdin_tty", lambda: False)
        assert auth_commands._detect_client_name() == "Unspecified (MCP)"

    def test_tty_empty_returns_empty_string(
        self, monkeypatch, clear_client_env,
    ):
        """TTY + no signals → empty string.  The caller (``_resolve_
        client_name``) decides whether to prompt."""
        from kiln.cli import auth_commands

        monkeypatch.setattr(
            auth_commands, "_sniff_client_from_parent_process", lambda: "",
        )
        monkeypatch.setattr(auth_commands, "_is_stdin_tty", lambda: True)
        assert auth_commands._detect_client_name() == ""


class TestResolveClientName:
    """``_resolve_client_name`` — explicit flag > detect > prompt."""

    def test_explicit_flag_wins_over_env(self, monkeypatch, clear_client_env):
        """``--client "Foo"`` must beat whatever the env would detect."""
        from kiln.cli import auth_commands

        monkeypatch.setenv("CLAUDE_DESKTOP_SESSION", "sess-abc")
        monkeypatch.setattr(
            auth_commands, "_sniff_client_from_parent_process", lambda: "",
        )
        assert auth_commands._resolve_client_name("Foo") == "Foo"

    def test_explicit_empty_string_is_honoured(
        self, monkeypatch, clear_client_env,
    ):
        """``--client ""`` means 'I don't want a label' — no prompt,
        no sniff, no fallback."""
        from kiln.cli import auth_commands

        monkeypatch.setenv("CLAUDE_DESKTOP_SESSION", "sess-abc")
        # Prompt must NOT be called.
        monkeypatch.setattr(
            auth_commands, "_prompt_for_client_name",
            lambda: pytest.fail("prompt invoked with explicit empty flag"),
        )
        assert auth_commands._resolve_client_name("") == ""

    def test_explicit_flag_is_40_char_capped(
        self, monkeypatch, clear_client_env,
    ):
        from kiln.cli import auth_commands

        long_name = "X" * 100
        result = auth_commands._resolve_client_name(long_name)
        assert len(result) == 40
        assert result == "X" * 40

    def test_no_flag_falls_through_to_prompt_on_tty(
        self, monkeypatch, clear_client_env,
    ):
        """No env + no proc + TTY → _prompt_for_client_name is called."""
        from kiln.cli import auth_commands

        monkeypatch.setattr(
            auth_commands, "_sniff_client_from_parent_process", lambda: "",
        )
        monkeypatch.setattr(auth_commands, "_is_stdin_tty", lambda: True)
        called: dict[str, bool] = {"prompted": False}

        def fake_prompt() -> str:
            called["prompted"] = True
            return "PromptedValue"

        monkeypatch.setattr(
            auth_commands, "_prompt_for_client_name", fake_prompt,
        )
        result = auth_commands._resolve_client_name(None)
        assert called["prompted"] is True
        assert result == "PromptedValue"

    def test_no_flag_headless_returns_unspecified_mcp_without_prompt(
        self, monkeypatch, clear_client_env,
    ):
        """No env + no proc + non-TTY → 'Unspecified (MCP)', NOT prompt."""
        from kiln.cli import auth_commands

        monkeypatch.setattr(
            auth_commands, "_sniff_client_from_parent_process", lambda: "",
        )
        monkeypatch.setattr(auth_commands, "_is_stdin_tty", lambda: False)

        def fail_prompt() -> str:
            pytest.fail("prompt should not be invoked in headless mode")

        monkeypatch.setattr(
            auth_commands, "_prompt_for_client_name", fail_prompt,
        )
        assert auth_commands._resolve_client_name(None) == "Unspecified (MCP)"


class TestPairForwardsClientName:
    """`kiln pair --client` forwards the resolved name in the request body."""

    def test_pair_sends_client_name_in_body(
        self, auth_home, monkeypatch, clear_client_env,
    ):
        import click
        from click.testing import CliRunner
        from kiln.cli import auth_commands
        from kiln.cli.auth_commands import register_auth_cli

        captured: dict = {}

        def fake_post(path, body, *, bearer=None, timeout=15.0):
            captured["path"] = path
            captured["body"] = body
            # Mimic a successful pairing response.
            return {
                "success": True,
                "access_token": "at",
                "refresh_token": "rt",
                "email": "adam@kiln3d.com",
                "auth_uid": "uid-1",
                "tier": "pro",
                "has_entitlement": True,
            }

        def fake_get(path, *, bearer=None, timeout=10.0):
            return (200, {})

        monkeypatch.setattr(auth_commands, "_http_post", fake_post)
        monkeypatch.setattr(auth_commands, "_http_get", fake_get)
        # Pin the detector off so the --client flag is the only signal.
        monkeypatch.setattr(
            auth_commands, "_sniff_client_from_parent_process", lambda: "",
        )

        g = click.Group("kiln")
        register_auth_cli(g)
        r = CliRunner().invoke(
            g,
            ["pair", "KLN-ABCD-EFGH", "--client", "Claude Desktop"],
        )
        assert r.exit_code == 0, r.output
        assert captured["path"] == "/api/auth/pairing/claim"
        assert captured["body"].get("client_name") == "Claude Desktop"

    def test_pair_auto_detects_when_flag_omitted(
        self, auth_home, monkeypatch, clear_client_env,
    ):
        """No ``--client`` flag + a detectable env var → the auto-
        detected name rides in the body."""
        import click
        from click.testing import CliRunner
        from kiln.cli import auth_commands
        from kiln.cli.auth_commands import register_auth_cli

        monkeypatch.setenv("CURSOR_EDITOR", "1")
        monkeypatch.setattr(
            auth_commands, "_sniff_client_from_parent_process", lambda: "",
        )

        captured: dict = {}

        def fake_post(path, body, *, bearer=None, timeout=15.0):
            captured["body"] = body
            return {
                "success": True,
                "access_token": "at",
                "refresh_token": "rt",
                "email": "adam@kiln3d.com",
                "auth_uid": "uid-1",
                "tier": "pro",
                "has_entitlement": True,
            }

        monkeypatch.setattr(auth_commands, "_http_post", fake_post)
        monkeypatch.setattr(
            auth_commands, "_http_get",
            lambda path, *, bearer=None, timeout=10.0: (200, {}),
        )

        g = click.Group("kiln")
        register_auth_cli(g)
        r = CliRunner().invoke(g, ["pair", "KLN-ABCD-EFGH"])
        assert r.exit_code == 0, r.output
        assert captured["body"].get("client_name") == "Cursor"


class TestInviteForwardsClientName:
    """`kiln invite --client` forwards the resolved name in the request body."""

    def test_invite_sends_client_name_in_body(
        self, auth_home, monkeypatch, clear_client_env,
    ):
        import json as _json
        import click
        from click.testing import CliRunner
        from kiln.cli import auth_commands
        from kiln.cli.auth_commands import register_auth_cli

        # Seed a valid saved session — invite needs a bearer to forward.
        (auth_home / ".kiln").mkdir(mode=0o700, exist_ok=True)
        (auth_home / ".kiln" / "auth_tokens.json").write_text(
            _json.dumps({"access_token": "tok", "refresh_token": "ref"})
        )

        captured: dict = {}

        def fake_post(path, body, *, bearer=None, timeout=15.0):
            captured["path"] = path
            captured["body"] = body
            return {
                "success": True,
                "code": "KLN-ABCD-EFGH",
                "expires_at": "2099-01-01T00:10:00Z",
                "verify_url": "https://app.kiln3d.com/settings/agent",
            }

        monkeypatch.setattr(auth_commands, "_http_post", fake_post)
        monkeypatch.setattr(
            auth_commands, "_sniff_client_from_parent_process", lambda: "",
        )

        g = click.Group("kiln")
        register_auth_cli(g)
        r = CliRunner().invoke(g, ["invite", "--client", "Codex"])
        assert r.exit_code == 0, r.output
        assert captured["path"] == "/api/auth/pairing/invite"
        assert captured["body"].get("client_name") == "Codex"
        # Refresh_token must still be there — the --client flag
        # augments, not replaces, the body shape.
        assert captured["body"].get("refresh_token") == "ref"
