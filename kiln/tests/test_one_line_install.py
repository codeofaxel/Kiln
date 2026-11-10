"""The one-line installer: connect Kiln to every AI app, take it back exactly.

Two halves are covered here.  The Python half (:mod:`kiln.cli.app_wiring`
behind ``kiln install-mcp`` / ``kiln uninstall-mcp``) is driven through the
real commands against a scratch home seeded with each app's settings.  The
shell half (``install.sh`` at the repository root) is run for real with
stand-in ``uv`` and ``kiln`` programs, so the tests see exactly which
commands it runs, with which arguments, and where their input comes from.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import tomllib
from click.testing import CliRunner

REPO = Path(__file__).resolve().parents[2]
INSTALL_SH = REPO / "install.sh"
INSTALL_PS1 = REPO / "install.ps1"
SRC = REPO / "kiln" / "src"

# A child process gets this interpreter's whole import path: a scratch HOME
# would otherwise hide a per-user site-packages.
CHILD_PYTHONPATH = os.pathsep.join([str(SRC), *(p for p in sys.path if p)])

posix_only = pytest.mark.skipif(os.name == "nt", reason="install.sh is the macOS/Linux installer")


# ---------------------------------------------------------------------------
# A scratch computer
# ---------------------------------------------------------------------------


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A scratch home where no AI app is on PATH: only folders count."""
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("USERPROFILE", str(h))
    monkeypatch.setenv("APPDATA", str(h / "AppData" / "Roaming"))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    monkeypatch.setenv("PATH", str(empty_bin))
    monkeypatch.setenv("KILN_AUTH_HOME", str(h))

    from kiln.cli import install_mcp

    monkeypatch.setattr(install_mcp, "_DEFAULT_CLIENTS", install_mcp._clients_from_registry())
    return h


@pytest.fixture()
def kiln_bin(tmp_path: Path) -> Path:
    b = tmp_path / "tools" / "bin" / "kiln"
    b.parent.mkdir(parents=True)
    b.write_text("#!/bin/sh\n", encoding="utf-8")
    b.chmod(0o755)
    return b


def _desktop_settings(h: Path) -> Path:
    from kiln.cli.app_wiring import APPS

    return next(a for a in APPS if a.id == "claude_desktop").settings_path()


def _seed(h: Path) -> dict[str, Path]:
    """Seed a computer with every situation the plan has a word for."""
    desktop = _desktop_settings(h)
    desktop.parent.mkdir(parents=True)  # app present, no settings yet -> add
    files = {
        # A sibling server and a second copy of Kiln under another name.
        "claude_code": h / ".claude.json",
        # Kiln's hosted connector under the name kiln -> update.
        "cursor": h / ".cursor" / "mcp.json",
        # A sibling table -> add, keeping the sibling byte for byte.
        "codex": h / ".codex" / "config.toml",
        # Comments: not JSON this reader accepts -> can't read, never written.
        "gemini": h / ".gemini" / "settings.json",
    }
    files["claude_code"].write_text(
        json.dumps(
            {
                "numStartups": 7,
                "mcpServers": {
                    "filesystem": {"command": "/usr/bin/fs-mcp", "args": []},
                    "kiln-old": {"command": "/old/venv/bin/kiln", "args": ["serve"]},
                },
            }
        ),
        encoding="utf-8",
    )
    for key in ("cursor", "codex", "gemini"):
        files[key].parent.mkdir(parents=True)
    files["cursor"].write_text(
        '{"mcpServers": {"kiln": {"url": "https://api.kiln3d.com/mcp"}}}',
        encoding="utf-8",
    )
    files["codex"].write_text(
        'model = "gpt-5.5"\n\n[mcp_servers.docs]\nurl = "https://developers.openai.com/mcp"\n',
        encoding="utf-8",
    )
    files["gemini"].write_text('{\n  // mine\n  "theme": "dark"\n}\n', encoding="utf-8")
    (h / ".grok").mkdir()  # app present, nothing yet -> add
    files["claude_desktop"] = desktop
    return files


def _snapshot(root: Path) -> dict[str, str]:
    """Every file and folder under ``root``, with file contents hashed."""
    out: dict[str, str] = {}
    for p in sorted(root.rglob("*")):
        rel = str(p.relative_to(root))
        out[rel] = "dir" if p.is_dir() else hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def _install(*args: str):
    from kiln.cli.install_mcp import install_mcp

    return CliRunner().invoke(install_mcp, list(args))


def _uninstall(*args: str):
    from kiln.cli.install_mcp import uninstall_mcp

    return CliRunner().invoke(uninstall_mcp, list(args))


# ---------------------------------------------------------------------------
# The plan, per app
# ---------------------------------------------------------------------------


def test_plan_names_each_app_and_what_will_happen(home: Path, kiln_bin: Path) -> None:
    """Every app gets one line saying add / update / leave / not found /
    skip, a second copy of Kiln is named as a duplicate to remove, and a
    hosted connector being replaced is said out loud."""
    _seed(home)
    result = _install("--dry-run", "--command", str(kiln_bin))
    assert result.exit_code == 0, result.output
    lines = {re.sub(r"\s+", " ", ln.strip()) for ln in result.output.splitlines() if ln.strip()}

    def line_for(display: str) -> str:
        plan_lines = [ln for ln in lines if ln[:1] in "+~·!-"]
        return next(ln for ln in plan_lines if f" {display} " in f" {ln} ")

    assert line_for("Claude Code").startswith("+ add")
    assert "- remove duplicate 'kiln-old' (another copy of Kiln)" in lines
    assert line_for("Claude Desktop").startswith("+ add")
    assert line_for("Codex").startswith("+ add")
    assert line_for("Cursor").startswith("· online")
    assert "uses Kiln online, so it stays that way" in line_for("Cursor")
    assert line_for("Gemini CLI").startswith("! skip")
    assert line_for("Grok Build").startswith("+ add")
    assert line_for("OpenClaw").startswith("· not found")
    assert "Dry run: nothing was changed." in result.output


def test_running_again_leaves_everything(home: Path, kiln_bin: Path) -> None:
    """The second run with nothing new to do writes nothing at all."""
    _seed(home)
    assert _install("--command", str(kiln_bin)).exit_code == 0
    snap = _snapshot(home)

    result = _install("--command", str(kiln_bin))

    assert result.exit_code == 0, result.output
    assert "Everything was already set up.  Nothing changed." in result.output
    assert _snapshot(home) == snap


def test_an_update_keeps_the_way_back_to_before_the_first_install(
    home: Path,
    kiln_bin: Path,
    tmp_path: Path,
) -> None:
    """An update rewrites Kiln's entry, but the receipt keeps the state from
    before the FIRST install, so uninstall still returns there."""
    files = _seed(home)
    old = {"mcpServers": {"kiln": {"command": "/old/venv/bin/kiln", "args": ["serve"]}}}
    files["cursor"].write_text(json.dumps(old), encoding="utf-8")
    assert _install("--command", str(kiln_bin), "--only", "cursor").exit_code == 0
    newer = tmp_path / "newer" / "kiln"
    newer.parent.mkdir()
    newer.write_text("#!/bin/sh\n", encoding="utf-8")

    result = _install("--command", str(newer), "--only", "cursor")
    assert result.exit_code == 0, result.output
    assert "~ update" in result.output
    assert _uninstall("--receipt-only").exit_code == 0

    assert json.loads(files["cursor"].read_text(encoding="utf-8")) == old


def test_every_registry_app_is_reachable_by_name(home: Path, kiln_bin: Path) -> None:
    """``--only`` takes every app in the registry, dashes or underscores,
    and the health audit reads every MCP-settings app the installer writes."""
    from kiln.cli.app_wiring import APPS
    from kiln.cli.mcp_config_audit import audit_all_mcp_clients

    for app in APPS:
        result = _install("--dry-run", "--command", str(kiln_bin), "--only", app.id.replace("_", "-"))
        assert result.exit_code == 0, result.output
        assert app.display in result.output
    for app in APPS:
        if app.fmt in ("json", "toml"):
            app.settings_path().parent.mkdir(parents=True, exist_ok=True)
            app.settings_path().write_text("", encoding="utf-8")
    audited = {r.client for r in audit_all_mcp_clients()}
    assert {a.display for a in APPS if a.fmt in ("json", "toml")} <= audited


# ---------------------------------------------------------------------------
# Merge, never clobber
# ---------------------------------------------------------------------------


def test_install_merges_and_never_writes_what_it_cannot_read(home: Path, kiln_bin: Path) -> None:
    files = _seed(home)
    gemini_before = files["gemini"].read_bytes()

    result = _install("--command", str(kiln_bin))

    assert result.exit_code == 0, result.output
    entry = {"command": str(kiln_bin), "args": ["serve"]}
    code = json.loads(files["claude_code"].read_text(encoding="utf-8"))
    assert code["numStartups"] == 7
    assert code["mcpServers"] == {
        "filesystem": {"command": "/usr/bin/fs-mcp", "args": []},
        "kiln": entry,
    }
    assert json.loads(files["cursor"].read_text(encoding="utf-8"))["mcpServers"] == {
        "kiln": {"url": "https://api.kiln3d.com/mcp"},
    }  # Kiln online stays; no second Kiln beside it
    codex = files["codex"].read_text(encoding="utf-8")
    assert codex.startswith('model = "gpt-5.5"\n\n[mcp_servers.docs]\nurl = "https://developers.openai.com/mcp"\n')
    assert tomllib.loads(codex)["mcp_servers"]["kiln"] == entry
    assert tomllib.loads((home / ".grok/config.toml").read_text(encoding="utf-8")) == {
        "mcp_servers": {"kiln": entry},
    }
    assert files["gemini"].read_bytes() == gemini_before
    # Each edited file was backed up before it changed.
    backups = {p.name.split("-")[0] for p in (home / ".kiln/install/backups").iterdir()}
    assert backups == {"claude_code", "codex"}


def test_a_toml_file_kiln_cannot_edit_safely_is_left_untouched(home: Path, kiln_bin: Path) -> None:
    """Codex/Grok also accept servers written as one inline table.  A table
    header added beside it would make the file unreadable, so the change
    must be refused and the file left exactly as it was."""
    settings = home / ".codex" / "config.toml"
    settings.parent.mkdir()
    original = 'mcp_servers = { docs = { url = "https://example.com/mcp" } }\n'
    settings.write_text(original, encoding="utf-8")

    result = _install("--command", str(kiln_bin), "--only", "codex")

    assert settings.read_text(encoding="utf-8") == original
    assert "nothing was written" in result.output.lower()
    assert not (home / ".kiln/install/receipt.json").exists()


# ---------------------------------------------------------------------------
# Uninstall takes back exactly what the receipt says
# ---------------------------------------------------------------------------


def test_uninstall_restores_every_file(home: Path, kiln_bin: Path) -> None:
    """Untouched since install: each file comes back byte for byte, and
    files and folders Kiln created are gone."""
    _seed(home)
    (home / ".openclaw").mkdir()  # present, so Kiln creates its skill folder
    before = _snapshot(home)

    assert _install("--command", str(kiln_bin)).exit_code == 0
    assert (home / ".openclaw/skills/kiln/SKILL.md").exists()
    result = _uninstall("--receipt-only")

    assert result.exit_code == 0, result.output
    after = {k: v for k, v in _snapshot(home).items() if not k.startswith(".kiln")}
    assert after == before


def test_uninstall_keeps_what_the_app_changed_since(home: Path, kiln_bin: Path) -> None:
    """An app rewrites its own settings all the time.  Uninstall reverses
    Kiln's entries one by one: Kiln's entry goes, the duplicate it removed
    comes back, and the app's own later change stays."""
    files = _seed(home)
    assert _install("--command", str(kiln_bin)).exit_code == 0
    data = json.loads(files["claude_code"].read_text(encoding="utf-8"))
    data["numStartups"] = 8
    files["claude_code"].write_text(json.dumps(data), encoding="utf-8")

    result = _uninstall("--receipt-only")

    assert result.exit_code == 0, result.output
    after = json.loads(files["claude_code"].read_text(encoding="utf-8"))
    assert after == {
        "numStartups": 8,
        "mcpServers": {
            "filesystem": {"command": "/usr/bin/fs-mcp", "args": []},
            "kiln-old": {"command": "/old/venv/bin/kiln", "args": ["serve"]},
        },
    }


def test_uninstall_leaves_an_entry_someone_changed_after_kiln(home: Path, kiln_bin: Path) -> None:
    files = _seed(home)
    files["cursor"].write_text("{}", encoding="utf-8")
    assert _install("--command", str(kiln_bin), "--only", "cursor").exit_code == 0
    mine = {"mcpServers": {"kiln": {"command": "/my/own/kiln", "args": ["serve", "--x"]}}}
    files["cursor"].write_text(json.dumps(mine), encoding="utf-8")

    result = _uninstall("--receipt-only")

    assert json.loads(files["cursor"].read_text(encoding="utf-8")) == mine
    assert "left 'kiln' alone" in result.output


# ---------------------------------------------------------------------------
# A dry run changes nothing
# ---------------------------------------------------------------------------


def test_dry_run_changes_nothing(home: Path, kiln_bin: Path) -> None:
    _seed(home)
    before = _snapshot(home)
    assert _install("--dry-run", "--command", str(kiln_bin)).exit_code == 0
    assert _snapshot(home) == before
    assert _install("--command", str(kiln_bin)).exit_code == 0
    installed = _snapshot(home)
    assert _uninstall("--dry-run").exit_code == 0
    assert _snapshot(home) == installed
    assert before != installed


@posix_only
def test_the_whole_cli_dry_run_writes_nothing_into_kiln_home(tmp_path: Path) -> None:
    """Run as a real process, ``kiln`` counts each invocation in ~/.kiln.
    A dry run promises to change nothing, so it must not."""
    h = tmp_path / "home"
    (h / ".cursor").mkdir(parents=True)
    # Telemetry is left on (turning it off would hide the very write this
    # pins); a dead proxy keeps anything from leaving the machine.
    env = {
        "HOME": str(h),
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": CHILD_PYTHONPATH,
        "KILN_AUTH_HOME": str(h),
        **_NO_NETWORK,
    }
    before = _snapshot(h)
    proc = subprocess.run(
        [sys.executable, "-m", "kiln", "install-mcp", "--dry-run", "--command", "/opt/kiln/bin/kiln"],
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "Cursor" in proc.stdout
    assert _snapshot(h) == before


# ---------------------------------------------------------------------------
# install.sh, run for real with stand-in uv and kiln
# ---------------------------------------------------------------------------

_STUB_UV = r"""#!/bin/sh
echo "uv $*" >> "$STUB_LOG"
case "$1 $2" in
  "tool dir") echo "$STUB_BIN" ;;
  "tool install") mkdir -p "$STUB_BIN"; cp "$STUB_KILN" "$STUB_BIN/kiln"; chmod +x "$STUB_BIN/kiln" ;;
  "tool list") [ -x "$STUB_BIN/kiln" ] && echo "kiln3d v0" ;;
  "tool uninstall") rm -f "$STUB_BIN/kiln" ;;
  "tool run") shift 2; while [ "$1" != kiln ]; do shift; done; shift; exec "$STUB_KILN" "$@" ;;
esac
"""

# Records each call and whether its input is a terminal.  ``install-mcp``
# and ``uninstall-mcp`` run the real commands from this checkout.
_STUB_KILN = r"""#!/bin/sh
if [ -t 0 ]; then tty=tty; else tty=notty; fi
echo "kiln $* [$tty]" >> "$STUB_LOG"
case "$1" in
  install-mcp|uninstall-mcp) exec "$STUB_PYTHON" -m kiln "$@" ;;
  install-openscad) exit 0 ;;
esac
exit 0
"""


_NO_NETWORK = {
    "HTTPS_PROXY": "http://127.0.0.1:9",
    "HTTP_PROXY": "http://127.0.0.1:9",
    "https_proxy": "http://127.0.0.1:9",
    "http_proxy": "http://127.0.0.1:9",
}


def _stubs(tmp_path: Path) -> dict[str, str]:
    h = tmp_path / "home"
    h.mkdir(exist_ok=True)
    stub_dir = tmp_path / "stub-bin"
    stub_dir.mkdir()
    (stub_dir / "uv").write_text(_STUB_UV, encoding="utf-8")
    (stub_dir / "uv").chmod(0o755)
    kiln = tmp_path / "stub-kiln"
    kiln.write_text(_STUB_KILN, encoding="utf-8")
    kiln.chmod(0o755)
    return {
        "HOME": str(h),
        "PATH": f"{stub_dir}:/usr/bin:/bin",
        "PYTHONPATH": CHILD_PYTHONPATH,
        "KILN_AUTH_HOME": str(h),
        "STUB_LOG": str(tmp_path / "calls.log"),
        "STUB_BIN": str(h / ".local" / "bin"),
        "STUB_KILN": str(kiln),
        "STUB_PYTHON": sys.executable,
        "KILN_TELEMETRY": "false",
        **_NO_NETWORK,
    }


def _run_piped(env: dict[str, str], *args: str) -> subprocess.CompletedProcess:
    """``cat install.sh | sh -s -- args``, with no terminal attached."""
    script = INSTALL_SH.read_text(encoding="utf-8")
    return subprocess.run(
        ["sh", "-s", "--", *args],
        input=script,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        start_new_session=True,
    )


def _calls(env: dict[str, str]) -> list[str]:
    log = Path(env["STUB_LOG"])
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


@posix_only
def test_script_dry_run_changes_nothing(tmp_path: Path) -> None:
    env = _stubs(tmp_path)
    h = Path(env["HOME"])
    (h / ".cursor").mkdir()
    (h / ".cursor" / "mcp.json").write_text('{"mcpServers": {}}', encoding="utf-8")
    before = _snapshot(h)

    proc = _run_piped(env, "--dry-run")

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "+ add" in proc.stdout and "Cursor" in proc.stdout
    assert "Dry run finished. Nothing was changed." in proc.stdout
    assert _snapshot(h) == before
    calls = _calls(env)
    assert not any(c.startswith("uv tool install") for c in calls), calls
    assert not any("accept-terms" in c or c.startswith("kiln install-openscad ") for c in calls)


@posix_only
def test_script_installs_pinned_release_and_uninstall_restores(tmp_path: Path) -> None:
    env = _stubs(tmp_path)
    h = Path(env["HOME"])
    (h / ".cursor").mkdir()
    (h / ".cursor" / "mcp.json").write_text(
        '{"mcpServers": {"docs": {"url": "https://example.com/mcp"}}}',
        encoding="utf-8",
    )
    before = {k: v for k, v in _snapshot(h).items()}

    proc = _run_piped(env, "--version", "9.8.7")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "uv tool install --force --python 3.12 kiln3d==9.8.7" in _calls(env)
    cursor = json.loads((h / ".cursor/mcp.json").read_text(encoding="utf-8"))
    assert cursor["mcpServers"]["kiln"] == {"command": env["STUB_BIN"] + "/kiln", "args": ["serve"]}

    proc = _run_piped(env, "--uninstall")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "uv tool uninstall kiln3d" in _calls(env)
    after = {k: v for k, v in _snapshot(h).items() if not k.startswith((".kiln", ".local"))}
    assert after == before


@posix_only
def test_script_never_answers_the_terms_for_the_person(tmp_path: Path) -> None:
    """With no terminal (a pipe, CI) the terms are not asked and not
    accepted: the script says how to accept them, and that's all."""
    env = _stubs(tmp_path)
    proc = _run_piped(env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert not any("accept-terms" in c for c in _calls(env)), _calls(env)
    assert "kiln accept-terms" in proc.stdout


@posix_only
def test_script_reads_the_terms_answer_from_the_terminal(tmp_path: Path) -> None:
    """Under ``curl | sh`` stdin is the download.  The terms question must
    read the person's own terminal, and never pass ``--yes``."""
    import pty

    env = _stubs(tmp_path)
    script = tmp_path / "install.sh"
    shutil.copy(INSTALL_SH, script)
    # The child gets a real terminal of its own; the script still arrives on a pipe.
    pid, fd = pty.fork()
    if pid == 0:  # pragma: no cover - runs in the child
        os.execve("/bin/sh", ["sh", "-c", f'cat "{script}" | sh -s'], env)
    output = b""
    while True:
        try:
            chunk = os.read(fd, 4096)
        except OSError:
            break
        if not chunk:
            break
        output += chunk
        if b"[Y/n]" in chunk:
            os.write(fd, b"n\n")
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 0, output.decode(errors="replace")

    terms = [c for c in _calls(env) if c.startswith("kiln accept-terms")]
    assert terms == ["kiln accept-terms --if-needed [tty]"], _calls(env)


def test_accept_terms_if_needed_never_accepts_without_a_person(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kiln.cli import main as cli_main

    recorded: list[str] = []
    monkeypatch.setattr("kiln.terms.is_current", lambda **_: False)
    monkeypatch.setattr("kiln.terms.record_acceptance", lambda **kw: recorded.append(kw.get("method")))
    monkeypatch.setattr(cli_main, "_terms_gate_interactive", lambda: False)

    result = CliRunner().invoke(cli_main.cli, ["accept-terms", "--if-needed"])

    assert result.exit_code == 2, result.output
    assert recorded == []
    assert "accept them yourself" in result.output


# ---------------------------------------------------------------------------
# The pinned release
# ---------------------------------------------------------------------------


def test_installers_pin_the_current_release() -> None:
    version = tomllib.loads((REPO / "kiln/pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    sh = re.search(r'^KILN_DEFAULT_VERSION="([^"]+)"', INSTALL_SH.read_text(encoding="utf-8"), re.M)
    ps1 = re.search(r'\$KilnDefaultVersion = "([^"]+)"', INSTALL_PS1.read_text(encoding="utf-8"))
    assert sh and ps1
    assert sh.group(1) == ps1.group(1) == version


@pytest.mark.parametrize(
    ("entry", "kind"),
    [
        ({"command": "/old/venv/bin/kiln", "args": ["serve"]}, "local"),
        ({"command": "C:\\Tools\\kiln.exe", "args": ["serve"]}, "local"),
        ({"command": "python3", "args": ["-m", "kiln", "serve"]}, "local"),
        ({"command": "uvx", "args": ["kiln3d", "serve"]}, "local"),
        ({"command": "npx", "args": ["kiln3d"]}, "local"),
        ({"url": "https://api.kiln3d.com/mcp"}, "hosted"),
        ({"command": "/usr/bin/fs-mcp", "args": []}, None),
        ({"command": "kiln", "args": ["status"]}, None),
        ({"url": "https://evil-kiln3d.com/mcp"}, None),
        ({"type": "local", "command": ["uvx", "kiln3d", "serve"]}, "local"),
        ({"type": "stdio", "cmd": "/opt/bin/kiln", "args": ["serve"]}, "local"),
    ],
)
def test_recognizes_kiln_under_any_name(entry: dict, kind: str | None) -> None:
    """Every documented way of adding Kiln counts as Kiln, and nothing else does."""
    from kiln.cli.app_wiring import kiln_kind

    assert kiln_kind(entry) == kind


def test_health_repairs_kiln_in_every_app_it_installs_into(
    home: Path,
    kiln_bin: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``kiln health`` repairs a ``kiln`` entry whose program has gone
    missing.  It must do that in every app the installer writes, not only
    the first three."""
    from kiln.cli import mcp_config_audit, mcp_config_repair
    from kiln.cli.app_wiring import APPS

    for app in APPS:
        if app.id in ("cursor", "grok"):
            (home / f".{app.id}").mkdir()
    assert _install("--command", str(kiln_bin), "--only", "cursor,grok").exit_code == 0
    kiln_bin.unlink()  # the program the apps point at is gone
    working = kiln_bin.parent.parent / "new" / "kiln"
    working.parent.mkdir()
    working.write_text("#!/bin/sh\n", encoding="utf-8")
    working.chmod(0o755)
    monkeypatch.setattr(mcp_config_repair, "_resolve_working_kiln_command", lambda: str(working))

    actions = mcp_config_repair.repair_drifted_kiln_entries(mcp_config_audit.audit_all_mcp_clients())

    assert {a.client for a in actions} == {"Cursor", "Grok Build"}
    cursor = json.loads((home / ".cursor/mcp.json").read_text(encoding="utf-8"))
    grok = tomllib.loads((home / ".grok/config.toml").read_text(encoding="utf-8"))
    assert cursor["mcpServers"]["kiln"]["command"] == str(working)
    assert grok["mcp_servers"]["kiln"]["command"] == str(working)


@posix_only
def test_settings_keep_their_permissions_and_the_receipt_is_private(
    home: Path,
    kiln_bin: Path,
) -> None:
    """App settings can hold tokens.  An edited file keeps its own mode,
    a restored one gets its mode back, and the receipt and backups (which
    hold copies of those settings) are readable by this user only."""
    files = _seed(home)
    files["claude_code"].chmod(0o600)
    files["cursor"].chmod(0o644)

    assert _install("--command", str(kiln_bin)).exit_code == 0

    def mode(p: Path) -> int:
        return p.stat().st_mode & 0o777

    assert mode(files["claude_code"]) == 0o600
    assert mode(files["cursor"]) == 0o644
    assert mode(files["claude_desktop"]) == 0o600  # created by Kiln
    install = home / ".kiln" / "install"
    assert mode(install) == 0o700
    assert mode(install / "receipt.json") == 0o600
    assert mode(install / "backups") == 0o700
    assert {mode(b) for b in (install / "backups").iterdir()} == {0o600}

    assert _uninstall("--receipt-only").exit_code == 0
    assert mode(files["claude_code"]) == 0o600
    assert mode(files["cursor"]) == 0o644


# ---------------------------------------------------------------------------
# Every app, in its own spelling
# ---------------------------------------------------------------------------

_HERMES_BEFORE = """# Hermes settings
model: gpt-6.1
mcp_servers:
  # my files
  filesystem:
    command: "npx"
    args: ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
  kiln-old:
    command: /old/venv/bin/kiln
    args: [serve]

display:
  theme: dark   # keep me
"""

_GOOSE_BEFORE = """extensions:
  developer:
    enabled: true
    type: builtin
    name: developer
GOOSE_MODEL: gpt-6
"""


def _seed_more(h: Path) -> dict[str, Path]:
    from kiln.cli.app_wiring import APPS

    paths = {a.id: a.settings_path() for a in APPS}
    for app_id in ("copilot", "devin", "amp", "opencode", "cline", "lmstudio", "goose", "hermes"):
        paths[app_id].parent.mkdir(parents=True, exist_ok=True)
    sibling = {"command": "/usr/bin/fs-mcp", "args": []}
    paths["copilot"].write_text(json.dumps({"mcpServers": {"fs": sibling}}), encoding="utf-8")
    paths["amp"].write_text(
        json.dumps({"amp.theme": "dark", "amp.mcpServers": {"fs": sibling}}),
        encoding="utf-8",
    )
    paths["opencode"].write_text(
        json.dumps(
            {
                "model": "x",
                "mcp": {
                    "servers": {
                        "fs": {"type": "local", "command": ["/usr/bin/fs-mcp"]},
                        "old-kiln": {"type": "local", "command": ["uvx", "kiln3d", "serve"]},
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    paths["hermes"].write_text(_HERMES_BEFORE, encoding="utf-8")
    paths["goose"].write_text(_GOOSE_BEFORE, encoding="utf-8")
    return paths


def test_each_app_gets_kiln_in_its_own_spelling(home: Path, kiln_bin: Path) -> None:
    import yaml

    paths = _seed_more(home)
    result = _install("--command", str(kiln_bin))
    assert result.exit_code == 0, result.output
    k = str(kiln_bin)
    standard = {"command": k, "args": ["serve"]}

    def load(app_id: str):
        return json.loads(paths[app_id].read_text(encoding="utf-8"))

    assert load("copilot")["mcpServers"] == {"fs": {"command": "/usr/bin/fs-mcp", "args": []}, "kiln": standard}
    assert load("devin")["mcpServers"] == {"kiln": standard}
    assert load("cline")["mcpServers"] == {"kiln": standard}
    assert load("lmstudio")["mcpServers"] == {"kiln": standard}
    amp = load("amp")
    assert amp["amp.theme"] == "dark"
    assert amp["amp.mcpServers"]["kiln"] == standard
    opencode = load("opencode")["mcp"]["servers"]
    assert opencode == {
        "fs": {"type": "local", "command": ["/usr/bin/fs-mcp"]},
        "kiln": {"type": "local", "command": [k, "serve"]},
    }

    hermes_text = paths["hermes"].read_text(encoding="utf-8")
    hermes = yaml.safe_load(hermes_text)
    assert hermes["mcp_servers"]["kiln"] == standard
    assert "kiln-old" not in hermes["mcp_servers"]  # one Kiln per app
    assert hermes["display"] == {"theme": "dark"} and hermes["model"] == "gpt-6.1"
    for kept in ("# Hermes settings", "# my files", "# keep me"):
        assert kept in hermes_text

    goose = yaml.safe_load(paths["goose"].read_text(encoding="utf-8"))
    assert goose["GOOSE_MODEL"] == "gpt-6"
    assert goose["extensions"]["developer"]["type"] == "builtin"
    assert goose["extensions"]["kiln"] == {
        "name": "kiln",
        "type": "stdio",
        "enabled": True,
        "cmd": k,
        "args": ["serve"],
        "timeout": 300,
    }
    assert "remove duplicate 'old-kiln'" in re.sub(r"\s+", " ", result.output)

    # Uninstall puts every one of them back exactly.
    before_seed = {a: paths[a] for a in ("copilot", "amp", "opencode", "hermes", "goose")}
    assert _uninstall("--receipt-only").exit_code == 0
    assert paths["hermes"].read_text(encoding="utf-8") == _HERMES_BEFORE
    assert paths["goose"].read_text(encoding="utf-8") == _GOOSE_BEFORE
    assert "old-kiln" in load("opencode")["mcp"]["servers"]
    for app_id in ("devin", "cline", "lmstudio"):
        assert not paths[app_id].exists()
    assert set(before_seed) <= set(paths)


def test_yaml_edits_survive_an_app_rewriting_the_rest(home: Path, kiln_bin: Path) -> None:
    """Hermes rewrites its own file; uninstall then removes only Kiln's
    entry and brings the removed duplicate back, keeping Hermes's change."""
    import yaml

    paths = _seed_more(home)
    assert _install("--command", str(kiln_bin), "--only", "hermes").exit_code == 0
    text = paths["hermes"].read_text(encoding="utf-8").replace("model: gpt-6.1", "model: gpt-7")
    paths["hermes"].write_text(text, encoding="utf-8")

    assert _uninstall("--receipt-only").exit_code == 0

    after = yaml.safe_load(paths["hermes"].read_text(encoding="utf-8"))
    assert after["model"] == "gpt-7"
    assert set(after["mcp_servers"]) == {"filesystem", "kiln-old"}


def test_a_yaml_key_written_inline_is_left_untouched(home: Path, kiln_bin: Path) -> None:
    settings = home / ".hermes" / "config.yaml"
    settings.parent.mkdir()
    original = "mcp_servers: {fs: {command: /usr/bin/fs-mcp}}\n"
    settings.write_text(original, encoding="utf-8")

    result = _install("--command", str(kiln_bin), "--only", "hermes")

    assert settings.read_text(encoding="utf-8") == original
    assert "can't edit safely" in result.output


def test_web_apps_get_their_connector_address_not_a_pretend_install(home: Path, kiln_bin: Path) -> None:
    from kiln.cli.app_wiring import CHATGPT_CONNECTOR_URL, HOSTED_CONNECTOR_URL

    result = _install("--dry-run", "--command", str(kiln_bin))
    flat = re.sub(r"\s+", " ", result.output)
    assert f"ChatGPT {CHATGPT_CONNECTOR_URL}" in flat
    assert f"Claude on the web, Claude Duet {HOSTED_CONNECTOR_URL}" in flat
    assert f"Gemini {HOSTED_CONNECTOR_URL}" in flat and f"Grok {HOSTED_CONNECTOR_URL}" in flat
    assert "no installer can reach them" in flat
    only = _install("--dry-run", "--command", str(kiln_bin), "--only", "cursor")
    assert "no installer can reach them" not in only.output


def test_health_reads_every_settings_format_the_installer_writes(home: Path, kiln_bin: Path) -> None:
    from kiln.cli.mcp_config_audit import audit_all_mcp_clients

    _seed_more(home)
    assert _install("--command", str(kiln_bin)).exit_code == 0
    by_client = {r.client: r for r in audit_all_mcp_clients()}
    for display in ("Hermes Agent", "Goose", "Amp", "opencode"):
        r = by_client[display]
        assert r.parse_error is None, (display, r.parse_error)
        kiln = next(e for e in r.entries if e.name == "kiln")
        assert kiln.command == str(kiln_bin) and kiln.status == "ok", (display, kiln)


def test_nested_key_apps_reverse_entry_by_entry(home: Path, kiln_bin: Path) -> None:
    """opencode keeps servers two levels down; after opencode rewrites its
    own file, uninstall still finds Kiln there and brings the duplicate back."""
    paths = _seed_more(home)
    assert _install("--command", str(kiln_bin), "--only", "opencode").exit_code == 0
    data = json.loads(paths["opencode"].read_text(encoding="utf-8"))
    data["model"] = "y"
    paths["opencode"].write_text(json.dumps(data), encoding="utf-8")

    assert _uninstall("--receipt-only").exit_code == 0

    after = json.loads(paths["opencode"].read_text(encoding="utf-8"))
    assert after["model"] == "y"
    assert set(after["mcp"]["servers"]) == {"fs", "old-kiln"}


def test_claude_apps_are_told_where_an_account_connector_is_turned_off(home: Path, kiln_bin: Path) -> None:
    """Connectors synced from a claude.ai account can't be removed from a
    settings file, so the installer says where to turn that one off."""
    _seed(home)
    result = _install("--dry-run", "--command", str(kiln_bin))
    assert "claude.ai/customize/connectors" in result.output
    other = _install("--dry-run", "--command", str(kiln_bin), "--only", "codex")
    assert "claude.ai/customize/connectors" not in other.output
