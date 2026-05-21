"""``kiln install-mcp`` / ``kiln uninstall-mcp`` — auto-wire Kiln into
supported MCP client configs.

Short version:

- Detects Claude Desktop, Claude Code, and Codex by checking for their
  per-client config paths.  Anything else (Cursor, generic MCP clients)
  is served by ``kiln install-mcp --print``, which emits the raw JSON
  snippet to copy into whatever config your tool takes.
- Merges into existing configs — never overwrites unrelated entries.
- Refuses to touch a config file it can't parse, rather than silently
  clobbering hand-edited JSON or TOML.
- Requires a signed-in session (``kiln login`` or ``kiln pair <code>``)
  before writing anything — otherwise the installed config would put
  users on FREE tier and every pro-tool call would fail cold, training
  them to blame Kiln instead of the missing auth step.
- Every mutated file is printed on its own line so the user can audit
  what was touched at a glance.
- When Kiln is launched as a module (``python -m kiln``) instead of
  through the entry-point script, the installed config preserves that
  launch shape via a ``prefix_args`` list (``["-m", "kiln"]``) prepended
  to ``["serve"]``, so the MCP client invokes the same interpreter +
  module the installer is running under.

Companion command ``kiln uninstall-mcp`` removes the Kiln entry only —
never touches sibling MCP servers.
"""
from __future__ import annotations

import json
import os
import platform
import re
import shutil
import sys
from pathlib import Path
from typing import Any, NamedTuple

import click

# ═════════════════════════════════════════════════════════════════════
# Client discovery
# ═════════════════════════════════════════════════════════════════════


class MCPClient(NamedTuple):
    """One MCP client we know how to configure."""

    id: str              # stable machine id (`claude_desktop`, `codex`)
    display: str         # human label (`Claude Desktop`)
    config_path: Path    # path to the client config file
    config_kind: str = "json"
    # How to locate the ``mcpServers`` dict inside JSON config files.
    # Claude Desktop and Claude Code both put it at the root.  Codex
    # uses TOML and is handled by a separate writer below.
    server_key: str = "mcpServers"


def _claude_desktop_config_path() -> Path:
    """Return the per-OS Claude Desktop config path.

    Claude Desktop is shipped by Anthropic for macOS and Windows.  Linux
    isn't officially supported, but users running via wine / a port may
    use an XDG path, so we resolve it best-effort and let the caller
    skip if the parent doesn't exist."""
    system = platform.system()
    if system == "Darwin":
        return Path.home() / "Library/Application Support/Claude/claude_desktop_config.json"
    if system == "Windows":
        appdata = os.environ.get("APPDATA")
        base = Path(appdata) if appdata else Path.home() / "AppData/Roaming"
        return base / "Claude/claude_desktop_config.json"
    # Linux / other POSIX.
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "Claude/claude_desktop_config.json"


def _claude_code_config_path() -> Path:
    """Return the user-level Claude Code config path.

    Claude Code stores user-level MCP servers in ``~/.claude.json``.
    Project-level `.mcp.json` files are ALSO a thing, but those are
    opt-in per repo and usually committed — wiring those up from
    install-mcp would be surprising to users who expect their repo
    to stay untouched.  User-level is the blessed path."""
    return Path.home() / ".claude.json"


def _codex_config_path() -> Path:
    """Return Codex's user-level config path.

    OpenAI's Codex CLI and IDE extension share ``~/.codex/config.toml``.
    A project-scoped ``.codex/config.toml`` can also exist, but pairing
    Kiln is a machine/account concern, not a single-repo concern, so the
    installer writes the user-level config.
    """
    return Path.home() / ".codex/config.toml"


_DEFAULT_CLIENTS: list[MCPClient] = [
    MCPClient("claude_desktop", "Claude Desktop", _claude_desktop_config_path()),
    MCPClient("claude_code",    "Claude Code",    _claude_code_config_path()),
    MCPClient("codex",          "Codex",          _codex_config_path(), "toml"),
]


def _discover_clients() -> list[MCPClient]:
    """Return the canonical client list.  Kept as a function rather than
    a module-level constant so test harnesses can monkey-patch
    ``_DEFAULT_CLIENTS`` without caring about import order."""
    return list(_DEFAULT_CLIENTS)


# ═════════════════════════════════════════════════════════════════════
# JSON config read / merge / write
# ═════════════════════════════════════════════════════════════════════


_KILN_SERVER_KEY = "kiln"
_SERVER_ARGS = ["serve"]


def _resolve_launcher_path(path: Path) -> Path:
    """Return a launcher path that names a file which exists on disk.

    On Windows a pip/pipx console script installs as ``kiln.exe``, but
    the running process sees ``sys.argv[0]`` as the extension-less stem
    (``...\\kiln``).  MCP clients spawn the server with no shell, so a
    ``command`` that omits ``.exe`` fails with a spawn error.  When the
    given path is not itself a file, append the first Windows executable
    extension that resolves to a real file.

    A no-op on POSIX, where executables carry no extension.
    """
    if os.name != "nt" or path.exists():
        return path
    for ext in (".exe", ".cmd", ".bat"):
        candidate = path.with_name(path.name + ext)
        if candidate.is_file():
            return candidate
    return path


def _current_kiln_launch() -> tuple[str, list[str]]:
    """Return ``(command, prefix_args)`` for the kiln launch the MCP
    client should reproduce.

    Three cases the installer must distinguish:

    1. **Installed entry-point binary** — ``sys.argv[0]`` ends in
       ``kiln`` (the pip-shim script).  Return that resolved path with
       no prefix args; the MCP client runs ``<kiln> serve``.
    2. **Module launch** — ``python -m kiln`` lands here with
       ``sys.argv[0]`` pointing at ``.../kiln/__main__.py``.  The MCP
       client can't run a ``__main__.py`` directly; instead it must
       invoke the same Python interpreter with ``-m kiln serve``.
       Return ``(sys.executable, ["-m", "kiln"])``.
    3. **Fallback** — anything else (test harness, an oddly-named
       wrapper) falls back to ``shutil.which("kiln")``, or the literal
       string ``"kiln"`` if PATH has none.

    Writing the literal ``"kiln"`` blindly would make an MCP client
    launch whichever public binary happens to be first on PATH —
    silently swapping the user's tier-aware kiln for an unrelated one.
    """
    executable = Path(sys.argv[0]).expanduser()
    if executable.name == "kiln":
        try:
            resolved = executable.resolve()
        except OSError:
            resolved = executable
        return str(_resolve_launcher_path(resolved)), []
    if executable.name == "__main__.py" and executable.parent.name == "kiln":
        return sys.executable, ["-m", "kiln"]
    if executable.parent != Path("."):
        candidate = executable if executable.is_absolute() else (Path.cwd() / executable)
        try:
            resolved = candidate.resolve()
            if resolved.exists():
                return str(resolved), []
        except OSError:
            pass
    found = shutil.which("kiln")
    if found:
        return str(Path(found).resolve()), []
    return "kiln", []


def _current_kiln_command() -> str:
    """Backwards-compatible alias returning just the command path.

    ``kiln.cli.mcp_config_repair`` imports this name; keep it stable.
    """
    return _current_kiln_launch()[0]


def _server_entry(
    command: str | None = None,
    *,
    prefix_args: list[str] | None = None,
) -> dict[str, Any]:
    """Build the Kiln MCP server entry for client config files.

    If ``command`` is None, derive both ``command`` and ``prefix_args``
    from the running interpreter via ``_current_kiln_launch()``.  If a
    ``command`` is supplied but ``prefix_args`` is not, default the
    prefix to empty — caller is explicitly pointing at a binary that
    runs ``kiln serve`` directly.
    """
    if command is None:
        derived_command, derived_prefix = _current_kiln_launch()
        command = derived_command
        if prefix_args is None:
            prefix_args = derived_prefix
    return {
        "command": command,
        "args": list(prefix_args or []) + list(_SERVER_ARGS),
    }


def _path_kiln_warning(command: str) -> str | None:
    """Return a warning when PATH would resolve a different Kiln binary."""
    path_kiln = shutil.which("kiln")
    if not path_kiln:
        return None
    try:
        path_resolved = str(Path(path_kiln).resolve())
        command_resolved = str(Path(command).expanduser().resolve())
    except OSError:
        return None
    if path_resolved == command_resolved:
        return None
    return (
        f"Using {command_resolved} for MCP because it is the command running "
        f"this installer. Your PATH resolves `kiln` to {path_resolved}; "
        "that may expose a different tool surface."
    )


class _ConfigParseError(click.ClickException):
    """Raised when a client's config file exists but isn't valid JSON.

    We intentionally never overwrite these — the user may have
    hand-edited something subtle.  Report the path + parse error and
    let them fix it."""


def _read_config(path: Path) -> dict[str, Any]:
    """Load a JSON config file, returning ``{}`` if the file doesn't
    exist yet.  Raises ``_ConfigParseError`` if the file exists but
    isn't valid JSON — never silently overwrites."""
    if not path.exists():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise _ConfigParseError(
            f"Couldn't read {path}: {exc}"
        ) from exc
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise _ConfigParseError(
            f"{path} exists but isn't valid JSON ({exc.msg} at line {exc.lineno}).\n"
            f"  Fix or delete the file, then re-run `kiln install-mcp`."
        ) from exc
    if not isinstance(data, dict):
        raise _ConfigParseError(
            f"{path} isn't a JSON object at the root.  "
            f"Fix or delete it, then re-run `kiln install-mcp`."
        )
    return data


def _write_config_atomic(path: Path, data: dict[str, Any]) -> None:
    """Write ``data`` to ``path`` atomically.  Creates the parent
    directory with restrictive permissions if it doesn't exist.  Uses
    a temp file + rename so a crash mid-write can never leave a
    partial file that Claude Desktop would refuse to parse."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".kiln-tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=False), encoding="utf-8")
    tmp.replace(path)


def _validate_toml_if_possible(path: Path, text: str) -> None:
    """Fail early on invalid TOML when a parser is available.

    Python 3.11+ ships ``tomllib``; Python 3.10 environments may not
    have a TOML parser installed.  In that case we still do a narrow
    table replacement rather than taking a dependency just for one
    config writer.
    """
    if not text.strip():
        return
    parser = None
    try:  # Python 3.11+
        import tomllib as parser  # type: ignore[no-redef]
    except ModuleNotFoundError:  # pragma: no cover - depends on runtime
        try:
            import tomli as parser  # type: ignore[no-redef]
        except ModuleNotFoundError:
            parser = None
    if parser is None:
        return
    try:
        parser.loads(text)
    except Exception as exc:
        raise _ConfigParseError(
            f"{path} exists but isn't valid TOML ({exc}).\n"
            f"  Fix or delete the file, then re-run `kiln install-mcp`."
        ) from exc


def _read_text_config(path: Path) -> str:
    """Read a text config file, returning an empty string if absent."""
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise _ConfigParseError(
            f"Couldn't read {path}: {exc}"
        ) from exc


def _write_text_atomic(path: Path, text: str) -> None:
    """Atomically write text config, creating the parent directory."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".kiln-tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


_TOML_TABLE_RE = re.compile(r"^\s*\[([A-Za-z0-9_.-]+)\]\s*(?:#.*)?$")


def _replace_toml_table(text: str, table: str, replacement: str) -> str:
    """Replace one TOML table and its child tables, or append it.

    This keeps the rest of Codex's config byte-for-byte apart from the
    Kiln block.  It deliberately removes ``[mcp_servers.kiln.*]`` child
    tables too so an old env/cwd override cannot survive a reinstall.
    """
    lines = text.splitlines(keepends=True)
    start: int | None = None
    end: int | None = None
    for index, line in enumerate(lines):
        match = _TOML_TABLE_RE.match(line)
        if not match:
            continue
        name = match.group(1)
        if start is None:
            if name == table or name.startswith(f"{table}."):
                start = index
            continue
        if not (name == table or name.startswith(f"{table}.")):
            end = index
            break
    if start is not None:
        if end is None:
            end = len(lines)
        new_lines = lines[:start] + [replacement] + lines[end:]
        return "".join(new_lines)

    if not replacement:
        return text

    prefix = text
    if prefix and not prefix.endswith("\n"):
        prefix += "\n"
    if prefix and not prefix.endswith("\n\n"):
        prefix += "\n"
    return prefix + replacement


def _codex_server_block(entry: dict[str, Any]) -> str:
    """Return the Codex ``config.toml`` block for Kiln."""
    command = json.dumps(str(entry["command"]))
    args = json.dumps(list(entry.get("args") or []))
    return (
        "[mcp_servers.kiln]\n"
        f"command = {command}\n"
        f"args = {args}\n"
    )


def _install_into_toml(client: MCPClient, entry: dict[str, Any]) -> tuple[str, Path]:
    before_exists = client.config_path.exists()
    text = _read_text_config(client.config_path)
    _validate_toml_if_possible(client.config_path, text)
    next_text = _replace_toml_table(
        text,
        "mcp_servers.kiln",
        _codex_server_block(entry),
    )
    if next_text == text:
        return "unchanged", client.config_path
    _write_text_atomic(client.config_path, next_text)
    return ("updated" if before_exists else "installed"), client.config_path


def _install_into(client: MCPClient, entry: dict[str, Any]) -> tuple[str, Path]:
    """Merge the Kiln entry into one client's config.

    Returns ``(status, path)`` where status is one of:
      ``"installed"``: the entry was added.
      ``"updated"``:   the entry existed and was refreshed.
      ``"unchanged"``: the entry matched exactly what we'd write.
    """
    if client.config_kind == "toml":
        return _install_into_toml(client, entry)

    cfg = _read_config(client.config_path)
    servers = cfg.get(client.server_key)
    if not isinstance(servers, dict):
        # Preserve anything else that was in the file — only replace
        # the server-map section if it was missing or the wrong shape.
        servers = {}

    existing = servers.get(_KILN_SERVER_KEY)
    is_new = existing is None
    is_same = existing == entry
    servers[_KILN_SERVER_KEY] = dict(entry)
    cfg[client.server_key] = servers

    if is_same:
        return "unchanged", client.config_path
    _write_config_atomic(client.config_path, cfg)
    return ("installed" if is_new else "updated"), client.config_path


def _uninstall_from(client: MCPClient) -> tuple[str, Path]:
    """Remove the Kiln entry from one client's config.

    Returns the same shape as ``_install_into`` with:
      ``"removed"``:    the entry was present and is now gone.
      ``"absent"``:     the entry wasn't there to begin with."""
    if client.config_kind == "toml":
        if not client.config_path.exists():
            return "absent", client.config_path
        text = _read_text_config(client.config_path)
        _validate_toml_if_possible(client.config_path, text)
        next_text = _replace_toml_table(text, "mcp_servers.kiln", "")
        if next_text == text:
            return "absent", client.config_path
        _write_text_atomic(client.config_path, next_text)
        return "removed", client.config_path

    if not client.config_path.exists():
        return "absent", client.config_path
    cfg = _read_config(client.config_path)
    servers = cfg.get(client.server_key)
    if not isinstance(servers, dict) or _KILN_SERVER_KEY not in servers:
        return "absent", client.config_path
    servers.pop(_KILN_SERVER_KEY, None)
    # If that was the only entry, keep the ``mcpServers`` key as an
    # empty dict rather than deleting it — less churn vs. whatever the
    # user had before, and preserves any JSON formatter's expectations.
    cfg[client.server_key] = servers
    _write_config_atomic(client.config_path, cfg)
    return "removed", client.config_path


# ═════════════════════════════════════════════════════════════════════
# Auth precondition
# ═════════════════════════════════════════════════════════════════════


def _require_signed_in() -> None:
    """Raise a ClickException with actionable guidance if the user
    hasn't signed in via ``kiln login`` or ``kiln pair``.

    Without a session on disk, the installed MCP server would start on
    FREE tier, every pro tool would fail with TIER_REQUIRED, and the
    user would blame Kiln instead of the missing auth step.  Better to
    refuse upfront with a clear next action."""
    home = os.environ.get("KILN_AUTH_HOME") or str(Path.home())
    tokens_path = Path(home) / ".kiln" / "auth_tokens.json"
    if not tokens_path.is_file():
        raise click.ClickException(
            "You're not signed in yet.\n\n"
            "  Run one of:\n"
            "    kiln login              # OAuth from this terminal\n"
            "    kiln pair <code>        # paste code from app.kiln3d.com\n\n"
            "  Then re-run `kiln install-mcp`."
        )
    try:
        data = json.loads(tokens_path.read_text(encoding="utf-8"))
    except Exception as err:
        raise click.ClickException(
            f"{tokens_path} exists but isn't readable JSON.  "
            "Delete the file and run `kiln login` again."
        ) from err
    if not str(data.get("access_token") or "").strip():
        raise click.ClickException(
            "Your session file has no access_token.  "
            "Run `kiln login` or `kiln pair <code>` again."
        )


# ═════════════════════════════════════════════════════════════════════
# Click commands
# ═════════════════════════════════════════════════════════════════════


def _select_clients(only: tuple[str, ...], skip: tuple[str, ...]) -> list[MCPClient]:
    """Resolve ``--only`` / ``--skip`` against the discovered client set.

    Unknown ids are a hard error — if you typed ``--only claude-desktop``
    (dash, not underscore) we want you to know that now, not discover a
    silent no-op at runtime."""
    clients = _discover_clients()
    valid_ids = {c.id for c in clients}
    for chosen in list(only) + list(skip):
        if chosen not in valid_ids:
            raise click.ClickException(
                f"Unknown client id: {chosen!r}.  "
                f"Valid ids: {sorted(valid_ids)}"
            )
    if only:
        return [c for c in clients if c.id in only]
    if skip:
        return [c for c in clients if c.id not in skip]
    return clients


def _generic_snippet(entry: dict[str, Any]) -> str:
    return json.dumps(
        {
            "mcpServers": {
                "kiln": entry,
            },
        },
        indent=2,
    )


@click.command("install-mcp")
@click.option(
    "--only", multiple=True, metavar="ID",
    help="Only install into these clients (claude_desktop, claude_code, codex).  Repeat for multiple.",
)
@click.option(
    "--skip", multiple=True, metavar="ID",
    help="Skip these clients.  Repeat for multiple.",
)
@click.option(
    "--print", "print_snippet", is_flag=True,
    help="Print a generic MCP snippet instead of installing.  Use for editors / agents we don't auto-detect.",
)
@click.option(
    "--command",
    "command_override",
    default=None,
    help=(
        "Kiln executable for the MCP client to launch. Defaults to the "
        "current installer binary, not whichever `kiln` happens to be first on PATH."
    ),
)
@click.option(
    "--force", is_flag=True,
    help="Install even if you aren't signed in.  Rarely wanted — the "
         "resulting config runs on FREE tier.",
)
def install_mcp(
    only: tuple[str, ...],
    skip: tuple[str, ...],
    print_snippet: bool,
    command_override: str | None,
    force: bool,
) -> None:
    """Auto-wire Kiln into supported MCP client configs.

    After ``kiln login`` or ``kiln pair <code>``, run this to make your
    AI agent see Kiln's tools.  Safely merges into existing configs —
    never clobbers sibling MCP servers.

    If your agent isn't Claude Desktop, Claude Code, or Codex, run with
    ``--print`` to get a copy-paste-able snippet for any MCP client
    (Cursor, custom MCP setups, etc.).
    """
    if command_override is not None:
        entry = _server_entry(command_override)
    else:
        entry = _server_entry()
    command = str(entry["command"])

    if print_snippet:
        click.echo(_generic_snippet(entry))
        click.echo("")
        click.echo(
            "Paste the snippet into your MCP client's config.  "
            "It points at the Kiln executable that can serve this tool surface."
        )
        return

    if not force:
        _require_signed_in()

    clients = _select_clients(only, skip)

    # Ensure the command is launchable — otherwise Claude Desktop shows
    # a cryptic spawn error on first launch.  Better to catch it now.
    command_path = Path(command).expanduser()
    command_is_path = command_path.parent != Path(".")
    if command_is_path and not command_path.exists():
        raise click.ClickException(
            f"Couldn't find the Kiln executable at {command}.\n\n"
            "  Pass a working path with:\n"
            "    kiln install-mcp --command /path/to/kiln"
        )
    if not command_is_path and not shutil.which(command):
        raise click.ClickException(
            f"Couldn't find `{command}` on your PATH.\n\n"
            "  The MCP config must point at a launchable Kiln command.\n\n"
            "  Install with: pip install kiln3d\n"
            "  Then re-run `kiln install-mcp`."
        )

    results: list[tuple[MCPClient, str, Path]] = []
    skipped: list[tuple[MCPClient, str]] = []
    for c in clients:
        # Skip clients whose parent dir doesn't exist AND whose app
        # isn't installed — creating the dir wouldn't help them.  The
        # heuristic: if the GRANDPARENT (e.g. ``~/Library/Application
        # Support``) is missing too, this is almost certainly a fresh
        # machine where the agent isn't installed at all.
        # For Claude Code, config lives at ``~/.claude.json`` so the
        # parent is $HOME (always exists) — never skipped.
        if c.id in {"claude_desktop", "codex"} and not c.config_path.parent.exists():
            skipped.append((c, "not installed"))
            continue
        status, path = _install_into(c, entry)
        results.append((c, status, path))

    # Output: one line per client, ✓ or · depending on status.  Then a
    # single restart hint.  Quiet when everything was unchanged.
    click.echo("")
    for c, status, path in results:
        mark = "✓" if status in ("installed", "updated") else "·"
        label = {
            "installed": "installed",
            "updated":   "updated  ",
            "unchanged": "unchanged",
        }[status]
        click.echo(f"  {mark} {label}  {c.display:<16} → {path}")
    for c, reason in skipped:
        click.echo(f"  · skipped    {c.display:<16} ({reason})")

    warning = _path_kiln_warning(command)
    if warning:
        click.echo("")
        click.echo(f"  Note: {warning}")

    did_write = any(s in ("installed", "updated") for _, s, _ in results)
    if did_write:
        click.echo("")
        args_suffix = " ".join(entry.get("args") or [])
        click.echo(f"  MCP command: {entry['command']} {args_suffix}".rstrip())
        click.echo(
            "  Restart Claude Desktop, open a fresh Codex session, or reload "
            "your MCP client to pick up the new server."
        )
    elif not results and not skipped:
        # No clients matched --only/--skip filters.
        click.echo("  No clients matched.  Nothing to install.")


@click.command("uninstall-mcp")
@click.option(
    "--only", multiple=True, metavar="ID",
    help="Only uninstall from these clients.",
)
@click.option(
    "--skip", multiple=True, metavar="ID",
    help="Skip these clients.",
)
def uninstall_mcp(only: tuple[str, ...], skip: tuple[str, ...]) -> None:
    """Remove the Kiln entry from your MCP client configs.

    Only touches the ``kiln`` entry — leaves every other MCP server
    alone.
    """
    clients = _select_clients(only, skip)

    results: list[tuple[MCPClient, str, Path]] = []
    for c in clients:
        try:
            status, path = _uninstall_from(c)
        except _ConfigParseError as exc:
            # Can't parse: treat as absent so we don't clobber.
            click.echo(f"  ! {exc.message}", err=True)
            continue
        results.append((c, status, path))

    click.echo("")
    for c, status, path in results:
        mark = "✓" if status == "removed" else "·"
        label = "removed  " if status == "removed" else "absent   "
        click.echo(f"  {mark} {label}  {c.display:<16} → {path}")
    if any(s == "removed" for _, s, _ in results):
        click.echo("")
        click.echo("  Restart Claude Desktop / Claude Code to stop loading Kiln.")


# ═════════════════════════════════════════════════════════════════════
# Registration
# ═════════════════════════════════════════════════════════════════════


def register_install_mcp_cli(cli_group: click.Group) -> None:
    """Attach ``kiln install-mcp`` and ``kiln uninstall-mcp`` to the
    CLI group."""
    cli_group.add_command(install_mcp)
    cli_group.add_command(uninstall_mcp)
