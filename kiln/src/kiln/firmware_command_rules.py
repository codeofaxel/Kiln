"""Commands one printer's firmware must not be sent, or quietly ignores.

Kiln's G-code check judges temperatures and speeds against a printer's limits.
Some firmware also has a command that hangs it, or takes a command another
firmware pauses on and does nothing.  Those are facts about one model, so they
live in ``data/firmware_command_rules.json`` keyed by catalogue id.  The line
validator a typed command and a print file pass through reads them here, and
so does a live interception session, which keeps its own rule list.

A refused command is blocked like any other blocked command.  An ignored one
passes with a warning that says what the printer will not do.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

_RULES_PATH = Path(__file__).resolve().parent / "data" / "firmware_command_rules.json"

#: The command word at the head of a cleaned line: a letter and its number.
_COMMAND_WORD = re.compile(r"\s*[A-Za-z]\s*\d+(?:\.\d+)?")

REFUSED = "refused"
IGNORED = "ignored"


@dataclass(frozen=True)
class _Rule:
    command: str
    parameter: str | None
    why: str

    def names(self, command: str, line: str) -> bool:
        if command != self.command:
            return False
        if self.parameter is None:
            return True
        # Firmware takes every letter after the command word as the start of
        # a parameter, spaced or not ("M2003P", "M2003CP"), so the letter
        # counts wherever it appears.  That errs toward refusing, which is the
        # side to err on for a command that must not be sent.
        head = _COMMAND_WORD.match(line)
        rest = line[head.end() :] if head else line
        return self.parameter in rest.upper()


@dataclass(frozen=True)
class CommandRules:
    """One printer's rules."""

    printer_id: str
    refused: tuple[_Rule, ...]
    ignored: tuple[_Rule, ...]

    def refused_patterns(self) -> list[tuple[str, str]]:
        """Each refused command as ``(regex over a cleaned line, the sentence)``,
        for a door that matches lines by pattern (live interception).  A
        parameter counts wherever it sits after the command, as :meth:`_Rule.names`
        reads it."""
        out = []
        for rule in self.refused:
            tail = f"[^;]*{re.escape(rule.parameter)}" if rule.parameter else ""
            out.append((rf"^\s*{re.escape(rule.command)}(?!\d){tail}", f"{rule.why}. Kiln will not send it."))
        return out

    def judge(self, command: str, line: str) -> tuple[str, str] | None:
        """``(REFUSED | IGNORED, the sentence to say)`` for *line*, whose
        command word is *command*, or ``None`` when no rule names it."""
        for rule in self.refused:
            if rule.names(command, line):
                return REFUSED, f"{rule.why}. Kiln will not send it."
        for rule in self.ignored:
            if rule.names(command, line):
                return IGNORED, f"{rule.why}."
        return None


def _rules(raw: Any) -> tuple[_Rule, ...]:
    out = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, Mapping) or not item.get("command") or not item.get("why"):
            continue
        parameter = str(item["with"]).strip().upper() if item.get("with") else None
        out.append(
            _Rule(
                command=str(item["command"]).strip().upper(),
                parameter=parameter,
                why=str(item["why"]).strip().rstrip("."),
            )
        )
    return tuple(out)


@lru_cache(maxsize=1)
def _table() -> dict[str, CommandRules]:
    try:
        data = json.loads(_RULES_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    printers = data.get("printers") if isinstance(data, dict) else None
    table = {}
    for key, row in (printers or {}).items():
        if isinstance(row, Mapping):
            table[str(key)] = CommandRules(str(key), _rules(row.get("refused")), _rules(row.get("ignored")))
    return table


def rules_for(printer_id: str | None) -> CommandRules | None:
    """The rules for the printer *printer_id* names, in any spelling the
    catalogue knows, or ``None``.  A printer with no row has no rule."""
    if not printer_id:
        return None
    table = _table()
    if not table:
        return None
    from kiln.printers.bed_fit import _printer_id_candidates

    for candidate in _printer_id_candidates(printer_id):
        if candidate in table:
            return table[candidate]
    from kiln.printer_profile_ids import map_printer_hint_to_profile_id

    resolved = map_printer_hint_to_profile_id(str(printer_id))
    return table.get(resolved) if resolved else None


__all__ = ["IGNORED", "REFUSED", "CommandRules", "rules_for"]
