"""Read-only audit of installed MCP-client configurations.

``kiln install-mcp`` writes Kiln's MCP entry into the configs of every
client we support (Claude Desktop, Claude Code, Codex).  Once written,
nothing watches whether those entries stay valid — and they don't, in
practice.  A renamed venv, a deleted Python install, a hand-edit that
flips a path to a sibling repo, or a stale dev-experiment config all
silently leave the client pointed at a binary that no longer exists.

The user's first signal is the MCP client's yellow "Server disconnected"
banner at next launch, with no actionable detail and no path to
self-recovery — they have to grep configs by hand.

This module surfaces the same checks ``kiln install-mcp`` already
performs implicitly (read the JSON / TOML, find the entry, look at the
``command``) and exposes them as a structured audit so ``kiln health``
can report drift with a one-row warning per affected client.

Read-only.  Never writes.  Never raises on a missing or unparseable
config — those become reported findings.  Fast: at most one stat per
configured server entry plus three small file reads.

Public surface:

* :func:`audit_all_mcp_clients` — entry point used by ``kiln health``.
* :class:`ClientAuditResult` / :class:`ServerEntryAuditResult` — the
  structured findings.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kiln.cli.install_mcp import (
    _claude_code_config_path,
    _claude_desktop_config_path,
    _codex_config_path,
)


# Status of a single ``mcp_servers.<name>`` entry.  We use plain
# strings rather than an Enum so the JSON output of ``kiln health``
# stays trivially serializable; the value set is small and the
# semantic difference between "missing" vs "not_executable" matters
# in the human-facing line.
STATUS_OK = "ok"
STATUS_COMMAND_MISSING = "command_missing"
STATUS_COMMAND_NOT_EXECUTABLE = "command_not_executable"
STATUS_ENTRY_MALFORMED = "entry_malformed"


@dataclass
class ServerEntryAuditResult:
    """One MCP server entry inside a client config (e.g. the ``kiln``
    key inside ``Claude Desktop``'s ``mcpServers``).

    ``status`` is one of the ``STATUS_*`` constants in this module;
    ``detail`` is a one-line human-readable description, present only
    when ``status != STATUS_OK``.
    """

    name: str
    command: str | None
    status: str
    detail: str | None = None

    @property
    def is_ok(self) -> bool:
        return self.status == STATUS_OK


@dataclass
class ClientAuditResult:
    """All of one client's ``mcp_servers`` entries, plus the client's
    config path and whether that file exists at all.

    A client whose config doesn't exist isn't a problem — many users
    only install Kiln into one or two clients.  That's reflected by
    ``config_exists=False`` and an empty ``entries`` list, which the
    ``kiln health`` renderer treats as "skip this row" rather than
    "warn the user."
    """

    client: str
    path: Path
    config_exists: bool
    entries: list[ServerEntryAuditResult] = field(default_factory=list)
    parse_error: str | None = None

    @property
    def has_drift(self) -> bool:
        """True iff the file parsed and at least one entry isn't OK.

        A ``parse_error`` is reported separately by the renderer — it's
        a different failure mode (corrupt config) than entry-level
        drift, and conflating them in one bool would lose information
        for the JSON consumer.
        """
        if not self.config_exists:
            return False
        return any(not entry.is_ok for entry in self.entries)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def audit_all_mcp_clients() -> list[ClientAuditResult]:
    """Audit every supported MCP client's config in one pass.

    Returns one :class:`ClientAuditResult` per client, in display
    order.  Never raises; every failure mode is encoded as a status
    on the result.  Order matches the user's mental model — Claude
    Desktop first (most common), then Claude Code, then Codex — so
    the ``kiln health`` output reads consistently across runs.
    """
    return [
        _audit_claude_json("Claude Desktop", _claude_desktop_config_path()),
        _audit_claude_json("Claude Code", _claude_code_config_path()),
        _audit_codex_toml("Codex", _codex_config_path()),
    ]


# ---------------------------------------------------------------------------
# Per-client parsers
# ---------------------------------------------------------------------------


def _audit_claude_json(client: str, path: Path) -> ClientAuditResult:
    """Audit a Claude Desktop or Claude Code config (both are JSON
    with the same ``mcpServers`` shape)."""
    if not path.exists():
        return ClientAuditResult(client=client, path=path, config_exists=False)
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw) if raw.strip() else {}
    except (OSError, json.JSONDecodeError) as exc:
        return ClientAuditResult(
            client=client,
            path=path,
            config_exists=True,
            parse_error=f"{type(exc).__name__}: {exc}",
        )

    servers = data.get("mcpServers") if isinstance(data, dict) else None
    if not isinstance(servers, dict):
        return ClientAuditResult(
            client=client,
            path=path,
            config_exists=True,
        )

    entries: list[ServerEntryAuditResult] = []
    for name, raw_entry in servers.items():
        entries.append(_check_server_entry(name, raw_entry))
    return ClientAuditResult(
        client=client,
        path=path,
        config_exists=True,
        entries=entries,
    )


# Match ``[mcp_servers.<name>]`` table headers.  Restrictive intentionally:
# Codex's documented MCP config uses bare-key dotted notation; we don't
# need to handle quoted keys to flag a broken binary path.
_CODEX_TABLE_RE = re.compile(r"^\s*\[mcp_servers\.([A-Za-z0-9_-]+)\]\s*(?:#.*)?$")
_CODEX_COMMAND_RE = re.compile(r'^\s*command\s*=\s*"((?:[^"\\]|\\.)*)"\s*(?:#.*)?$')


def _audit_codex_toml(client: str, path: Path) -> ClientAuditResult:
    """Audit a Codex config (TOML with ``[mcp_servers.<name>]`` tables).

    Prefers Python 3.11's stdlib ``tomllib``; falls back to
    ``tomli``; falls back to a tiny line-level matcher that recovers
    just the ``command`` field per ``mcp_servers.*`` table.  The
    line-level path keeps ``kiln health`` usable on Python 3.10
    without forcing a new dependency — it only needs to find the
    ``command`` field, not parse arbitrary TOML semantics.
    """
    if not path.exists():
        return ClientAuditResult(client=client, path=path, config_exists=False)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return ClientAuditResult(
            client=client,
            path=path,
            config_exists=True,
            parse_error=f"{type(exc).__name__}: {exc}",
        )

    parser = _toml_parser()
    if parser is not None:
        try:
            data = parser.loads(text)
        except Exception as exc:  # noqa: BLE001 — any TOML parse error becomes a finding
            return ClientAuditResult(
                client=client,
                path=path,
                config_exists=True,
                parse_error=f"TOMLDecodeError: {exc}",
            )
        servers = data.get("mcp_servers") if isinstance(data, dict) else None
        if not isinstance(servers, dict):
            return ClientAuditResult(client=client, path=path, config_exists=True)
        entries = [_check_server_entry(name, entry) for name, entry in servers.items()]
        return ClientAuditResult(
            client=client,
            path=path,
            config_exists=True,
            entries=entries,
        )

    # No TOML parser available — scan line-level.  We only extract the
    # ``command`` field per ``mcp_servers.<name>`` table; that's all
    # the auditor needs to verify the binary exists.
    entries: list[ServerEntryAuditResult] = []
    current_name: str | None = None
    current_command: str | None = None
    for line in text.splitlines():
        table_match = _CODEX_TABLE_RE.match(line)
        if table_match:
            if current_name is not None:
                entries.append(_check_server_entry(
                    current_name, {"command": current_command},
                ))
            current_name = table_match.group(1)
            current_command = None
            continue
        if current_name is None:
            continue
        cmd_match = _CODEX_COMMAND_RE.match(line)
        if cmd_match:
            current_command = cmd_match.group(1).encode("utf-8").decode("unicode_escape")
    if current_name is not None:
        entries.append(_check_server_entry(
            current_name, {"command": current_command},
        ))
    return ClientAuditResult(
        client=client,
        path=path,
        config_exists=True,
        entries=entries,
    )


def _toml_parser():  # type: ignore[no-untyped-def]
    """Return the best available TOML parser, or ``None``.

    Mirrors ``install_mcp._validate_toml_if_possible``'s import dance
    so the two modules stay consistent on which TOML parsers they
    accept.
    """
    try:
        import tomllib  # type: ignore[no-redef]
        return tomllib
    except ModuleNotFoundError:
        pass
    try:
        import tomli  # type: ignore[no-redef]
        return tomli
    except ModuleNotFoundError:
        return None


# ---------------------------------------------------------------------------
# Entry-level binary check
# ---------------------------------------------------------------------------


def _check_server_entry(name: str, raw_entry: Any) -> ServerEntryAuditResult:
    """Verify one MCP-server entry's ``command`` points at an
    executable file."""
    if not isinstance(raw_entry, dict):
        return ServerEntryAuditResult(
            name=name,
            command=None,
            status=STATUS_ENTRY_MALFORMED,
            detail=f"entry must be an object, got {type(raw_entry).__name__}",
        )
    command = raw_entry.get("command")
    if not isinstance(command, str) or not command.strip():
        return ServerEntryAuditResult(
            name=name,
            command=None if not isinstance(command, str) else command,
            status=STATUS_ENTRY_MALFORMED,
            detail="entry has no `command` field",
        )

    # We don't resolve symlinks before checking, because a config can
    # legitimately point at a symlink (e.g. ``~/.local/bin/kiln`` →
    # versioned binary).  ``os.access`` follows symlinks transparently
    # and answers the right question: can this user execute the
    # eventual target file?
    expanded = Path(command).expanduser()
    if not expanded.exists():
        return ServerEntryAuditResult(
            name=name,
            command=command,
            status=STATUS_COMMAND_MISSING,
            detail=f"binary not found at {expanded}",
        )
    if not os.access(str(expanded), os.X_OK):
        return ServerEntryAuditResult(
            name=name,
            command=command,
            status=STATUS_COMMAND_NOT_EXECUTABLE,
            detail=f"binary at {expanded} is not executable",
        )
    return ServerEntryAuditResult(
        name=name,
        command=command,
        status=STATUS_OK,
    )


# ---------------------------------------------------------------------------
# Render helpers — for ``kiln health`` callers
# ---------------------------------------------------------------------------


def to_json_payload(results: list[ClientAuditResult]) -> list[dict[str, Any]]:
    """Translate audit results into JSON-friendly dicts for the
    ``--json`` mode of ``kiln health``.  Stable key names; never
    raises."""
    payload: list[dict[str, Any]] = []
    for r in results:
        payload.append({
            "client": r.client,
            "path": str(r.path),
            "config_exists": r.config_exists,
            "parse_error": r.parse_error,
            "entries": [
                {
                    "name": e.name,
                    "command": e.command,
                    "status": e.status,
                    "detail": e.detail,
                }
                for e in r.entries
            ],
        })
    return payload
