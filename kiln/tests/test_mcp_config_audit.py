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
        from click.testing import CliRunner
        from kiln.cli.main import cli

        desktop = tmp_path / "desktop.json"
        desktop.write_text(json.dumps({"mcpServers": {
            "kiln": {"command": str(tmp_path / "no-such-binary")},
        }}))
        _redirect_client_paths(
            monkeypatch, desktop=desktop, code=tmp_path / "x", codex=tmp_path / "y",
        )

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
