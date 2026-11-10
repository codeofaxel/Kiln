"""Connect Kiln to every AI app on the computer, and take it back exactly.

One registry (:data:`APPS`) names each app Kiln can connect to: where its
settings live, what format they are in, and how to tell the app is on this
computer.  One planner (:func:`plan_apps`) turns that registry into a plan
per app, one writer (:func:`apply_plan`) carries the plan out, and one
reverser (:func:`undo_receipt`) takes back what the receipt says was done.
Adding an app is a new row in :data:`APPS`; no app has its own code path.

Contract:

* Kiln appears in an app at most once, under the name ``kiln``.  Another
  entry in the same settings file that also starts Kiln (a hand-added copy
  under another name, or Kiln's hosted connector) is listed in the plan as a
  duplicate and removed; the receipt keeps it so an uninstall restores it.
* A settings file that cannot be read is never written.  A file whose new
  contents do not read back as intended is never written either.
* Every file is copied to ``~/.kiln/install/backups`` before its first
  change, and every change is written to ``~/.kiln/install/receipt.json``.
  The receipt keeps the state from BEFORE the first install, so running the
  installer again (an update) never moves the point an uninstall returns to.
* An uninstall restores a file byte for byte when nothing else has touched
  it since Kiln's only write; otherwise it reverses entry by entry, and an
  entry someone changed after Kiln wrote it is left alone and reported.
* A dry run reads and plans; it writes nothing, receipt included.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import tomllib

KILN_NAME = "kiln"
RECEIPT_VERSION = 1

# Actions a plan can carry for one entry.
ADD = "add"
UPDATE = "update"
LEAVE = "leave"
REMOVE_DUPLICATE = "remove duplicate"
NOT_FOUND = "not found"
CANT_READ = "can't read"


class SettingsUnreadable(Exception):
    """A settings file exists but cannot be read or parsed."""


# ═════════════════════════════════════════════════════════════════════
# The registry
# ═════════════════════════════════════════════════════════════════════


def _home() -> Path:
    return Path.home()


def _claude_desktop_dir() -> Path:
    system = platform.system()
    if system == "Darwin":
        return _home() / "Library/Application Support/Claude"
    if system == "Windows":
        appdata = os.environ.get("APPDATA")
        return (Path(appdata) if appdata else _home() / "AppData/Roaming") / "Claude"
    xdg = os.environ.get("XDG_CONFIG_HOME")
    return (Path(xdg) if xdg else _home() / ".config") / "Claude"


@dataclass(frozen=True)
class App:
    """One AI app Kiln can connect to.

    ``fmt`` is ``json`` (an ``mcpServers`` object), ``toml`` (``[mcp_servers.*]``
    tables) or ``skill`` (a skill folder holding Kiln's SKILL.md).  The app is
    on this computer when any ``markers`` path exists or any ``commands`` is on
    PATH.
    """

    id: str
    display: str
    settings: Callable[[], Path]
    fmt: str
    markers: Callable[[], tuple[Path, ...]] = lambda: ()
    commands: tuple[str, ...] = ()
    restart: str = ""

    def settings_path(self) -> Path:
        return self.settings()

    def is_present(self) -> bool:
        if any(p.exists() for p in self.markers()):
            return True
        return any(shutil.which(c) for c in self.commands)


APPS: tuple[App, ...] = (
    App(
        "claude_code",
        "Claude Code",
        lambda: _home() / ".claude.json",
        "json",
        markers=lambda: (_home() / ".claude.json", _home() / ".claude"),
        commands=("claude",),
        restart="start a new Claude Code session",
    ),
    App(
        "claude_desktop",
        "Claude Desktop",
        lambda: _claude_desktop_dir() / "claude_desktop_config.json",
        "json",
        markers=lambda: (_claude_desktop_dir(),),
        restart="quit and reopen Claude Desktop",
    ),
    App(
        "codex",
        "Codex",
        lambda: _home() / ".codex/config.toml",
        "toml",
        markers=lambda: (_home() / ".codex",),
        commands=("codex",),
        restart="open a fresh Codex session",
    ),
    App(
        "cursor",
        "Cursor",
        lambda: _home() / ".cursor/mcp.json",
        "json",
        markers=lambda: (_home() / ".cursor",),
        commands=("cursor", "cursor-agent"),
        restart="reload Cursor",
    ),
    App(
        "gemini",
        "Gemini CLI",
        lambda: _home() / ".gemini/settings.json",
        "json",
        markers=lambda: (_home() / ".gemini",),
        commands=("gemini",),
        restart="start a new Gemini CLI session",
    ),
    App(
        "grok",
        "Grok Build",
        lambda: _home() / ".grok/config.toml",
        "toml",
        markers=lambda: (_home() / ".grok",),
        commands=("grok",),
        restart="start a new Grok Build session",
    ),
    App(
        "openclaw",
        "OpenClaw",
        lambda: _home() / ".openclaw/skills/kiln/SKILL.md",
        "skill",
        markers=lambda: (_home() / ".openclaw",),
        commands=("openclaw",),
        restart="start a new OpenClaw chat",
    ),
)


def app_ids() -> list[str]:
    return [a.id for a in APPS]


def normalize_id(raw: str) -> str:
    return raw.strip().lower().replace("-", "_")


# ═════════════════════════════════════════════════════════════════════
# Formats: read one named entry, write one named entry
# ═════════════════════════════════════════════════════════════════════
#
# Every format works on the whole file text, so the planner, the writer and
# the reverser are the same for every app.  A "fragment" is what one entry
# looks like in that format: a JSON object, the raw text of a TOML table, or
# the SKILL.md text.


def _read_text(path: Path) -> str | None:
    if not path.exists():
        return None
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SettingsUnreadable(f"couldn't read {path}: {exc}") from exc


class _JsonFormat:
    key = "mcpServers"

    def parse(self, path: Path, text: str | None) -> dict[str, Any]:
        if text is None or not text.strip():
            return {}
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise SettingsUnreadable(f"{path} isn't valid JSON ({exc.msg} at line {exc.lineno})") from exc
        if not isinstance(data, dict):
            raise SettingsUnreadable(f"{path} isn't a JSON object at the top")
        servers = data.get(self.key)
        if servers is not None and not isinstance(servers, dict):
            raise SettingsUnreadable(f"{path} has an {self.key} that isn't an object")
        return data

    def entries(self, path: Path, text: str | None) -> dict[str, Any]:
        return dict(self.parse(path, text).get(self.key) or {})

    def get(self, path: Path, text: str | None, name: str) -> Any:
        return self.entries(path, text).get(name)

    def put(self, path: Path, text: str | None, name: str, fragment: Any) -> str:
        data = self.parse(path, text)
        servers = dict(data.get(self.key) or {})
        if fragment is None:
            servers.pop(name, None)
        else:
            servers[name] = fragment
        data[self.key] = servers
        return json.dumps(data, indent=2) + "\n"

    def fragment(self, entry: dict[str, Any], skill_text: str) -> Any:
        return dict(entry)

    def is_empty(self, text: str) -> bool:
        data = json.loads(text) if text.strip() else {}
        return data in ({}, {self.key: {}})


_TOML_HEADER = re.compile(r"^\s*\[([^\[\]]+)\]\s*(?:#.*)?$")
_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


class _TomlFormat:
    def parse(self, path: Path, text: str | None) -> dict[str, Any]:
        if text is None or not text.strip():
            return {}
        try:
            return tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            raise SettingsUnreadable(f"{path} isn't valid TOML ({exc})") from exc

    def entries(self, path: Path, text: str | None) -> dict[str, Any]:
        servers = self.parse(path, text).get("mcp_servers") or {}
        return dict(servers) if isinstance(servers, dict) else {}

    @staticmethod
    def _span(text: str, name: str) -> tuple[int, int] | None:
        """Line span of ``[mcp_servers.<name>]`` and its child tables."""
        table = f"mcp_servers.{name}"
        lines = text.splitlines(keepends=True)
        start = end = None
        for i, line in enumerate(lines):
            m = _TOML_HEADER.match(line)
            if not m:
                continue
            header = m.group(1).strip()
            inside = header == table or header.startswith(table + ".")
            if start is None and inside:
                start = i
            elif start is not None and not inside:
                end = i
                break
        if start is None:
            return None
        return start, (end if end is not None else len(lines))

    def get(self, path: Path, text: str | None, name: str) -> Any:
        if not text:
            return None
        self.parse(path, text)
        span = self._span(text, name)
        if span is None:
            return None
        lines = text.splitlines(keepends=True)
        return "".join(lines[span[0] : span[1]])

    def put(self, path: Path, text: str | None, name: str, fragment: Any) -> str:
        text = text or ""
        if not _BARE_KEY.match(name):
            raise SettingsUnreadable(f"{path}: can't edit the entry named {name!r}")
        block = fragment or ""
        if block and not block.endswith("\n"):
            block += "\n"
        span = self._span(text, name)
        lines = text.splitlines(keepends=True)
        if span is not None:
            new = "".join(lines[: span[0]]) + block + "".join(lines[span[1] :])
        elif not block:
            new = text
        else:
            prefix = text
            if prefix and not prefix.endswith("\n"):
                prefix += "\n"
            if prefix.strip() and not prefix.endswith("\n\n"):
                prefix += "\n"
            new = prefix + block
        return new

    def fragment(self, entry: dict[str, Any], skill_text: str) -> Any:
        return (
            f"[mcp_servers.{KILN_NAME}]\n"
            f"command = {json.dumps(str(entry['command']))}\n"
            f"args = {json.dumps(list(entry.get('args') or []))}\n"
        )

    def is_empty(self, text: str) -> bool:
        return not text.strip()


class _SkillFormat:
    """A skill folder: the 'entry' is the whole SKILL.md file."""

    def entries(self, path: Path, text: str | None) -> dict[str, Any]:
        return {KILN_NAME: text} if text is not None else {}

    def get(self, path: Path, text: str | None, name: str) -> Any:
        return text

    def put(self, path: Path, text: str | None, name: str, fragment: Any) -> str | None:
        return fragment  # None removes the file

    def fragment(self, entry: dict[str, Any], skill_text: str) -> Any:
        return skill_text

    def is_empty(self, text: str) -> bool:
        return False


FORMATS: dict[str, Any] = {"json": _JsonFormat(), "toml": _TomlFormat(), "skill": _SkillFormat()}


# ═════════════════════════════════════════════════════════════════════
# Recognizing Kiln under another name
# ═════════════════════════════════════════════════════════════════════

HOSTED_HOST = "kiln3d.com"


def kiln_kind(entry: Any) -> str | None:
    """``"local"`` when an MCP entry starts Kiln on this computer,
    ``"hosted"`` when it points at Kiln's hosted connector, else ``None``."""
    if not isinstance(entry, dict):
        return None
    for key in ("url", "httpUrl", "serverUrl"):
        url = entry.get(key)
        if isinstance(url, str):
            host = re.sub(r"^[a-z]+://", "", url.strip().lower()).split("/")[0].split(":")[0]
            if host == HOSTED_HOST or host.endswith("." + HOSTED_HOST):
                return "hosted"
    command = entry.get("command")
    if not isinstance(command, str):
        return None
    args = [a for a in (entry.get("args") or []) if isinstance(a, str)]
    stem = Path(command.replace("\\", "/")).name.lower()
    for ext in (".exe", ".cmd", ".bat"):
        if stem.endswith(ext):
            stem = stem[: -len(ext)]
    if stem == "kiln" and "serve" in args:
        return "local"
    if stem.startswith("python") and "-m" in args:
        i = args.index("-m")
        if i + 1 < len(args) and args[i + 1] == "kiln":
            return "local"
    if stem in ("uv", "uvx", "npx") and any(a.split("[")[0].split("=")[0].split("@")[0] == "kiln3d" for a in args):
        return "local"
    return None


# ═════════════════════════════════════════════════════════════════════
# Planning
# ═════════════════════════════════════════════════════════════════════


@dataclass
class Step:
    name: str
    action: str
    before: Any
    after: Any
    note: str = ""


@dataclass
class AppPlan:
    app: App
    path: Path
    action: str  # the headline: add / update / leave / not found / can't read
    steps: list[Step] = field(default_factory=list)
    reason: str = ""

    @property
    def writes(self) -> bool:
        return any(s.action in (ADD, UPDATE, REMOVE_DUPLICATE) for s in self.steps)


def _plan_one(app: App, entry: dict[str, Any], skill_text: str) -> AppPlan:
    path = app.settings_path()
    if not app.is_present():
        return AppPlan(app, path, NOT_FOUND)
    fmt = FORMATS[app.fmt]
    try:
        text = _read_text(path)
        current = fmt.entries(path, text)
    except SettingsUnreadable as exc:
        return AppPlan(app, path, CANT_READ, reason=str(exc))

    wanted = fmt.fragment(entry, skill_text)
    steps: list[Step] = []
    for name, value in current.items():
        if name == KILN_NAME or app.fmt == "skill":
            continue
        kind = kiln_kind(value)
        if kind is None:
            continue
        before = fmt.get(path, text, name)
        note = "Kiln's hosted connector" if kind == "hosted" else "another copy of Kiln"
        if app.fmt == "toml" and not _BARE_KEY.match(name):
            return AppPlan(
                app,
                path,
                CANT_READ,
                reason=f"{path} has {note} under {name!r}, which Kiln can't edit safely",
            )
        steps.append(Step(name, REMOVE_DUPLICATE, before, None, note))

    existing = fmt.get(path, text, KILN_NAME)
    if existing is None:
        kiln_step = Step(KILN_NAME, ADD, None, wanted)
    elif _same(app.fmt, existing, wanted):
        kiln_step = Step(KILN_NAME, LEAVE, existing, existing)
    else:
        note = "was Kiln's hosted connector" if kiln_kind(current.get(KILN_NAME)) == "hosted" else ""
        kiln_step = Step(KILN_NAME, UPDATE, existing, wanted, note)
    steps.append(kiln_step)

    headline = kiln_step.action
    if headline == LEAVE and any(s.action == REMOVE_DUPLICATE for s in steps):
        headline = UPDATE
    return AppPlan(app, path, headline, steps)


def _same(fmt: str, a: Any, b: Any) -> bool:
    if fmt == "toml":
        return tomllib.loads(a or "") == tomllib.loads(b or "")
    return a == b


def plan_apps(
    entry: dict[str, Any],
    *,
    apps: list[App] | None = None,
    only: list[str] | None = None,
    skill_text: str | None = None,
) -> list[AppPlan]:
    """Plan Kiln's place in every app (or the given / ``only`` ones)."""
    pool = list(APPS) if apps is None else apps
    chosen = [a for a in pool if not only or a.id in only]
    text = skill_text if skill_text is not None else _skill_text()
    return [_plan_one(a, entry, text) for a in chosen]


def _skill_text() -> str:
    try:
        from kiln.skill_manifest import get_skill_definition_path

        return get_skill_definition_path().read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return ""


# ═════════════════════════════════════════════════════════════════════
# The receipt
# ═════════════════════════════════════════════════════════════════════


def install_dir() -> Path:
    return _home() / ".kiln" / "install"


def receipt_path() -> Path:
    return install_dir() / "receipt.json"


def _sha(text: str | None) -> str | None:
    return None if text is None else hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_receipt(path: Path | None = None) -> dict[str, Any]:
    path = path or receipt_path()
    if not path.exists():
        return {"version": RECEIPT_VERSION, "files": {}}
    data = json.loads(path.read_text(encoding="utf-8"))
    data.setdefault("files", {})
    return data


def _private_dir(path: Path) -> None:
    """Create a folder only this user can open (the receipt and backups
    hold copies of app settings, which can carry tokens)."""
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(path, 0o700)


def _atomic_write(path: Path, text: str, *, mode: int | None = None) -> None:
    """Replace ``path`` in one step, keeping its permissions.

    A file that exists keeps its own mode; a new one is ``mode`` or, by
    default, readable by this user only.  The temporary file is created with
    that mode, so the content is never readable by others even briefly.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if mode is None:
        try:
            mode = path.stat().st_mode & 0o777
        except FileNotFoundError:
            mode = 0o600
    tmp = path.with_name(path.name + ".kiln-tmp")
    tmp.unlink(missing_ok=True)
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, mode)
    try:
        if os.name != "nt":
            os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fd = -1
            fh.write(text)
    finally:
        if fd != -1:
            os.close(fd)
    tmp.replace(path)


def _save_receipt(receipt: dict[str, Any], path: Path) -> None:
    _private_dir(path.parent)
    _atomic_write(path, json.dumps(receipt, indent=2) + "\n", mode=0o600)


# ═════════════════════════════════════════════════════════════════════
# Carrying out a plan
# ═════════════════════════════════════════════════════════════════════


@dataclass
class Outcome:
    plan: AppPlan
    done: bool
    message: str = ""


def apply_plan(
    plans: list[AppPlan],
    *,
    kiln_version: str = "",
    receipt_file: Path | None = None,
) -> list[Outcome]:
    """Write every plan that changes something, recording each change."""
    receipt_file = receipt_file or receipt_path()
    receipt = load_receipt(receipt_file)
    outcomes: list[Outcome] = []
    for plan in plans:
        if not plan.writes:
            outcomes.append(Outcome(plan, False))
            continue
        try:
            _apply_one(plan, receipt, receipt_file.parent)
        except SettingsUnreadable as exc:
            outcomes.append(Outcome(plan, False, str(exc)))
            continue
        outcomes.append(Outcome(plan, True))
        receipt["kiln_version"] = kiln_version
        receipt["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        _save_receipt(receipt, receipt_file)
    return outcomes


def _apply_one(plan: AppPlan, receipt: dict[str, Any], base: Path) -> None:
    fmt = FORMATS[plan.app.fmt]
    path = plan.path
    original = _read_text(path)
    text: str | None = original
    for step in plan.steps:
        if step.action in (ADD, UPDATE, REMOVE_DUPLICATE):
            text = fmt.put(path, text, step.name, step.after)
    _verify(plan, text)

    key = str(path)
    record = receipt["files"].get(key)
    if record is None:
        backup = None
        if original is not None:
            backup_dir = base / "backups"
            _private_dir(base)
            _private_dir(backup_dir)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            backup = backup_dir / f"{plan.app.id}-{stamp}{path.suffix or '.txt'}"
            shutil.copy2(path, backup)
            if os.name != "nt":
                os.chmod(backup, 0o600)
        created: list[str] = []
        folder = path.parent
        while not folder.exists():
            created.append(str(folder))
            folder = folder.parent
        record = {
            "app": plan.app.id,
            "format": plan.app.fmt,
            "existed_before": original is not None,
            "mode": (path.stat().st_mode & 0o777) if original is not None else None,
            "created_dirs": created,
            "backup": str(backup) if backup else None,
            "writes": 0,
            "entries": {},
        }
        receipt["files"][key] = record
    for step in plan.steps:
        if step.action not in (ADD, UPDATE, REMOVE_DUPLICATE):
            continue
        entry = record["entries"].setdefault(step.name, {"before": step.before})
        entry["written"] = step.after
        entry["action"] = entry.get("action") or step.action

    if text is None:
        if path.exists():
            path.unlink()
    else:
        _atomic_write(path, text)
    record["writes"] += 1
    record["sha256"] = _sha(text)


def _verify(plan: AppPlan, text: str | None) -> None:
    """Refuse to write a file that does not read back as planned."""
    fmt = FORMATS[plan.app.fmt]
    try:
        for step in plan.steps:
            got = fmt.get(plan.path, text, step.name)
            want = step.after if step.action != LEAVE else step.before
            if not (got == want or (plan.app.fmt == "toml" and got and want and _same("toml", got, want))):
                raise SettingsUnreadable(f"{plan.path}: the change didn't read back as planned, so nothing was written")
        if plan.app.fmt == "toml":
            parsed = (tomllib.loads(text or "").get("mcp_servers") or {}).get(KILN_NAME)
            want = tomllib.loads(next(s.after for s in plan.steps if s.name == KILN_NAME) or "")
            if parsed != want.get("mcp_servers", {}).get(KILN_NAME):
                raise SettingsUnreadable(
                    f"{plan.path}: Kiln's entry didn't read back as planned, so nothing was written"
                )
    except (tomllib.TOMLDecodeError, SettingsUnreadable, json.JSONDecodeError) as exc:
        if isinstance(exc, SettingsUnreadable) and "read back" in str(exc):
            raise
        raise SettingsUnreadable(
            f"{plan.path}: the change would leave the file unreadable, so nothing was written"
        ) from exc


# ═════════════════════════════════════════════════════════════════════
# Taking it back
# ═════════════════════════════════════════════════════════════════════


@dataclass
class UndoResult:
    path: str
    app: str
    how: str  # "restored backup" / "reversed" / "removed file" / "nothing to do"
    left_alone: list[str] = field(default_factory=list)
    message: str = ""


def undo_receipt(
    *,
    receipt_file: Path | None = None,
    only: list[str] | None = None,
    dry_run: bool = False,
) -> list[UndoResult]:
    """Reverse exactly what the receipt says was done."""
    receipt_file = receipt_file or receipt_path()
    receipt = load_receipt(receipt_file)
    results: list[UndoResult] = []
    for key, record in list(receipt["files"].items()):
        if only and record["app"] not in only:
            continue
        results.append(_undo_file(Path(key), record, dry_run))
        if not dry_run and not results[-1].message:
            receipt["files"].pop(key)
    if not dry_run:
        if receipt["files"]:
            _save_receipt(receipt, receipt_file)
        elif receipt_file.exists():
            stamp = time.strftime("%Y%m%d-%H%M%S")
            receipt_file.replace(receipt_file.with_name(f"receipt-undone-{stamp}.json"))
    return results


def _undo_file(path: Path, record: dict[str, Any], dry_run: bool) -> UndoResult:
    app = record["app"]
    fmt = FORMATS[record["format"]]
    try:
        current = _read_text(path)
    except SettingsUnreadable as exc:
        return UndoResult(str(path), app, "nothing to do", message=str(exc))
    untouched = current is not None and _sha(current) == record.get("sha256")

    if untouched and record["writes"] == 1:
        if record["existed_before"] and record.get("backup") and Path(record["backup"]).exists():
            if not dry_run:
                shutil.copyfile(record["backup"], path)
                if record.get("mode") is not None and os.name != "nt":
                    os.chmod(path, record["mode"])
            return UndoResult(str(path), app, "restored backup")
        if not record["existed_before"]:
            if not dry_run:
                path.unlink()
                _prune_empty_parents(path, record)
            return UndoResult(str(path), app, "removed file")

    text = current
    left: list[str] = []
    try:
        for name, entry in record["entries"].items():
            now = fmt.get(path, text, name)
            if now == entry["before"]:
                continue
            if now is not None and not _equal(record["format"], now, entry["written"]):
                left.append(name)
                continue
            text = fmt.put(path, text, name, entry["before"])
    except SettingsUnreadable as exc:
        return UndoResult(str(path), app, "nothing to do", message=str(exc))

    if text == current:
        return UndoResult(str(path), app, "nothing to do", left_alone=left)
    remove_file = not record["existed_before"] and (text is None or fmt.is_empty(text))
    if not dry_run:
        if remove_file:
            if path.exists():
                path.unlink()
            _prune_empty_parents(path, record)
        else:
            _atomic_write(path, text or "")
    return UndoResult(str(path), app, "removed file" if remove_file else "reversed", left_alone=left)


def _equal(fmt: str, a: Any, b: Any) -> bool:
    try:
        return _same(fmt, a, b)
    except tomllib.TOMLDecodeError:
        return a == b


def _prune_empty_parents(path: Path, record: dict[str, Any]) -> None:
    """Remove the folders Kiln created for this file, if they are empty."""
    for folder in record.get("created_dirs") or []:
        try:
            Path(folder).rmdir()
        except OSError:
            break
