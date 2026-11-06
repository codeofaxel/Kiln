"""Commands one printer's firmware must not be sent, or quietly ignores.

Kiln's G-code check judges temperatures and speeds against a printer's limits.
Some firmware also has a command that hangs it, or takes a command another
firmware pauses on and does nothing.  Those are facts about one model, so they
live in ``data/firmware_command_rules.json`` keyed by catalogue id, and the
one line validator every G-code door passes through reads them here.

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
    instead: str | None = None

    def names(self, command: str, line: str) -> bool:
        if command != self.command:
            return False
        if self.parameter is None:
            return True
        # Firmware reads a parameter glued to the command ("M2003P") as
        # readily as a spaced one, so the letter is looked for anywhere after
        # the command word, wherever a parameter can begin.
        head = _COMMAND_WORD.match(line)
        rest = line[head.end() :] if head else line
        return re.search(rf"(?<![A-Za-z]){re.escape(self.parameter)}", rest, re.IGNORECASE) is not None


@dataclass(frozen=True)
class CommandRules:
    """One printer's rules."""

    printer_id: str
    refused: tuple[_Rule, ...]
    ignored: tuple[_Rule, ...]

    def judge(self, command: str, line: str) -> tuple[str, str] | None:
        """``(REFUSED | IGNORED, the sentence to say)`` for *line*, whose
        command word is *command*, or ``None`` when no rule names it."""
        for rule in self.refused:
            if rule.names(command, line):
                return REFUSED, f"{rule.why}. Kiln will not send it."
        for rule in self.ignored:
            if rule.names(command, line):
                tail = f" {rule.instead}." if rule.instead else ""
                return IGNORED, f"{rule.why}.{tail}"
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
                instead=str(item["instead"]).strip().rstrip(".") if item.get("instead") else None,
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
