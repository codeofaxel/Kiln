"""Install Kiln's MCP server entry into supported agent clients."""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any

import click

_SERVER_ARGS = ["serve"]


def _current_kiln_launch() -> tuple[str, list[str]]:
    executable = Path(sys.argv[0]).expanduser()
    if executable.name == "kiln":
        try:
            return str(executable.resolve()), []
        except OSError:
            return str(executable), []
    if executable.name == "__main__.py" and executable.parent.name == "kiln":
        return sys.executable, ["-m", "kiln"]
    return shutil.which("kiln") or "kiln", []


def _current_kiln_command() -> str:
    return _current_kiln_launch()[0]


def _server_entry(
    command: str | None = None,
    *,
    prefix_args: list[str] | None = None,
) -> dict[str, Any]:
    if command is None:
        command, detected_prefix = _current_kiln_launch()
        prefix_args = detected_prefix
    return {
        "command": command,
        "args": list(prefix_args or []) + list(_SERVER_ARGS),
    }


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise click.ClickException(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise click.ClickException(f"{path} must contain a JSON object.")
    return value


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def _claude_desktop_config_path() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/Claude/claude_desktop_config.json"
    if os.name == "nt":
        appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData/Roaming")
        return Path(appdata) / "Claude/claude_desktop_config.json"
    return Path.home() / ".config/Claude/claude_desktop_config.json"


def _claude_code_config_path() -> Path:
    return Path.home() / ".claude.json"


def _codex_config_path() -> Path:
    return Path.home() / ".codex/config.toml"


def _read_text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _validate_toml_if_possible(path: Path, text: str) -> None:
    if not text.strip():
        return
    parser = None
    try:
        import tomllib as parser  # type: ignore[no-redef]
    except ModuleNotFoundError:  # pragma: no cover - Python 3.10
        try:
            import tomli as parser  # type: ignore[no-redef]
        except ModuleNotFoundError:
            parser = None
    if parser is None:
        return
    try:
        parser.loads(text)
    except Exception as exc:
        raise click.ClickException(f"{path} is not valid TOML: {exc}") from exc


_TOML_TABLE_RE = re.compile(r"^\s*\[([A-Za-z0-9_.-]+)\]\s*(?:#.*)?$")


def _replace_toml_table(text: str, table: str, replacement: str) -> str:
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
        return "".join(lines[:start] + [replacement] + lines[end:])
    if not replacement:
        return text
    prefix = text
    if prefix and not prefix.endswith("\n"):
        prefix += "\n"
    if prefix and not prefix.endswith("\n\n"):
        prefix += "\n"
    return prefix + replacement


def _codex_server_block(
    command: str | None = None,
    *,
    prefix_args: list[str] | None = None,
) -> str:
    entry = _server_entry(command, prefix_args=prefix_args)
    return (
        "[mcp_servers.kiln]\n"
        f"command = {json.dumps(str(entry['command']))}\n"
        f"args = {json.dumps(list(entry.get('args') or []))}\n"
    )


def _install_into(client: str, path: Path, entry: dict[str, Any]) -> str:
    before_exists = path.exists()
    if client == "Codex":
        text = _read_text(path)
        _validate_toml_if_possible(path, text)
        next_text = _replace_toml_table(
            text,
            "mcp_servers.kiln",
            _codex_server_block(
                str(entry["command"]),
                prefix_args=list(entry.get("args") or [])[: -len(_SERVER_ARGS)],
            ),
        )
        _write_text(path, next_text)
        return "updated" if before_exists else "created"

    data = _read_json(path)
    if client == "Claude Desktop" or client == "Claude Code":
        servers = data.setdefault("mcpServers", {})
        if not isinstance(servers, dict):
            raise click.ClickException(f"{path}: mcpServers must be a JSON object.")
        servers["kiln"] = entry
    else:  # pragma: no cover
        raise click.ClickException(f"Unsupported MCP client: {client}")

    _write_json(path, data)
    return "updated" if before_exists else "created"


def _path_kiln_warning(command: str) -> str | None:
    path_kiln = shutil.which("kiln")
    if not path_kiln:
        return None
    try:
        selected = str(Path(command).expanduser().resolve())
        path_selected = str(Path(path_kiln).expanduser().resolve())
    except OSError:
        return None
    if selected != path_selected:
        return (
            "Your shell resolves `kiln` to a different executable. "
            f"MCP will use {selected}; PATH currently finds {path_selected}."
        )
    return None


def _generic_snippet(command: str | None = None) -> str:
    return json.dumps({"mcpServers": {"kiln": _server_entry(command)}}, indent=2)


@click.command("install-mcp")
@click.option(
    "--client",
    type=click.Choice(["claude-desktop", "claude-code", "codex", "all"], case_sensitive=False),
    default="all",
    show_default=True,
    help="Which client config to update.",
)
@click.option(
    "--command",
    "command_override",
    help="Kiln executable path to write into MCP configs.",
)
@click.option(
    "--print",
    "print_snippet",
    is_flag=True,
    help="Print a generic MCP JSON snippet instead of editing files.",
)
def install_mcp(
    client: str,
    command_override: str | None,
    print_snippet: bool,
) -> None:
    """Install the local Kiln MCP server into supported agent clients."""
    entry = _server_entry(command_override)
    command = str(entry["command"])
    selected = client.lower()
    if Path(command).is_absolute() and not Path(command).exists():
        raise click.ClickException(
            f"Kiln executable not found: {command}. "
            "Pass the executable that runs `kiln serve`, for example "
            "`kiln install-mcp --command /path/to/kiln`."
        )

    if print_snippet:
        if selected == "codex":
            click.echo(_codex_server_block(command_override))
        else:
            click.echo(json.dumps({"mcpServers": {"kiln": entry}}, indent=2))
        click.echo("")
        click.echo("Add this to your MCP client config. It points at the Kiln executable that serves the tool surface.")
        return

    targets: list[tuple[str, Path]] = []
    if selected in {"claude-desktop", "all"}:
        targets.append(("Claude Desktop", _claude_desktop_config_path()))
    if selected in {"claude-code", "all"}:
        targets.append(("Claude Code", _claude_code_config_path()))
    if selected in {"codex", "all"}:
        targets.append(("Codex", _codex_config_path()))

    for label, path in targets:
        action = _install_into(label, path, entry)
        click.echo(f"{action.title()} {label}: {path}")

    warning = _path_kiln_warning(command)
    if warning:
        click.echo("")
        click.echo(f"Note: {warning}")
    click.echo("")
    click.echo(f"MCP command: {command} {' '.join(entry.get('args') or [])}")
    prefix_args = list(entry.get("args") or [])[: -len(_SERVER_ARGS)]
    whoami_command = " ".join([command] + prefix_args + ["whoami"])
    click.echo(
        "Restart clients that do not hot-load MCP config changes, open a fresh "
        f"Codex session if needed, then run `{whoami_command}` to verify your tier."
    )


def register_install_mcp_cli(cli_group: click.Group) -> None:
    """Attach ``kiln install-mcp``."""
    cli_group.add_command(install_mcp)
