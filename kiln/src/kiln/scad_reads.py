"""Which files an OpenSCAD source would read.

OpenSCAD reads a file in two ways: ``include <...>`` / ``use <...>``, and the
built-ins that take a file (``import``, ``surface``, the ``dxf_*`` family),
by a positional argument or a ``file`` / ``filename`` argument.
:func:`file_reads` lists every such read in a source, comments left out.
A read whose file is not written as one plain quoted string (a variable, a
``str(...)``, a module parameter) is listed with no path, because nothing
short of running the source says which file it is.

It is the one reader of this in Kiln: a signed-in install asks it before a
build leaves the computer, and Kiln's servers ask it of every source they
are handed.

Kiln ships two OpenSCAD libraries, :data:`LIBRARIES`, on OpenSCAD's library
path.  :func:`reads_beyond_bundled_libraries` says whether a source reads
anything else: a file of the person's own, or a read whose file it cannot
name.  A signed-in install builds such a source on its own computer
(:mod:`kiln.served_public`), since a computer elsewhere cannot open it.
"""

from __future__ import annotations

#: The OpenSCAD libraries Kiln ships, as ``include`` / ``use`` name them.
LIBRARIES: tuple[str, ...] = ("BOSL2", "MCAD")

#: Built-ins that read a file named by a positional argument.
READERS = frozenset({
    "import", "surface", "import_stl", "import_off", "import_dxf",
    "dxf_linear_extrude", "dxf_rotate_extrude", "dxf_dim", "dxf_cross",
})
#: The argument name that carries a file on those and on the extrudes.
FILE_ARGS = frozenset({"file", "filename"})

#: A source holding none of these words reads no file: every read starts with
#: one of them, so a long source without them needs no closer look.
_READ_WORDS = ("include", "use", *sorted(READERS), *sorted(FILE_ARGS))

_OPENERS, _CLOSERS = "([{", ")]}"

#: Words that are values, never a file, in a reader's positional argument.
_LITERAL_WORDS = frozenset({"true", "false", "undef"})
_LITERAL_PUNCT = frozenset({"[", "]", ",", "-", "+", "."})

_Token = tuple[str, str]


def _tokens(code: str) -> list[_Token]:
    """*code* as ``(kind, text)`` tokens with comments dropped: ``str``,
    ``id``, ``num``, ``inc`` (the target of an include/use) and ``p``."""
    out: list[_Token] = []
    i, n = 0, len(code)
    while i < n:
        c = code[i]
        if c.isspace():
            i += 1
        elif code.startswith("//", i):
            end = code.find("\n", i)
            i = n if end < 0 else end + 1
        elif code.startswith("/*", i):
            end = code.find("*/", i + 2)
            i = n if end < 0 else end + 2
        elif c == '"':
            j = i + 1
            while j < n and code[j] != '"':
                j += 2 if code[j] == "\\" else 1
            out.append(("str", code[i + 1:min(j, n)]))
            i = j + 1
        elif c.isalpha() or c in "_$":
            j = i + 1
            while j < n and (code[j].isalnum() or code[j] == "_"):
                j += 1
            word = code[i:j]
            k = j
            while k < n and code[k].isspace():
                k += 1
            if word in ("include", "use") and k < n and code[k] == "<":
                end = code.find(">", k)
                out.append(("inc", code[k + 1:n if end < 0 else end].strip()))
                i = n if end < 0 else end + 1
            else:
                out.append(("id", word))
                i = j
        elif c.isdigit():
            j = i + 1
            while j < n and (code[j].isalnum() or code[j] == "."):
                j += 1
            out.append(("num", code[i:j]))
            i = j
        elif code[i:i + 2] in ("==", "!=", "<=", ">=", "&&", "||"):
            out.append(("p", code[i:i + 2]))
            i += 2
        else:
            out.append(("p", c))
            i += 1
    return out


def _argument(tokens: list[_Token], start: int) -> tuple[list[_Token], int]:
    """The tokens of one argument from *start*, and the index of what ends it
    (a ``,`` or the call's closing bracket)."""
    depth = 0
    i = start
    while i < len(tokens):
        kind, text = tokens[i]
        if kind == "p":
            if text in _OPENERS:
                depth += 1
            elif text in _CLOSERS:
                if depth == 0:
                    break
                depth -= 1
            elif text == "," and depth == 0:
                break
        i += 1
    return tokens[start:i], i


def _plain_literal(argument: list[_Token]) -> bool:
    """Whether *argument* is only numbers, booleans and lists of them: a
    value that cannot name a file (a convexity, an origin, a scale)."""
    return bool(argument) and all(
        kind == "num"
        or (kind == "id" and text in _LITERAL_WORDS)
        or (kind == "p" and text in _LITERAL_PUNCT)
        for kind, text in argument
    )


def file_reads(code: str) -> list[tuple[str, str | None]]:
    """Every file *code* would read, as ``(how, path)``.

    ``how`` is ``"include"`` (an ``include`` or ``use``) or ``"read"`` (a
    built-in that reads a file).  ``path`` is ``None`` when the source does
    not name the file as one plain quoted string.

    Every argument of a reading built-in is judged, not only the first:
    OpenSCAD binds a positional argument to the file however many named
    ones come before it (``import(convexity=2, "/x.stl")`` reads
    ``/x.stl``).  A positional argument that is only numbers and booleans
    names no file; any other one is a read.  A named ``file=`` or
    ``filename=`` is a read in any call.  An ``include`` or ``use`` whose
    target cannot be taken as written is a read of an unknown file.
    """
    if not any(word in code for word in _READ_WORDS):
        return []
    tokens = _tokens(code)
    reads: list[tuple[str, str | None]] = []
    depth = 0
    for i, (kind, text) in enumerate(tokens):
        if kind == "p" and text in _OPENERS:
            depth += 1
        elif kind == "p" and text in _CLOSERS:
            depth = max(0, depth - 1)
        elif kind == "inc":
            reads.append(("include", text))
        elif kind != "id" or i + 1 >= len(tokens):
            continue
        elif text in ("include", "use") and tokens[i + 1] == ("p", "<"):
            # Something between the word and its target (a comment) kept the
            # tokenizer from reading the target: a read of an unknown file.
            reads.append(("include", None))
        elif text in READERS and tokens[i + 1] == ("p", "("):
            start = i + 2
            while start < len(tokens):
                argument, end = _argument(tokens, start)
                named = len(argument) >= 2 and argument[0][0] == "id" and argument[1] == ("p", "=")
                if argument and not named and not _plain_literal(argument):
                    single = len(argument) == 1 and argument[0][0] == "str"
                    reads.append(("read", argument[0][1] if single else None))
                if end >= len(tokens) or tokens[end] != ("p", ","):
                    break
                start = end + 1
        elif text in FILE_ARGS and depth > 0 and tokens[i + 1] == ("p", "="):
            value, _end = _argument(tokens, i + 2)
            reads.append(("read", value[0][1] if len(value) == 1 and value[0][0] == "str" else None))
    return reads


def is_bundled_library(path: str | None) -> bool:
    """Whether an ``include`` / ``use`` target is a file inside one of
    :data:`LIBRARIES`, named from the library's own folder."""
    if path is None or "\\" in path or "\x00" in path or path.startswith("/"):
        return False
    parts = path.split("/")
    return ".." not in parts and parts[0] in LIBRARIES


def reads_beyond_bundled_libraries(code: str) -> bool:
    """Whether *code* reads any file other than one of Kiln's bundled
    libraries through ``include`` / ``use``.  A read whose file the source
    does not name counts."""
    return any(
        how != "include" or not is_bundled_library(path)
        for how, path in file_reads(code)
    )


__all__ = [
    "FILE_ARGS",
    "LIBRARIES",
    "READERS",
    "file_reads",
    "is_bundled_library",
    "reads_beyond_bundled_libraries",
]
