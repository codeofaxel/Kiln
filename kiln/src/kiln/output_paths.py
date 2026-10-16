"""Where a tool may write a file the caller named.

The hazard
----------

Many tools take a parameter saying where to save what they make
(``output_path``, ``output_dir``, ``save_path``, ...).  Those tools wrote to
whatever the caller named.  The caller is usually an AI agent, and an agent
can be steered by text it reads (a model page, a file, a web result) into
naming a path the person never would: ``~/.zshrc``, ``~/.ssh/authorized_keys``,
a config file.  The tool then replaced that file with a mesh, a slice or an
image.  The bytes are never the attacker's choice, but the file is gone.

The rule
--------

A path the CALLER names for a tool to write is accepted only when:

* a file path ends in a type Kiln makes (a mesh, a CAD or print file, an
  image, a PDF), after symlinks are followed, so ``part.stl`` pointing at
  ``~/.zshrc`` is judged as ``~/.zshrc``;
* it is not inside a hidden folder in the home directory (``~/.ssh``,
  ``~/.config``, ...) or a system folder.  Kiln's own ``~/.kiln`` is allowed;
* when ``KILN_OUTPUT_DIR`` is set, it is inside that folder or the system
  temp folder.

A location Kiln chooses itself (the parameter left empty) is not judged:
the rule is about what a caller can name.

Where it sits
-------------

On the tool function itself, attached when the tool is registered
(:func:`install`), because every door that runs a tool ends there: the MCP
handler, the web-to-printer bridge, ``confirm_action``, and any plugin
registering on the same server.  Keyed on the PARAMETER name, so a tool
added later is covered without knowing this module exists.
"""

from __future__ import annotations

import functools
import inspect
import os
import tempfile
from pathlib import Path
from typing import Any

#: Environment variable that, when set, confines every caller-named output
#: location to one folder (plus the system temp folder).
OUTPUT_DIR_ENV = "KILN_OUTPUT_DIR"

#: Refusal code carried in the standard error envelope.
REFUSAL_CODE = "OUTPUT_PATH_REFUSED"

#: File types Kiln makes.  A caller-named output file must end in one.
ARTIFACT_SUFFIXES = frozenset({
    # meshes
    ".stl", ".3mf", ".obj", ".ply", ".off", ".amf", ".glb", ".gltf",
    # CAD and source
    ".step", ".stp", ".scad", ".dxf", ".svg",
    # print files
    ".gcode", ".bgcode", ".gco", ".ufp",
    # images and documents
    ".png", ".jpg", ".jpeg", ".webp", ".pdf",
})

#: Tools whose output is a different kind of file, and the types they make.
TOOL_EXTRA_SUFFIXES: dict[str, frozenset[str]] = {
    "backup_database": frozenset({".db", ".sqlite", ".sqlite3"}),
    "export_audit_trail": frozenset({".json", ".jsonl", ".csv"}),
    "export_audit_log": frozenset({".json", ".jsonl", ".csv"}),
}

#: Parameters whose name says "file" but whose value is a folder.
DIRECTORY_PARAMS: frozenset[tuple[str, str]] = frozenset({
    ("download_generated_model", "output_path"),
})

_WRITE_WORDS = frozenset({"output", "dest", "save"})
_FILE_ENDS = frozenset({"path", "file"})
_DIR_ENDS = frozenset({"dir", "directory"})

#: System folders no caller-named output belongs in.
_SYSTEM_ROOTS = tuple(
    Path(p) for p in (
        "/etc", "/bin", "/sbin", "/usr", "/System", "/Library",
        "/private/etc", "/private/var/db", "/boot", "/dev", "/proc", "/sys",
    )
)

_GUARD_MARK = "__kiln_output_path_guard__"


def write_param_kind(tool_name: str, param: str) -> str | None:
    """``"file"``, ``"dir"``, or None when *param* does not name a write location."""
    words = param.lower().split("_")
    if len(words) < 2 or not _WRITE_WORDS.intersection(words[:-1]):
        return None
    if (tool_name, param) in DIRECTORY_PARAMS or words[-1] in _DIR_ENDS:
        return "dir"
    if words[-1] in _FILE_ENDS:
        return "file"
    return None


def _resolve(raw: str) -> Path:
    # strict=False: an output usually does not exist yet, but every part of
    # it that does (a symlinked file, a symlinked folder) is followed.
    return Path(os.path.expanduser(raw)).resolve(strict=False)


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _temp_root() -> Path:
    return Path(tempfile.gettempdir()).resolve()


def _protected_reason(path: Path) -> str | None:
    """Why *path* is somewhere no caller-named output belongs, or None."""
    home = Path.home().resolve()
    if _inside(path, home) and path != home:
        rel = path.relative_to(home).parts
        if rel and rel[0].startswith(".") and rel[0] != ".kiln":
            return f"it is inside the hidden folder ~/{rel[0]}"
        # A hidden file or folder deeper down (~/project/.git/hooks/...).
        for part in rel[1:]:
            if part.startswith("."):
                return f"it is inside a hidden folder ({part})"
    for root in _SYSTEM_ROOTS:
        try:
            root = root.resolve()
        except OSError:
            continue
        if _inside(path, root):
            return f"it is inside the system folder {root}"
    return None


def check_write_location(tool_name: str, param: str, raw: Any) -> str | None:
    """The reason Kiln refuses to write *raw* for *param*, or None to allow it."""
    kind = write_param_kind(tool_name, param)
    if kind is None or not isinstance(raw, str) or not raw.strip():
        return None
    try:
        path = _resolve(raw.strip())
    except (OSError, RuntimeError, ValueError):
        return f"{param} {raw!r} could not be resolved to a location"

    confine = os.environ.get(OUTPUT_DIR_ENV, "").strip()
    if confine:
        root = _resolve(confine)
        if not (_inside(path, root) or _inside(path, _temp_root())):
            return (
                f"{param} must be inside {root} (set by {OUTPUT_DIR_ENV}) "
                "or the system temp folder"
            )

    if kind == "file":
        allowed = ARTIFACT_SUFFIXES | TOOL_EXTRA_SUFFIXES.get(tool_name, frozenset())
        if path.suffix.lower() not in allowed:
            shown = path.name if path.name == Path(raw).name else f"{Path(raw).name} -> {path.name}"
            return (
                f"{param} must name a file of a type this tool makes "
                f"({', '.join(sorted(allowed))}); got {shown}"
            )

    reason = _protected_reason(path)
    if reason:
        return f"{param} {raw!r} is refused because {reason}"
    return None


def refusal_for_call(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any] | None:
    """The standard error envelope for the first refused write location, or None."""
    for param, raw in arguments.items():
        reason = check_write_location(tool_name, param, raw)
        if reason:
            return {
                "success": False,
                "error": {
                    "code": REFUSAL_CODE,
                    "message": (
                        f"Kiln did not write anything. {reason}. Choose a "
                        "location in a normal folder (for example the "
                        "Desktop or Documents) with a file name ending in "
                        "the type being made, or leave it empty and Kiln "
                        "picks one."
                    ),
                    "retryable": False,
                },
            }
    return None


def guard_function(tool_name: str, fn: Any) -> Any:
    """*fn*, refusing any call whose caller-named write location is refused.

    Returns *fn* itself when it has no write-location parameter, or is
    already guarded.  Keeps sync and async functions as they were.
    """
    if getattr(fn, _GUARD_MARK, False):
        return fn
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return fn
    if not any(write_param_kind(tool_name, p) for p in sig.parameters):
        return fn

    def _named_arguments(args: tuple, kwargs: dict) -> dict[str, Any]:
        try:
            return dict(sig.bind_partial(*args, **kwargs).arguments)
        except TypeError:
            return dict(kwargs)

    if inspect.iscoroutinefunction(fn):
        @functools.wraps(fn)
        async def guarded_async(*args: Any, **kwargs: Any) -> Any:
            refusal = refusal_for_call(tool_name, _named_arguments(args, kwargs))
            if refusal is not None:
                return refusal
            return await fn(*args, **kwargs)

        guarded: Any = guarded_async
    else:
        @functools.wraps(fn)
        def guarded_sync(*args: Any, **kwargs: Any) -> Any:
            refusal = refusal_for_call(tool_name, _named_arguments(args, kwargs))
            if refusal is not None:
                return refusal
            return fn(*args, **kwargs)

        guarded = guarded_sync
    setattr(guarded, _GUARD_MARK, True)
    return guarded


def _guard_tool(tool: Any) -> None:
    fn = getattr(tool, "fn", None)
    name = getattr(tool, "name", None) or getattr(fn, "__name__", "")
    if fn is None:
        return
    guarded = guard_function(name, fn)
    if guarded is not fn:
        tool.fn = guarded


def install(mcp: Any) -> bool:
    """Guard every tool on *mcp*, now and as later ones register.

    Idempotent.  Returns False when *mcp* has no tool registry this module
    recognises (the guard is then not in place, and the caller should know).
    """
    manager = getattr(mcp, "_tool_manager", None)
    tools = getattr(manager, "_tools", None)
    add_tool = getattr(manager, "add_tool", None)
    if manager is None or not isinstance(tools, dict) or add_tool is None:
        return False
    for tool in list(tools.values()):
        _guard_tool(tool)
    if getattr(add_tool, _GUARD_MARK, False):
        return True

    @functools.wraps(add_tool)
    def add_tool_guarded(*args: Any, **kwargs: Any) -> Any:
        tool = add_tool(*args, **kwargs)
        _guard_tool(tool)
        return tool

    setattr(add_tool_guarded, _GUARD_MARK, True)
    manager.add_tool = add_tool_guarded
    return True
