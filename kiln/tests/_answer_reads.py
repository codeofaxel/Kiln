"""Where the package reads ``error`` off an answer from Kiln's servers.

An answer's ``error`` may hold a sentence or a code word, so a door that
shows it can show a bare code, and a door that compares it stops working
the day the servers put a sentence there.  The one reader of a refusal is
:func:`kiln.served_answer.read_refusal`; this finds, from source, every
read of ``error`` that goes around it.

How an answer is followed, statically:

* it starts where a module on the roster of doors to Kiln's servers
  (``served_answer.HOSTED_DOORS`` and ``outbound_guard.SENDERS``) parses a
  reply (``resp.json()``, ``json.loads(resp.read())``), and at the served
  door's own calls (:data:`SERVED_CALLS`);
* it follows assignments, unpacking, ``for`` targets, ``with`` targets,
  ``x.get(...)`` and ``x[...]`` of an answer, ``dict(x)`` and ``{**x}``,
  and the reader's own wording helpers (an answer worded is still an
  answer);
* it follows a function that returns an answer to every call of it, in its
  own module and through ``from m import f`` / ``import m`` in any other,
  and an answer passed to a function of the same module into that
  function's parameter.

A finding is ``(module, function, line)``.  Honest bounds: an answer kept
on an object's attribute, in a closure's outer scope, or passed through a
function of another module as an argument is not followed.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
PACKAGE = SRC / "kiln"

#: The reader's home: the one module that reads ``error`` raw on purpose.
READER = "kiln.served_answer"

#: The served door's calls: each returns an answer from Kiln's servers.
SERVED_CALLS = frozenset({"_pro_api_call", "_anonymous_api_call", "_await_served_job", "ask_briefly"})

#: The reader's wording helpers: what they return is the answer they were
#: handed, worded.
WORDED = frozenset({"in_words", "tool_answer_in_words", "envelope_for_http"})

#: Copies of an answer that are still the answer.
_COPIES = frozenset({"dict", "copy", "deepcopy"})


def module_name(path: Path) -> str:
    parts = list(path.relative_to(SRC).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def roster(sources: dict[str, str]) -> set[str]:
    """The modules that talk to Kiln's servers themselves, read from the two
    rosters' own source (never imported, so a planted tree is judged by its
    own lists)."""
    found: set[str] = set()
    for module, name in ((READER, "HOSTED_DOORS"), ("kiln.outbound_guard", "SENDERS")):
        tree = ast.parse(sources[module])
        for node in tree.body:
            if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") == name:
                for key in node.value.keys:  # type: ignore[union-attr]
                    value = key.value  # type: ignore[union-attr]
                    found.add(value[:-3].replace("/", ".") if value.endswith(".py") else value)
    found.discard(READER)
    return found


def package_sources() -> dict[str, str]:
    return {module_name(p): p.read_text(encoding="utf-8") for p in sorted(PACKAGE.rglob("*.py"))}


def _dotted(node: ast.AST) -> tuple[str, ...]:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return tuple(reversed(parts))


def _own_nodes(fn: ast.AST) -> Iterator[ast.AST]:
    """Every node of *fn*'s body, not descending into functions defined in it."""
    stack = list(ast.iter_child_nodes(fn))
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            stack.extend(ast.iter_child_nodes(node))


def _names(target: ast.AST) -> list[str]:
    """The names a binding target binds: a name, or the names in a tuple or
    list of them.  ``x[k] = answer`` binds no name, and leaves ``x`` as it was."""
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, ast.Starred):
        return _names(target.value)
    if isinstance(target, (ast.Tuple, ast.List)):
        return [n for e in target.elts for n in _names(e)]
    return []


def _is_error_key(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value == "error"


@dataclass
class _Function:
    module: str
    name: str
    node: ast.AST
    params: list[str]
    calls: set[str] = field(default_factory=set)
    #: ``(value, target)`` for every binding in the body.
    binds: list[tuple[ast.AST, ast.AST]] = field(default_factory=list)
    returns: list[ast.AST] = field(default_factory=list)
    call_nodes: list[ast.Call] = field(default_factory=list)
    #: ``(line, receiver)`` for every read of ``error``.
    reads: list[tuple[int, ast.AST]] = field(default_factory=list)


@dataclass
class _Module:
    name: str
    tree: ast.Module
    on_roster: bool
    functions: dict[str, list[_Function]] = field(default_factory=dict)
    aliases: dict[str, str] = field(default_factory=dict)
    imported: dict[str, tuple[str, str]] = field(default_factory=dict)


def _note(fn: _Function, node: ast.AST) -> None:
    """Record what *node* means for the taint of *fn*."""
    if isinstance(node, ast.Call):
        dotted = _dotted(node.func)
        if dotted:
            fn.calls.add(dotted[-1])
        fn.call_nodes.append(node)
    if isinstance(node, ast.Assign):
        fn.binds += [(node.value, t) for t in node.targets]
    elif isinstance(node, (ast.AnnAssign, ast.AugAssign, ast.NamedExpr)) and node.value is not None:
        fn.binds.append((node.value, node.target))
    elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
        fn.binds.append((node.iter, node.target))
    elif isinstance(node, ast.withitem) and node.optional_vars is not None:
        fn.binds.append((node.context_expr, node.optional_vars))
    elif isinstance(node, ast.Return) and node.value is not None:
        fn.returns.append(node.value)
    receiver = _error_read(node)
    if receiver is not None:
        fn.reads.append((node.lineno, receiver))


#: Modules already indexed, by ``(name, source, on the roster)``.
_INDEXED: dict[tuple[str, str, bool], _Module] = {}


class Scan:
    """One pass over *sources* (module name -> source text)."""

    def __init__(self, sources: dict[str, str]) -> None:
        self.on_roster = roster(sources)
        self._texts = {name: text for name, text in sources.items() if name != READER}
        self.modules: dict[str, _Module] = {}
        self.answers: dict[str, set[str]] = {}
        #: For a function that returns an answer: ``None`` when the whole
        #: return is one, else the tuple positions that are.
        self.shapes: dict[tuple[str, str], frozenset[int] | None] = {}
        self.tainted_params: dict[tuple[str, str], set[str]] = {}
        self._answer_names_anywhere: set[str] = set()
        self._reached: dict[int, _Function] = {}
        self._callers: dict[str, list[_Function]] = {}
        self._run()

    def _load(self, name: str) -> _Module | None:
        """*name* indexed (once per source text, across scans), with its
        functions filed by what they call."""
        if name in self.modules:
            return self.modules[name]
        text = self._texts.get(name)
        if text is None:
            return None
        key = (name, text, name in self.on_roster)
        mod = _INDEXED.get(key)
        if mod is None:
            try:
                mod = self._index(name, _parsed(text))
            except SyntaxError:
                return None
            _INDEXED[key] = mod
        self.modules[name] = mod
        self.answers[name] = set()
        for fns in mod.functions.values():
            for fn in fns:
                for called in fn.calls:
                    self._callers.setdefault(called, []).append(fn)
        return mod

    def _load_callers_of(self, name: str, defined_in: str = "") -> list[_Function]:
        """Load every module whose text calls *name* (and, for a function of
        *defined_in*, names that module, as an import of it must); the
        functions that call it."""
        needle = f"{name}("
        tail = defined_in.rsplit(".", 1)[-1]
        for module, text in self._texts.items():
            if module in self.modules or needle not in text:
                continue
            if not tail or tail in text:
                self._load(module)
        return list(self._callers.get(name, ()))

    # -- indexing ----------------------------------------------------------

    def _index(self, name: str, tree: ast.Module) -> _Module:
        """One walk of *tree*: each node goes to the innermost function
        holding it."""
        mod = _Module(name=name, tree=tree, on_roster=name in self.on_roster)
        stack: list[tuple[ast.AST, _Function | None]] = [(tree, None)]
        while stack:
            node, owner = stack.pop()
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                args = node.args
                params = [a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)]
                fn = _Function(module=name, name=node.name, node=node, params=params)
                mod.functions.setdefault(node.name, []).append(fn)
                stack.extend((child, fn) for child in ast.iter_child_nodes(node))
                continue
            if isinstance(node, ast.Import):
                for alias in node.names:
                    mod.aliases[alias.asname or alias.name.split(".")[0]] = (
                        alias.name if alias.asname else alias.name.split(".")[0]
                    )
            elif isinstance(node, ast.ImportFrom) and node.module:
                for alias in node.names:
                    local = alias.asname or alias.name
                    mod.aliases[local] = f"{node.module}.{alias.name}"
                    mod.imported[local] = (node.module, alias.name)
            if owner is not None:
                _note(owner, node)
            stack.extend((child, owner) for child in ast.iter_child_nodes(node))
        return mod

    # -- what an expression is ---------------------------------------------

    def _answer_key(self, mod: _Module, call: ast.Call) -> tuple[str, str] | None:
        """``(module, function)`` of the package function *call* resolves to,
        when that function returns an answer."""
        dotted = _dotted(call.func)
        if not dotted:
            return None
        last = dotted[-1]
        if len(dotted) == 1:
            if last in mod.functions:
                return (mod.name, last) if last in self.answers[mod.name] else None
            if last in mod.imported:
                where, real = mod.imported[last]
                return (where, real) if real in self.answers.get(where, ()) else None
            return None
        if dotted[0] in ("self", "cls") and len(dotted) == 2:
            return (mod.name, last) if last in self.answers[mod.name] else None
        where = mod.aliases.get(dotted[0])
        if where in self.answers and len(dotted) == 2 and last in self.answers[where]:
            return (where, last)
        return None

    def _shape(self, mod: _Module, call: ast.Call) -> frozenset[int] | None:
        """The tuple positions of *call*'s return that are answers, or
        ``None`` when the whole return is (or the call is no answer at all)."""
        dotted = _dotted(call.func)
        if dotted and dotted[-1] == "ask_briefly":
            return frozenset({0})
        key = self._answer_key(mod, call)
        return self.shapes.get(key) if key else None

    def _answer_call(self, mod: _Module, call: ast.Call, names: set[str]) -> bool:
        dotted = _dotted(call.func)
        if not dotted:
            return False
        last = dotted[-1]
        if last in SERVED_CALLS:
            return True
        if last in WORDED or (len(dotted) == 1 and last in _COPIES):
            return any(self._tainted(mod, a, names) for a in (*call.args, *(k.value for k in call.keywords)))
        if mod.on_roster and last == "json" and not call.args and len(dotted) >= 2:
            return True
        if mod.on_roster and dotted == ("json", "loads") and call.args:
            return any(
                isinstance(sub, ast.Call) and _dotted(sub.func)[-1:] in (("read",), ("decode",))
                for sub in ast.walk(call.args[0])
            )
        return self._answer_key(mod, call) is not None

    def _tainted(self, mod: _Module, expr: ast.AST | None, names: set[str]) -> bool:
        if expr is None:
            return False
        if isinstance(expr, ast.Name):
            return expr.id in names
        if isinstance(expr, ast.Call):
            if self._answer_call(mod, expr, names):
                return True
            func = expr.func
            return isinstance(func, ast.Attribute) and func.attr == "get" and self._tainted(mod, func.value, names)
        if isinstance(expr, ast.BoolOp):
            return any(self._tainted(mod, v, names) for v in expr.values)
        if isinstance(expr, ast.IfExp):
            return self._tainted(mod, expr.body, names) or self._tainted(mod, expr.orelse, names)
        if isinstance(expr, ast.Subscript):
            return self._tainted(mod, expr.value, names)
        if isinstance(expr, (ast.Tuple, ast.List)):
            return any(self._tainted(mod, e, names) for e in expr.elts)
        if isinstance(expr, ast.Dict):
            return any(k is None and self._tainted(mod, v, names) for k, v in zip(expr.keys, expr.values, strict=True))
        if isinstance(expr, (ast.Await, ast.NamedExpr, ast.Starred)):
            return self._tainted(mod, expr.value, names)
        return False

    def _taint_of(self, mod: _Module, fn: _Function) -> set[str]:
        names = set(self.tainted_params.get((mod.name, fn.name), set()))
        if not names and not fn.calls & self._sources(mod):
            return names
        while True:
            grown = False
            for value, target in fn.binds:
                if not self._tainted(mod, value, names):
                    continue
                hit = _names(target)
                if isinstance(target, (ast.Tuple, ast.List)) and isinstance(value, ast.Call):
                    shape = self._shape(mod, value)
                    if shape is not None and not any(isinstance(e, ast.Starred) for e in target.elts):
                        hit = [n for i, e in enumerate(target.elts) if i in shape for n in _names(e)]
                for name in hit:
                    if name not in names:
                        names.add(name)
                        grown = True
            if not grown:
                return names

    def _sources(self, mod: _Module) -> set[str]:
        """Every name a call that may return an answer can end with, in *mod*."""
        out = set(SERVED_CALLS) | self._answer_names_anywhere
        if mod.on_roster:
            out |= {"json", "loads"}
        return out

    # -- the fixpoint ------------------------------------------------------

    def _run(self) -> None:
        queue: list[_Function] = []
        for name in sorted(self.on_roster):
            mod = self._load(name)
            if mod is not None:
                queue += [fn for fns in mod.functions.values() for fn in fns]
        for served in sorted(SERVED_CALLS):
            queue += self._load_callers_of(served)
        while queue:
            fn = queue.pop()
            self._reached[id(fn)] = fn
            mod = self.modules[fn.module]
            names = self._taint_of(mod, fn)
            if not names and not fn.calls & self._sources(mod):
                continue
            whole = False
            positions: set[int] = set()
            for value in fn.returns:
                if isinstance(value, ast.Tuple):
                    positions |= {i for i, e in enumerate(value.elts) if self._tainted(mod, e, names)}
                elif self._tainted(mod, value, names):
                    whole = True
            if whole or positions:
                key = (mod.name, fn.name)
                shape = None if whole or self.shapes.get(key, frozenset()) is None else frozenset(
                    positions | set(self.shapes.get(key) or ())
                )
                if key not in self.shapes or self.shapes[key] != shape:
                    self.shapes[key] = shape
                    self.answers[mod.name].add(fn.name)
                    self._answer_names_anywhere.add(fn.name)
                    queue.extend(self._load_callers_of(fn.name, mod.name))
            for node in fn.call_nodes:
                dotted = _dotted(node.func)
                local = dotted[-1] if dotted and (len(dotted) == 1 or dotted[0] in ("self", "cls")) else ""
                if local not in mod.functions:
                    continue
                callee = mod.functions[local][0]
                offset = 1 if len(dotted) == 2 and callee.params[:1] in (["self"], ["cls"]) else 0
                handed = {
                    callee.params[i + offset]
                    for i, arg in enumerate(node.args)
                    if i + offset < len(callee.params) and self._tainted(mod, arg, names)
                }
                handed |= {k.arg for k in node.keywords if k.arg and self._tainted(mod, k.value, names)}
                if handed:
                    held = self.tainted_params.setdefault((mod.name, local), set())
                    if not handed <= held:
                        held |= handed
                        queue.extend(mod.functions[local])

    # -- what is found ------------------------------------------------------

    def raw_reads(self) -> list[tuple[str, str, int]]:
        """Every read of ``error`` off an answer, outside the reader."""
        found: set[tuple[str, str, int]] = set()
        for fn in self._reached.values():
            mod = self.modules[fn.module]
            names = self._taint_of(mod, fn)
            for line, receiver in fn.reads:
                if self._tainted(mod, receiver, names):
                    found.add((mod.name, fn.name, line))
        return sorted(found)


def _error_read(node: ast.AST) -> ast.AST | None:
    """The receiver when *node* reads ``error`` (``x["error"]``,
    ``x.get("error")``, ``x.pop("error")``), else ``None``."""
    if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load) and _is_error_key(node.slice):
        return node.value
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in ("get", "pop")
        and node.args
        and _is_error_key(node.args[0])
    ):
        return node.func.value
    return None


#: A code word, spelled out here so the guard does not lean on the reader.
_CODE_WORD = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)*")


def _code_words(node: ast.AST) -> list[str]:
    """The code words *node* spells (a string, or a tuple / list / set of them)."""
    values: Iterable[ast.AST] = node.elts if isinstance(node, (ast.Tuple, ast.List, ast.Set)) else (node,)
    return [
        v.value for v in values
        if isinstance(v, ast.Constant) and isinstance(v.value, str) and _CODE_WORD.fullmatch(v.value)
    ]


def code_comparisons(sources: dict[str, str]) -> list[tuple[str, str, int]]:
    """Every comparison of an ``error`` with a code word, anywhere in the
    package outside the reader: a comparison that stops matching the day
    ``error`` holds the sentence."""
    found: list[tuple[str, str, int]] = []
    for name, text in sources.items():
        if name == READER or ('"error"' not in text and "'error'" not in text):
            continue
        stack: list[tuple[ast.AST, str]] = [(_parsed(text), "")]
        while stack:
            node, owner = stack.pop()
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                owner = node.name
            elif isinstance(node, ast.Compare) and owner:
                sides = [node.left, *node.comparators]
                if any(_error_read(s) is not None for s in sides) and any(_code_words(s) for s in sides):
                    found.append((name, owner, node.lineno))
            stack.extend((child, owner) for child in ast.iter_child_nodes(node))
    return sorted(found)


_PARSED: dict[str, ast.Module] = {}


def _parsed(text: str) -> ast.Module:
    """*text* parsed, once per source text."""
    tree = _PARSED.get(text)
    if tree is None:
        tree = _PARSED[text] = ast.parse(text)
    return tree
