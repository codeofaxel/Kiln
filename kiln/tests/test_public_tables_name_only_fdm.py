"""The shipped knowledge tables speak only of filament printing.

Kiln drives FDM printers.  A data note that names another machine class, or a
process word from one, is read by every install and served by every free door
that returns the row, so these tables stay free of them.  The words are spelled
here because the word IS the rule; a pattern reworded into silence would pass
every table forever.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import kiln

_DATA = Path(kiln.__file__).parent / "data"

_TABLES = sorted((_DATA / "design_knowledge").glob("*.json")) + [
    _DATA / "component_catalog.json",
    _DATA / "design_templates.json",
]

_WORDS = re.compile(
    r"\b(?:resins?|m?sla|cnc|lasers?|laser-\w+|machin(?:ing|ed|ist)|"
    r"injection[ -]?mou?ld\w*|sinter(?:ed|ing)?|carbide|coolant)\b",
    re.IGNORECASE,
)


def _strings(node, path=""):
    if isinstance(node, str):
        yield path, node
    elif isinstance(node, dict):
        for key, value in node.items():
            yield from _strings(value, f"{path}.{key}")
    elif isinstance(node, list):
        for i, value in enumerate(node):
            yield from _strings(value, f"{path}[{i}]")


@pytest.mark.parametrize("table", _TABLES, ids=lambda p: p.relative_to(_DATA).as_posix())
def test_table_names_no_other_machine_class(table: Path):
    data = json.loads(table.read_text(encoding="utf-8"))
    hits = [
        f"{path}: ...{text[max(0, m.start() - 40):m.end() + 40]}..."
        for path, text in _strings(data)
        for m in _WORDS.finditer(text)
    ]
    assert not hits, f"{table.name}:\n" + "\n".join(hits)
