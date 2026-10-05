#!/usr/bin/env python3
"""Every import of Kiln's own code names something that exists.

The 2026-03-20 change that moved the ordering code out of public Kiln left
seven of the twelve ``kiln order`` terminal commands, and ``kiln
fulfillment-materials``, importing ``kiln.fulfillment``, a package no tree
has held since.  Nothing noticed for six months, and both
reasons are general:

* The imports sit inside the command bodies.  Importing the CLI and
  collecting the tests both succeed; the break waits for someone to type the
  command.
* The commands' tests are ``skipif(not _has_fulfillment, ...)``, where the
  flag is "``import kiln.fulfillment`` succeeds".  A test that skips when a
  module is missing, over a module that is missing everywhere, skips forever,
  and a skip is a line nobody reads.

So this reads every ``import`` and ``from ... import`` of a first-party
module (one whose top package is a tree handed to :func:`scan`), wherever it
sits: at module level, in a function, behind ``try/except ImportError``
(which makes a missing module silent rather than safe).  It fails:

* ``needs_private``   a public source file imports, outside ``try/except
  ImportError``, a module or name public Kiln does not have: one only the
  private package provides, or one that exists nowhere.  Every plain install
  crashes when that line runs: the ``kiln order`` commands' shape.
* ``missing_module``  a guarded import of a module that exists in no tree, a
  fallback that is always taken (judged only where both trees are present).
* ``skips_forever``   a test skips on a first-party module that exists in no
  tree (``pytest.importorskip``, or a guarded import), so it never runs.

A module whose top package is not a tree handed in is UNJUDGED and counted,
never passed: public Kiln's own run cannot see kiln-pro, so its imports of
kiln-pro are judged where both trees are present (kiln-pro's run of this
same script).

Findings that existed the day this landed are in :data:`KNOWN`, which may
only shrink: a known finding that is fixed is reported STALE until its entry
is deleted, and a new finding fails.

Usage::

    python3 scripts/audit_import_targets.py                  # public Kiln
    python3 scripts/audit_import_targets.py --tree kiln_pro=/path/kiln_pro
"""

from __future__ import annotations

import argparse
import ast
import sys
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PUBLIC_TREE = REPO / "kiln" / "src" / "kiln"
PUBLIC_TESTS = REPO / "kiln" / "tests"

#: Findings present when this check landed (2026-10-05), as
#: ``(path relative to its tree's parent, module, name)``; ``name`` is ``""``
#: for a missing module.  Shrink only.
KNOWN: frozenset[tuple[str, str, str]] = frozenset({
    # Routing a print across printers (the fleet router moved to the private
    # package); reached from the print command's routing option.
    ("kiln/src/kiln/cli/main.py", "kiln.job_router", ""),
})

#: The two first-party top packages: public Kiln, and the private package.
PUBLIC_TOP = "kiln"
PRIVATE_TOP = "kiln_pro"

_IMPORT_ERRORS = {"ImportError", "ModuleNotFoundError", "Exception", "BaseException"}


@dataclass(frozen=True)
class Finding:
    kind: str
    path: str
    line: int
    module: str
    name: str
    guarded: bool

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.path, self.module, self.name)

    def __str__(self) -> str:
        what = f"{self.module}.{self.name}" if self.name else self.module
        how = " (inside try/except, so it fails silently)" if self.guarded else ""
        return f"{self.kind:14s} {self.path}:{self.line}  {what}{how}"


class _Index:
    """What a set of trees holds: every module, and each module's top-level names."""

    def __init__(self, trees: dict[str, Path], provided: dict[str, str] | None = None) -> None:
        self.trees = {top: Path(root) for top, root in trees.items()}
        #: Every first-party top package is in hand, so a module found in no
        #: tree exists nowhere (kiln-pro's run of this script).
        self.complete = {PUBLIC_TOP, PRIVATE_TOP} <= set(self.trees)
        #: ``kiln.*`` paths the private package registers at import time,
        #: mapped to the module that answers them (kiln_pro's compat shims).
        self.shims = dict(provided or {})
        self.files: dict[str, Path] = {}
        for top, root in self.trees.items():
            for path in root.rglob("*.py"):
                if "__pycache__" in path.parts:
                    continue
                rel = path.relative_to(root).with_suffix("")
                parts = [top, *rel.parts]
                if parts[-1] == "__init__":
                    parts = parts[:-1]
                self.files[".".join(parts)] = path
            # A folder of modules with no __init__.py is still importable.
            for folder in [root, *[p for p in root.rglob("*") if p.is_dir()]]:
                if "__pycache__" in folder.parts:
                    continue
                dotted = ".".join([top, *folder.relative_to(root).parts])
                self.files.setdefault(dotted, folder)
        self._names: dict[str, set[str] | None] = {}

    def judged(self, module: str) -> bool:
        return module.split(".")[0] in self.trees

    def provided(self, module: str) -> bool:
        """True when *module* exists only once the private package is
        imported: a compat shim, or anything under ``kiln_pro``."""
        for alias, real in self.shims.items():
            if module == alias or module.startswith(alias + "."):
                rest = module[len(alias):]
                return self.exists(real + rest) if self.judged(real) else True
        return module.split(".")[0] == PRIVATE_TOP and self.exists(module)

    def is_public(self, path: Path) -> bool:
        root = self.trees.get(PUBLIC_TOP)
        return root is not None and root in path.parents

    def exists(self, module: str) -> bool:
        return module in self.files

    def names(self, module: str, _depth: int = 0) -> set[str] | None:
        """Top-level names *module* defines; ``None`` when they cannot be
        known statically (a module ``__getattr__``, a folder, a parse error)."""
        if module in self._names:
            return self._names[module]
        path = self.files.get(module)
        found: set[str] | None
        if path is None or path.is_dir():
            found = None
        else:
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (OSError, SyntaxError, UnicodeDecodeError):
                tree = None
            found = None if tree is None else self._top_level(tree, module, _depth)
        self._names[module] = found
        return found

    def _top_level(self, tree: ast.Module, module: str, depth: int) -> set[str] | None:
        names: set[str] = set()
        package = module if self.files[module].name == "__init__.py" else module.rpartition(".")[0]
        literals = _string_lists(tree)
        exported = _globals_loop_names(tree, literals)
        if exported is None:
            return None
        names |= exported

        def visit(body: list[ast.stmt]) -> bool:
            for node in body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    if node.name == "__getattr__":
                        return False
                    names.add(node.name)
                elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    for target in targets:
                        for sub in ast.walk(target):
                            if isinstance(sub, ast.Name):
                                names.add(sub.id)
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        names.add(alias.asname or alias.name.split(".")[0])
                elif isinstance(node, ast.ImportFrom):
                    for alias in node.names:
                        if alias.name == "*":
                            source = _absolute(node, package)
                            more = self.names(source, depth + 1) if depth < 5 and self.judged(source) else None
                            if more is None:
                                return False
                            names.update(more)
                        else:
                            names.add(alias.asname or alias.name)
                elif isinstance(node, (ast.For, ast.AsyncFor, ast.With, ast.AsyncWith)):
                    for sub in ast.walk(getattr(node, "target", None) or ast.Pass()):
                        if isinstance(sub, ast.Name):
                            names.add(sub.id)
                    if not visit(node.body):
                        return False
                elif isinstance(node, ast.If):
                    if not visit(node.body) or not visit(node.orelse):
                        return False
                elif isinstance(node, ast.Try):
                    blocks = [node.body, node.orelse, node.finalbody]
                    blocks += [h.body for h in node.handlers]
                    if not all(visit(b) for b in blocks):
                        return False
            return True

        return names if visit(tree.body) else None


def _string_lists(tree: ast.Module) -> dict[str, list[str]]:
    """Module-level ``NAME = [...]`` / ``(...)`` lists made only of strings."""
    found: dict[str, list[str]] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, (ast.List, ast.Tuple)):
            items = node.value.elts
            if items and all(isinstance(e, ast.Constant) and isinstance(e.value, str) for e in items):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        found[target.id] = [e.value for e in items]
    return found


def _globals_loop_names(tree: ast.Module, literals: dict[str, list[str]]) -> set[str] | None:
    """Names a module binds through ``globals()``.

    The one shape read is a loop over a literal list of names, binding each
    through ``globals()[name] = ...`` (public Kiln's server re-exports the
    tools that moved into plugins that way).  Any other write through
    ``globals()`` means the module's names cannot be known, and ``None``
    says so, so nothing is reported against them.
    """
    names: set[str] = set()
    understood: set[int] = set()
    for loop in ast.walk(tree):
        if not (isinstance(loop, ast.For) and isinstance(loop.target, ast.Name)):
            continue
        if isinstance(loop.iter, ast.Name):
            items = literals.get(loop.iter.id)
        elif isinstance(loop.iter, (ast.List, ast.Tuple)):
            items = [e.value for e in loop.iter.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
        else:
            items = None
        for node in ast.walk(loop):
            if _globals_write(node) is not None and items is not None:
                key = _globals_write(node)
                if isinstance(key, ast.Name) and key.id == loop.target.id:
                    names.update(items)
                    understood.add(id(node))
    for node in ast.walk(tree):
        if _globals_write(node) is not None and id(node) not in understood:
            return None
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "update"
            and _is_globals_call(node.func.value)
        ):
            return None
    return names


def _is_globals_call(node: ast.expr) -> bool:
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "globals"


def _globals_write(node: ast.AST) -> ast.expr | None:
    """The key of a ``globals()[key] = ...`` assignment, else ``None``."""
    if isinstance(node, ast.Assign):
        for target in node.targets:
            if isinstance(target, ast.Subscript) and _is_globals_call(target.value):
                return target.slice
    return None


def _first_party(module: str) -> bool:
    return module.split(".")[0] in (PUBLIC_TOP, PRIVATE_TOP)


def _absolute(node: ast.ImportFrom, package: str) -> str:
    if not node.level:
        return node.module or ""
    base = package.split(".")
    if node.level > 1:
        base = base[: len(base) - (node.level - 1)]
    return ".".join([*base, node.module] if node.module else base)


def _guards_import(handler: ast.ExceptHandler) -> bool:
    if handler.type is None:
        return True
    kinds = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    return any(isinstance(k, (ast.Name, ast.Attribute)) and (k.id if isinstance(k, ast.Name) else k.attr) in _IMPORT_ERRORS for k in kinds)


def _suppresses_import(expr: ast.expr) -> bool:
    """``contextlib.suppress(ImportError)`` (or a broader error) as a guard."""
    if not (isinstance(expr, ast.Call) and expr.args):
        return False
    func = expr.func
    named = (isinstance(func, ast.Name) and func.id == "suppress") or (
        isinstance(func, ast.Attribute) and func.attr == "suppress"
    )
    return named and any(
        isinstance(a, (ast.Name, ast.Attribute)) and (a.id if isinstance(a, ast.Name) else a.attr) in _IMPORT_ERRORS
        for a in expr.args
    )


def _is_type_checking(test: ast.expr) -> bool:
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


def _imports(tree: ast.AST):
    """``(node, guarded)`` for every import outside ``if TYPE_CHECKING:``."""

    def walk(node: ast.AST, guarded: bool):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.If) and _is_type_checking(child.test):
                for sub in child.orelse:
                    yield from walk(sub, guarded)
                continue
            if isinstance(child, (ast.With, ast.AsyncWith)) and any(
                _suppresses_import(item.context_expr) for item in child.items
            ):
                for stmt in child.body:
                    if isinstance(stmt, (ast.Import, ast.ImportFrom)):
                        yield stmt, True
                    yield from walk(stmt, True)
                continue
            if isinstance(child, ast.Try):
                inner = guarded or any(_guards_import(h) for h in child.handlers)
                for stmt in child.body:
                    if isinstance(stmt, (ast.Import, ast.ImportFrom)):
                        yield stmt, inner
                    yield from walk(stmt, inner)
                for part in (*child.handlers, *child.orelse, *child.finalbody):
                    yield from walk(part, guarded)
                continue
            if isinstance(child, (ast.Import, ast.ImportFrom)):
                yield child, guarded
            yield from walk(child, guarded)

    yield from walk(tree, False)


def _module_of(path: Path, index: _Index) -> str:
    for top, root in index.trees.items():
        try:
            rel = path.relative_to(root).with_suffix("")
        except ValueError:
            continue
        parts = [top, *rel.parts]
        return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)
    return ""


def _judge_import(
    index: _Index, path: Path, shown: str, line: int, target: str, name: str,
    guarded: bool, is_test: bool, unjudged: set[str],
) -> list[Finding]:
    """What one imported module (and name) says, or ``[]``."""
    if not _first_party(target):
        return []
    private = target.split(".")[0] == PRIVATE_TOP or (
        not index.exists(target)
        and any(target == alias or target.startswith(alias + ".") for alias in index.shims)
    )
    in_public = index.is_public(path) and not is_test
    if index.exists(target) and not private:
        if name and not index.exists(f"{target}.{name}"):
            defined = index.names(target)
            if defined is not None and name not in defined:
                if in_public and not guarded:
                    return [Finding("needs_private", shown, line, target, name, guarded)]
                # Guarded, or in a test: the private package may set the name
                # on the public module at runtime (it does, for the licence
                # manager), which no reading of public source can see.
                unjudged.add(f"{target}.{name}")
        return []
    # From here the module is not public Kiln's own: the private package
    # provides it, or nothing does.
    if in_public and not guarded:
        return [Finding("needs_private", shown, line, target, "", guarded)]
    if guarded and not is_test and not index.is_public(path):
        # The private package's own guarded imports are judged by its
        # runtime check (scripts/audit_guarded_imports.py there), which sees
        # what it sets on public modules; one owner per question.
        return []
    if not index.complete:
        unjudged.add(target)
        return []
    if not index.provided(target):
        kind = "skips_forever" if is_test and guarded else "missing_module"
        return [Finding(kind, shown, line, target, "", guarded)]
    return []


def _scan_file(path: Path, shown: str, index: _Index, *, is_test: bool, unjudged: set[str]) -> list[Finding]:
    """Findings for one file.  On a plain install only public Kiln exists, so
    a public file's unguarded import of anything the private package
    provides is a crash waiting for whoever walks that line."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return []
    module = _module_of(path, index)
    package = module if path.name == "__init__.py" else module.rpartition(".")[0]
    found: list[Finding] = []
    seen: set[tuple[str, int, str, str]] = set()
    for node, guarded in _imports(tree):
        if isinstance(node, ast.Import):
            targets = [(alias.name, "") for alias in node.names]
        else:
            source = _absolute(node, package)
            targets = [(source, alias.name) for alias in node.names if alias.name != "*"] or [(source, "")]
        for target, name in targets:
            for finding in _judge_import(index, path, shown, node.lineno, target, name, guarded, is_test, unjudged):
                mark = (finding.kind, finding.line, finding.module, finding.name)
                if mark not in seen:
                    seen.add(mark)
                    found.append(finding)
    if is_test:
        for call in ast.walk(tree):
            if (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr == "importorskip"
                and call.args
                and isinstance(call.args[0], ast.Constant)
                and isinstance(call.args[0].value, str)
            ):
                target = call.args[0].value
                if _first_party(target) and not index.exists(target) and not index.provided(target):
                    if index.complete:
                        found.append(Finding("skips_forever", shown, call.lineno, target, "", True))
                    else:
                        unjudged.add(target)
    return found


def scan(
    trees: dict[str, Path],
    *,
    test_dirs: list[Path] | tuple[Path, ...] = (),
    shown_from: Path | None = None,
    provided: dict[str, str] | None = None,
) -> tuple[list[Finding], set[str]]:
    """``(findings, unjudged modules)`` for every file in *trees* and *test_dirs*.

    *provided* is the private package's compat-shim map (``kiln.X`` →
    ``kiln_pro.Y``), passed by the run that has both trees.
    """
    index = _Index(trees, provided)
    unjudged: set[str] = set()
    found: list[Finding] = []

    def shown(path: Path) -> str:
        """*path* relative to the repository that holds it, so a finding
        reads the same whichever repository runs the check."""
        for parent in path.parents:
            if (parent / ".git").exists():
                return str(path.relative_to(parent))
        base = shown_from or path.anchor
        try:
            return str(path.relative_to(base))
        except ValueError:
            return str(path)

    for root in index.trees.values():
        for path in sorted(root.rglob("*.py")):
            if "__pycache__" not in path.parts:
                found += _scan_file(path, shown(path), index, is_test=False, unjudged=unjudged)
    for root in test_dirs:
        for path in sorted(Path(root).rglob("*.py")):
            if "__pycache__" not in path.parts and (path.name.startswith("test_") or path.name == "conftest.py"):
                found += _scan_file(path, shown(path), index, is_test=True, unjudged=unjudged)
    return found, unjudged


def read_shims(path: Path) -> dict[str, str]:
    """The private package's ``_COMPAT_SHIMS`` literal, read without importing it."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
        if any(isinstance(t, ast.Name) and t.id == "_COMPAT_SHIMS" for t in targets) and isinstance(node.value, ast.Dict):
            return {
                k.value: v.value
                for k, v in zip(node.value.keys, node.value.values)
                if isinstance(k, ast.Constant) and isinstance(v, ast.Constant)
            }
    raise ValueError(f"no _COMPAT_SHIMS literal in {path}")


def judge(found: list[Finding], known: frozenset[tuple[str, str, str]]) -> tuple[list[Finding], list[tuple[str, str, str]]]:
    """``(new findings, stale known entries)``."""
    keys = {f.key for f in found}
    return [f for f in found if f.key not in known], sorted(k for k in known if k not in keys)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--tree", action="append", default=[], help="extra TOP=PATH tree, e.g. kiln_pro=../Kiln-pro/kiln_pro")
    parser.add_argument("--tests", action="append", default=[], help="extra test folder")
    parser.add_argument("--known-from", default="", help="Python file whose KNOWN replaces this one's")
    parser.add_argument("--shims-from", default="", help="the private package's __init__.py, read for its compat-shim map")
    args = parser.parse_args(argv)

    trees = {"kiln": PUBLIC_TREE}
    tests = [PUBLIC_TESTS]
    for spec in args.tree:
        top, _, path = spec.partition("=")
        trees[top] = Path(path)
    tests += [Path(t) for t in args.tests]
    known = KNOWN
    if args.known_from:
        namespace: dict = {}
        exec(compile(Path(args.known_from).read_text(encoding="utf-8"), args.known_from, "exec"), namespace)
        known = frozenset(namespace["KNOWN"])

    shims = read_shims(Path(args.shims_from)) if args.shims_from else None
    found, unjudged = scan(trees, test_dirs=tests, shown_from=REPO, provided=shims)
    new, stale = judge(found, known)
    for finding in new:
        print(f"NEW    {finding}")
    for entry in stale:
        print(f"STALE  {entry}  — fixed; delete it from KNOWN")
    print(
        f"[import-targets] {len(found)} finding(s): {len(new)} new, {len(found) - len(new)} known, "
        f"{len(stale)} stale; {len(unjudged)} cross-tree module(s) unjudged here"
    )
    return 2 if new or stale else 0


if __name__ == "__main__":
    sys.exit(main())
