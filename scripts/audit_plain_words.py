#!/usr/bin/env python3
"""Every sentence Kiln shows a person is plain words: the guard.

Reads a source tree's syntax and finds the sentences a person is shown when
Kiln refuses or fails (``kiln.plain_words``):

* a person-facing field of a reply (``error``, ``message``, ``remedy``,
  ``note``, ``warning``, ``hint``, ``reason``, ``suggestion``) written as a
  literal or f-string, set later (``reply["error"] = ...``) or passed as a
  keyword of any call (``Result(message=...)``); the message given to an
  error helper (``_error_dict``, ``_refusal(error=, remedy_message=)``), a
  web error's ``detail``, a terminal error (``click.echo(..., err=True)``,
  ``ClickException``), and the message of Kiln's own failure
  (``raise PrinterError(...)``), which is shown as written;
* and anywhere a string is BUILT from a caught exception (an f-string,
  ``+``, ``%`` or ``.format``), except a log line, a developer warning, an
  ``assert``, a builtin exception (its words are judged when said), a
  ``detail`` and a name that holds technical text on purpose (``cause``,
  ``detail``, ``debug``, ``trace``);
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
import builtins
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "kiln" / "src"))

from kiln.plain_words import PERSON_FIELDS, technical_words

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
#: Keys that make a dict a reply; ``message``, ``note`` and ``reason`` are
#: judged only in one (a commit's message or an op's reason is data).
_REPLY_KEYS = frozenset({"success", "status", "ok", "code", "error", "retryable", "refused"})
#: Fields that are a sentence for a person in any dict.
_ALWAYS_SAID = frozenset({"error", "remedy", "hint", "warning", "suggestion"})
#: An exception's parts that are Python's text, not Kiln's words.
_RAW_PARTS = frozenset({"args", "msg", "strerror", "doc", "colno", "pos", "filename"})
#: Where a sentence passes the chokepoint that rewords it.
_REWORDED_AT_THE_CHOKEPOINT = frozenset({"_error_dict()", "_error_dict(message=)", "error_dict()", "error_dict(message=)"})
#: Python's own exception classes: a message given to one is judged by its
#: words when it is said, so it is not Kiln's sentence yet.
_BUILTIN_FAILURES = frozenset(
    n for n in dir(builtins) if isinstance(getattr(builtins, n), type) and issubclass(getattr(builtins, n), BaseException)
)
#: Log lines and developer warnings: technical on purpose.
_LOG_ROOTS = frozenset({"logger", "logging", "log", "_log", "_logger", "LOG", "LOGGER", "warnings"})
#: A name that holds technical text on purpose.
_TECHNICAL_NAME = re.compile(r"detail|cause|debug|trace|(?:^|_)tb(?:$|_)|(?:^|_)log(?:$|_)", re.IGNORECASE)
#: Readers of a mapping, whose keyword is never a sentence.
_LOOKUPS = frozenset({"get", "pop", "setdefault"})
#: Paths that are not shipped code a person meets.
_SKIP_PARTS = ("tests", "test", "scripts", "migrations", ".claude", "node_modules", "__pycache__")


def _name(func: ast.AST) -> str:
    return func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""


def _message_parts(expr: ast.AST) -> set[str]:
    """The variables a message is glued together from (``words + "..."``,
    ``f"{words} ..."``), not every name the expression mentions."""
    if isinstance(expr, ast.Name):
        return {expr.id}
    if isinstance(expr, ast.BinOp) and isinstance(expr.op, (ast.Add, ast.Mod)):
        return _message_parts(expr.left) | _message_parts(expr.right)
    if isinstance(expr, ast.JoinedStr):
        return {v.value.id for v in expr.values if isinstance(v, ast.FormattedValue) and isinstance(v.value, ast.Name)}
    return set()


def _spoken_names(func: ast.AST) -> set[str]:
    """Names in *func* that a person-facing message is built from: a Kiln
    failure's message, a person field of a reply, or a sentence call."""
    names: set[str] = set()
    for node in ast.walk(func):
        if isinstance(node, ast.Call):
            name = _name(node.func)
            if (name.endswith(("Error", "Refusal", "Refused")) and name not in _BUILTIN_FAILURES
                    and node.args):
                names |= _message_parts(node.args[0])
            if name in _SENTENCE_CALLS:
                index, keywords = _SENTENCE_CALLS[name]
                if index is not None and len(node.args) > index:
                    names |= _message_parts(node.args[index])
                names |= {p for kw in node.keywords if kw.arg in keywords for p in _message_parts(kw.value)}
            names |= {p for kw in node.keywords if kw.arg in PERSON_FIELDS for p in _message_parts(kw.value)}
        elif isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if isinstance(key, ast.Constant) and key.value in PERSON_FIELDS:
                    names |= _message_parts(value)
        elif isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Subscript) and isinstance(t.slice, ast.Constant) and t.slice.value in PERSON_FIELDS
            for t in node.targets
        ):
            names |= _message_parts(node.value)
    return names


class _Scan(ast.NodeVisitor):
    def __init__(self, rel: str) -> None:
        self.rel = rel
        self.scope: list[str] = []
        self.caught: list[set[str]] = [set()]
        self.found: list[tuple[str, int, str]] = []   # (key, line, text)
        self.judged: set[int] = set()   # nodes already read as a sentence
        self.quiet = 0                  # > 0 inside text that is technical on purpose
        self.spoken: list[set[str]] = [set()]   # names a person's message is built from, per function

    # -- where we are -------------------------------------------------------
    def _enter(self, node: ast.AST, name: str) -> None:
        self.scope.append(name)
        self.caught.append(set())
        self.spoken.append(_spoken_names(node) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) else set())
        self.generic_visit(node)
        self.spoken.pop()
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
        self.judged.add(id(value))
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
            # The exception's own text, its class name or Python's parts of
            # it.  Another attribute (``exc.verdict``, ``exc.remedy``) is a
            # field Kiln's own failure carries in words and is not raw.
            if isinstance(n, ast.Attribute):
                if n.attr == "__name__":
                    return is_exc(n.value) or (
                        isinstance(n.value, ast.Call) and _name(n.value.func) == "type"
                        and any(is_exc(a) for a in n.value.args)
                    ) or (isinstance(n.value, ast.Attribute) and n.value.attr == "__class__"
                          and is_exc(n.value.value))
                return n.attr in _RAW_PARTS and is_exc(n.value)
            if isinstance(n, ast.Subscript):
                return is_exc(n.value)
            if isinstance(n, ast.BoolOp):
                return any(is_exc(v) for v in n.values)
            if isinstance(n, ast.Call) and _name(n.func) in ("str", "repr", "format", "type"):
                return any(is_exc(a) for a in n.args)
            return isinstance(n, ast.Name) and n.id in caught

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

    def _quietly(self, node: ast.AST) -> None:
        """Visit *node* as text that is technical on purpose."""
        self.quiet += 1
        self.visit(node)
        self.quiet -= 1

    def _built(self, node: ast.AST) -> None:
        """A string built from a caught exception, wherever it sits."""
        if not self.quiet and id(node) not in self.judged and self._text(node)[1]:
            self._judge("built", node)

    def visit_JoinedStr(self, node: ast.JoinedStr) -> None:
        self._built(node)
        self.generic_visit(node)

    def visit_BinOp(self, node: ast.BinOp) -> None:
        if isinstance(node.op, (ast.Add, ast.Mod)) and any(
            isinstance(side, (ast.Constant, ast.JoinedStr)) for side in (node.left, node.right)
        ):
            self._built(node)
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            if (isinstance(target, ast.Subscript) and isinstance(target.slice, ast.Constant)
                    and target.slice.value in PERSON_FIELDS):
                self._judge(f"[{target.slice.value}]", node.value)
        named = [t.id for t in node.targets if isinstance(t, ast.Name)]
        shown = [n for n in named if n in self.spoken[-1]]
        if shown:
            # Text kept under any name is a sentence once a message is built from it.
            self._judge(f"{shown[0]} (shown)", node.value)
        elif named and all(_TECHNICAL_NAME.search(n) for n in named):
            self._quietly(node.value)
            return
        pairs = [t for t in node.targets if isinstance(t, ast.Tuple)]
        if len(node.targets) == 1 and pairs and isinstance(node.value, ast.Tuple) and len(pairs[0].elts) == len(node.value.elts):
            # ``said, detail = sentence, technical``: each value by its own name.
            for target, value in zip(pairs[0].elts, node.value.elts):
                if isinstance(target, ast.Name) and _TECHNICAL_NAME.search(target.id):
                    self._quietly(value)
                else:
                    self.visit(value)
            return
        self.generic_visit(node)

    def visit_Assert(self, node: ast.Assert) -> None:
        self.visit(node.test)
        if node.msg is not None:
            self._quietly(node.msg)

    def visit_Raise(self, node: ast.Raise) -> None:
        failure = node.exc
        name = _name(failure.func) if isinstance(failure, ast.Call) else ""
        if name in _BUILTIN_FAILURES or not name:
            if node.exc is not None:
                self._quietly(node.exc)
            if node.cause is not None:
                self.visit(node.cause)
            return
        # Kiln's own failure is shown as written: its message is a sentence.
        # A helper handed the caught failure itself (``raise _wrap(exc)``)
        # builds the failure from the object, not from its text.
        handed_the_failure = name.lstrip("_")[:1].islower() and isinstance(failure.args[0] if failure.args else None, ast.Name)
        if failure.args and not handed_the_failure:
            self._judge(f"raise {name}", failure.args[0])
        self.generic_visit(node)

    def visit_Dict(self, node: ast.Dict) -> None:
        keys = {k.value for k in node.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)}
        reply = bool(keys & _REPLY_KEYS)
        for key, value in zip(node.keys, node.values):
            if isinstance(key, ast.Constant) and key.value in PERSON_FIELDS and (
                reply or key.value in _ALWAYS_SAID
            ):
                if isinstance(value, ast.Dict):
                    for k2, v2 in zip(value.keys, value.values):
                        if isinstance(k2, ast.Constant) and k2.value in ("message", "error", "sentence"):
                            self._judge(f"{key.value}.{k2.value}", v2)
                else:
                    self._judge(str(key.value), value)
            elif isinstance(key, ast.Constant) and key.value == "detail":
                self._quietly(value)
                continue
            if key is not None:
                self.visit(key)
            self.visit(value)

    def visit_Call(self, node: ast.Call) -> None:
        name = _name(node.func)
        root = node.func
        while isinstance(root, ast.Attribute):
            root = root.value
        if (isinstance(root, ast.Name) and root.id in _LOG_ROOTS) or "redact" in name or name.startswith("detail_"):
            self.quiet += 1
            self.generic_visit(node)
            self.quiet -= 1
            return
        if (name == "format" and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Constant)
                and not self.quiet
                and any(self._text(a)[1] for a in [*node.args, *(k.value for k in node.keywords)])):
            self.found.append((f"{self.rel}::{'.'.join(self.scope) or '<module>'}::built::raw error",
                               node.lineno, str(node.func.value.value)))
        # Kiln's own failure, made here and raised later: its message is a sentence.
        if (name.endswith(("Error", "Refusal", "Refused")) and name not in _BUILTIN_FAILURES
                and name not in _SENTENCE_CALLS and node.args and id(node.args[0]) not in self.judged):
            self._judge(f"{name}()", node.args[0])
        if name not in _SENTENCE_CALLS and name not in _LOOKUPS:
            for kw in node.keywords:
                if kw.arg in PERSON_FIELDS or kw.arg == "remedy_message":
                    self._judge(f"{name}({kw.arg}=)", kw.value)
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
        self.visit(node.func)
        for arg in node.args:
            self.visit(arg)
        for kw in node.keywords:
            if kw.arg == "detail" and name != "HTTPException":
                self._quietly(kw.value)   # technical on purpose
            else:
                self.visit(kw.value)


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
