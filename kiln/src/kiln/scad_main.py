"""OpenSCAD sources whose design is a ``main()`` module.

A source may hold definitions only: named modules (one of them ``main``),
functions, variable assignments and ``include``/``use`` lines, with nothing
drawn at the top level.  Such a source compiles to its ``main()``.  A source
that draws anything at the top level, a ``main();`` call included, compiles
exactly as written.

:func:`kiln.openscad_runner.run_openscad` applies this to every input file it
hands OpenSCAD, through :func:`runnable_inputs`: a module-only file is
replaced, for that one launch, by a wrapper in a temporary folder that
includes it and calls ``main()``.  The file itself is never changed and
nothing is written beside it; its own relative includes and imports still
resolve, because OpenSCAD reads them relative to the file that names them.
Pinned by ``tests/test_scad_main.py``.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

_MAIN_DEF = re.compile(r"\bmodule\s+main\s*\(")
_ASSIGNMENT = re.compile(r"^\$?[A-Za-z_]\w*\s*=(?!=)")
_DEFINITION = re.compile(r"^(module|function)\b")
_IMPORT = re.compile(r"^(include|use)\s*<")

# OpenSCAD options whose next argument is a value, not an input file.
_VALUE_OPTIONS = frozenset({"-o", "-d", "-m", "-p", "-P", "-D", "--summary-file"})


def call_main(source: str) -> str:
    """*source* with ``main()`` called after it."""
    return f"{source}\nmain();\n"


def _strip_comments_and_strings(source: str) -> str:
    """*source* with comments removed and string literals emptied."""
    out: list[str] = []
    i, n = 0, len(source)
    while i < n:
        ch = source[i]
        if source.startswith("//", i):
            j = source.find("\n", i)
            i = n if j < 0 else j
        elif source.startswith("/*", i):
            j = source.find("*/", i + 2)
            i = n if j < 0 else j + 2
        elif ch == '"':
            j = i + 1
            while j < n and source[j] != '"':
                j += 2 if source[j] == "\\" else 1
            out.append('""')
            i = j + 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _top_level_statements(code: str) -> list[str]:
    """The depth-0 statements of comment-free *code*, each stripped."""
    statements: list[str] = []
    depth = 0
    start = 0
    for i, ch in enumerate(code):
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
            if ch == "}" and depth == 0:
                statements.append(code[start : i + 1].strip())
                start = i + 1
        elif ch == ";" and depth == 0:
            statements.append(code[start : i + 1].strip())
            start = i + 1
    tail = code[start:].strip()
    if tail:
        statements.append(tail)
    return [s for s in statements if s and s != ";"]


def is_module_only(source: str) -> bool:
    """True when *source* defines ``main()`` and draws nothing at the top level."""
    if not _MAIN_DEF.search(source or ""):
        return False
    code = _strip_comments_and_strings(source)
    if not _MAIN_DEF.search(code):
        return False
    return all(
        _DEFINITION.match(s) or _ASSIGNMENT.match(s) or _IMPORT.match(s)
        for s in _top_level_statements(code)
    )


def runnable(source: str) -> str:
    """The text that compiles to *source*'s design."""
    return call_main(source) if is_module_only(source) else source


def _input_positions(argv: Sequence[str]) -> list[int]:
    """Indices in *argv* that name an input ``.scad`` file."""
    positions: list[int] = []
    skip = False
    for i, arg in enumerate(argv):
        if i == 0 or skip:
            skip = False
            continue
        if arg in _VALUE_OPTIONS:
            skip = True
        elif not arg.startswith("-") and arg.lower().endswith(".scad"):
            positions.append(i)
    return positions


def _read(path: str) -> str | None:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


@contextmanager
def runnable_inputs(argv: Sequence[str], *, cwd: str | None = None) -> Iterator[list[str]]:
    """*argv* with each module-only input file swapped for one that calls ``main()``.

    The swap lasts for the ``with`` block; the wrappers are removed on exit.
    Any other argument is passed through unchanged.
    """
    out = list(argv)
    workdir: str | None = None
    try:
        for i in _input_positions(out):
            path = os.path.join(cwd, out[i]) if cwd else out[i]
            text = _read(path)
            if text is None or not is_module_only(text):
                continue
            if workdir is None:
                workdir = tempfile.mkdtemp(prefix="kiln_main_")
            target = Path(path).resolve().as_posix()
            wrapper = (
                f"include <{target}>\nmain();\n"
                if ">" not in target
                else call_main(text)
            )
            wrapped = os.path.join(workdir, f"{len(os.listdir(workdir))}_{Path(path).name}")
            Path(wrapped).write_text(wrapper, encoding="utf-8")
            out[i] = wrapped
        yield out
    finally:
        if workdir is not None:
            shutil.rmtree(workdir, ignore_errors=True)
