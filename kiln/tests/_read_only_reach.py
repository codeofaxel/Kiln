"""What a read-only tool can reach that changes something.

A tool marked read-only may run without the person being asked, so it must
never reach a printer command, a write or delete on a saved record, a spent
allowance, a stored secret handed back in plain text, or another tool that
changes things.  The label is stated by hand at each definition, and a
hand-stated label can be wrong in a way no reader of the tool's own body
sees: ``auto_detect_nozzle`` read a printer's report and, two calls down,
refreshed the person's saved nozzle record (found 2026-10-28 by a security
review after every tool had been labelled).

So this walks each read-only tool's body and the Kiln functions it calls, a
few calls deep, resolving names the way Python does when the tool runs: its
closure, its module's globals, and the imports inside its body.  It is
shared by public Kiln's ``test_read_only_tools_change_nothing.py`` and
kiln-pro's twin, so both repos judge with one set of rules.

Honest bounds: it follows functions, not methods on objects it cannot name,
so a write behind ``self.<method>`` or a callable passed in is unseen; a
store is recognised by its receiver's name; files written to a temporary or
preview location are allowed (rendering a preview is read-only by
``kiln.tool_annotations``' own definition), so an overwrite of the person's
own file through ``write_bytes`` is unseen too, and so is a file deleted
with ``unlink`` (in practice Kiln's own cache and temp clean-up; a person's
saved things are deleted through a store or a delete tool, which are
judged); and a request to Kiln's servers is not judged, because served
read-only answers are requests too.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import re
import textwrap
import types
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from kiln.printers.base import PrinterAdapter

#: How many calls deep the walk follows from a tool's own body.
DEPTH = 4

#: PrinterAdapter methods that only read.  Every other public method of the
#: adapter changes the printer, so it is a command (derived from the class, so
#: a new command an adapter grows is covered the day it lands).
_ADAPTER_READ_PREFIXES = (
    "get_", "list_", "read_", "is_", "has_", "camera_", "homed_", "stream_",
    "snapshot_", "reported_", "declared_",
)
_ADAPTER_READ_NAMES = frozenset({
    "capabilities", "name", "motion_facts", "purge_station", "homing_commanded",
    "external_camera", "frame_source", "disconnect", "head_position",
    # These configure Kiln's own adapter object when it is built; nothing is
    # sent to the printer.
    "set_safety_profile", "set_external_camera",
})

#: A call on one of these receivers with one of these methods writes a store.
_STORE_RECEIVER = re.compile(r"(store|backend|ledger|registry|outbox|repo|_db|^db)$", re.I)
_STORE_WRITES = frozenset({
    "set", "save", "delete", "upsert", "insert", "put", "remove", "update",
    "add", "record", "write", "clear", "purge", "drop",
})
_EMPTIES = frozenset({"clear", "purge", "drop"})
_SPENDS = re.compile(r"(^_?check_\w*quota$|check_and_increment|^spend_|^consume_|^charge_)")
#: A call into the credential store's plaintext readers hands a secret back.
_REVEALS = re.compile(r"^(retrieve|reveal|decrypt)", re.I)
_REVEAL_RECEIVER = re.compile(r"(cred|vault|secret)", re.I)
_SQL_WRITE = re.compile(
    r"^\s*(INSERT\s+(OR\s+\w+\s+)?INTO|REPLACE\s+INTO|UPDATE\s+\S+\s+SET|DELETE\s+FROM)\b"
)


def printer_commands() -> frozenset[str]:
    public = {n for n in dir(PrinterAdapter) if not n.startswith("_")}

    def is_read(name: str) -> bool:
        bare = name.removeprefix("async_")
        return bare.startswith(_ADAPTER_READ_PREFIXES) or bare in _ADAPTER_READ_NAMES

    return frozenset(n for n in public if not is_read(n))


@dataclass(frozen=True)
class Reach:
    """One thing a read-only tool reaches that changes something."""

    tool: str
    what: str      # e.g. "printer command start_print"
    via: str       # the call chain, "tool -> helper -> helper"

    def __str__(self) -> str:
        return f"{self.tool}: {self.what} (via {self.via})"


def _is_kiln(obj: Any) -> bool:
    module = getattr(obj, "__module__", "") or ""
    return module == "kiln" or module.startswith(("kiln.", "kiln_pro"))


@lru_cache(maxsize=None)
def _tree(fn: types.FunctionType) -> ast.AST | None:
    try:
        return ast.parse(textwrap.dedent(inspect.getsource(fn)))
    except (OSError, TypeError, SyntaxError, IndentationError):
        return None


def _scope(fn: types.FunctionType, tree: ast.AST) -> dict[str, Any]:
    """Names visible inside *fn*: closure cells, globals, imports in its body."""
    names: dict[str, Any] = dict(fn.__globals__)
    for var, cell in zip(fn.__code__.co_freevars, fn.__closure__ or ()):
        try:
            names[var] = cell.cell_contents
        except ValueError:
            continue
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            for alias in node.names:
                try:
                    module = importlib.import_module(node.module)
                    names[alias.asname or alias.name] = getattr(module, alias.name)
                except Exception:  # noqa: BLE001 — an unimportable name is unseen
                    continue
        elif isinstance(node, ast.Import):
            for alias in node.names:
                try:
                    names[alias.asname or alias.name.split(".")[0]] = importlib.import_module(
                        alias.name if alias.asname else alias.name.split(".")[0]
                    )
                except Exception:  # noqa: BLE001
                    continue
    return names


def _resolve(expr: ast.expr, names: dict[str, Any]) -> Any:
    if isinstance(expr, ast.Name):
        return names.get(expr.id)
    if isinstance(expr, ast.Attribute):
        base = _resolve(expr.value, names)
        if isinstance(base, (types.ModuleType, type)):
            return getattr(base, expr.attr, None)
    return None


def _receiver_name(expr: ast.expr) -> str:
    if isinstance(expr, ast.Name):
        return expr.id
    if isinstance(expr, ast.Attribute):
        return expr.attr
    if isinstance(expr, ast.Call):
        return _receiver_name(expr.func)
    return ""


def _effects(tree: ast.AST, commands: frozenset[str]) -> list[str]:
    """What *tree*'s own code does that changes something, by call shape."""
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if _SQL_WRITE.match(node.value):
                found.append(f"SQL write {node.value.split()[0].upper()}")
            continue
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if isinstance(func, ast.Attribute) and name in commands:
            found.append(f"printer command {name}")
        if isinstance(func, ast.Attribute) and name in _STORE_WRITES:
            receiver = _receiver_name(func.value)
            # A writer is handed what it writes; ``store.record()`` with
            # nothing passed is the getter.  Only the emptying verbs take none.
            hands_something = bool(node.args or node.keywords) or name in _EMPTIES
            if _STORE_RECEIVER.search(receiver) and hands_something:
                found.append(f"store write {receiver}.{name}")
        if _SPENDS.search(name):
            found.append(f"spends an allowance ({name})")
        if (
            isinstance(func, ast.Attribute)
            and _REVEALS.search(name)
            and _REVEAL_RECEIVER.search(_receiver_name(func.value))
        ):
            found.append(f"reveals a stored secret ({_receiver_name(func.value)}.{name})")
    return found


def reach(tool: str, fn: Any, changing_tools: dict[Any, str]) -> list[Reach]:
    """Everything *fn* (a read-only tool) reaches that changes something.

    *changing_tools* maps each tool function that is NOT read-only to its
    name, so a read-only tool calling one directly is a finding too.
    """
    commands = printer_commands()
    found: list[Reach] = []
    seen: set[int] = set()

    def walk(current: Any, chain: list[str], depth: int) -> None:
        current = inspect.unwrap(current)
        if not isinstance(current, types.FunctionType) or id(current) in seen:
            return
        seen.add(id(current))
        tree = _tree(current)
        if tree is None:
            return
        via = " -> ".join(chain)
        for what in _effects(tree, commands):
            found.append(Reach(tool, what, via))
        if depth >= DEPTH:
            return
        names = _scope(current, tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            target = _resolve(node.func, names)
            target = inspect.unwrap(target) if callable(target) else target
            if target is None or target is current:
                continue
            if target in changing_tools:
                found.append(Reach(tool, f"calls the tool {changing_tools[target]}", via))
                continue
            if (
                isinstance(target, types.FunctionType)
                and "credential" in (target.__module__ or "")
                and _REVEALS.search(target.__name__)
            ):
                found.append(Reach(tool, f"reveals a stored secret ({target.__name__})", via))
                continue
            if isinstance(target, types.FunctionType) and _is_kiln(target):
                walk(target, [*chain, target.__name__], depth + 1)

    walk(fn, [tool], 0)
    return found


def read_only_tools(registry: dict[str, Any]) -> tuple[dict[str, Any], dict[Any, str]]:
    """(read-only tool name -> function, changing tool function -> name)."""
    read_only: dict[str, Any] = {}
    changing: dict[Any, str] = {}
    for name, tool in registry.items():
        annotations = getattr(tool, "annotations", None)
        fn = inspect.unwrap(getattr(tool, "fn", None))
        if fn is None:
            continue
        wire = annotations.model_dump(by_alias=True) if annotations is not None else {}
        if wire.get("readOnlyHint") is True:
            read_only[name] = fn
        else:
            changing[fn] = name
    return read_only, changing
