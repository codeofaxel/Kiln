"""``kiln install-mcp`` / ``kiln uninstall-mcp`` — connect Kiln to the AI
apps on this computer, and take it back out.

The apps, their settings files and the plan / write / undo engine live in
:mod:`kiln.cli.app_wiring`; this module is the command line on top of it,
plus the launch-command helpers other commands import.

- Detects every app in :data:`kiln.cli.app_wiring.APPS`.  Anything else is
  served by ``--print``, which emits a JSON snippet to paste.
- Merges into existing settings and never writes a file it can't read.
- Backs up each file before its first change and keeps a receipt, so
  ``kiln uninstall-mcp`` takes back exactly what was done.
- ``--dry-run`` shows the plan per app and changes nothing.
- When Kiln is launched as a module (``python -m kiln``), the entry keeps
  that launch shape via ``prefix_args`` (``["-m", "kiln"]``).
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


def _clients_from_registry() -> list[MCPClient]:
    """Every app in the shared registry, as an :class:`MCPClient`."""
    from kiln.cli.app_wiring import APPS

    return [MCPClient(a.id, a.display, a.settings_path(), a.fmt) for a in APPS]


_DEFAULT_CLIENTS: list[MCPClient] = _clients_from_registry()


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
        f"Your AI apps will start Kiln from {command_resolved}. Typing `kiln` "
        f"in a terminal runs a different copy, {path_resolved}, which may be "
        "a different version."
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


def _is_signed_in() -> bool:
    """Whether a usable session token is on disk.

    Kiln runs fully for LOCAL use with NO account, so this predicate never
    blocks the install.  It only decides whether to show a one-line
    invitation to sign in (cross-device sync + Pro).  Signing in matters
    when the user reaches for a hosted/Pro tool, and ``kiln signin`` sets it
    up then — forcing it at install time is friction before any value."""
    home = os.environ.get("KILN_AUTH_HOME") or str(Path.home())
    tokens_path = Path(home) / ".kiln" / "auth_tokens.json"
    if not tokens_path.is_file():
        return False
    try:
        data = json.loads(tokens_path.read_text(encoding="utf-8"))
    except Exception:
        return False
    return bool(str(data.get("access_token") or "").strip())


# ═════════════════════════════════════════════════════════════════════
# Click commands
# ═════════════════════════════════════════════════════════════════════


def _split_ids(values: tuple[str, ...]) -> list[str]:
    """Accept ``--only a --only b`` and ``--only a,b``; dashes or underscores."""
    from kiln.cli.app_wiring import normalize_id

    out: list[str] = []
    for value in values:
        out.extend(normalize_id(v) for v in value.split(",") if v.strip())
    return out


def _select_clients(only: tuple[str, ...], skip: tuple[str, ...]) -> list[MCPClient]:
    """Resolve ``--only`` / ``--skip`` against the discovered client set.

    Unknown ids are a hard error — a typo should say so now, not turn
    into a silent no-op."""
    clients = _discover_clients()
    valid_ids = {c.id for c in clients}
    only_ids, skip_ids = _split_ids(only), _split_ids(skip)
    for chosen in only_ids + skip_ids:
        if chosen not in valid_ids:
            raise click.ClickException(
                f"Unknown app: {chosen!r}.  "
                f"Valid ids: {', '.join(sorted(valid_ids))}"
            )
    if only_ids:
        return [c for c in clients if c.id in only_ids]
    if skip_ids:
        return [c for c in clients if c.id not in skip_ids]
    return clients


def _as_apps(clients: list[MCPClient]) -> list[Any]:
    """Map clients onto registry apps.

    A client that matches a registry app (same id, same settings path)
    keeps that app's detection and restart wording.  Any other client
    (a test harness, a custom path) counts as present when its settings
    folder exists."""
    from kiln.cli.app_wiring import APPS, App

    by_id = {a.id: a for a in APPS}
    apps = []
    for c in clients:
        known = by_id.get(c.id)
        if known is not None and known.settings_path() == c.config_path:
            apps.append(known)
            continue
        apps.append(
            App(
                c.id, c.display, (lambda p=c.config_path: p), c.config_kind,
                markers=(lambda p=c.config_path: (p.parent,)),
                restart=known.restart if known else f"restart {c.display}",
            )
        )
    return apps


def _short(path: Path) -> str:
    try:
        return "~/" + str(path.relative_to(Path.home()))
    except ValueError:
        return str(path)


_MARKS = {
    "add": "+", "update": "~", "leave": "·", "remove duplicate": "-",
    "not found": "·", "can't read": "!",
}


def _echo_plan(plans: list[Any]) -> None:
    from kiln.cli.app_wiring import REMOVE_DUPLICATE

    for plan in plans:
        mark = _MARKS.get(plan.action, "·")
        where = _short(plan.path)
        if plan.action == "not found":
            click.echo(f"  {mark} {'not found':<10} {plan.app.display}")
            continue
        if plan.action == "can't read":
            click.echo(f"  {mark} {'skip':<10} {plan.app.display:<15} {plan.reason}")
            continue
        if plan.action == "leave":
            click.echo(f"  {mark} {'leave':<10} {plan.app.display:<15} {where} (already set up)")
            continue
        kiln_step = next(s for s in plan.steps if s.name == "kiln")
        verb = kiln_step.action if kiln_step.action != "leave" else "tidy"
        note = f" ({kiln_step.note})" if kiln_step.note else ""
        click.echo(f"  {mark} {verb:<10} {plan.app.display:<15} {where}{note}")
        for step in plan.steps:
            if step.action == REMOVE_DUPLICATE:
                click.echo(
                    f"  - {'remove':<10} {'':<15} duplicate {step.name!r} ({step.note})"
                )


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
    help="Only these apps (claude_code, claude_desktop, codex, cursor, gemini, grok, copilot, "
    "devin, amp, opencode, cline, lmstudio, goose, hermes, openclaw).  "
         "Repeat or comma-separate.",
)
@click.option(
    "--skip", multiple=True, metavar="ID",
    help="Skip these apps.  Repeat or comma-separate.",
)
@click.option(
    "--dry-run", is_flag=True,
    help="Show what would change in each app, and change nothing.",
)
@click.option(
    "--print", "print_snippet", is_flag=True,
    help="Print a generic MCP snippet instead of installing.  Use for apps Kiln doesn't detect.",
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
    "--receipt", "receipt_file", default=None, hidden=True,
    type=click.Path(path_type=Path),
    help="Where to keep the receipt (default ~/.kiln/install/receipt.json).",
)
@click.option(
    "--force", is_flag=True, hidden=True,
    help="(deprecated) Install is account-free by default; --force now only "
         "suppresses the one-line sign-in suggestion.  Kept so existing "
         "scripts and docs that pass --force keep working.",
)
def install_mcp(
    only: tuple[str, ...],
    skip: tuple[str, ...],
    dry_run: bool,
    print_snippet: bool,
    command_override: str | None,
    receipt_file: Path | None,
    force: bool,
) -> None:
    """Connect Kiln to every AI app on this computer.

    Finds every AI app Kiln knows (Claude Code, Claude Desktop, Codex,
    Cursor, Gemini CLI, Grok Build, GitHub Copilot, Devin Desktop, Amp,
    opencode, Cline, LM Studio, Goose, Hermes Agent, OpenClaw), shows the
    plan for each, and carries it out.  Web apps get the address to add.
    Merges into existing settings, never rewrites a file it can't read,
    backs up what it edits, and writes a receipt so ``kiln uninstall-mcp``
    takes back exactly what was done.  No account needed for local use.

    For an app Kiln doesn't detect, ``--print`` gives a snippet to paste.
    """
    from kiln.cli import app_wiring

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

    if not dry_run and not force and not _is_signed_in():
        click.echo(
            "  You're set up for local use — no account needed.\n"
            "  Run `kiln signin` anytime to sync across devices and unlock Pro.",
            err=True,
        )

    clients = _select_clients(only, skip)

    # A config that points at a missing binary shows up later as a cryptic
    # spawn error inside the app.  Catch it now.
    command_path = Path(command).expanduser()
    command_is_path = command_path.parent != Path(".")
    if dry_run:
        pass  # a dry run may name where Kiln WILL be installed
    elif command_is_path and not command_path.exists():
        raise click.ClickException(
            f"Couldn't find the Kiln executable at {command}.\n\n"
            "  Pass a working path with:\n"
            "    kiln install-mcp --command /path/to/kiln"
        )
    elif not command_is_path and not shutil.which(command):
        raise click.ClickException(
            f"Couldn't find `{command}` on your PATH.\n\n"
            "  The MCP config must point at a launchable Kiln command.\n\n"
            "  Install with: pip install kiln3d\n"
            "  Then re-run `kiln install-mcp`."
        )

    plans = app_wiring.plan_apps(entry, apps=_as_apps(clients))
    if not plans:
        click.echo("  No apps matched.  Nothing to install.")
        return

    click.echo("")
    click.echo("  Kiln's plan for your AI apps:" if dry_run else "  Connecting Kiln to your AI apps:")
    _echo_plan(plans)

    if any(p.action == app_wiring.CANT_READ for p in plans):
        click.echo(
            "    Kiln never rewrites settings it can't read.  Fix that file and run "
            "this again, or add Kiln by hand: kiln install-mcp --print"
        )

    if not only:
        click.echo("")
        click.echo("  Web apps keep Kiln in your account, so no installer can reach them.")
        click.echo("  Add Kiln in each one's connector settings by its address:")
        for name, url in app_wiring.WEB_APPS:
            click.echo(f"    {name:<31} {url}")

    if dry_run:
        click.echo("")
        click.echo("  Dry run: nothing was changed.")
        return

    from kiln import __version__

    outcomes = app_wiring.apply_plan(
        plans, kiln_version=__version__, receipt_file=receipt_file,
    )
    failed = [o for o in outcomes if o.message]
    changed = [o for o in outcomes if o.done]
    for o in failed:
        click.echo(f"  ! {o.plan.app.display}: {o.message}", err=True)
    if failed:
        click.echo(
            "    Nothing was written to those files.  Fix them, run this again, "
            "or add Kiln by hand with `kiln install-mcp --print`.",
            err=True,
        )

    warning = _path_kiln_warning(command)
    if warning:
        click.echo("")
        click.echo(f"  Note: {warning}")

    if changed:
        click.echo("")
        args_suffix = " ".join(entry.get("args") or [])
        click.echo(f"  MCP command: {entry['command']} {args_suffix}".rstrip())
        click.echo(f"  Receipt: {_short(receipt_file or app_wiring.receipt_path())}")
        click.echo("  To load Kiln:")
        for o in changed:
            click.echo(f"    {o.plan.app.display}: {o.plan.app.restart}")
    elif not failed:
        click.echo("")
        click.echo("  Everything was already set up.  Nothing changed.")


@click.command("uninstall-mcp")
@click.option(
    "--only", multiple=True, metavar="ID",
    help="Only these apps.  Repeat or comma-separate.",
)
@click.option(
    "--skip", multiple=True, metavar="ID",
    help="Skip these apps.",
)
@click.option(
    "--dry-run", is_flag=True,
    help="Show what would be taken back, and change nothing.",
)
@click.option(
    "--receipt-only", is_flag=True,
    help="Take back only what the receipt lists; leave every other file alone.",
)
@click.option(
    "--receipt", "receipt_file", default=None, hidden=True,
    type=click.Path(path_type=Path),
)
def uninstall_mcp(
    only: tuple[str, ...],
    skip: tuple[str, ...],
    dry_run: bool,
    receipt_only: bool,
    receipt_file: Path | None,
) -> None:
    """Take Kiln back out of your AI apps.

    With a receipt from ``kiln install-mcp``, takes back exactly what it
    says: each file goes back to how it was, and anything changed since
    Kiln wrote it is left alone and named.  Apps the receipt doesn't cover
    lose only their ``kiln`` entry (unless ``--receipt-only``); every other
    server is left alone.
    """
    from kiln.cli import app_wiring

    clients = _select_clients(only, skip)
    receipt_at = receipt_file or app_wiring.receipt_path()
    receipt = app_wiring.load_receipt(receipt_at)
    covered = set(receipt["files"])
    if covered:
        ids = [c.id for c in clients]
        undone = app_wiring.undo_receipt(receipt_file=receipt_at, only=ids, dry_run=dry_run)
        click.echo("")
        names = {c.id: c.display for c in clients}
        for r in undone:
            display = names.get(r.app, r.app)
            if r.message:
                click.echo(f"  ! {display:<15} {r.message}", err=True)
                continue
            verb = ("would be " if dry_run else "") + r.how
            click.echo(f"  ✓ {display:<15} {_short(Path(r.path))}: {verb}")
            for name in r.left_alone:
                click.echo(
                    f"    left {name!r} alone: it changed after Kiln wrote it", err=True,
                )
    if receipt_only:
        clients = []
    clients = [c for c in clients if str(c.config_path) not in covered]

    results: list[tuple[str, str, Path]] = []
    for app in _as_apps(clients):
        if app.fmt == "skill":
            continue
        fmt, path = app.format(), app.settings_path()
        try:
            text = app_wiring._read_text(path)
            present = text is not None and fmt.get(path, text, app_wiring.KILN_NAME) is not None
            if present and not dry_run:
                app_wiring._atomic_write(path, fmt.put(path, text, app_wiring.KILN_NAME, None))
        except app_wiring.SettingsUnreadable as exc:
            # Can't read it: leave it alone rather than clobber it.
            click.echo(f"  ! {exc}", err=True)
            continue
        if present:
            results.append((app.display, "would remove" if dry_run else "removed", path))

    if results:
        click.echo("")
    for display, status, path in results:
        click.echo(f"  ✓ {display:<15} {_short(path)}: {status} the kiln entry")
    if dry_run:
        click.echo("\n  Dry run: nothing was changed.")
    else:
        click.echo("\n  Restart your AI apps to stop loading Kiln.")


# ═════════════════════════════════════════════════════════════════════
# Registration
# ═════════════════════════════════════════════════════════════════════


def register_install_mcp_cli(cli_group: click.Group) -> None:
    """Attach ``kiln install-mcp`` and ``kiln uninstall-mcp`` to the
    CLI group."""
    cli_group.add_command(install_mcp)
    cli_group.add_command(uninstall_mcp)
