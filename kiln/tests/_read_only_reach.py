"""What a read-only tool can reach that changes something.

A tool marked read-only may run without the person being asked, so it must
never reach a printer command, a write or delete on a saved record, a spent
allowance, a stored secret handed back in plain text, an announcement on the
event bus, or another tool that changes things, and it must not write where
the caller tells it to.  The label is stated by hand at each definition, and a
hand-stated label can be wrong in a way no reader of the tool's own body
sees: ``auto_detect_nozzle`` read a printer's report and, two calls down,
refreshed the person's saved nozzle record (found 2026-10-28 by a security
review after every tool had been labelled).

So this walks each read-only tool's body and the Kiln functions it calls,
resolving names the way Python does when the tool runs: its closure, its
module's globals, and the imports inside its body.  It follows a call on an
object when it can name the object's class (``self``, a value a Kiln
function's return annotation names, a class it builds), every implementation
of that method on Kiln's subclasses, a function handed to a thread, a timer
or an executor, and a printer adapter's command gate, which reads and
writes the one-printer engagement.  It is shared by public Kiln's
``test_read_only_tools_change_nothing.py`` and kiln-pro's twin, so both repos
judge with one set of rules.

The first version of this walk missed three real findings a review lap then
found (2026-10-28): a failure announced from caller-supplied readings that
ended a live print's watchdog (a method on an engine object, then an event
publish), status reads that move the one-printer engagement (an adapter's
command gate, a leading-underscore allowance, a bare ``_write_store``), and
previews that wrote over a file the caller named.  Each is a rule below.

Honest bounds: a call on an object whose class nothing names, a lambda, and a
callable built at runtime are unseen; a store is recognised by its receiver's
or function's name; files a tool writes to a temporary or preview location of
its own are allowed (rendering a preview is read-only by
``kiln.tool_annotations``' own definition), so only a location the CALLER
names is judged, and only by its parameter's name; a file deleted with
``unlink`` is unseen (in practice Kiln's own cache and temp clean-up); and a
request to Kiln's servers is not judged, because served read-only answers are
requests too.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import pkgutil
import re
import textwrap
import types
import typing
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from kiln.printers.base import PrinterAdapter

#: How many calls deep the walk follows from a tool's own body.  Four missed
#: the engagement gate (tool -> adapter read -> gate -> allowance -> store).
DEPTH = 8

#: PrinterAdapter methods that only read.  Every other public method of the
#: adapter, or of any adapter subclass, changes the printer, so it is a command
#: (derived from the classes, so a command a brand's adapter grows is covered
#: the day it lands).
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
    # Kiln's side only: opening or finding a connection, switching Kiln's own
    # push listener, reading a colour or a file Kiln already holds, and
    # packing a file on this computer.
    "connect", "discover", "enable_push_monitoring", "disable_push_monitoring",
    "active_filament_color", "filament_count_3mf", "wrap_gcode_as_3mf",
})

#: A call on one of these receivers with one of these methods writes a store.
_STORE_RECEIVER = re.compile(r"(store|backend|ledger|registry|outbox|repo|_db|^db)$", re.I)
_STORE_WRITES = frozenset({
    "set", "save", "delete", "upsert", "insert", "put", "remove", "update",
    "add", "record", "write", "clear", "purge", "drop",
})
_EMPTIES = frozenset({"clear", "purge", "drop"})
#: A plain call to a function named like a store writer (``_write_store``,
#: ``save_ledger``) writes one; leading underscores do not hide it.
_BARE_STORE_WRITE = re.compile(
    r"^_*(write|save|persist|upsert|delete|purge)_(\w+_)?"
    r"(store|ledger|record|records|registry|state|db|history|sidecar|outbox)$"
)
_SPENDS = re.compile(r"^_*(check_\w*quota$|check_and_increment|spend_|consume_|charge_)")
#: A call into the credential store's plaintext readers hands a secret back.
_REVEALS = re.compile(r"^(retrieve|reveal|decrypt)", re.I)
_REVEAL_RECEIVER = re.compile(r"(cred|vault|secret)", re.I)
_SQL_WRITE = re.compile(
    r"^\s*(INSERT\s+(OR\s+\w+\s+)?INTO|REPLACE\s+INTO|UPDATE\s+\S+\s+SET|DELETE\s+FROM)\b"
)
#: Publishing on the event bus reaches every subscriber.  An event type a Kiln
#: handler subscribes to by name is acted on (a print-ended event retires that
#: printer's watchdog and starts its heater cooldown); one only the wildcard
#: listeners hear is saved and sent to the person's webhooks, which tells them
#: something and changes nothing.  An event whose type the walk cannot read
#: counts as acted on.
_PUBLISHES = frozenset({"publish", "publish_batch"})
_BUS_RECEIVER = re.compile(r"(bus|events?)$", re.I)
_FIRES_HOOKS = frozenset({"emit_post", "emit_pre"})
#: Calls that run a function they are handed, now or on another thread.  A
#: function handed to anything else (a hook registered for later, a sort key)
#: is not run by the read itself.
_LAUNCHERS = {
    "Thread": "target", "Timer": 1, "submit": 0, "to_thread": 0,
    "run_in_executor": 1, "start_new_thread": 0, "call_soon": 0,
    "call_soon_threadsafe": 0, "call_later": 1,
}
#: A parameter naming where the tool writes its output.  The caller chooses
#: the place, so whatever sits there is theirs.
_OUTPUT_PARAM = re.compile(
    r"^(output|out|dest|destination|save|export|bundle|render)_(path|dir|directory|file|folder)$"
)


def _adapter_classes() -> tuple[type, ...]:
    """PrinterAdapter and every subclass, with every adapter module imported."""
    import kiln.printers

    for module in pkgutil.iter_modules(kiln.printers.__path__):
        try:
            importlib.import_module(f"kiln.printers.{module.name}")
        except Exception:  # noqa: BLE001 — an adapter whose deps are absent is unseen
            continue
    return _with_subclasses(PrinterAdapter)


@lru_cache(maxsize=None)
def acted_on_events() -> frozenset[str]:
    """Event types a Kiln handler subscribes to by name on the server's bus."""
    import kiln.server as server

    bus = server._get_event_bus()
    return frozenset(
        getattr(kind, "name", str(kind))
        for kind, handlers in bus._handlers.items()
        if any(_is_kiln(handler) for handler, _ in handlers)
    )


@lru_cache(maxsize=None)
def printer_commands() -> frozenset[str]:
    def is_read(name: str) -> bool:
        bare = name.removeprefix("async_")
        return bare.startswith(_ADAPTER_READ_PREFIXES) or bare in _ADAPTER_READ_NAMES

    names: set[str] = set()
    for cls in _adapter_classes():
        names.update(
            n for n in vars(cls) if not n.startswith("_") and callable(getattr(cls, n, None))
        )
    names.update(n for n in dir(PrinterAdapter) if not n.startswith("_"))
    return frozenset(n for n in names if not is_read(n))


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


def _unwrap(fn: Any) -> Any:
    """*fn* without its decorators, except a printer adapter's command gate,
    which reads and writes the one-printer engagement and so is walked."""
    return inspect.unwrap(fn, stop=lambda f: getattr(f, "_kiln_engagement_wrapped", False))


@lru_cache(maxsize=None)
def _tree(fn: types.FunctionType) -> ast.AST | None:
    # Read through the code object: ``getsource`` on a function follows
    # ``__wrapped__`` and would hand back the original under a gate.
    try:
        return ast.parse(textwrap.dedent(inspect.getsource(fn.__code__)))
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


@lru_cache(maxsize=None)
def _with_subclasses(cls: type) -> tuple[type, ...]:
    """*cls* and every Kiln subclass of it, so a call on the base reaches
    each brand's own implementation."""
    found: list[type] = [cls]
    stack = [cls]
    while stack:
        for sub in stack.pop().__subclasses__():
            if sub not in found and _is_kiln(sub):
                found.append(sub)
                stack.append(sub)
    return tuple(found)


@lru_cache(maxsize=None)
def _returns(fn: Any) -> type | None:
    """The Kiln class *fn*'s return annotation names, if any."""
    try:
        hint = typing.get_type_hints(fn).get("return")
    except Exception:  # noqa: BLE001 — an unresolvable annotation names nothing
        return None
    for candidate in (hint, *typing.get_args(hint)):
        if isinstance(candidate, type) and _is_kiln(candidate):
            return candidate
    return None


def _class_of(expr: ast.expr, names: dict[str, Any], local: dict[str, type]) -> type | None:
    """The Kiln class of the object *expr* evaluates to, when it can be named."""
    if isinstance(expr, ast.Name):
        if expr.id in local:
            return local[expr.id]
        value = names.get(expr.id)
        return type(value) if value is not None and _is_kiln(type(value)) else None
    if isinstance(expr, ast.Call):
        made = _resolve(expr.func, names)
        if isinstance(made, type):
            return made if _is_kiln(made) else None
        if callable(made):
            return _returns(made)
    return None


def _local_classes(fn: Any, tree: ast.AST, names: dict[str, Any]) -> dict[str, type]:
    """Variables in *tree* whose class is known: ``self``/``cls`` inside a
    method, and ``x = Kiln()`` / ``x = kiln_fn()`` with a typed return."""
    local: dict[str, type] = {}
    outer, _, _ = fn.__qualname__.rpartition(".")
    owner = names.get(outer) if outer and "<locals>" not in outer else None
    if isinstance(owner, type):
        local["self"] = local["cls"] = owner
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and (cls := _class_of(node.value, names, local)) is not None
        ):
            local[node.targets[0].id] = cls
    return local


def _methods(cls: type, name: str) -> list[Any]:
    """Every Kiln implementation of *name* on *cls* and its subclasses."""
    found = []
    for klass in _with_subclasses(cls):
        attr = inspect.getattr_static(klass, name, None)
        attr = getattr(attr, "__func__", attr)
        if isinstance(attr, types.FunctionType) and attr not in found:
            found.append(attr)
    return found


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
        if isinstance(func, ast.Name) and _BARE_STORE_WRITE.match(name):
            found.append(f"store write {name}")
        if _SPENDS.search(name):
            found.append(f"spends an allowance ({name})")
        if (
            isinstance(func, ast.Attribute)
            and _REVEALS.search(name)
            and _REVEAL_RECEIVER.search(_receiver_name(func.value))
        ):
            found.append(f"reveals a stored secret ({_receiver_name(func.value)}.{name})")
        if (
            isinstance(func, ast.Attribute)
            and name in _PUBLISHES
            and _BUS_RECEIVER.search(_receiver_name(func.value))
        ):
            kinds = _event_kinds(tree)
            acted = sorted(kinds & acted_on_events()) if kinds else ["an event of unknown type"]
            if acted:
                found.append(f"announces {', '.join(acted)} on the event bus, which Kiln acts on")
        if name in _FIRES_HOOKS:
            found.append(f"fires the account's hooks ({name})")
    return found


def _event_kinds(tree: ast.AST) -> set[str]:
    """The ``EventType.<NAME>`` members a function's code names."""
    return {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "EventType"
    }


def _writes_where_told(fn: Any) -> list[str]:
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return []
    return [f"writes where the caller says ({p})" for p in params if _OUTPUT_PARAM.match(p)]


def _call_targets(
    node: ast.Call, names: dict[str, Any], local: dict[str, type]
) -> list[Any]:
    """The functions *node* runs: what it calls, the methods it reaches on an
    object of a known class, a class's ``__init__``, and a Kiln function it
    hands to a thread, a timer or an executor."""
    targets: list[Any] = []
    direct = _resolve(node.func, names)
    if isinstance(direct, type) and _is_kiln(direct):
        targets.extend(_methods(direct, "__init__"))
    elif direct is not None:
        targets.append(direct)
    elif isinstance(node.func, ast.Attribute):
        cls = _class_of(node.func.value, names, local)
        if cls is not None:
            targets.extend(_methods(cls, node.func.attr))
    launcher = _receiver_name(node.func)
    slot = _LAUNCHERS.get(launcher)
    if isinstance(slot, int) and len(node.args) > slot:
        handed_off = [node.args[slot]]
    elif isinstance(slot, str):
        handed_off = [k.value for k in node.keywords if k.arg == slot]
    else:
        handed_off = []
    for arg in handed_off:
        handed = _resolve(arg, names) if isinstance(arg, (ast.Name, ast.Attribute)) else None
        if isinstance(handed, types.FunctionType) and _is_kiln(handed):
            targets.append(handed)
    return targets


def reach(tool: str, fn: Any, changing_tools: dict[Any, str]) -> list[Reach]:
    """Everything *fn* (a read-only tool) reaches that changes something.

    *changing_tools* maps each tool function that is NOT read-only to its
    name, so a read-only tool calling one directly is a finding too.
    """
    commands = printer_commands()
    found = [Reach(tool, what, tool) for what in _writes_where_told(_unwrap(fn))]
    seen: set[int] = set()

    def walk(current: Any, chain: list[str], depth: int) -> None:
        current = _unwrap(current)
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
        local = _local_classes(current, tree, names)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for target in _call_targets(node, names, local):
                target = _unwrap(target) if callable(target) else target
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
