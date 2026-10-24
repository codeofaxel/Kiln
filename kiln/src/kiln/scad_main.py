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

:func:`with_marks` writes the text and artwork carved into or raised onto a
mesh into the source the mesh began as, so a decorated part's source is the
whole part (:mod:`kiln.mesh_marks` records the marks).
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


#: What a mark module is called, by the kind of mark it draws.
_MARK_NAMES = {"text": "text", "svg": "artwork", "heightmap": "relief"}
#: The resolution a mark is compiled at on its own (OpenSCAD's defaults), so a
#: mark drawn inside a source that sets its own ``$fn`` comes out the same.
_MARK_RESOLUTION = "$fn = 0; $fa = 12; $fs = 2;"
_FILE_READ = re.compile(r"\b(import|surface)\s*\(")


def _blank_comments_and_strings(source: str) -> str:
    """*source* with comments and string contents replaced by spaces, same length."""
    out = list(source)
    i, n = 0, len(source)
    while i < n:
        if source.startswith("//", i):
            j = source.find("\n", i)
            j = n if j < 0 else j
        elif source.startswith("/*", i):
            j = source.find("*/", i + 2)
            j = n if j < 0 else j + 2
        elif source[i] == '"':
            j = i + 1
            while j < n and source[j] != '"':
                j += 2 if source[j] == "\\" else 1
            for k in range(i + 1, min(j, n)):
                if out[k] != "\n":
                    out[k] = " "
            i = j + 1
            continue
        else:
            i += 1
            continue
        for k in range(i, j):
            if out[k] != "\n":
                out[k] = " "
        i = j
    return "".join(out)


def _free_name(code: str, wanted: str) -> str:
    """*wanted*, or *wanted* with a number after it, that *code* does not define."""
    name, k = wanted, 1
    while re.search(rf"\bmodule\s+{re.escape(name)}\s*\(", code):
        k += 1
        name = f"{wanted}_{k}"
    return name


def with_marks(source: str, marks: Sequence[dict]) -> str:
    """*source*'s design with each mark applied to it, in order.

    *source* defines ``main()`` and draws nothing at the top level; so does
    the result.  Its ``main`` is renamed ``undecorated``, each mark becomes a
    named module of its own (``text_1``, ``text_2``, ...), and a new
    ``main()`` cuts or adds them in the order they were applied.  A mark is
    ``{"op": "difference" | "union", "scad", "kind"}``, as
    :mod:`kiln.mesh_marks` records it.

    :raises ValueError: if *source* is not module-only, a mark's op is
        neither, or a mark reads a file (an imported image or SVG), which a
        kept source cannot carry.
    """
    if not is_module_only(source):
        raise ValueError("source must define main() and draw nothing at the top level")
    code = _blank_comments_and_strings(source)
    for mark in marks:
        if mark.get("op") not in ("difference", "union"):
            raise ValueError(f"unknown mark op {mark.get('op')!r}")
        if _FILE_READ.search(_blank_comments_and_strings(mark.get("scad", ""))):
            raise ValueError("a mark that reads a file cannot be kept in a source")

    match = _MAIN_DEF.search(code)
    base = _free_name(code, "undecorated")
    renamed = source[: match.start()] + f"module {base}(" + source[match.end() :]

    taken = _blank_comments_and_strings(renamed)
    counts: dict[str, int] = {}
    modules: list[str] = []
    names: list[str] = []
    for mark in marks:
        stem = _MARK_NAMES.get(mark.get("kind", ""), "mark")
        counts[stem] = counts.get(stem, 0) + 1
        name = _free_name(taken, f"{stem}_{counts[stem]}")
        taken += f"\nmodule {name}("
        names.append(name)
        body = "\n".join(f"    {line}" for line in mark["scad"].strip().splitlines())
        modules.append(f"module {name}() {{\n    {_MARK_RESOLUTION}\n{body}\n}}")

    # Consecutive marks with the same op share one boolean: A - B - C.
    expr = f"{base}();"
    i = 0
    while i < len(marks):
        op = marks[i]["op"]
        run = [names[i]]
        while i + len(run) < len(marks) and marks[i + len(run)]["op"] == op:
            run.append(names[i + len(run)])
        inner = "\n".join([expr, *(f"{name}();" for name in run)])
        indented = "\n".join(f"    {line}" for line in inner.splitlines())
        expr = f"{op}() {{\n{indented}\n}}"
        i += len(run)
    main = "module main() {\n" + "\n".join(f"    {line}" for line in expr.splitlines()) + "\n}"
    return "\n".join([renamed.rstrip(), *modules, main]) + "\n"


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
