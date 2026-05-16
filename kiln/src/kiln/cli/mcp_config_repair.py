"""Self-heal stale ``command:`` paths in MCP-client configs.

``kiln install-mcp`` writes Kiln's MCP entry into each supported
client's config file once.  The ``kiln.cli.mcp_config_audit`` module
detects when a written entry has gone stale — a deleted venv, a
renamed Python install, a hand-edit that flips the path to a sibling
repo — and reports the drift.  This module closes the loop: when
``kiln health`` finds a drifted ``kiln`` entry whose binary is
missing or non-executable, it rewrites only that entry's ``command``
field with a working path resolved the same way ``install-mcp``
resolves it (``install_mcp._current_kiln_command()``).

Scope is intentionally narrow:

* Only entries whose name is ``kiln`` are eligible for repair.  Sibling
  MCP servers (other tools the user has installed) are out of scope —
  we don't know how to resolve their binaries.
* Only entries whose status is ``command_missing`` or
  ``command_not_executable`` get rewritten.  An OK entry is never
  rewritten to a "different" OK binary; a malformed-entry status
  signals structural breakage that a path swap won't fix.
* The repair is surgical: for JSON, only the ``mcpServers.kiln.command``
  string changes; for TOML, only the ``command = "..."`` line inside
  the ``[mcp_servers.kiln]`` table changes.  Every other byte in the
  file is preserved — user prefs in Claude configs, sibling servers
  in ``mcpServers``, comments and whitespace in Codex TOML.
* Writes are atomic via ``os.replace`` on a same-directory temp file
  so a crash mid-write cannot corrupt the config.
* If no working ``kiln`` binary can be resolved, the entry is left
  alone — better the original warning than a fresh broken path.

Public surface:

* :func:`repair_drifted_kiln_entries` — entry point used by ``kiln
  health``.  Returns a list of ``RepairAction`` describing each
  rewrite that was performed.
* :class:`RepairAction` — one repaired entry's before/after.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kiln.cli.install_mcp import _current_kiln_command
from kiln.cli.mcp_config_audit import (
    STATUS_COMMAND_MISSING,
    STATUS_COMMAND_NOT_EXECUTABLE,
    ClientAuditResult,
    ServerEntryAuditResult,
)

# Statuses that mean "the entry exists and is well-formed, but its
# binary path is broken."  These are the only statuses we self-heal —
# malformed entries need structural fixes ``install-mcp`` should
# regenerate, and an OK entry is by definition not in need of repair.
_REPAIRABLE_STATUSES = frozenset({
    STATUS_COMMAND_MISSING,
    STATUS_COMMAND_NOT_EXECUTABLE,
})

# Only the entry named ``kiln`` is eligible.  Other MCP servers in the
# same config (filesystem, git, etc.) may also have stale paths but
# their resolution is the responsibility of whoever installed them.
_REPAIRABLE_ENTRY_NAME = "kiln"


@dataclass
class RepairAction:
    """One performed rewrite.  ``old`` and ``new`` are the literal
    string values that appeared / will appear in the ``command``
    field of the affected entry; ``client`` and ``entry`` mirror the
    audit-result fields so the renderer can phrase the one-line
    summary without re-deriving them.
    """

    client: str
    entry: str
    path: Path
    old: str
    new: str


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def repair_drifted_kiln_entries(
    results: Iterable[ClientAuditResult],
) -> list[RepairAction]:
    """Repair every drifted ``kiln`` entry across all audit results.

    Returns the list of rewrites actually performed, in audit order.
    Empty when nothing needed repair (the common case after the first
    run).  Never raises: a config that resists rewriting — permissions,
    parse failures, unexpected file shapes — is left untouched and
    omitted from the return list, preserving the original audit
    warning so the user still sees something.
    """
    actions: list[RepairAction] = []
    resolved_target: str | None = None
    for result in results:
        if not result.config_exists or result.parse_error is not None:
            continue
        for entry in result.entries:
            if not _is_repairable(entry):
                continue
            if resolved_target is None:
                resolved_target = _resolve_working_kiln_command()
                if resolved_target is None:
                    # No working binary to point at — leave every
                    # drifted entry as-is so the audit warning stays
                    # visible.  Bailing out completely is fine because
                    # the resolver result is process-wide deterministic;
                    # no per-entry retry would succeed.
                    return actions
            if entry.command is not None and _paths_equivalent(
                entry.command, resolved_target,
            ):
                # The audit flagged this entry as drifted, but the
                # path it already holds resolves to the same binary
                # we'd write.  Rewriting would be a no-op churn; skip.
                continue
            action = _rewrite_entry(result, entry, resolved_target)
            if action is not None:
                actions.append(action)
    return actions


# ---------------------------------------------------------------------------
# Resolver — find a working ``kiln`` binary
# ---------------------------------------------------------------------------


def _resolve_working_kiln_command() -> str | None:
    """Return an absolute path to a working ``kiln`` executable, or
    ``None`` if none can be found.

    Mirrors ``install_mcp._current_kiln_command()`` for primary
    resolution.  Adds a final ``shutil.which`` fallback so that even
    when this Python process happens to have been launched without a
    ``kiln`` entry point in ``sys.argv[0]`` (e.g. embedded in a test
    runner), we can still locate a usable binary on ``PATH``.
    """
    candidate = _current_kiln_command()
    if _is_usable_binary(candidate):
        return _absolute(candidate)
    fallback = shutil.which("kiln")
    if fallback and _is_usable_binary(fallback):
        return _absolute(fallback)
    return None


def _is_usable_binary(path: str | None) -> bool:
    if not path:
        return False
    expanded = Path(path).expanduser()
    if not expanded.exists():
        return False
    return os.access(str(expanded), os.X_OK)


def _absolute(path: str) -> str:
    """Return an absolute path string, resolving symlinks only when
    that succeeds.  Symlinked installs (``~/.local/bin/kiln`` →
    versioned binary) are common, so don't insist on resolution."""
    expanded = Path(path).expanduser()
    try:
        return str(expanded.resolve())
    except OSError:
        return str(expanded)


def _paths_equivalent(a: str, b: str) -> bool:
    """Compare two filesystem paths under the same expand-and-resolve
    rules used by the audit.  Used to suppress no-op churn when an
    entry is flagged as drifted but happens to point at the same
    binary we'd write."""
    try:
        return _absolute(a) == _absolute(b)
    except OSError:
        return a == b


def _is_repairable(entry: ServerEntryAuditResult) -> bool:
    return (
        entry.name == _REPAIRABLE_ENTRY_NAME
        and entry.status in _REPAIRABLE_STATUSES
    )


# ---------------------------------------------------------------------------
# Per-client rewriters
# ---------------------------------------------------------------------------


def _rewrite_entry(
    result: ClientAuditResult,
    entry: ServerEntryAuditResult,
    new_command: str,
) -> RepairAction | None:
    """Dispatch to the JSON or TOML rewriter based on the client.

    Returns ``None`` and leaves the file untouched if the rewrite
    can't proceed — the audit warning then stays visible to the user.
    """
    old = entry.command or ""
    try:
        if result.client in ("Claude Desktop", "Claude Code"):
            rewrote = _rewrite_claude_json(result.path, entry.name, new_command)
        elif result.client == "Codex":
            rewrote = _rewrite_codex_toml(result.path, entry.name, new_command)
        else:
            return None
    except OSError:
        return None
    if not rewrote:
        return None
    return RepairAction(
        client=result.client,
        entry=entry.name,
        path=result.path,
        old=old,
        new=new_command,
    )


def _rewrite_claude_json(path: Path, entry_name: str, new_command: str) -> bool:
    """Surgically replace ``mcpServers.<entry_name>.command`` in a
    Claude-style JSON config.  Returns True if the file was written.

    Reads the JSON, mutates only the target ``command`` value, and
    writes the result atomically.  Every other key in the file — top-
    level user preferences, sibling MCP servers, the entry's own
    ``args`` and any other metadata — is preserved unchanged.  The
    serialized form matches ``install_mcp._write_json`` (indent=2,
    sort_keys, trailing newline) so subsequent ``install-mcp`` runs
    produce byte-identical files.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(data, dict):
        return False
    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        return False
    entry = servers.get(entry_name)
    if not isinstance(entry, dict):
        return False
    if entry.get("command") == new_command:
        return False
    entry["command"] = new_command
    serialized = json.dumps(data, indent=2, sort_keys=True) + "\n"
    _atomic_write_text(path, serialized)
    return True


# Match the table header for ``[mcp_servers.<name>]``.  The capture
# group is the bare name; whitespace and trailing comments are
# tolerated to match the auditor's permissive parsing.
_TOML_TABLE_HEADER_RE = re.compile(
    r"^\s*\[mcp_servers\.([A-Za-z0-9_-]+)\]\s*(?:#.*)?$",
)
# Detect any other table header so we can stop scanning when we leave
# the ``[mcp_servers.<name>]`` block.
_TOML_ANY_TABLE_RE = re.compile(r"^\s*\[[^\]]+\]\s*(?:#.*)?$")
# A ``command = "..."`` assignment at the top level of the block.
# Indentation is tolerated, escapes inside the string are preserved
# verbatim by capturing the whole RHS.
_TOML_COMMAND_LINE_RE = re.compile(
    r'^(\s*command\s*=\s*)"((?:[^"\\]|\\.)*)"(\s*(?:#.*)?)$',
)


def _rewrite_codex_toml(path: Path, entry_name: str, new_command: str) -> bool:
    """Replace the ``command = "..."`` line inside the
    ``[mcp_servers.<entry_name>]`` table.

    The rest of the file — comments, sibling tables, args lists, blank
    lines, line endings — is preserved byte-for-byte.  Only the bytes
    between the opening ``"`` and the closing ``"`` of the targeted
    ``command`` value change.  Line-level instead of round-tripping
    through a TOML library because every Python-stdlib TOML library
    is read-only (no serializer), and pulling in ``tomli-w`` just to
    swap one string is unnecessary.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    lines = text.splitlines(keepends=True)
    in_target = False
    rewritten = False
    new_lines: list[str] = []
    for line in lines:
        if not rewritten:
            header = _TOML_TABLE_HEADER_RE.match(line)
            if header is not None:
                in_target = header.group(1) == entry_name
                new_lines.append(line)
                continue
            if in_target and _TOML_ANY_TABLE_RE.match(line):
                # Left the target table without finding a ``command =``
                # line.  Don't synthesize one — the entry was malformed
                # and the audit would have flagged it as such; we only
                # repair entries the audit confirms have a command
                # field.  Defensive bail-out preserves the file.
                in_target = False
            if in_target:
                cmd_match = _TOML_COMMAND_LINE_RE.match(line)
                if cmd_match is not None:
                    prefix, current_value, suffix = (
                        cmd_match.group(1),
                        cmd_match.group(2),
                        cmd_match.group(3),
                    )
                    encoded = _toml_escape(new_command)
                    if current_value == encoded:
                        return False
                    new_lines.append(f'{prefix}"{encoded}"{suffix}\n')
                    rewritten = True
                    in_target = False
                    continue
        new_lines.append(line)
    if not rewritten:
        return False
    _atomic_write_text(path, "".join(new_lines))
    return True


def _toml_escape(value: str) -> str:
    """Escape ``value`` for inclusion in a TOML basic string.

    Mirrors the subset of escapes the auditor decodes (only the
    backslash and double-quote characters need encoding for the
    paths we generate — kiln binaries never contain control chars).
    """
    return value.replace("\\", "\\\\").replace('"', '\\"')


# ---------------------------------------------------------------------------
# Atomic write helper
# ---------------------------------------------------------------------------


def _atomic_write_text(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically via a same-directory temp
    file and ``os.replace``.

    ``os.replace`` is atomic on every supported platform and survives
    a crash mid-write — the destination either contains the old
    contents or the new contents, never a truncated mix.  Writing
    into a sibling temp file in the same directory ensures the
    replace is a rename, not a cross-device copy.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path_str = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=str(path.parent),
    )
    tmp_path = Path(tmp_path_str)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp_path, path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


# ---------------------------------------------------------------------------
# Render helper — for ``kiln health`` callers
# ---------------------------------------------------------------------------


def to_json_payload(actions: list[RepairAction]) -> list[dict[str, Any]]:
    """Translate repair actions into JSON-friendly dicts for the
    ``--json`` mode of ``kiln health``.  Stable key names; never
    raises."""
    return [
        {
            "client": a.client,
            "entry": a.entry,
            "path": str(a.path),
            "old": a.old,
            "new": a.new,
        }
        for a in actions
    ]
