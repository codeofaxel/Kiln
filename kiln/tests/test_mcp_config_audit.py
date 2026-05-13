"""Tests for ``kiln.cli.mcp_config_audit``.

Covers the audit-only happy / drift / corrupt-config paths plus the
``kiln health`` integration that consumes the audit results.  Every
test patches the per-client config-path getters so the suite never
touches the user's real Claude / Codex configs — and every test
points the ``command`` field at files inside ``tmp_path`` so we
exercise the exists / executable checks against artifacts we own.
"""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _make_executable(path: Path) -> Path:
    """Create a small file at ``path`` and mark it executable.  Used
    as the stand-in for a real ``kiln`` binary in the audit tests."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\necho stub\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _redirect_client_paths(monkeypatch, *, desktop, code, codex) -> None:
    """Point every audit-side path helper at the tmp paths.  Both the
    audit module and ``install_mcp`` (re-exported into audit) need
    the override since audit imports the path functions by name."""
    from kiln.cli import install_mcp, mcp_config_audit

    for module in (install_mcp, mcp_config_audit):
        monkeypatch.setattr(
            module, "_claude_desktop_config_path", lambda p=desktop: p, raising=False,
        )
        monkeypatch.setattr(
            module, "_claude_code_config_path", lambda p=code: p, raising=False,
        )
        monkeypatch.setattr(
            module, "_codex_config_path", lambda p=codex: p, raising=False,
        )


# ---------------------------------------------------------------------------
# Audit core — ServerEntryAuditResult
# ---------------------------------------------------------------------------


class TestServerEntryCheck:
    def test_existing_executable_is_ok(self, tmp_path):
        from kiln.cli.mcp_config_audit import _check_server_entry, STATUS_OK

        binary = _make_executable(tmp_path / "kiln")
        result = _check_server_entry("kiln", {"command": str(binary)})
        assert result.is_ok
        assert result.status == STATUS_OK
        assert result.detail is None

    def test_missing_command_is_command_missing(self, tmp_path):
        from kiln.cli.mcp_config_audit import (
            _check_server_entry,
            STATUS_COMMAND_MISSING,
        )

        result = _check_server_entry(
            "kiln", {"command": str(tmp_path / "nope" / "kiln")},
        )
        assert result.status == STATUS_COMMAND_MISSING
        assert "not found" in (result.detail or "")
        assert not result.is_ok

    def test_non_executable_command_is_flagged(self, tmp_path):
        from kiln.cli.mcp_config_audit import (
            _check_server_entry,
            STATUS_COMMAND_NOT_EXECUTABLE,
        )

        plain = tmp_path / "kiln"
        plain.write_text("not executable")
        # Strip every execute bit — readable but not runnable.
        plain.chmod(plain.stat().st_mode & ~0o111)
        result = _check_server_entry("kiln", {"command": str(plain)})
        assert result.status == STATUS_COMMAND_NOT_EXECUTABLE
        assert "not executable" in (result.detail or "")

    def test_entry_with_no_command_is_malformed(self, tmp_path):
        from kiln.cli.mcp_config_audit import (
            _check_server_entry,
            STATUS_ENTRY_MALFORMED,
        )

        result = _check_server_entry("kiln", {"args": ["serve"]})
        assert result.status == STATUS_ENTRY_MALFORMED

    def test_non_dict_entry_is_malformed(self):
        from kiln.cli.mcp_config_audit import (
            _check_server_entry,
            STATUS_ENTRY_MALFORMED,
        )

        result = _check_server_entry("kiln", "not a dict")
        assert result.status == STATUS_ENTRY_MALFORMED

    def test_tilde_in_command_is_expanded(self, tmp_path, monkeypatch):
        """``~/.local/bin/kiln`` is a common config shape; the audit
        must expand it before checking existence or the user gets a
        false negative."""
        from kiln.cli.mcp_config_audit import _check_server_entry, STATUS_OK

        monkeypatch.setenv("HOME", str(tmp_path))
        binary = _make_executable(tmp_path / ".local" / "bin" / "kiln")
        assert binary.exists()
        result = _check_server_entry("kiln", {"command": "~/.local/bin/kiln"})
        assert result.status == STATUS_OK


# ---------------------------------------------------------------------------
# Audit core — per-client parsers
# ---------------------------------------------------------------------------


class TestClaudeJsonAudit:
    def test_missing_file_reports_no_drift(self, tmp_path, monkeypatch):
        from kiln.cli.mcp_config_audit import audit_all_mcp_clients

        _redirect_client_paths(
            monkeypatch,
            desktop=tmp_path / "desktop.json",  # doesn't exist
            code=tmp_path / "code.json",        # doesn't exist
            codex=tmp_path / "codex.toml",      # doesn't exist
        )
        results = audit_all_mcp_clients()
        # Three clients, none with configs → no drift, no false noise.
        for r in results:
            assert not r.config_exists
            assert r.entries == []
            assert not r.has_drift

    def test_happy_path_one_entry_per_client(self, tmp_path, monkeypatch):
        from kiln.cli.mcp_config_audit import audit_all_mcp_clients

        binary = _make_executable(tmp_path / "kiln")
        desktop = tmp_path / "desktop.json"
        desktop.write_text(json.dumps({"mcpServers": {"kiln": {
            "command": str(binary), "args": ["serve"],
        }}}))
        code = tmp_path / "code.json"
        code.write_text(json.dumps({"mcpServers": {"kiln": {
            "command": str(binary), "args": ["serve"],
        }}}))
        codex = tmp_path / "codex.toml"
        codex.write_text(
            f'[mcp_servers.kiln]\ncommand = "{binary}"\nargs = ["serve"]\n',
        )
        _redirect_client_paths(
            monkeypatch, desktop=desktop, code=code, codex=codex,
        )

        results = audit_all_mcp_clients()
        for r in results:
            assert r.config_exists, r.client
            assert not r.has_drift, r.client
            assert len(r.entries) == 1
            assert r.entries[0].name == "kiln"
            assert r.entries[0].is_ok

    def test_drift_when_command_path_missing(self, tmp_path, monkeypatch):
        from kiln.cli.mcp_config_audit import (
            audit_all_mcp_clients,
            STATUS_COMMAND_MISSING,
        )

        desktop = tmp_path / "desktop.json"
        desktop.write_text(json.dumps({"mcpServers": {"kiln": {
            "command": str(tmp_path / "no-such-file"), "args": ["serve"],
        }}}))
        _redirect_client_paths(
            monkeypatch, desktop=desktop, code=tmp_path / "x", codex=tmp_path / "y",
        )

        results = audit_all_mcp_clients()
        desktop_result = next(r for r in results if r.client == "Claude Desktop")
        assert desktop_result.has_drift
        assert desktop_result.entries[0].status == STATUS_COMMAND_MISSING

    def test_corrupt_json_reports_parse_error(self, tmp_path, monkeypatch):
        from kiln.cli.mcp_config_audit import audit_all_mcp_clients

        desktop = tmp_path / "desktop.json"
        desktop.write_text("{ not valid json")
        _redirect_client_paths(
            monkeypatch, desktop=desktop, code=tmp_path / "x", codex=tmp_path / "y",
        )

        results = audit_all_mcp_clients()
        desktop_result = next(r for r in results if r.client == "Claude Desktop")
        assert desktop_result.config_exists
        assert desktop_result.parse_error is not None
        assert "JSONDecodeError" in desktop_result.parse_error
        assert desktop_result.entries == []

    def test_config_without_mcp_servers_section_is_neutral(
        self, tmp_path, monkeypatch,
    ):
        """A config that exists but has no ``mcpServers`` block is
        fine — the user installed Kiln elsewhere.  Auditor must NOT
        treat this as drift; the renderer hides clients with empty
        entries."""
        from kiln.cli.mcp_config_audit import audit_all_mcp_clients

        desktop = tmp_path / "desktop.json"
        desktop.write_text(json.dumps({"unrelated": "config"}))
        _redirect_client_paths(
            monkeypatch, desktop=desktop, code=tmp_path / "x", codex=tmp_path / "y",
        )

        results = audit_all_mcp_clients()
        r = next(x for x in results if x.client == "Claude Desktop")
        assert r.config_exists
        assert r.parse_error is None
        assert r.entries == []
        assert not r.has_drift


# ---------------------------------------------------------------------------
# Codex TOML path — both tomllib and the line-level fallback
# ---------------------------------------------------------------------------


class TestCodexTomlAudit:
    def test_parses_command_field_with_stdlib_tomllib(self, tmp_path, monkeypatch):
        """Codex configs use ``[mcp_servers.<name>]`` TOML tables —
        verify the parser path produces a verified-OK entry when the
        command exists."""
        from kiln.cli.mcp_config_audit import audit_all_mcp_clients

        binary = _make_executable(tmp_path / "kiln")
        codex = tmp_path / "codex.toml"
        codex.write_text(
            f'[mcp_servers.kiln]\ncommand = "{binary}"\nargs = ["serve"]\n',
        )
        _redirect_client_paths(
            monkeypatch, desktop=tmp_path / "x", code=tmp_path / "y", codex=codex,
        )

        results = audit_all_mcp_clients()
        codex_result = next(r for r in results if r.client == "Codex")
        assert codex_result.entries[0].is_ok

    def test_drift_in_codex_command_is_flagged(self, tmp_path, monkeypatch):
        from kiln.cli.mcp_config_audit import (
            audit_all_mcp_clients,
            STATUS_COMMAND_MISSING,
        )

        codex = tmp_path / "codex.toml"
        codex.write_text(
            f'[mcp_servers.kiln]\ncommand = "{tmp_path / "nope"}"\n',
        )
        _redirect_client_paths(
            monkeypatch, desktop=tmp_path / "x", code=tmp_path / "y", codex=codex,
        )

        results = audit_all_mcp_clients()
        codex_result = next(r for r in results if r.client == "Codex")
        assert codex_result.has_drift
        assert codex_result.entries[0].status == STATUS_COMMAND_MISSING

    def test_line_level_fallback_when_no_toml_parser(self, tmp_path, monkeypatch):
        """If neither ``tomllib`` nor ``tomli`` import, the auditor
        falls back to a line-level scan that still recovers the
        ``command`` field per ``mcp_servers.<name>`` table — enough
        to verify the binary path."""
        import kiln.cli.mcp_config_audit as audit_mod

        binary = _make_executable(tmp_path / "kiln")
        codex = tmp_path / "codex.toml"
        codex.write_text(
            f'[mcp_servers.kiln]\ncommand = "{binary}"\nargs = ["serve"]\n'
            f'[mcp_servers.other]\ncommand = "/no-such/binary"\n',
        )
        _redirect_client_paths(
            monkeypatch, desktop=tmp_path / "x", code=tmp_path / "y", codex=codex,
        )
        monkeypatch.setattr(audit_mod, "_toml_parser", lambda: None)

        results = audit_mod.audit_all_mcp_clients()
        codex_result = next(r for r in results if r.client == "Codex")
        assert len(codex_result.entries) == 2
        kiln_entry = next(e for e in codex_result.entries if e.name == "kiln")
        other_entry = next(e for e in codex_result.entries if e.name == "other")
        assert kiln_entry.is_ok
        assert not other_entry.is_ok


# ---------------------------------------------------------------------------
# JSON-payload shape
# ---------------------------------------------------------------------------


class TestJsonPayload:
    def test_payload_is_serialisable_and_complete(self, tmp_path, monkeypatch):
        from kiln.cli.mcp_config_audit import (
            audit_all_mcp_clients,
            to_json_payload,
        )

        binary = _make_executable(tmp_path / "kiln")
        desktop = tmp_path / "desktop.json"
        desktop.write_text(json.dumps({"mcpServers": {
            "kiln": {"command": str(binary), "args": ["serve"]},
            "stale": {"command": str(tmp_path / "no-such-binary")},
        }}))
        _redirect_client_paths(
            monkeypatch, desktop=desktop, code=tmp_path / "x", codex=tmp_path / "y",
        )

        payload = to_json_payload(audit_all_mcp_clients())
        # Round-trip through json to prove serialisability.
        round_tripped = json.loads(json.dumps(payload))
        desktop_payload = next(
            p for p in round_tripped if p["client"] == "Claude Desktop"
        )
        assert desktop_payload["config_exists"] is True
        assert desktop_payload["parse_error"] is None
        names = {e["name"] for e in desktop_payload["entries"]}
        assert names == {"kiln", "stale"}
        statuses = {e["name"]: e["status"] for e in desktop_payload["entries"]}
        assert statuses["kiln"] == "ok"
        assert statuses["stale"] == "command_missing"


# ---------------------------------------------------------------------------
# kiln health integration
# ---------------------------------------------------------------------------


class TestHealthCliIntegration:
    """End-to-end check that ``kiln health`` surfaces drift in both
    pretty and JSON modes.  The slicer / printer / kiln-version
    checks are stubbed where convenient so the test focuses on the
    MCP-config rows."""

    def test_json_mode_includes_mcp_clients_section(
        self, tmp_path, monkeypatch,
    ):
        from click.testing import CliRunner
        from kiln.cli.main import cli

        binary = _make_executable(tmp_path / "kiln")
        desktop = tmp_path / "desktop.json"
        desktop.write_text(json.dumps({"mcpServers": {
            "kiln": {"command": str(binary), "args": ["serve"]},
        }}))
        _redirect_client_paths(
            monkeypatch, desktop=desktop, code=tmp_path / "x", codex=tmp_path / "y",
        )

        result = CliRunner().invoke(cli, ["health", "--json"])
        assert result.exit_code == 0, result.output
        body = json.loads(result.output)
        data = body.get("data") if isinstance(body, dict) else None
        assert data is not None, body
        assert "mcp_clients" in data
        assert data.get("mcp_clients_ok") is True
        desktop_payload = next(
            p for p in data["mcp_clients"] if p["client"] == "Claude Desktop"
        )
        assert desktop_payload["entries"][0]["status"] == "ok"

    def test_pretty_mode_warns_on_drift_with_recovery_command(
        self, tmp_path, monkeypatch,
    ):
        """When drift is detected and the self-heal pass cannot
        resolve a working kiln binary, the original warning + the
        ``kiln install-mcp`` copy-paste recovery line must still
        render.  Force the resolver to find nothing so this test
        targets the no-self-heal-possible path only; the post-heal
        happy path is covered by ``TestHealthSelfHeal``."""
        from click.testing import CliRunner
        from kiln.cli import mcp_config_repair
        from kiln.cli.main import cli

        desktop = tmp_path / "desktop.json"
        desktop.write_text(json.dumps({"mcpServers": {
            "kiln": {"command": str(tmp_path / "no-such-binary")},
        }}))
        _redirect_client_paths(
            monkeypatch, desktop=desktop, code=tmp_path / "x", codex=tmp_path / "y",
        )
        monkeypatch.setattr(
            mcp_config_repair, "_current_kiln_command",
            lambda: str(tmp_path / "no-such-binary"),
        )
        monkeypatch.setattr(mcp_config_repair.shutil, "which", lambda _name: None)

        result = CliRunner().invoke(cli, ["health"])
        assert result.exit_code == 0, result.output
        out = result.output
        assert "Claude Desktop" in out
        # The recovery line must include both the [x] mark and the
        # exact command the user should run — that's the difference
        # between "yellow banner with no clue" and "one line tells me
        # what to do."
        assert "[x] Claude Desktop" in out
        assert "kiln install-mcp" in out
        # And there's no spurious ``Repaired`` line either.
        assert "Repaired " not in out

    def test_pretty_mode_stays_quiet_when_no_clients_configured(
        self, tmp_path, monkeypatch,
    ):
        """If the user has no MCP client configs at all, ``kiln health``
        shouldn't render anything new — the legacy four-row output
        stays exactly as it was."""
        from click.testing import CliRunner
        from kiln.cli.main import cli

        _redirect_client_paths(
            monkeypatch,
            desktop=tmp_path / "no" / "desktop.json",
            code=tmp_path / "no" / "code.json",
            codex=tmp_path / "no" / "codex.toml",
        )

        result = CliRunner().invoke(cli, ["health"])
        assert result.exit_code == 0, result.output
        # No MCP-related lines emitted at all.
        for line in result.output.splitlines():
            assert "Claude Desktop" not in line
            assert "Claude Code" not in line
            assert "Codex" not in line
            assert "install-mcp" not in line

    def test_auditor_failure_renders_soft_warning_not_crash(
        self, tmp_path, monkeypatch,
    ):
        """Auditor failures (import error, unexpected raise, future
        regression) must NEVER crash ``kiln health``.  Force
        ``audit_all_mcp_clients`` to raise and verify the command
        still exits 0, the legacy rows still render, and the soft
        warning surfaces the underlying error message."""
        from click.testing import CliRunner
        from kiln.cli import main as main_mod
        from kiln.cli import mcp_config_audit as audit_mod

        def _boom() -> None:
            raise RuntimeError("simulated auditor regression")

        # ``main.health`` does ``from kiln.cli.mcp_config_audit import
        # audit_all_mcp_clients`` at call time, so patching the module
        # attribute is enough to redirect the function lookup.
        monkeypatch.setattr(audit_mod, "audit_all_mcp_clients", _boom)

        result = CliRunner().invoke(main_mod.cli, ["health"])
        assert result.exit_code == 0, result.output
        assert "MCP config audit failed" in result.output
        assert "simulated auditor regression" in result.output

    def test_line_level_toml_fallback_tolerates_bad_escape(
        self, tmp_path, monkeypatch,
    ):
        """A malformed escape sequence in a Codex command string must
        not crash the audit — fall back to the raw value so the
        binary check still runs.  Real-world trigger: a Windows-style
        backslash path in a TOML string (or any unsupported escape)."""
        import kiln.cli.mcp_config_audit as audit_mod

        codex = tmp_path / "codex.toml"
        # ``\z`` is not a valid TOML escape; decode("unicode_escape")
        # would raise UnicodeDecodeError on the literal value.  The
        # auditor must fall back gracefully and still produce a result.
        codex.write_text(
            '[mcp_servers.kiln]\ncommand = "/path/that\\z-bad-escape"\n',
        )
        _redirect_client_paths(
            monkeypatch, desktop=tmp_path / "x", code=tmp_path / "y", codex=codex,
        )
        monkeypatch.setattr(audit_mod, "_toml_parser", lambda: None)

        # The path won't exist (expected), but ``audit_all_mcp_clients``
        # must return without raising.
        results = audit_mod.audit_all_mcp_clients()
        codex_result = next(r for r in results if r.client == "Codex")
        assert codex_result.config_exists
        assert codex_result.parse_error is None  # bad escape, not bad TOML
        assert len(codex_result.entries) == 1


# ---------------------------------------------------------------------------
# Self-heal — kiln.cli.mcp_config_repair
# ---------------------------------------------------------------------------


class TestRepair:
    """Direct tests of the repair module.  Each test seeds a config
    with a broken ``command:`` path, runs the repair, and verifies
    (a) the file was rewritten surgically (only the target ``command``
    field changes), (b) every other byte is preserved, and (c)
    re-running the repair on the now-correct file is a no-op
    (idempotency)."""

    def test_repairs_claude_desktop_with_missing_binary(
        self, tmp_path, monkeypatch,
    ):
        from kiln.cli import mcp_config_repair
        from kiln.cli.mcp_config_audit import audit_all_mcp_clients

        working = _make_executable(tmp_path / "bin" / "kiln")
        broken = tmp_path / "ghost" / "kiln"
        desktop = tmp_path / "claude_desktop_config.json"
        original = {
            "mcpServers": {
                "kiln": {"command": str(broken), "args": ["serve"]},
                "other-server": {"command": "/usr/bin/echo"},
            },
            "userPref": {"preserve": True},
        }
        desktop.write_text(json.dumps(original, indent=2) + "\n")
        _redirect_client_paths(
            monkeypatch, desktop=desktop, code=tmp_path / "x", codex=tmp_path / "y",
        )
        monkeypatch.setattr(
            mcp_config_repair, "_current_kiln_command", lambda: str(working),
        )

        actions = mcp_config_repair.repair_drifted_kiln_entries(
            audit_all_mcp_clients(),
        )
        assert len(actions) == 1
        assert actions[0].client == "Claude Desktop"
        assert actions[0].entry == "kiln"
        assert actions[0].old == str(broken)
        # ``new`` resolves the kiln path the same way the audit does.
        assert Path(actions[0].new).resolve() == Path(working).resolve()

        # File was surgically rewritten: only the ``kiln`` entry's
        # ``command`` changed; every other key in the document survives
        # untouched.
        after = json.loads(desktop.read_text())
        assert after["mcpServers"]["kiln"]["command"] == actions[0].new
        assert after["mcpServers"]["kiln"]["args"] == ["serve"]
        assert after["mcpServers"]["other-server"] == {"command": "/usr/bin/echo"}
        assert after["userPref"] == {"preserve": True}

    def test_repairs_codex_toml_preserving_comments_and_siblings(
        self, tmp_path, monkeypatch,
    ):
        from kiln.cli import mcp_config_repair
        from kiln.cli.mcp_config_audit import audit_all_mcp_clients

        working = _make_executable(tmp_path / "bin" / "kiln")
        broken = tmp_path / "ghost" / "kiln"
        codex = tmp_path / "config.toml"
        original = (
            "# top-level comment\n"
            "\n"
            "[mcp_servers.kiln]\n"
            f'command = "{broken}"  # was venv\n'
            'args = ["serve"]\n'
            "\n"
            "[mcp_servers.other]\n"
            'command = "/usr/bin/echo"\n'
            'args = []\n'
            "\n"
            "[unrelated]\n"
            'value = "keep me"\n'
        )
        codex.write_text(original)
        _redirect_client_paths(
            monkeypatch, desktop=tmp_path / "x", code=tmp_path / "y", codex=codex,
        )
        monkeypatch.setattr(
            mcp_config_repair, "_current_kiln_command", lambda: str(working),
        )

        actions = mcp_config_repair.repair_drifted_kiln_entries(
            audit_all_mcp_clients(),
        )
        assert len(actions) == 1
        assert actions[0].client == "Codex"

        after = codex.read_text()
        # The kiln entry's ``command`` was rewritten; everything else
        # — comments, sibling table, args, blank lines — is preserved.
        assert f'command = "{actions[0].new}"  # was venv\n' in after
        assert '[mcp_servers.other]\n' in after
        assert 'command = "/usr/bin/echo"\n' in after
        assert '[unrelated]\n' in after
        assert '# top-level comment\n' in after

    def test_repair_is_idempotent(self, tmp_path, monkeypatch):
        from kiln.cli import mcp_config_repair
        from kiln.cli.mcp_config_audit import audit_all_mcp_clients

        working = _make_executable(tmp_path / "bin" / "kiln")
        broken = tmp_path / "ghost" / "kiln"
        desktop = tmp_path / "desktop.json"
        desktop.write_text(json.dumps({"mcpServers": {
            "kiln": {"command": str(broken), "args": ["serve"]},
        }}))
        _redirect_client_paths(
            monkeypatch, desktop=desktop, code=tmp_path / "x", codex=tmp_path / "y",
        )
        monkeypatch.setattr(
            mcp_config_repair, "_current_kiln_command", lambda: str(working),
        )

        first = mcp_config_repair.repair_drifted_kiln_entries(
            audit_all_mcp_clients(),
        )
        assert len(first) == 1

        # Re-run.  The config now points at a real binary; the audit
        # reports no drift; repair must be a no-op.
        second = mcp_config_repair.repair_drifted_kiln_entries(
            audit_all_mcp_clients(),
        )
        assert second == []

    def test_repair_skips_when_no_working_binary_found(
        self, tmp_path, monkeypatch,
    ):
        """If we can't resolve a working kiln binary, do NOTHING.
        The user's audit warning stays visible; we don't churn the
        file by writing a fresh broken path."""
        from kiln.cli import mcp_config_repair
        from kiln.cli.mcp_config_audit import audit_all_mcp_clients

        broken = tmp_path / "ghost" / "kiln"
        desktop = tmp_path / "desktop.json"
        original = json.dumps({"mcpServers": {
            "kiln": {"command": str(broken), "args": ["serve"]},
        }}) + "\n"
        desktop.write_text(original)
        _redirect_client_paths(
            monkeypatch, desktop=desktop, code=tmp_path / "x", codex=tmp_path / "y",
        )
        monkeypatch.setattr(
            mcp_config_repair, "_current_kiln_command", lambda: str(broken),
        )
        # And ``shutil.which`` also returns no kiln.
        monkeypatch.setattr(mcp_config_repair.shutil, "which", lambda _name: None)

        actions = mcp_config_repair.repair_drifted_kiln_entries(
            audit_all_mcp_clients(),
        )
        assert actions == []
        # File untouched.
        assert desktop.read_text() == original

    def test_repair_ignores_non_kiln_entries(self, tmp_path, monkeypatch):
        """Sibling MCP servers — filesystem, git, anything not named
        ``kiln`` — are out of scope.  Their drift stays a warning;
        the repair pass does not touch them."""
        from kiln.cli import mcp_config_repair
        from kiln.cli.mcp_config_audit import audit_all_mcp_clients

        working = _make_executable(tmp_path / "bin" / "kiln")
        broken = tmp_path / "ghost" / "other"
        desktop = tmp_path / "desktop.json"
        desktop.write_text(json.dumps({"mcpServers": {
            "kiln": {"command": str(working), "args": ["serve"]},
            "other-server": {"command": str(broken)},
        }}))
        _redirect_client_paths(
            monkeypatch, desktop=desktop, code=tmp_path / "x", codex=tmp_path / "y",
        )
        monkeypatch.setattr(
            mcp_config_repair, "_current_kiln_command", lambda: str(working),
        )

        actions = mcp_config_repair.repair_drifted_kiln_entries(
            audit_all_mcp_clients(),
        )
        assert actions == []
        after = json.loads(desktop.read_text())
        assert after["mcpServers"]["other-server"]["command"] == str(broken)

    def test_repair_skips_ok_entries(self, tmp_path, monkeypatch):
        """An OK entry must never be rewritten to a 'different' OK
        binary — that's surprising churn.  Even if ``_current_kiln_command``
        would return a different path, an OK entry is left alone."""
        from kiln.cli import mcp_config_repair
        from kiln.cli.mcp_config_audit import audit_all_mcp_clients

        original_binary = _make_executable(tmp_path / "a" / "kiln")
        different_binary = _make_executable(tmp_path / "b" / "kiln")
        desktop = tmp_path / "desktop.json"
        desktop.write_text(json.dumps({"mcpServers": {
            "kiln": {"command": str(original_binary), "args": ["serve"]},
        }}))
        _redirect_client_paths(
            monkeypatch, desktop=desktop, code=tmp_path / "x", codex=tmp_path / "y",
        )
        monkeypatch.setattr(
            mcp_config_repair,
            "_current_kiln_command",
            lambda: str(different_binary),
        )

        actions = mcp_config_repair.repair_drifted_kiln_entries(
            audit_all_mcp_clients(),
        )
        assert actions == []
        # File still points at the original.
        after = json.loads(desktop.read_text())
        assert after["mcpServers"]["kiln"]["command"] == str(original_binary)

    def test_repair_skips_when_drifted_path_already_equivalent(
        self, tmp_path, monkeypatch,
    ):
        """Edge case: a drifted entry already points (via symlink) at
        the same target the resolver would write.  Treat as no-op,
        not a churn write — the original warning will stop appearing
        once the underlying symlink is repaired by other means."""
        from kiln.cli import mcp_config_repair
        from kiln.cli.mcp_config_audit import audit_all_mcp_clients

        target = _make_executable(tmp_path / "real" / "kiln")
        # The entry points at the resolved target directly, but the
        # binary check has been forced to fail (simulating a
        # transient ``X_OK`` denial that the audit caught but the
        # resolver later succeeds against).  In that situation
        # ``_paths_equivalent`` should suppress the write.
        desktop = tmp_path / "desktop.json"
        original = json.dumps({"mcpServers": {
            "kiln": {"command": str(target), "args": ["serve"]},
        }}, indent=2) + "\n"
        desktop.write_text(original)
        _redirect_client_paths(
            monkeypatch, desktop=desktop, code=tmp_path / "x", codex=tmp_path / "y",
        )
        # Force the audit to consider the entry broken even though
        # the binary exists, then verify the repair declines to
        # rewrite to the equivalent path.
        import kiln.cli.mcp_config_audit as audit_mod
        original_check = audit_mod._check_server_entry

        def _fake_check(name, entry):
            base = original_check(name, entry)
            if name == "kiln" and base.is_ok:
                return audit_mod.ServerEntryAuditResult(
                    name=name,
                    command=base.command,
                    status=audit_mod.STATUS_COMMAND_NOT_EXECUTABLE,
                    detail="forced for test",
                )
            return base

        monkeypatch.setattr(audit_mod, "_check_server_entry", _fake_check)
        monkeypatch.setattr(
            mcp_config_repair, "_current_kiln_command", lambda: str(target),
        )

        actions = mcp_config_repair.repair_drifted_kiln_entries(
            audit_all_mcp_clients(),
        )
        assert actions == []
        # File untouched (byte-identical to what we wrote).
        assert desktop.read_text() == original

    def test_repair_atomic_temp_file_cleaned_on_success(
        self, tmp_path, monkeypatch,
    ):
        """Verify the atomic-write helper doesn't leave temp files
        behind in the config directory after a successful rewrite."""
        from kiln.cli import mcp_config_repair
        from kiln.cli.mcp_config_audit import audit_all_mcp_clients

        working = _make_executable(tmp_path / "bin" / "kiln")
        broken = tmp_path / "ghost" / "kiln"
        desktop = tmp_path / "desktop.json"
        desktop.write_text(json.dumps({"mcpServers": {
            "kiln": {"command": str(broken)},
        }}))
        _redirect_client_paths(
            monkeypatch, desktop=desktop, code=tmp_path / "x", codex=tmp_path / "y",
        )
        monkeypatch.setattr(
            mcp_config_repair, "_current_kiln_command", lambda: str(working),
        )

        mcp_config_repair.repair_drifted_kiln_entries(audit_all_mcp_clients())
        # No leftover ``.tmp`` siblings in the config directory.
        leftover = sorted(
            p.name for p in desktop.parent.iterdir() if p.name.endswith(".tmp")
        )
        assert leftover == [], leftover


# ---------------------------------------------------------------------------
# kiln health integration — self-heal output
# ---------------------------------------------------------------------------


class TestHealthSelfHeal:
    def test_pretty_prints_one_repair_line_then_clean_status(
        self, tmp_path, monkeypatch,
    ):
        """A broken ``kiln`` entry is fixed in-place; the user sees
        exactly one ``Repaired ...`` line and the post-repair status
        row is clean (no ``[x]`` warning for an entry we just
        fixed)."""
        from click.testing import CliRunner
        from kiln.cli import mcp_config_repair
        from kiln.cli.main import cli

        working = _make_executable(tmp_path / "bin" / "kiln")
        broken = tmp_path / "ghost" / "kiln"
        desktop = tmp_path / "desktop.json"
        desktop.write_text(json.dumps({"mcpServers": {
            "kiln": {"command": str(broken), "args": ["serve"]},
        }}))
        _redirect_client_paths(
            monkeypatch, desktop=desktop, code=tmp_path / "x", codex=tmp_path / "y",
        )
        monkeypatch.setattr(
            mcp_config_repair, "_current_kiln_command", lambda: str(working),
        )

        result = CliRunner().invoke(cli, ["health"])
        assert result.exit_code == 0, result.output
        out = result.output
        # Exactly one ``Repaired`` line.
        repaired_lines = [
            line for line in out.splitlines() if line.startswith("Repaired ")
        ]
        assert len(repaired_lines) == 1
        assert "Repaired Claude Desktop:" in repaired_lines[0]
        assert " → " in repaired_lines[0]
        # Post-repair status row is clean — no [x] warning for kiln.
        assert "[x] Claude Desktop" not in out

    def test_pretty_prints_nothing_when_no_repair_needed(
        self, tmp_path, monkeypatch,
    ):
        """Idempotency: running ``kiln health`` against an already-
        correct config must not produce any ``Repaired`` lines, and
        must not modify the file."""
        from click.testing import CliRunner
        from kiln.cli import mcp_config_repair
        from kiln.cli.main import cli

        working = _make_executable(tmp_path / "bin" / "kiln")
        desktop = tmp_path / "desktop.json"
        original = json.dumps({"mcpServers": {
            "kiln": {"command": str(working), "args": ["serve"]},
        }}, indent=2, sort_keys=True) + "\n"
        desktop.write_text(original)
        _redirect_client_paths(
            monkeypatch, desktop=desktop, code=tmp_path / "x", codex=tmp_path / "y",
        )
        monkeypatch.setattr(
            mcp_config_repair, "_current_kiln_command", lambda: str(working),
        )

        # First run.
        first = CliRunner().invoke(cli, ["health"])
        assert first.exit_code == 0, first.output
        assert "Repaired " not in first.output
        # Second run.
        second = CliRunner().invoke(cli, ["health"])
        assert second.exit_code == 0, second.output
        assert "Repaired " not in second.output
        # File byte-identical.
        assert desktop.read_text() == original

    def test_json_mode_surfaces_repair_payload(self, tmp_path, monkeypatch):
        """``kiln health --json`` includes a ``mcp_clients_repaired``
        array describing every rewrite (empty when none)."""
        from click.testing import CliRunner
        from kiln.cli import mcp_config_repair
        from kiln.cli.main import cli

        working = _make_executable(tmp_path / "bin" / "kiln")
        broken = tmp_path / "ghost" / "kiln"
        desktop = tmp_path / "desktop.json"
        desktop.write_text(json.dumps({"mcpServers": {
            "kiln": {"command": str(broken), "args": ["serve"]},
        }}))
        _redirect_client_paths(
            monkeypatch, desktop=desktop, code=tmp_path / "x", codex=tmp_path / "y",
        )
        monkeypatch.setattr(
            mcp_config_repair, "_current_kiln_command", lambda: str(working),
        )

        result = CliRunner().invoke(cli, ["health", "--json"])
        assert result.exit_code == 0, result.output
        body = json.loads(result.output)
        data = body.get("data") if isinstance(body, dict) else None
        assert data is not None, body
        assert "mcp_clients_repaired" in data
        repairs = data["mcp_clients_repaired"]
        assert len(repairs) == 1
        assert repairs[0]["client"] == "Claude Desktop"
        assert repairs[0]["entry"] == "kiln"
        assert repairs[0]["old"] == str(broken)
        # Post-repair, the audit section reports clean.
        assert data["mcp_clients_ok"] is True
