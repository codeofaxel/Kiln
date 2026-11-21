#!/usr/bin/env python3
"""Every sentence Kiln shows a person is plain words: the guard.

Reads a source tree's syntax and finds the sentences a person is shown when
Kiln refuses or fails (``kiln.plain_words``):

* a person-facing field of a reply (``error``, ``message``, ``remedy``,
  ``note``, ``warning``, ``hint``, ``reason``, ``suggestion``) written as a
  literal or f-string, the message given to an error helper
  (``_error_dict``, ``_refusal(error=, remedy_message=)``), a web error's
  ``detail``, and a terminal error (``click.echo(..., err=True)``,
  ``ClickException``);
* judged two ways: a TECHNICAL word in its literal text (``technical_words``),
  and a RAW error, a sentence built from a caught exception
  (``str(exc)``, ``f"... {exc}"``), whose text is Python's, never Kiln's.

A finding is keyed ``path::function::where::kind`` (no line numbers, so an
edit above it does not move it).  ``--baseline FILE`` judges against the
findings recorded there: a new one fails, a recorded one that is gone is
stale and fails too (remove its line), so the list only shrinks.  The
technical text a person does not need goes in the reply's ``detail``.

Usage: audit_plain_words.py TREE [--baseline FILE] [--write-baseline FILE] [--list]
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "kiln" / "src"))

from kiln.plain_words import PERSON_FIELDS, technical_words  # noqa: E402

#: Helpers whose first argument (or named argument) is a sentence a person reads.
_SENTENCE_CALLS: dict[str, tuple[int | None, tuple[str, ...]]] = {
    "_error_dict": (0, ("message",)),
    "error_dict": (0, ("message",)),
    "_refusal": (None, ("error", "remedy_message")),
    "_refuse": (2, ("message",)),
    "HTTPException": (None, ("detail",)),
    "ClickException": (0, ("message",)),
    "UsageError": (0, ("message",)),
    "BadParameter": (0, ("message",)),
}
#: Where a sentence passes the chokepoint that rewords it.
_REWORDED_AT_THE_CHOKEPOINT = frozenset({"_error_dict()", "_error_dict(message=)", "error_dict()", "error_dict(message=)"})
#: Paths that are not shipped code a person meets.
_SKIP_PARTS = ("tests", "test", "scripts", "migrations", ".claude", "node_modules", "__pycache__")


def _name(func: ast.AST) -> str:
    return func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""


class _Scan(ast.NodeVisitor):
    def __init__(self, rel: str) -> None:
        self.rel = rel
        self.scope: list[str] = []
        self.caught: list[set[str]] = [set()]
        self.found: list[tuple[str, int, str]] = []   # (key, line, text)

    # -- where we are -------------------------------------------------------
    def _enter(self, node: ast.AST, name: str) -> None:
        self.scope.append(name)
        self.caught.append(set())
        self.generic_visit(node)
        self.caught.pop()
        self.scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._enter(node, node.name)

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._enter(node, node.name)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.name:
            self.caught[-1].add(node.name)
        self.generic_visit(node)

    # -- what a person reads -------------------------------------------------
    def _caught_names(self) -> set[str]:
        return set().union(*self.caught)

    def _judge(self, where: str, value: ast.AST) -> None:
        line = getattr(value, "lineno", 0)
        func = ".".join(self.scope) or "<module>"
        text, raw = self._text(value)
        # The envelope every door builds rewords a caught failure itself
        # (kiln.server._error_dict), once for all.
        reworded_there = where in _REWORDED_AT_THE_CHOKEPOINT
        if raw and not reworded_there:
            self.found.append((f"{self.rel}::{func}::{where}::raw error", line, text or ""))
        kinds = technical_words(text) if text and " " in text.strip() else []
        for kind in kinds:
            self.found.append((f"{self.rel}::{func}::{where}::{kind}", line, text))

    def _text(self, node: ast.AST) -> tuple[str | None, bool]:
        """The literal words of *node* and whether a caught exception fills it."""
        caught = self._caught_names()

        def is_exc(n: ast.AST) -> bool:
            base = n
            while isinstance(base, (ast.Attribute, ast.Subscript)):
                base = base.value
            if isinstance(base, ast.Call) and _name(base.func) in ("str", "repr", "format"):
                return any(is_exc(a) for a in base.args)
            return isinstance(base, ast.Name) and base.id in caught

        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value, False
        if isinstance(node, ast.JoinedStr):
            parts, raw = [], False
            for v in node.values:
                if isinstance(v, ast.Constant):
                    parts.append(str(v.value))
                elif isinstance(v, ast.FormattedValue):
                    raw = raw or is_exc(v.value)
                    parts.append(" ")
            return "".join(parts), raw
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Mod)):
            lt, lr = self._text(node.left)
            rt, rr = self._text(node.right)
            return (lt or "") + " " + (rt or ""), lr or rr or is_exc(node.right)
        if isinstance(node, ast.Call) and _name(node.func) in ("str", "repr"):
            return None, any(is_exc(a) for a in node.args)
        return None, is_exc(node)

    def visit_Dict(self, node: ast.Dict) -> None:
        for key, value in zip(node.keys, node.values):
            if isinstance(key, ast.Constant) and key.value in PERSON_FIELDS:
                if isinstance(value, ast.Dict):
                    for k2, v2 in zip(value.keys, value.values):
                        if isinstance(k2, ast.Constant) and k2.value in ("message", "error", "sentence"):
                            self._judge(f"{key.value}.{k2.value}", v2)
                else:
                    self._judge(str(key.value), value)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        name = _name(node.func)
        if name in _SENTENCE_CALLS:
            index, keywords = _SENTENCE_CALLS[name]
            if index is not None and len(node.args) > index:
                self._judge(f"{name}()", node.args[index])
            for kw in node.keywords:
                if kw.arg in keywords:
                    self._judge(f"{name}({kw.arg}=)", kw.value)
        elif name in ("echo", "secho") and any(
            kw.arg == "err" and isinstance(kw.value, ast.Constant) and kw.value.value for kw in node.keywords
        ) and node.args:
            self._judge("terminal error", node.args[0])
        self.generic_visit(node)


def scan(tree: Path) -> list[tuple[str, int, str]]:
    """Every finding under *tree*'s shipped code, sorted by key."""
    found: list[tuple[str, int, str]] = []
    for path in sorted(tree.rglob("*.py")):
        rel_parts = path.relative_to(tree).parts
        if any(p in _SKIP_PARTS or p.startswith("test_") for p in rel_parts):
            continue
        try:
            syntax = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
        except SyntaxError:
            continue
        visitor = _Scan(path.relative_to(tree).as_posix())
        visitor.visit(syntax)
        found.extend(visitor.found)
    return sorted(set(found))


def _read_baseline(path: Path) -> set[str]:
    if not path.exists():
        return set()
    return {ln.strip() for ln in path.read_text().splitlines() if ln.strip() and not ln.startswith("#")}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("tree", type=Path)
    ap.add_argument("--baseline", type=Path)
    ap.add_argument("--write-baseline", type=Path)
    ap.add_argument("--list", action="store_true", help="print every finding with its line and words")
    args = ap.parse_args(argv)
    found = scan(args.tree)
    keys = sorted({k for k, _l, _t in found})
    if args.list:
        for key, line, text in found:
            print(f"{key} (line {line}): {text[:160]!r}")
    if args.write_baseline:
        args.write_baseline.write_text(
            "# Sentences a person is shown with technical words or raw error text,\n"
            "# frozen to shrink: fix one and delete its line.  scripts/audit_plain_words.py\n"
            + "".join(f"{k}\n" for k in keys)
        )
    print(f"[plain-words] {len(keys)} finding(s) in {args.tree}")
    if args.baseline is None:
        return 0
    known = _read_baseline(args.baseline)
    new, stale = sorted(set(keys) - known), sorted(known - set(keys))
    for key in new:
        print(f"NEW   {key}")
    for key in stale:
        print(f"STALE {key}  (fixed: delete its line from {args.baseline.name})")
    if new or stale:
        print("[plain-words] a person is shown plain words: say it in everyday words, and put the "
              "technical text in the reply's `detail`.")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
