#!/usr/bin/env python3
"""Name every test file that READS what a diff changed — the targeted set.

"The test files covering the touched modules" (CLAUDE.md § Build & Test)
is only as good as the list, and a module's own ``test_<module>.py`` is
not the whole list.  A roster (an allowlist, ``_FREE_TOOLS``,
``VIEWER_TOOLS``, a category map) is cross-referenced by SIBLING ledgers
whose tests assert the two agree, and those tests live under the
sibling's name.  2026-09-21, in kiln-pro: a
tool joined a hosted allowlist with the allowlist's own audit green, and
the sibling ledger's test stayed red until a later lap ran it, because
the six test files that read that roster were never listed.

This script derives the readers mechanically::

    python3 kiln/scripts/tests_reading.py            # against origin/main
    python3 kiln/scripts/tests_reading.py main       # against a ref
    python3 kiln/scripts/tests_reading.py --run      # and run them (PYTHONPATH=src, from kiln/)

For every changed Python file it collects the module's import names
(``kiln.parametric``, ``kiln.plugins.design_tools``) and every top-level symbol
the diff added, removed or edited (``_STOP_KEYWORDS``, ``def
parse_openscad_parameters``, ``class ParameterDef``), then greps ``tests/`` for any of them.  It
over-approximates on purpose: a test that merely mentions a symbol is
cheap to run and expensive to miss.

A module is read however the import spells it.  ``import kiln.gcode`` and
``from kiln.gcode import x`` write the dotted path out, so the grep sees
them; ``from kiln import gcode`` never does, so each test's imports are
also parsed and that spelling counted as ``kiln.gcode``.
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import os
import re
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_TESTS = _ROOT / "kiln" / "tests"
_SRC = Path("kiln") / "src"
_HUNK = re.compile(r"^@@ -(?P<os>\d+)(?:,(?P<oc>\d+))? \+(?P<ns>\d+)(?:,(?P<nc>\d+))? @@")
_GENERIC = frozenset({"main", "plugin", "register", "name", "description", "test", "solve", "validate", "profile", "summary", "model", "emit", "arc"})
_BROAD_PACKAGES = frozenset({"kiln", "kiln.plugins", "kiln.printers", "kiln.cli", "kiln.scripts", "scripts"})


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=_ROOT, check=True, capture_output=True, text=True
    ).stdout


def changed_files(base: str) -> tuple[list[Path], set[Path]]:
    """Changed Python files outside tests/, and which of them are brand new."""
    out = _git("diff", "--name-only", f"{base}...HEAD", "--", "*.py")
    dirty = _git("diff", "--name-only", "HEAD", "--", "*.py")
    added = _git("diff", "--name-only", "--diff-filter=A", f"{base}...HEAD", "--", "*.py")
    names = {line.strip() for line in (out + dirty).splitlines() if line.strip()}
    files = sorted(Path(n) for n in names if not n.startswith("kiln/tests/"))
    new = {Path(n.strip()) for n in added.splitlines() if n.strip()}
    return files, new


def module_names(path: Path) -> set[str]:
    """The dotted import paths a test would name: the module and its package.

    Bare stems are not needles — ``emit`` and ``model`` are English — and the
    top-level packages are too broad to mean anything.

    A file outside the importable source tree is a script: no test can import
    it, so every test that has one loads it by path (``root / "scripts" /
    "audit_x.py"``, ``"kiln/scripts/audit_x.py"``).  Its file NAME is the
    needle, which each of those spellings contains, and the surrounding
    pattern keeps ``old_audit_x.py`` and ``audit_x.pyc`` out.
    """
    parts = path.with_suffix("").parts
    if parts[:2] == ("kiln", "src"):
        parts = parts[2:]
    elif path.suffix == ".py" and path.stem not in _GENERIC and path.stem != "__init__":
        return {path.name}
    if parts[-1] == "__init__":
        parts = parts[:-1]
    names: set[str] = set()
    dotted = ".".join(parts)
    if dotted not in _BROAD_PACKAGES:
        names.add(dotted)
    parent = ".".join(parts[:-1])
    if parent and parent not in _BROAD_PACKAGES:
        names.add(parent)
    return names


def _changed_lines(base: str, path: Path) -> tuple[set[int], set[int]]:
    """Line numbers the diff touched: (in the file at HEAD, in the file at base)."""
    new_lines: set[int] = set()
    old_lines: set[int] = set()
    diff = _git("diff", "-U0", f"{base}...HEAD", "--", str(path))
    diff += _git("diff", "-U0", "HEAD", "--", str(path))
    for line in diff.splitlines():
        m = _HUNK.match(line)
        if not m:
            continue
        ns, nc = int(m.group("ns")), int(m.group("nc") or "1")
        os_, oc = int(m.group("os")), int(m.group("oc") or "1")
        new_lines.update(range(ns, ns + max(nc, 1)))
        old_lines.update(range(os_, os_ + max(oc, 1)))
    return new_lines, old_lines


def _top_level_names(source: str, lines: set[int]) -> set[str]:
    """Top-level symbols whose span contains any of ``lines``.

    An edit inside a roster literal (``REMOTE_TOOL_ALLOWLIST = frozenset({
    ... })``) is an edit to the roster, however deep the line sits.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    found: set[str] = set()

    def touched(node: ast.AST) -> bool:
        end = getattr(node, "end_lineno", node.lineno)
        return any(node.lineno <= n <= end for n in lines)

    for node in tree.body:
        if not touched(node):
            continue
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            found.add(node.name)
            # A tool registered inside a plugin class's ``register`` is a
            # nested def; its tests call it by ITS name, not the class's.
            for inner in ast.walk(node):
                if inner is not node and isinstance(
                    inner, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
                ) and touched(inner):
                    found.add(inner.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                if isinstance(t, ast.Name):
                    found.add(t.id)
    return found


def changed_symbols(base: str, path: Path) -> set[str]:
    new_lines, old_lines = _changed_lines(base, path)
    names: set[str] = set()
    head = (_ROOT / path)
    if head.is_file():
        names |= _top_level_names(head.read_text(encoding="utf-8"), new_lines)
    with contextlib.suppress(subprocess.CalledProcessError):  # a brand-new file
        names |= _top_level_names(_git("show", f"{base}:{path}"), old_lines)
    # Rosters and constants are worth chasing however they are named; a
    # private helper is its module's own business and that module's tests
    # are already on the list via the dotted path.
    return {
        n for n in names
        if n not in _GENERIC and len(n) >= 4 and (n.isupper() or not n.startswith("_"))
    }


def _from_imports(text: str) -> set[str]:
    """Each ``from a.b import c`` in *text*, as ``a.b.c``, wherever it sits.

    The one import spelling that never writes a module's dotted path out.
    A name that is not a module (``from kiln.gcode import parse``) comes out
    as ``kiln.gcode.parse``, which still sits inside the module it came from.
    """
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return set()
    return {
        f"{node.module}.{alias.name}"
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module and not node.level
        for alias in node.names
        if alias.name != "*"
    }


def readers(needles: set[str]) -> list[Path]:
    hits: list[Path] = []
    pattern = re.compile(
        r"(?<![\w.])(?:" + "|".join(re.escape(n) for n in sorted(needles, key=len, reverse=True)) + r")(?![\w])"
    )
    modules = {n for n in needles if "." in n}
    # A module's last name is only a cheap filter before parsing, never a
    # needle of its own: ``emit`` and ``model`` are English.
    last_names = {m.rsplit(".", 1)[1] for m in modules}
    # pytest collects the subfolders too (tests/regression/), so they are read.
    for test in sorted(_TESTS.rglob("test_*.py")):
        try:
            text = test.read_text(encoding="utf-8")
        except OSError:
            continue
        if pattern.search(text) or (
            any(name in text for name in last_names)
            and any(i == m or i.startswith(m + ".") for i in _from_imports(text) for m in modules)
        ):
            hits.append(test.relative_to(_ROOT))
    return hits


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("base", nargs="?", default="origin/main")
    ap.add_argument("--run", action="store_true", help="run the tests found")
    args = ap.parse_args()

    files, new = changed_files(args.base)
    if not files:
        print("no changed Python files outside tests/")
        return 0
    needles: set[str] = set()
    for path in files:
        needles |= module_names(path)
        if path not in new:  # a brand-new module is reached only by import
            needles |= changed_symbols(args.base, path)
    tests = readers(needles)
    print(f"changed: {', '.join(str(f) for f in files)}")
    print(f"needles: {', '.join(sorted(needles))}")
    print(f"tests reading them ({len(tests)}):")
    for t in tests:
        print(f"  {t}")
    if args.run and tests:
        cmd = [sys.executable, "-m", "pytest", "-q", *(str(t.relative_to("kiln")) for t in tests)]
        print("$ PYTHONPATH=src", " ".join(cmd))
        env = dict(os.environ, PYTHONPATH="src")
        return subprocess.call(cmd, cwd=_ROOT / "kiln", env=env)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
