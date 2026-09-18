"""Every door that starts a print, read from the source, and whether it is gated.

A consent rule wired into some of the commands that start prints is not a
rule: the agent that does not want to render a preview calls a different
command.  The rule is only as good as the list of doors it covers, and a
list kept by hand is the same bug wearing a different hat — the CLI's
``print`` command was not on it, and a test print went to the machine with
nobody shown a preview.

So the list is not kept by hand.  This module walks the source and finds
every place a print can be started or queued to start later:

* a call to ``<something>.start_print(...)`` — the adapter, the machine;
* a call that puts a job into the queue the scheduler drains
  (``submit``, ``submit_result``, ``submit_job_result``, ``save_job``);
* a call to ``<something>.send_gcode(...)`` — raw words to the firmware,
  which can start a file on the printer (``M23``/``M24``, ``M32``) around
  every other door.

For the first two, it asks whether the function containing the call — or a
function enclosing that one, for the closures the pipelines and watchers
use — calls a clearing helper directly: the tools' gate, the sign-off
primitives it is built on, or the CLI's.  For the raw door, the clearing
helper is the G-code validator, which refuses the print-start words; a raw
send of fixed commands that are visibly not a start (``["G28"]``) clears
itself.  A door that has none of these is BYPASSING, unless it is named
below with the reason it is allowed to be.

``kiln doctor`` prints the verdict; a test fails on any bypassing door, so a
new start path is refused by CI until it takes the gate.  Same discipline as
``test_every_door_aims``: read the source, so a door added next year is
covered without anyone remembering this file exists.  The adapter template
in ``kiln.printers.base`` is the backstop at run time; this is the one that
says so before anything runs.
"""

from __future__ import annotations

import ast
import pathlib
import re
from dataclasses import dataclass

_SRC = pathlib.Path(__file__).parent

#: The modules a print can be started from.  ``kiln.printers`` is where the
#: adapters DEFINE ``start_print``; every caller lives here.
_MODULE_GLOBS = (
    "server.py",
    "pipelines.py",
    "scheduler.py",
    "job_splitter.py",
    "bed_leveling.py",
    "emergency.py",
    "plugins/*.py",
    "cli/*.py",
)

#: A call by one of these names is a door.
_START_CALLS = frozenset({"start_print"})
_QUEUE_CALLS = frozenset({"submit", "submit_result", "submit_job_result", "save_job"})
_RAW_CALLS = frozenset({"send_gcode"})

#: A raw send is cleared by the validator that refuses print-start words.
RAW_GATE_HELPERS = frozenset({
    "validate_gcode",
    "validate_gcode_for_printer",
    "_validate_gcode_impl",
})

#: The words the validator refuses; a fixed literal send is judged against
#: them here so the walk does not have to trust a comment.  Kept in step
#: with ``kiln.gcode._PRINT_START_COMMANDS`` by a test.
PRINT_START_WORDS = frozenset({"M23", "M24", "M32"})

#: A direct call to one of these, in the door's function or one enclosing
#: it, clears the door: the tools' gate, the sign-off primitives it grants
#: through (``kiln.print_signoff``), and the CLI's two.
GATE_HELPERS = frozenset({
    "_preview_gate_error",
    "token_verdict",
    "grant",
    "grant_from_record",
    "cli_gate",
    "confirm_print_at_terminal",
})

#: Receivers whose ``submit`` is a thread pool, not a print queue.
_NOT_A_QUEUE = ("executor", "pool", "loop")

#: Doors allowed to start a print without calling a helper themselves, each
#: with the reason.  Named here rather than skipped silently, and reported
#: by ``kiln doctor`` as exemptions so the count is honest.
EXEMPT: dict[tuple[str, str], str] = {
    ("server.py", "_persist_event"): (
        "mirrors a job the queue already holds into the database on a "
        "queue event; not a submission"
    ),
    ("server.py", "set_temperature"): (
        "raw send of the heater-off setpoints it builds itself (M104 S0, M140 S0)"
    ),
    ("bed_leveling.py", "trigger_level"): (
        "raw send of the one leveling command chosen from a fixed table"
    ),
    ("emergency.py", "_send_emergency_gcode"): (
        "raw send of the emergency stop sequence; nothing here starts a print"
    ),
}


@dataclass(frozen=True)
class PrintDoor:
    """One place in the source a print can be started from."""

    module: str
    function: str
    line: int
    kind: str  # "start" (adapter.start_print), "queue" (a job the scheduler will start), "raw" (send_gcode)
    gated_by: str | None  # a gate helper, "exempt: <reason>", or None when bypassing

    @property
    def bypasses(self) -> bool:
        return self.gated_by is None

    @property
    def exempt(self) -> bool:
        return bool(self.gated_by and self.gated_by.startswith("exempt:"))

    @property
    def label(self) -> str:
        return f"{self.module}::{self.function}"


def _modules() -> list[pathlib.Path]:
    out: list[pathlib.Path] = []
    for pattern in _MODULE_GLOBS:
        out.extend(sorted(_SRC.glob(pattern)))
    return out


def _call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def _receiver_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Attribute):
        base = func.value
        if isinstance(base, ast.Name):
            return base.id
        if isinstance(base, ast.Attribute):
            return base.attr
        if isinstance(base, ast.Call):
            return _call_name(base)
    return ""


def _is_door(node: ast.Call) -> str | None:
    """``"start"``, ``"queue"`` or ``None``."""
    if not isinstance(node.func, ast.Attribute):
        # ``start_print(...)`` by bare name is the server tool, which is
        # itself a door found here and gated inside; it is not the machine.
        return None
    name = node.func.attr
    if name in _START_CALLS:
        return "start"
    if name in _RAW_CALLS:
        return "raw"
    if name in _QUEUE_CALLS:
        receiver = _receiver_name(node).lower()
        if any(word in receiver for word in _NOT_A_QUEUE):
            return None
        return "queue"
    return None


_CMD_WORD = re.compile(r"^\s*([A-Za-z])\s*(\d+)")


def _static_text(node: ast.AST) -> str | None:
    """The text of a string literal, or the leading fixed part of an f-string.

    Only the command word matters, and it is the first thing on the line, so
    an f-string is readable only when it STARTS with literal text; one that
    opens with an interpolation (``f"{word} S0"``) has a word nobody can
    read from the source.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr) and node.values:
        head = node.values[0]
        if isinstance(head, ast.Constant) and isinstance(head.value, str):
            return head.value
    return None


def _fixed_commands_are_not_a_start(node: ast.Call) -> bool:
    """True when every command in the call is a literal whose word is
    known and is not a print start.  A variable, or an unparseable word,
    is not vouched for."""
    if not node.args:
        return False
    arg = node.args[0]
    elements = list(arg.elts) if isinstance(arg, (ast.List, ast.Tuple)) else [arg]
    if not elements:
        return False
    for element in elements:
        text = _static_text(element)
        if text is None:
            return False
        m = _CMD_WORD.match(text)
        if m is None:
            return False
        if f"{m.group(1).upper()}{int(m.group(2))}" in PRINT_START_WORDS:
            return False
    return True


_FunctionNode = ast.FunctionDef | ast.AsyncFunctionDef


def _direct_calls(fn: _FunctionNode) -> set[str]:
    """Names called in ``fn``'s own body, not inside functions nested in it.

    Plugin tools live inside one ``register()``; a gate call inside one
    tool must not vouch for its siblings.
    """
    names: set[str] = set()
    stack: list[ast.AST] = list(fn.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        if isinstance(node, ast.Call):
            names.add(_call_name(node))
        stack.extend(ast.iter_child_nodes(node))
    return names


def _enclosing_chain(tree: ast.AST, lineno: int) -> list[_FunctionNode]:
    """Functions containing ``lineno``, innermost first."""
    holders = [
        f for f in ast.walk(tree)
        if isinstance(f, _FunctionNode) and f.lineno <= lineno <= (f.end_lineno or 0)
    ]
    return sorted(holders, key=lambda f: (f.end_lineno or 0) - f.lineno)


def enumerate_print_doors() -> list[PrintDoor]:
    """Every door in the source, in file order, each with its verdict."""
    doors: list[PrintDoor] = []
    for path in _modules():
        rel = str(path.relative_to(_SRC))
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            kind = _is_door(node)
            if kind is None:
                continue
            chain = _enclosing_chain(tree, node.lineno)
            if not chain:
                continue  # module-level example code, not a runnable door
            owner = chain[0].name
            gated_by: str | None = None
            helpers = RAW_GATE_HELPERS if kind == "raw" else GATE_HELPERS
            if kind == "raw" and _fixed_commands_are_not_a_start(node):
                gated_by = "fixed commands"
            for fn in chain:
                if gated_by:
                    break
                hit = _direct_calls(fn) & helpers
                if hit:
                    gated_by = sorted(hit)[0]
            if gated_by is None:
                for fn in chain:
                    reason = EXEMPT.get((rel, fn.name))
                    if reason:
                        gated_by = f"exempt: {reason}"
                        break
            doors.append(PrintDoor(rel, owner, node.lineno, kind, gated_by))
    return doors


def stale_exemptions(doors: list[PrintDoor] | None = None) -> list[str]:
    """Exemptions naming a door that no longer exists — a dead line in a
    safety table is a line somebody will copy."""
    doors = enumerate_print_doors() if doors is None else doors
    seen = {(d.module, d.function) for d in doors}
    # An exemption may name an enclosing function rather than the closure
    # that holds the call, so accept either level.
    enclosing: set[tuple[str, str]] = set()
    for path in _modules():
        rel = str(path.relative_to(_SRC))
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for d in doors:
            if d.module == rel:
                for fn in _enclosing_chain(tree, d.line):
                    enclosing.add((rel, fn.name))
    return [f"{m}::{f}" for (m, f) in EXEMPT if (m, f) not in seen | enclosing]


def summarize(doors: list[PrintDoor] | None = None) -> tuple[bool, str]:
    """``(ok, one line for kiln doctor)``."""
    doors = enumerate_print_doors() if doors is None else doors
    bypassing = sorted({d.label for d in doors if d.bypasses})
    exempt = sorted({d.label for d in doors if d.exempt})
    stale = stale_exemptions(doors)
    total = len({(d.module, d.function, d.line) for d in doors})
    if bypassing or stale:
        parts = []
        if bypassing:
            parts.append("DOORS BYPASSING: " + ", ".join(bypassing))
        if stale:
            parts.append("stale exemptions: " + ", ".join(stale))
        return False, f"{total} start doors; " + "; ".join(parts)
    line = f"{total} start doors, all gated"
    if exempt:
        line += f" ({len(exempt)} named exemption{'s' if len(exempt) != 1 else ''}: {', '.join(exempt)})"
    return True, line
