"""Commands one printer's firmware must not be sent, or quietly ignores."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

_RULES_PATH = Path(__file__).resolve().parent / "data" / "firmware_command_rules.json"

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
        """Each refused command as ``(regex over a cleaned line, the sentence)``, for a door that matches lines by pattern (live interception)."""
        out = []
        for rule in self.refused:
            tail = f"[^;]*{re.escape(rule.parameter)}" if rule.parameter else ""
            out.append((rf"^\s*{re.escape(rule.command)}(?!\d){tail}", f"{rule.why}. Kiln will not send it."))
        return out

    def judge(self, command: str, line: str) -> tuple[str, str] | None:
        """``(REFUSED | IGNORED, the sentence to say)`` for *line*, whose command word is *command*, or ``None`` when no rule names it."""
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
    """The rules for the printer *printer_id* names, in any spelling the catalogue knows, or ``None``."""
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
