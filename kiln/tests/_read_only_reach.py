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
judge with one set of rules, and each judges its own code: public Kiln's
check walks only what a plain install has (``public_only``), so it answers
the same on a machine with kiln-pro installed as in public CI, and kiln-pro's
twin judges kiln-pro's tools plus what kiln-pro adds to public ones
(:func:`added_by_pro`), which runs only where kiln-pro is installed.

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
#: ``save_ledger``, or a bare ``_write`` / ``_save``) writes one; leading
#: underscores do not hide it.  A bare ``_purge`` or ``_delete`` is not
#: counted: those name actions too (a motion plan's filament purge).  The bare verbs and the ``marker`` / ``file`` /
#: ``json`` / ``cache`` endings joined 2026-10-31, when the consent-window
#: store (``_write``) and the cool-down note (``_write_marker``) were found
#: unseen; a cache is counted too and decided by name below, because a
#: writer the walk cannot see is one nobody decided.
_BARE_STORE_WRITE = re.compile(
    r"^_*(?:(write|save|persist|upsert)"
    r"|(write|save|persist|upsert|delete|purge)_(\w+_)?"
    r"(store|ledger|record|records|registry|state|db|history|sidecar|outbox|marker|file|json|cache))$"
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
def acted_on_events(public_only: bool = False) -> frozenset[str]:
    """Event types a Kiln handler subscribes to by name on the server's bus
    (*public_only*: a public Kiln handler, the ones a plain install has)."""
    import kiln.server as server

    ours = _is_public if public_only else _is_kiln
    bus = server._get_event_bus()
    return frozenset(
        getattr(kind, "name", str(kind))
        for kind, handlers in bus._handlers.items()
        if any(ours(handler) for handler, _ in handlers)
    )


@lru_cache(maxsize=None)
def printer_commands(public_only: bool = False) -> frozenset[str]:
    def is_read(name: str) -> bool:
        bare = name.removeprefix("async_")
        return bare.startswith(_ADAPTER_READ_PREFIXES) or bare in _ADAPTER_READ_NAMES

    names: set[str] = set()
    for cls in _adapter_classes():
        if public_only and _is_pro(cls):
            continue
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
    #: The same chain by full name (``module.function``), for a decision
    #: about one of two functions that share a short name.
    where: tuple[str, ...] = ()

    def __str__(self) -> str:
        return f"{self.tool}: {self.what} (via {self.via})"


def _full_name(fn: Any) -> str:
    return f"{getattr(fn, '__module__', '')}.{getattr(fn, '__qualname__', getattr(fn, '__name__', ''))}"


def decided_by(hit: Reach, decided: dict[str, str]) -> str | None:
    """The key in *decided* that covers *hit*, or ``None``.

    A key is ``"<name>: <what>"``: *what* as the hit says it, and *name* the
    tool or any function on the hit's chain, by its own name or, where two
    functions share one (``engagement.observe``, ``hardware_stops.observe``),
    by its full ``module.function`` name.  Deciding a writer once therefore
    covers every tool that reaches it, and only that writer.
    """
    steps = set(hit.via.split(" -> ")) | set(hit.where)
    for key in decided:
        name, _, what = key.partition(": ")
        if what == hit.what and name in steps:
            return key
    return None


def _is_kiln(obj: Any) -> bool:
    module = getattr(obj, "__module__", "") or ""
    return module == "kiln" or module.startswith(("kiln.", "kiln_pro"))


def _is_pro(obj: Any) -> bool:
    """Code, a class or a value that exists only where kiln-pro is installed."""
    if isinstance(obj, types.ModuleType):
        return obj.__name__.startswith("kiln_pro")
    return (getattr(obj, "__module__", "") or "").startswith("kiln_pro")


def _is_public(obj: Any) -> bool:
    return _is_kiln(obj) and not _is_pro(obj)


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


def _scope(fn: types.FunctionType, tree: ast.AST, public_only: bool = False) -> dict[str, Any]:
    """Names visible inside *fn*: closure cells, globals, imports in its body.
    *public_only* leaves out everything kiln-pro provides, as a plain install
    (and public CI) would."""
    names: dict[str, Any] = dict(fn.__globals__)
    for var, cell in zip(fn.__code__.co_freevars, fn.__closure__ or ()):
        try:
            names[var] = cell.cell_contents
        except ValueError:
            continue
    for node in ast.walk(tree):
        if public_only and (
            (isinstance(node, ast.ImportFrom) and (node.module or "").startswith("kiln_pro"))
            or (isinstance(node, ast.Import) and any(a.name.startswith("kiln_pro") for a in node.names))
        ):
            continue
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            for alias in node.names:
                try:
                    value = _import_from(node.module, alias.name)
                except Exception:  # noqa: BLE001 — an unimportable name is unseen
                    continue
                names[alias.asname or alias.name] = value
        elif isinstance(node, ast.Import):
            for alias in node.names:
                try:
                    # The whole dotted path, so ``a.b.c`` resolves later as an
                    # attribute chain whatever else has been imported.
                    full = importlib.import_module(alias.name)
                    names[alias.asname or alias.name.split(".")[0]] = (
                        full if alias.asname else importlib.import_module(alias.name.split(".")[0])
                    )
                except Exception:  # noqa: BLE001
                    continue
    if public_only:
        # A name bound to kiln-pro (a guarded import at module level, a
        # compatibility shim under a ``kiln.`` path) is absent there too.
        names = {name: value for name, value in names.items() if not _is_pro(value)}
    return names


def _import_from(module_name: str, attr: str) -> Any:
    """What ``from module_name import attr`` binds, the way Python binds it.

    ``from kiln import consent_windows`` names a SUBMODULE, which is an
    attribute of ``kiln`` only once something has imported it.  Reading the
    attribute alone made the walk depend on test order: run by itself it
    could not see into ``kiln.consent_windows`` and called two status tools
    clean; run after a test that imported it, it found their writes
    (2026-10-31).  Python falls back to importing the submodule, and so
    does this.
    """
    module = importlib.import_module(module_name)
    try:
        return getattr(module, attr)
    except AttributeError:
        return importlib.import_module(f"{module_name}.{attr}")


def _resolve(expr: ast.expr, names: dict[str, Any]) -> Any:
    if isinstance(expr, ast.Name):
        return names.get(expr.id)
    if isinstance(expr, ast.Attribute):
        base = _resolve(expr.value, names)
        if isinstance(base, type):
            return getattr(base, expr.attr, None)
        if isinstance(base, types.ModuleType):
            try:
                return _import_from(base.__name__, expr.attr)
            except Exception:  # noqa: BLE001 — not a name that module has
                return None
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


def _class_of(
    expr: ast.expr, names: dict[str, Any], local: dict[str, type], ours: Any = _is_kiln,
) -> type | None:
    """The Kiln class of the object *expr* evaluates to, when it can be named."""
    if isinstance(expr, ast.Name):
        if expr.id in local:
            return local[expr.id]
        value = names.get(expr.id)
        return type(value) if value is not None and ours(type(value)) else None
    if isinstance(expr, ast.Call):
        made = _resolve(expr.func, names)
        if isinstance(made, type):
            return made if ours(made) else None
        if callable(made):
            made = _returns(made)
            return made if made is not None and ours(made) else None
    return None


def _local_classes(
    fn: Any, tree: ast.AST, names: dict[str, Any], ours: Any = _is_kiln,
) -> dict[str, type]:
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
            and (cls := _class_of(node.value, names, local, ours)) is not None
        ):
            local[node.targets[0].id] = cls
    return local


def _methods(cls: type, name: str, ours: Any = _is_kiln) -> list[Any]:
    """Every Kiln implementation of *name* on *cls* and its subclasses."""
    found = []
    for klass in _with_subclasses(cls):
        if not ours(klass):
            continue
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


def _effects(
    tree: ast.AST, commands: frozenset[str], acted: frozenset[str] | None = None,
) -> list[str]:
    """What *tree*'s own code does that changes something, by call shape.
    *acted*: the event types a handler acts on (default: every Kiln handler)."""
    acted = acted_on_events() if acted is None else acted
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
            heard = sorted(kinds & acted) if kinds else ["an event of unknown type"]
            if heard:
                found.append(f"announces {', '.join(heard)} on the event bus, which Kiln acts on")
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
    node: ast.Call, names: dict[str, Any], local: dict[str, type], ours: Any = _is_kiln,
) -> list[Any]:
    """The functions *node* runs: what it calls, the methods it reaches on an
    object of a known class, a class's ``__init__``, and a Kiln function it
    hands to a thread, a timer or an executor."""
    targets: list[Any] = []
    direct = _resolve(node.func, names)
    if isinstance(direct, type) and ours(direct):
        targets.extend(_methods(direct, "__init__", ours))
    elif direct is not None:
        targets.append(direct)
    elif isinstance(node.func, ast.Attribute):
        cls = _class_of(node.func.value, names, local, ours)
        if cls is not None:
            targets.extend(_methods(cls, node.func.attr, ours))
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
        if isinstance(handed, types.FunctionType) and ours(handed):
            targets.append(handed)
    return targets


def reach(
    tool: str, fn: Any, changing_tools: dict[Any, str], *, public_only: bool = False,
) -> list[Reach]:
    """Everything *fn* (a read-only tool) reaches that changes something.

    *changing_tools* maps each tool function that is NOT read-only to its
    name, so a read-only tool calling one directly is a finding too.
    *public_only* walks the code a plain install has -- nothing kiln-pro
    provides -- so the answer is the same on a machine with kiln-pro
    installed as in public CI; what kiln-pro adds is :func:`added_by_pro`.
    """
    ours = _is_public if public_only else _is_kiln
    commands = printer_commands(public_only)
    acted = acted_on_events(public_only)
    found = [
        Reach(tool, what, tool, (_full_name(_unwrap(fn)),))
        for what in _writes_where_told(_unwrap(fn))
    ]
    # Keyed by whether the chain has passed through kiln-pro, so a public
    # function reached first through kiln-pro is still walked as public.
    # Without it the full walk's public paths would depend on which way the
    # walk happened to arrive, and :func:`added_by_pro` would misattribute.
    seen: set[tuple[int, bool]] = set()

    def walk(current: Any, chain: list[str], where: list[str], depth: int, crossed: bool) -> None:
        current = _unwrap(current)
        if not isinstance(current, types.FunctionType) or (id(current), crossed) in seen:
            return
        seen.add((id(current), crossed))
        tree = _tree(current)
        if tree is None:
            return
        via, full = " -> ".join(chain), tuple(where)
        for what in _effects(tree, commands, acted):
            found.append(Reach(tool, what, via, full))
        if depth >= DEPTH:
            return
        names = _scope(current, tree, public_only)
        local = _local_classes(current, tree, names, ours)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for target in _call_targets(node, names, local, ours):
                target = _unwrap(target) if callable(target) else target
                if target is None or target is current:
                    continue
                if public_only and _is_pro(target):
                    continue
                if target in changing_tools:
                    found.append(Reach(tool, f"calls the tool {changing_tools[target]}", via, full))
                    continue
                if (
                    isinstance(target, types.FunctionType)
                    and "credential" in (target.__module__ or "")
                    and _REVEALS.search(target.__name__)
                ):
                    found.append(Reach(tool, f"reveals a stored secret ({target.__name__})", via, full))
                    continue
                if isinstance(target, types.FunctionType) and ours(target):
                    walk(
                        target, [*chain, target.__name__], [*where, _full_name(target)],
                        depth + 1, crossed or _is_pro(target),
                    )

    walk(fn, [tool], [_full_name(_unwrap(fn))], 0, _is_pro(_unwrap(fn)))
    return found


def added_by_pro(tool: str, fn: Any, changing_tools: dict[Any, str]) -> list[Reach]:
    """What kiln-pro being installed adds to public tool *fn*: every finding
    of the full walk that the public-only walk does not make -- a path into
    kiln-pro's code, an event only a kiln-pro handler acts on, a command only
    a kiln-pro adapter has.  It runs only where kiln-pro is installed (Kiln's
    server), so kiln-pro's twin of this check judges it, and public Kiln's
    judges the rest."""
    public = {str(hit) for hit in reach(tool, fn, changing_tools, public_only=True)}
    return [hit for hit in reach(tool, fn, changing_tools) if str(hit) not in public]


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


#: Decisions about writers that live in public Kiln, ``"<name>: <what>"`` ->
#: why the write changes nothing the person owns.  Kept beside the rules so a
#: writer is decided once, where it lives: public Kiln's check reads them for
#: its tools and kiln-pro's twin for its own.  Public Kiln's check fails a key
#: no finding of its tools matches.
DECIDED: dict[str, str] = {
    "kiln.signin_offer.claim: store write _write_ledger": (
        "Kiln notes that it already offered to sign the person in, so it does "
        "not offer again on every answer."
    ),
    "kiln.server._check_rate_limit: SQL write INSERT": (
        "a security-log row, written only when a tool is over its rate limit; "
        "the log is Kiln's own record."
    ),
    "kiln.preview_evidence._update: store write _write_ledger": (
        "Kiln's own receipt of what a preview really showed, or why its 3D link "
        "failed, which the print check reads so a print starts only after the "
        "part was seen."
    ),
    "kiln.plate_state.remember_camera: store write _write_store": (
        "the first look through a camera notes that it is there, for a printer "
        "whose make and model do not already say so, or a camera set up beside "
        "it; a model known to ship with one writes nothing."
    ),
    "kiln.plate_state.hand_over_frame: store write _write_store": (
        "keeps the picture a look just took in Kiln's own folder and notes it "
        "is the one in front of the person, so what they say about the plate "
        "is recorded against that picture."
    ),
    "kiln.printers.engagement.observe: store write _write_store": (
        "a look at a printer that is mid-print is watching it, and below the "
        "fleet tier Kiln watches one printer at a time, so the look makes it "
        "that printer; the answer says so (working_with), and a look never "
        "spends a return (Adam, 2026-10-30)."
    ),
    "kiln.hardware_stops.observe: store write _write_store": (
        "a look at a print with planned hardware stops writes down only what it "
        "is the first to see (the stop paused at, a stop gone past and how, the "
        "finish), never changes one or spends a moment it only showed, so every "
        "door says the same about a missed magnet (pinned in "
        "test_hardware_stops.py; Adam, 2026-10-30)."
    ),
    "kiln.printers.engagement._expire: store write _write_store": (
        "frees the one-printer slot when the printer Kiln was working with has "
        "finished or stopped answering; it only ever gives the person room."
    ),
    "kiln.consent_windows._turn_off: store write _write": (
        "closes an always allow whose printer name now reaches a different "
        "machine, the same closure the next print start makes, so status never "
        "shows on what the print would find off; it only ever withdraws a "
        "permission, and tells the account the copy is closed."
    ),
    "kiln.consent_windows._turn_off: SQL write INSERT": (
        "the audit row for that closure, so the person can see when and why "
        "always allow turned itself off."
    ),
    "kiln.consent_windows.sync_account_always: store write _write": (
        "brings this computer's copy of always allow in line with the Kiln "
        "account, where the person turned it on or off; the account is the "
        "record, and a print start asks the account itself."
    ),
    "kiln.consent_windows.sync_account_always: SQL write INSERT": (
        "the audit row for a copy made or closed to match the account."
    ),
    "kiln.daily_stats._record_name_count: store write _write": (
        "Kiln's own count, kept on this computer, of which tools ran and which "
        "nudges were shown today; nothing the person made or set changes."
    ),
    "kiln.daily_stats.record_agent_host_fact: store write _write": (
        "Kiln's own count of which chat app reached it today, on this computer."
    ),
    "kiln.daily_stats.record_event: store write _write": (
        "Kiln's own daily count of slices on this computer."
    ),
    "kiln.monitor_twin.note_sliced: store write _write_json": (
        "remembers, paths only, which model a slice was drawn from, so a print "
        "started from it later can show that model; nothing is copied."
    ),
    "kiln.community_sync.fetch_community_insights: store write _write_cache": (
        "an hour's cache of a summary Kiln's servers computed, so the same "
        "question does not ask the network twice."
    ),
    "kiln.printers.bambu_hms_text._refresh_runner: store write _write_cache": (
        "a cache of the printer maker's published fault-code wording."
    ),
    "kiln.version_check._refresh_runner: store write _write_cache": (
        "a cache of the newest Kiln version on PyPI."
    ),
    "kiln.stage_cache._refresh_doc: store write _write": (
        "a cache of the 3D stage page Kiln's servers publish, so pictures still "
        "draw offline."
    ),
}
