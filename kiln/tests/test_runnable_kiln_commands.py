"""A command Kiln tells a person to run is built where Kiln runs, never typed in.

Plugins start Kiln through uvx (the Claude Code plugin, the Hermes package), so
the person's terminal has no ``kiln`` and a message saying "run ``kiln signin``"
names a command that fails.  Messages build the line with
:func:`kiln.cli_command.kiln_command`, which names one that runs on this
computer.  This gate reads the real source and refuses a literal
``kiln <command>`` in any string outside a docstring and outside ``kiln/cli/``
(where the person is already running kiln).

The commands it knows are the CLI a plain ``pip install kiln3d`` has, read from
the live command group in a process where kiln-pro cannot load, so a new
command is covered the day it is added and a command only kiln-pro registers
never counts.  A string that names a command without asking anyone to type it
(a label, a description, the CLI's own words kept outside the CLI) sits in
``ALLOWED`` with its count and its reason; an entry the source no longer
matches fails as stale.  And every line built with ``kiln_command`` has to
name a command that exists there, with options it takes.
"""

from __future__ import annotations

import ast
import functools
import json
import os
import re
import shlex
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "kiln"

# Prints the plain-install command tree: path -> group?, options, argument room.
_TREE = r"""
import json, sys
sys.modules["kiln_pro"] = None
import click
from kiln.cli.main import cli

def walk(cmd, path, out):
    options = {}
    for p in cmd.params:
        if isinstance(p, click.Option):
            for o in p.opts + p.secondary_opts:
                options[o] = not (p.is_flag or p.count)
    nargs = [p.nargs for p in cmd.params if isinstance(p, click.Argument)]
    out[" ".join(path)] = {
        "group": isinstance(cmd, click.Group) and not cmd.invoke_without_command,
        "options": options,
        "room": -1 if -1 in nargs else sum(nargs),
    }
    for name, sub in getattr(cmd, "commands", {}).items():
        walk(sub, path + [name], out)

out = {}
walk(cli, [], out)
print(json.dumps(out))
"""

#: (file under kiln/src/kiln, "kiln <command>") -> (how many, why it stays literal).
ALLOWED: dict[tuple[str, str], tuple[int, str]] = {
    ("bridge_version.py", "kiln bridge"): (
        1,
        "RESTART_COMMAND is read only by `kiln bridge status` (cli/bridge_commands.py), "
        "where the person is already running kiln and every other line names it too",
    ),
    ("camera_check.py", "kiln auth"): (
        2,
        'the "cli" door\'s wording, chosen only for `kiln stream --check`, '
        "where the person is already running kiln",
    ),
    ("camera_check.py", "kiln stream"): (
        1,
        'the "cli" door\'s wording, chosen only for `kiln stream --check`',
    ),
    ("consent_windows.py", "kiln consent"): (
        1,
        "the audit log's name for the door a window was opened through, not an instruction",
    ),
    ("outbound_guard.py", "kiln manual"): (
        1,
        "describes what a CLI sender sends, in the senders roster; nobody is asked to run it",
    ),
    ("print_consent.py", "kiln consent"): (
        2,
        "the consent record's name for the door that gave the yes, not an instruction",
    ),
    ("printer_setup.py", "kiln set-model"): (
        2,
        "CLI_REMEDY is printed only by the CLI (cli/main.py, cli/printer_model_prompt.py); "
        "the agent's remedy is AGENT_REMEDY beside it",
    ),
    ("served_answer.py", "kiln signin"): (
        1,
        "describes a CLI module in the served-doors roster; nobody is asked to run it",
    ),
    ("skill_manifest.py", "kiln status"): (
        1,
        "the agent rule names the CLI only to say to use the tool instead",
    ),
    ("streaming.py", "kiln serve"): (
        1,
        "said by Kiln's hosted server about the person's own computer, whose command "
        "line that process cannot know",
    ),
}


@functools.lru_cache(maxsize=1)
def _plain_cli() -> dict[str, Any]:
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(SRC.parent), os.environ.get("PYTHONPATH", "")])}
    env["KILN_SANDBOX"] = "1"
    # About a second on an idle machine; the ceiling only bounds a hang, and a
    # machine running several test workers at once has measured past 120 s.
    out = subprocess.run(
        [sys.executable, "-c", _TREE], capture_output=True, text=True, env=env, timeout=600, check=True
    )
    return json.loads(out.stdout.strip().splitlines()[-1])


def _known_commands() -> list[str]:
    top = [path for path in _plain_cli() if path and " " not in path]
    return sorted(top, key=len, reverse=True)


def _pattern(commands: list[str]) -> re.Pattern[str]:
    # Not part of a path ("~/.kiln config"), a name ("kiln_signin") or a
    # longer word; "kiln" followed by the CLI's own command, as typed.
    names = "|".join(re.escape(c) for c in commands)
    return re.compile(rf"(?<![\w./~\-])kiln ({names})(?![\w\-])")


def _docstrings(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                ids.add(id(body[0].value))
    return ids


def literal_commands(source: str, pattern: re.Pattern[str]) -> list[tuple[int, str, str]]:
    """Each ``kiln <command>`` typed into a string: (line, command, the string).

    Python joins implicitly concatenated literals, f-string pieces included,
    before this sees them, so a command split across source lines is one hit.
    """
    tree = ast.parse(source)
    skip = _docstrings(tree)
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skip:
            hits.extend(
                (node.lineno, f"kiln {m.group(1)}", node.value) for m in pattern.finditer(node.value)
            )
    return hits


def _source_hits() -> dict[tuple[str, str], list[tuple[int, str]]]:
    pattern = _pattern(_known_commands())
    found: dict[tuple[str, str], list[tuple[int, str]]] = {}
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC)
        if rel.parts[0] == "cli":
            continue
        for line, command, text in literal_commands(path.read_text(encoding="utf-8"), pattern):
            found.setdefault((rel.as_posix(), command), []).append((line, text))
    return found


def test_no_message_types_a_kiln_command():
    over = []
    for key, hits in sorted(_source_hits().items()):
        allowed = ALLOWED.get(key, (0, ""))[0]
        if len(hits) > allowed:
            where = "; ".join(f"line {line}: {text.strip()[:90]!r}" for line, text in hits)
            over.append(f"{key[0]}: {len(hits)} x {key[1]!r} (allowed {allowed}) -- {where}")
    assert not over, (
        "Build the command with kiln.cli_command.kiln_command('<subcommand>') so it runs "
        "where no kiln is on PATH, or add an ALLOWED entry saying why nobody types it:\n  "
        + "\n  ".join(over)
    )


def test_every_allowed_entry_is_still_found():
    found = _source_hits()
    stale = [
        f"{path}: {command!r} allowed {count}, found {len(found.get((path, command), []))}"
        for (path, command), (count, _) in sorted(ALLOWED.items())
        if len(found.get((path, command), [])) < count
    ]
    assert not stale, "Lower or delete these ALLOWED entries:\n  " + "\n  ".join(stale)


def test_every_allowed_entry_says_why():
    assert all(count > 0 and why.strip() for count, why in ALLOWED.values())


# --- the reader sees every way a command can be typed -----------------------

_SHAPES = '''
"""Module docstring: run ``kiln signin`` -- never read."""
PLAIN = "Run `kiln signin` first."
SPLIT = (
    "run `kiln consent "
    "window --for 2h`"
)
def f(name):
    """Docstring: kiln doctor -- never read."""
    msg = f"try `kiln print {name}` or `kiln queue submit {name}`"
    return {"command": f"kiln design unlock {name}", "tool": "lock_design"}
FINE = ["~/.kiln setup", "kiln_signin", "the kiln setup-agent page", "kiln3d signin"]
'''


def test_the_reader_finds_each_shape_and_skips_docstrings():
    pattern = _pattern(["signin", "consent", "print", "queue", "design", "doctor", "setup"])
    commands = Counter(command for _, command, _ in literal_commands(_SHAPES, pattern))
    assert commands == Counter(
        {"kiln signin": 1, "kiln consent": 1, "kiln print": 1, "kiln queue": 1, "kiln design": 1}
    )


def test_the_known_commands_are_the_clis_own():
    names = _known_commands()
    assert {"signin", "accept-terms", "consent", "doctor", "setup", "plate"} <= set(names)


@pytest.mark.parametrize("subcommand", ["signin", "consent window --for 2h --printer NAME"])
def test_kiln_command_output_passes_the_gate_only_through_the_helper(subcommand, monkeypatch):
    """The helper's own output names kiln (that is the point); the gate reads source, not output."""
    from kiln import cli_command

    monkeypatch.setenv(cli_command.CLI_ENV, "uvx --from kiln3d==9.9.9 kiln")
    assert cli_command.kiln_command(subcommand).endswith(f"kiln {subcommand}")
    source = f"X = kiln_command({subcommand!r})\n"
    assert literal_commands(source, _pattern(_known_commands())) == []


# --- defects a lap found (2026-10-28; each fails at dd5d5be9) ---


def _words(node: ast.expr) -> list[str] | None:
    """The words of a literal subcommand; a formatted value reads as ``{}``."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value.split()
    if isinstance(node, ast.JoinedStr):
        return "".join(v.value if isinstance(v, ast.Constant) else " {} " for v in node.values).split()
    return None


def command_line_problem(words: list[str], tree: dict[str, Any]) -> str | None:
    """Why ``kiln <words>`` would not run on a plain install, or ``None``.

    Placeholders (``NAME``, ``<file>``, a formatted ``{}``) count as arguments.
    """
    path: list[str] = []
    for word in words:
        if " ".join([*path, word]) not in tree:
            break
        path.append(word)
    rest = words[len(path):]
    said = "kiln " + " ".join(path) if path else "kiln"
    if not path or tree[" ".join(path)]["group"]:
        return f"`{said}` has no command {rest[0]!r}" if rest else f"`{said}` needs one of its commands"
    node = tree[" ".join(path)]
    arguments = 0
    tokens = iter(rest)
    for word in tokens:
        if word.startswith("-") and word != "-":
            name = word.split("=", 1)[0]
            if name not in node["options"]:
                return f"`{said}` has no option {name}"
            if node["options"][name] and "=" not in word:
                next(tokens, None)
        else:
            arguments += 1
    if node["room"] != -1 and arguments > node["room"]:
        return f"`{said}` takes {node['room']} argument(s), not {arguments}"
    return None


def _named_lines() -> list[tuple[str, int, list[str] | None]]:
    """Each ``kiln_command(...)`` call outside the CLI: (file, line, its words)."""
    found = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC)
        if rel.parts[0] == "cli" or rel.name == "cli_command.py":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            func = getattr(node, "func", None)
            if not isinstance(node, ast.Call) or getattr(func, "id", getattr(func, "attr", None)) != "kiln_command":
                continue
            words = _words(node.args[0]) if node.args else None
            if words is not None:
                for value in node.args[1:]:
                    if isinstance(value, ast.Starred):
                        continue  # an optional flag and its value; their count is not known here
                    literal = value.value if isinstance(value, ast.Constant) else None
                    words.append(literal if isinstance(literal, str) and literal.startswith("-") else "{}")
            found.append((rel.as_posix(), node.lineno, words))
    return found


def test_every_named_command_exists_on_a_plain_install():
    """A lap found two messages naming commands no install has: a push refusal
    said a design id is "as `kiln design` lists it" (``design`` only holds
    lock/unlock/archive), and the fulfillment auth refusal said "See: kiln
    auth setup" (``auth`` saves printer credentials and takes no argument).
    The literal gate asked only whether a line was BUILT where Kiln runs, never
    whether the command it built exists."""
    tree = _plain_cli()
    lines = _named_lines()
    assert len(lines) > 50, "the reader stopped seeing kiln_command calls"
    problems = [
        f"{path}:{line}: " + (command_line_problem(words, tree) if words is not None
                              else "pass Kiln's own words as a literal, values after them")
        for path, line, words in lines
        if words is None or command_line_problem(words, tree)
    ]
    assert not problems, "Name a command a plain install has:\n  " + "\n  ".join(problems)


def test_the_command_reader_can_fail():
    tree = {
        "": {"group": True, "options": {}, "room": 0},
        "design": {"group": True, "options": {}, "room": 0},
        "design lock": {"group": False, "options": {"--json": False}, "room": 1},
        "consent": {"group": True, "options": {}, "room": 0},
        "consent window": {"group": False, "options": {"--for": True, "--printer": True}, "room": 0},
        "auth": {"group": False, "options": {"--name": True}, "room": 0},
    }
    assert command_line_problem(["design"], tree) == "`kiln design` needs one of its commands"
    assert command_line_problem(["merge", "run"], tree) == "`kiln` has no command 'merge'"
    assert command_line_problem(["auth", "setup"], tree) == "`kiln auth` takes 0 argument(s), not 1"
    assert command_line_problem(["consent", "window", "--fleet"], tree) == "`kiln consent window` has no option --fleet"
    assert command_line_problem(["consent", "window", "--for", "2h", "--printer", "{}"], tree) is None
    assert command_line_problem(["design", "lock", "{}", "--json"], tree) is None


def test_a_name_with_a_space_stays_one_argument(monkeypatch):
    """Printer and file names went into the line unquoted, so a printer called
    "Garage A1" was told `kiln consent window --for 2h --printer Garage A1`,
    which hands the CLI two words.  Every test used one-word names."""
    from kiln import cli_command, server

    monkeypatch.setenv(cli_command.CLI_ENV, "kiln")
    message = server._no_yes_at_home("start_print", "my part.3mf", "Garage A1")
    lines = [shlex.split(line) for line in re.findall(r"`([^`]*\bkiln [^`]*)`", message)]
    assert ["kiln", "print", "my part.3mf"] in lines, message
    assert any(line[-2:] == ["--printer", "Garage A1"] for line in lines), message


def test_a_value_is_quoted_the_windows_way_there(monkeypatch):
    """The design-history commands quoted their ids with POSIX single quotes,
    which Windows' cmd does not read."""
    from kiln import cli_command

    monkeypatch.setenv(cli_command.CLI_ENV, "kiln")
    monkeypatch.setattr(cli_command, "_ON_WINDOWS", True)
    assert cli_command.kiln_command("design unlock", "my mug") == 'kiln design unlock "my mug"'
    assert cli_command.kiln_command("cloud push", "mug") == "kiln cloud push mug"
