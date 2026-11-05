"""A setting OpenSCAD ignored, read by the name its module really uses.

OpenSCAD ignores an argument a module has no parameter for and builds the
part at the module's default (:func:`kiln.openscad_runner.ignored_settings`
reports it).  :func:`resolve` takes the source that was compiled, those
reports, and a function that compiles a variant of the source, and for each
ignored setting:

- finds the call on the reported line and the module's own parameter list:
  the source itself first, then the files it includes or uses, found on
  OpenSCAD's library path (with their documented meanings where the library
  writes them);
- takes at most one parameter the setting can mean, never one the call
  already sets and never one of a different kind of size;
- uses it only when a build of that one call, measured, changes the way the
  setting says (a size measures its value along the axis that changed).

It returns the corrected source with one plain line per setting, or, when it
cannot be sure, a refusal naming the module's real settings with a short
meaning for each.  Free at every tier: it is part of compiling.  Pinned by
``tests/test_scad_settings.py``.
"""

from __future__ import annotations

import contextlib
import os
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from kiln.scad_main import _blank_comments_and_strings as _blank

__all__ = ["Resolution", "rebuild", "resolve"]

#: Plain size words: what a caller may write, the parameter names that mean
#: the same, a pattern for the same size in a documented meaning, and how a
#: build is checked (1: an extent equals the value; 2: equals twice it;
#: 0: the part changed).
_KINDS: dict[str, tuple[frozenset[str], frozenset[str], str | None, int]] = {
    "height": (frozenset({"height", "tall", "h", "ht"}), frozenset({"h", "height"}),
               r"\bheight\b|\btall\b|\b(?:base|bottom)\b.*\bto\b.*\b(?:tips?|top)\b", 1),
    "width": (frozenset({"width", "wide", "w"}), frozenset({"w", "width"}), r"\bwidth\b|\bwide\b", 1),
    "length": (frozenset({"length", "len", "long", "l"}), frozenset({"l", "len", "length"}),
               r"\blength\b|\blong\b", 1),
    "thickness": (frozenset({"thickness", "thick", "thk"}), frozenset({"thickness", "thick"}),
                  r"\bthickness\b|\bthick\b", 1),
    "depth": (frozenset({"depth", "deep"}), frozenset({"depth"}), r"\bdepth\b|\bdeep\b", 1),
    "diameter": (frozenset({"diameter", "dia", "diam", "d", "od", "outer_diameter"}),
                 frozenset({"d", "dia", "diam", "diameter", "od"}),
                 r"(?<!inner )(?<!inside )(?<!pitch )\bdiameter\b", 1),
    "inner diameter": (frozenset({"id", "inner_diameter", "inside_diameter", "bore"}),
                       frozenset({"id", "bore"}), r"\b(?:inner|inside) diameter\b|\bbore\b", 0),
    "radius": (frozenset({"radius", "rad", "r"}), frozenset({"r", "radius", "rad"}),
               r"(?<!inner )(?<!inside )(?<!pitch )\bradius\b", 2),
    "pitch": (frozenset({"pitch", "circular_pitch", "thread_pitch"}), frozenset({"pitch"}), None, 0),
    "count": (frozenset({"count", "n", "num", "number", "qty", "quantity"}),
              frozenset({"count", "n", "num"}), r"\bnumber of\b", 0),
}
#: Kinds that are the same size seen two ways, so a refusal can say so.
_PAIRED = {"diameter": "radius", "radius": "diameter"}

#: Words that open a statement or expression, never a module call.
_NOT_CALLS = frozenset({"module", "function", "if", "for", "let", "each", "assert",
                        "echo", "intersection_for", "else", "include", "use"})
_CALL = re.compile(r"\b([A-Za-z_]\w*)\s*\(")
_NAMED_ARG = re.compile(r"\s*(\$?[A-Za-z_]\w*)\s*=(?!=)")
_LIBRARY_LINE = re.compile(r"^\s*(?:include|use)\s*<([^>]+)>", re.MULTILINE)
_ECHOED = re.compile(r"ECHO:\s*KILN_SETTING\s*=\s*(-?[\d.]+(?:e[-+]?\d+)?)", re.IGNORECASE)
_DEFAULT = re.compile(r"\s*\(?\bDefault:.*$", re.IGNORECASE)
#: How far a measured size may sit from the value and still be it.
_TOLERANCE = 0.01
#: The longest a meaning runs in a refusal.
_MEANING_SHOWN = 90
# Most ignored settings one compile resolves; more is refused, so the
# measuring builds stay bounded.
_MOST_SETTINGS = 3

Compile = Callable[[str], "tuple[str | None, str]"]


@dataclass
class Param:
    name: str
    meaning: str = ""


@dataclass
class Definition:
    module: str
    params: list[Param]


@dataclass
class _Arg:
    name: str | None
    start: int  # the argument's first character
    end: int  # one past its last
    value: str


@dataclass
class _Call:
    name: str
    at: int  # the name's first character
    args: list[_Arg]
    is_module: bool


@dataclass
class Resolution:
    """What :func:`resolve` decided.

    ``source`` is the corrected source and ``lines`` says what was read,
    one line per setting; or ``source`` is None and ``refusal`` says why.
    ``edits`` are ``(offset, old name, new name)`` in the compiled source,
    so the same correction applies to any text it begins with
    (:meth:`apply`).
    """

    source: str | None = None
    lines: list[str] = field(default_factory=list)
    refusal: str | None = None
    edits: list[tuple[int, str, str]] = field(default_factory=list)

    def apply(self, text: str) -> str | None:
        """*text* with the same renames, or None when it does not hold them."""
        for at, old, new in sorted(self.edits, reverse=True):
            if text[at:at + len(old)] != old:
                return None
            text = text[:at] + new + text[at + len(old):]
        return text


# --- reading the source -----------------------------------------------------


def _closing(code: str, opening: int) -> int:
    """The index of the bracket closing the one at *opening* (-1 if none)."""
    depth = 0
    for i in range(opening, len(code)):
        if code[i] in "([{":
            depth += 1
        elif code[i] in ")]}":
            depth -= 1
            if depth == 0:
                return i
    return -1


def _split(code: str, start: int, end: int) -> Iterator[tuple[int, int]]:
    """``(start, end)`` of each comma-separated part of ``code[start:end]``."""
    depth, begin = 0, start
    for i in range(start, end):
        if code[i] in "([{":
            depth += 1
        elif code[i] in ")]}":
            depth -= 1
        elif code[i] == "," and depth == 0:
            yield begin, i
            begin = i + 1
    if code[begin:end].strip():
        yield begin, end


def _line_span(code: str, line: int) -> tuple[int, int]:
    start = 0
    for _ in range(line - 1):
        start = code.find("\n", start) + 1
        if start == 0:
            return len(code), len(code)
    end = code.find("\n", start)
    return start, len(code) if end < 0 else end


def _calls_on_line(source: str, code: str, line: int) -> list[_Call]:
    """Every call whose name starts on *line* (1-based)."""
    start, end = _line_span(code, line)
    calls = []
    for match in _CALL.finditer(code, start, end):
        name = match.group(1)
        if name in _NOT_CALLS:
            continue
        opening = match.end() - 1
        closing = _closing(code, opening)
        if closing < 0:
            continue
        args = []
        for a, b in _split(code, opening + 1, closing):
            named = _NAMED_ARG.match(code, a, b)
            value_at = named.end() if named else a
            args.append(_Arg(named.group(1) if named else None, a, b, source[value_at:b].strip()))
        before = code[:match.start()].rstrip()
        after_library_line = bool(_LIBRARY_LINE.match(before[before.rfind("\n") + 1:]))
        is_module = (not before or before[-1] in ";{})!#%*" or before.endswith("else")
                     or after_library_line)
        calls.append(_Call(name, match.start(), args, is_module))
    return calls


def _parameters(code: str, opening: int) -> list[str]:
    closing = _closing(code, opening)
    return [code[a:b].split("=")[0].strip() for a, b in _split(code, opening + 1, closing)]


def _meanings(text: str, name: str) -> dict[str, str]:
    """``{parameter: meaning}`` from a library's documented ``Arguments:``
    block for *name*, defaults left out; empty when there is none."""
    header = re.search(rf"^//\s*(?:Function&)?Module:\s*{re.escape(name)}\(\)", text, re.MULTILINE)
    if not header:
        return {}
    found: dict[str, str] = {}
    inside = False
    for line in text[header.end():].splitlines()[1:]:
        if not line.startswith("//"):
            break
        body = line[2:]
        if re.match(r"\s*(?:Function&)?Module:", body):
            break
        if re.match(r"\s*Arguments:\s*$", body):
            inside = True
            continue
        if inside and re.match(r"\s?[A-Z][\w &()]*:", body):
            break
        entry = re.match(r"\s{2,}([\w$]+(?:\s*/\s*[\w$]+)*)\s*=\s*(.*)", body) if inside else None
        if entry:
            meaning = _DEFAULT.sub("", entry.group(2)).strip()
            for each in entry.group(1).split("/"):
                found.setdefault(each.strip(), meaning)
    return found


def _library_files(source: str, folders: list[str]) -> Iterator[Path]:
    """Every file *source* includes or uses, and theirs, found in *folders*."""
    queue, seen = [source], set()
    while queue:
        text = queue.pop(0)
        for wanted in _LIBRARY_LINE.findall(_blank(text)):
            for folder in folders:
                path = Path(folder) / wanted.strip()
                if path.is_file():
                    resolved = path.resolve()
                    if resolved not in seen:
                        seen.add(resolved)
                        with contextlib.suppress(OSError, UnicodeDecodeError):
                            queue.append(resolved.read_text(encoding="utf-8"))
                            yield resolved
                    break


def find_definition(source: str, name: str, folders: list[str], *, module: bool = True) -> Definition | None:
    """*name*'s parameter list: from *source* itself, else from the first
    library file reached through its includes and uses that defines it."""
    word = "module" if module else "function"
    pattern = re.compile(rf"\b{word}\s+{re.escape(name)}\s*\(")
    code = _blank(source)
    match = pattern.search(code)
    if match:
        return Definition(name, [Param(p) for p in _parameters(code, match.end() - 1)])
    for path in _library_files(source, folders):
        text = path.read_text(encoding="utf-8")
        code = _blank(text)
        match = pattern.search(code)
        if match:
            meanings = _meanings(text, name)
            return Definition(name, [Param(p, meanings.get(p, "")) for p in _parameters(code, match.end() - 1)])
    return None


# --- choosing a parameter -----------------------------------------------------


def _kind_of_word(word: str) -> str | None:
    word = word.lower().removesuffix("_mm")
    return next((kind for kind, (words, _, _, _) in _KINDS.items() if word in words), None)


def _kinds_of(param: Param) -> set[str]:
    kinds = set()
    for kind, (_, names, pattern, _) in _KINDS.items():
        if param.name.lower() in names or (pattern and param.meaning and re.search(pattern, param.meaning, re.IGNORECASE)):
            kinds.add(kind)
    return kinds


def _one_edit(a: str, b: str) -> bool:
    """True when *b* is *a* with one letter added, dropped, or two swapped."""
    if a == b or abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        diff = [i for i in range(len(a)) if a[i] != b[i]]
        return len(diff) == 2 and diff[1] == diff[0] + 1 and a[diff[0]] == b[diff[1]] and a[diff[1]] == b[diff[0]]
    short, long_ = (a, b) if len(a) < len(b) else (b, a)
    return any(long_[:i] + long_[i + 1:] == short for i in range(len(long_)))


def _candidates(word: str, definition: Definition, bound: set[str]) -> tuple[list[Param], str | None]:
    """The parameters *word* can mean, and the kind of size it names."""
    kind = _kind_of_word(word)
    free = [p for p in definition.params if p.name not in bound and p.name != word]
    by_kind = [p for p in free if kind and kind in _kinds_of(p)]
    by_spelling = [p for p in free if _one_edit(word.lower(), p.name.lower())]
    found = by_kind + [p for p in by_spelling if p not in by_kind]
    return found, kind


# --- measuring ----------------------------------------------------------------


def _bounds(path: str):
    import numpy as np

    from kiln.mesh_frame import load_mesh

    mesh = load_mesh(path, file_type="stl", force="mesh")
    if not len(mesh.faces):
        return None
    return np.asarray(mesh.bounds, dtype=float), abs(float(mesh.volume))


def _edited(text: str, edits: list[tuple[int, int, str]]) -> str:
    for start, end, new in sorted(edits, reverse=True):
        text = text[:start] + new + text[end:]
    return text


def _name_at(source: str, arg: _Arg) -> int:
    """Where *arg*'s name starts."""
    return arg.start + len(source[arg.start:arg.end]) - len(source[arg.start:arg.end].lstrip())


def _without(code: str, call: _Call, arg: _Arg) -> tuple[int, int, str]:
    """An edit blanking *arg* and one comma beside it, keeping every line."""
    index = call.args.index(arg)
    start, end = arg.start, arg.end
    if index > 0:
        start = code.rfind(",", 0, start)
    elif len(call.args) > 1:
        end = code.find(",", end) + 1
    return start, end, re.sub(r"[^\n]", " ", code[start:end])


@dataclass
class _Measured:
    axis: str = ""
    before: float = 0.0
    after: float = 0.0


def _confirm(compile_stl: Compile, source: str, code: str, call: _Call, arg: _Arg,
             new: str, factor: int) -> _Measured | None:
    """A measured build of the call with *arg* renamed *new*, against the
    same call without it, when it changes the way the setting says."""
    root = (call.at, call.at, "!")
    name_at = _name_at(source, arg)
    eq = source.index("=", name_at + len(arg.name or ""))
    value = f"let(kiln_v = ({arg.value})) echo(KILN_SETTING = kiln_v) kiln_v"
    renamed = [(name_at, name_at + len(arg.name or ""), new), (eq + 1, arg.end, f" {value}")]
    before_stl, _ = compile_stl(_edited(source, [root, _without(code, call, arg)]))
    after_stl, said = compile_stl(_edited(source, [root, *renamed]))
    if not before_stl or not after_stl:
        return None
    before, after = _bounds(before_stl), _bounds(after_stl)
    if before is None or after is None:
        return None
    (b_box, b_vol), (a_box, a_vol) = before, after
    changed = float(abs(a_box - b_box).max()) > _TOLERANCE or abs(a_vol - b_vol) > _TOLERANCE * max(b_vol, 1.0)
    if not changed:
        return None
    if not factor:
        return _Measured()
    values = {float(v) for v in _ECHOED.findall(said)}
    if len(values) != 1:
        return None
    expected = values.pop() * factor
    slack = max(_TOLERANCE, _TOLERANCE * abs(expected))
    b_ext, a_ext = b_box[1] - b_box[0], a_box[1] - a_box[0]
    for i, axis in enumerate("xyz"):
        if abs(a_ext[i] - expected) <= slack and abs(a_ext[i] - b_ext[i]) > slack:
            return _Measured(axis, float(b_ext[i]), float(a_ext[i]))
    return None


# --- the decision ---------------------------------------------------------------


def _settings_list(definition: Definition) -> str:
    shown = []
    for p in definition.params:
        if p.name.startswith("$"):
            continue
        meaning = p.meaning if len(p.meaning) <= _MEANING_SHOWN else p.meaning[:_MEANING_SHOWN - 3].rstrip() + "..."
        shown.append(f"{p.name} ({meaning.rstrip('.')})" if meaning else p.name)
    return ", ".join(shown)


def _number(text: str) -> float | None:
    try:
        return float(text)
    except ValueError:
        return None


def _refusal(word: str, arg: _Arg, line: int, definition: Definition | None, call: _Call,
             why: str) -> str:
    said = f"{word}={arg.value}"
    if definition is None:
        return (f"OpenSCAD ignored {said} on {call.name}() (line {line}): {call.name}() has no setting "
                f"by that name, and Kiln could not read its definition to say which it has. "
                f"Use the name {call.name}() really takes.")
    return (f"OpenSCAD ignored {said} on {call.name}() (line {line}): {call.name}() has no setting by "
            f"that name{why}. Its settings: {_settings_list(definition)}. Use one of these names.")


def resolve(source: str, ignored: list[tuple[str, int | None]], compile_stl: Compile,
            *, folders: list[str] | None = None) -> Resolution:
    """Read each setting OpenSCAD ignored by the name its module uses.

    :param source: The source OpenSCAD compiled, as compiled.
    :param ignored: ``(setting, line)`` pairs from
        :func:`kiln.openscad_runner.ignored_settings`.
    :param compile_stl: Compiles a variant of *source* to an STL and returns
        ``(path or None, OpenSCAD's messages)``.
    :param folders: Where included and used files are found; OpenSCAD's
        library path when not given.
    :returns: A :class:`Resolution`: the corrected source and its lines, or
        a refusal when any setting cannot be read with certainty.
    """
    if folders is None:
        from kiln.openscad_runner import openscad_env

        folders = [f for f in openscad_env().get("OPENSCADPATH", "").split(os.pathsep) if f]
    if len(ignored) > _MOST_SETTINGS:
        names = ", ".join(word for word, _ in ignored)
        return Resolution(refusal=f"OpenSCAD ignored {len(ignored)} settings ({names}); Kiln reads at most "
                                  f"{_MOST_SETTINGS} in one compile. Use the names the modules really take.")
    code = _blank(source)
    sites: list[tuple[int, _Call, _Arg]] = []
    for word, line in ignored:
        found = [(line, c, a) for c in _calls_on_line(source, code, line or 0) for a in c.args if a.name == word]
        if not line or not found:
            return Resolution(refusal=f"OpenSCAD ignored the setting {word}"
                              + (f" on line {line}" if line else "")
                              + ", and Kiln could not find the call that passed it. Use the name the module really takes.")
        sites.extend(found)

    # Right to left, so a rename never moves a setting still to be read.
    sites.sort(key=lambda s: s[2].start, reverse=True)
    current, lines, edits = source, [], []
    for line, call, arg in sites:
        word = arg.name or ""
        definition = find_definition(current, call.name, folders, module=call.is_module)
        if definition is not None and word in {p.name for p in definition.params}:
            continue  # this call takes it; another call on the line did not
        if definition is None or not call.is_module:
            return Resolution(refusal=_refusal(word, arg, line, definition, call, ""))
        positional = sum(1 for a in call.args if a.name is None)
        bound = {a.name for a in call.args if a.name} | {p.name for p in definition.params[:positional]}
        found, kind = _candidates(word, definition, bound)
        if len(found) != 1:
            why = ""
            if len(found) > 1:
                why = f", and it could mean {', '.join(p.name for p in found[:-1])} or {found[-1].name}"
            elif kind in _PAIRED:
                pair = [p for p in definition.params if p.name not in bound and _PAIRED[kind] in _kinds_of(p)]
                if pair:
                    value = _number(arg.value)
                    other = (value * 2 if kind == "radius" else value / 2) if value is not None else None
                    why = (f"; {pair[0].name} is a {_PAIRED[kind]}, so {word}={arg.value} would be "
                           f"{pair[0].name}={other:g}" if other is not None else
                           f"; {pair[0].name} is a {_PAIRED[kind]}, not a {kind}")
            return Resolution(refusal=_refusal(word, arg, line, definition, call, why))
        chosen = found[0]
        how = kind if kind and kind in _kinds_of(chosen) else next(iter(sorted(_kinds_of(chosen))), None)
        factor = _KINDS[how][3] if how else 0
        code_now = _blank(current)
        measured = _confirm(compile_stl, current, code_now, call, arg, chosen.name, factor)
        if measured is None:
            why = f", and a build reading it as {chosen.name} did not measure as {word}={arg.value} says"
            return Resolution(refusal=_refusal(word, arg, line, definition, call, why))
        spelled = _one_edit(word.lower(), chosen.name.lower()) and not (kind and kind in _kinds_of(chosen))
        if spelled:
            reason = f"{call.name}() spells it {chosen.name}"
        elif chosen.meaning:
            reason = f'{call.name}() calls that size {chosen.name}: "{chosen.meaning.rstrip(".")}"'
        else:
            reason = f"{call.name}() calls that size {chosen.name}"
        if measured.axis:
            outcome = (f"measured, the part is {measured.after:.4g} mm along {measured.axis}, where it was "
                       f"{measured.before:.4g} mm with {word} ignored")
        else:
            outcome = f"measured, {chosen.name} changes the part, which {word} did not"
        lines.insert(0, f"Kiln read {word}={arg.value} on {call.name}() (line {line}) as "
                        f"{chosen.name}={arg.value}: {reason}; {outcome}.")
        name_at = _name_at(current, arg)
        edits.append((name_at, word, chosen.name))
        current = current[:name_at] + chosen.name + current[name_at + len(word):]
    return Resolution(source=current, lines=lines, edits=edits)


def rebuild(cmd: list[str], scad_path: str, refused, *, timeout: float | None,
            output_path: str) -> tuple[object, Resolution]:
    """Resolve a compile the runner refused for ignored settings, and build it.

    *cmd* compiled *scad_path* to *output_path* and came back *refused*
    (``ignored_settings`` set, nothing left out).  Variants are written beside
    *scad_path*, so its relative includes still resolve, and removed after.
    Returns the final build's result and the :class:`Resolution`; when the
    resolution refuses, the result is *refused* with the refusal as the first
    line of its messages.
    """
    import subprocess
    import tempfile
    import time

    from kiln.openscad_runner import openscad_env, run_openscad

    folder = os.path.dirname(scad_path) or "."
    made: list[str] = []

    def variant(code: str) -> str:
        fd, path = tempfile.mkstemp(suffix=".scad", prefix="kiln_setting_", dir=folder)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(code)
        made.append(path)
        return path

    # The measuring builds share one time budget; the final build gets its own.
    deadline = time.monotonic() + timeout if timeout else None

    def run(code: str, stl: str, *, budgeted: bool = False):
        path = variant(code)
        made.append(stl)
        argv = [stl if a == output_path else path if a == scad_path else a for a in cmd]
        limit = timeout
        if budgeted and deadline is not None:
            limit = deadline - time.monotonic()
            if limit <= 0:
                raise subprocess.TimeoutExpired(argv, 0)
        return run_openscad(argv, timeout=limit, output_path=stl)

    def compile_stl(code: str) -> tuple[str | None, str]:
        fd, stl = tempfile.mkstemp(suffix=".stl", prefix="kiln_setting_", dir=folder)
        os.close(fd)
        try:
            result = run(code, stl, budgeted=True)
        except subprocess.TimeoutExpired:
            return None, ""
        built = result.returncode == 0 and os.path.isfile(stl) and os.path.getsize(stl) > 0
        said = result.stderr if isinstance(result.stderr, str) else (result.stderr or b"").decode("utf-8", "replace")
        return (stl if built else None), said

    source = Path(scad_path).read_text(encoding="utf-8")
    folders = [folder, *(f for f in openscad_env().get("OPENSCADPATH", "").split(os.pathsep) if f)]
    try:
        resolution = resolve(source, list(refused.ignored_settings), compile_stl, folders=folders)
        if resolution.source is None:
            said = refused.stderr if isinstance(refused.stderr, str) else refused.stderr.decode("utf-8", "replace")
            rest = said.split("\n", 1)[1] if "\n" in said else ""
            refused.stderr = f"{resolution.refusal}\n{rest}"
            return refused, resolution
        return run(resolution.source, output_path), resolution
    finally:
        for path in made:
            if path != output_path:
                with contextlib.suppress(OSError):
                    os.remove(path)
