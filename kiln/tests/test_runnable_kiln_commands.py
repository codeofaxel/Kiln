"""A command Kiln tells a person to run is built where Kiln runs, never typed in.

Plugins start Kiln through uvx (the Claude Code plugin, the Hermes package), so
the person's terminal has no ``kiln`` and a message saying "run ``kiln signin``"
names a command that fails.  Messages build the line with
:func:`kiln.cli_command.kiln_command`, which names one that runs on this
computer.  This gate reads the real source and refuses a literal
``kiln <command>`` in any string outside a docstring and outside ``kiln/cli/``
(where the person is already running kiln).

The commands it knows are the CLI's own, read from the live command group, so a
new command is covered the day it is added.  A string that names a command
without asking anyone to type it (a label, a description, the CLI's own words
kept outside the CLI) sits in ``ALLOWED`` with its count and its reason; an
entry the source no longer matches fails as stale.
"""

from __future__ import annotations

import ast
import re
from collections import Counter
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "kiln"

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


def _known_commands() -> list[str]:
    from kiln.cli.main import cli

    return sorted(cli.commands, key=len, reverse=True)


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
