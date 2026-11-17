"""No public tool hands out a material's whole record, or the whole table.

A per-material table answers one question at a time.  A tool that returns a
material's entire row, or most rows at once, lets a caller copy the table by
asking about each material in turn.  This runs the experiment: it calls every
read-only public tool that can be asked with nothing but a material, and
measures how much of each bundled per-material record comes back.

A tool that returns most of a record is listed under ``whole_record_doors``
in ``scripts/public_material_tables.yaml``: ``keep`` with a reason, or the
``baseline`` of doors present when the check was added, which only shrinks.
A new one fails.

Bounds: only tools every required input of which is a material are called;
a tool that also needs a file, a printer or a number is not.  A record is
recognised by its own field names and values together, so a reply that
renames every field is unseen.
"""
from __future__ import annotations

import asyncio
import importlib.util
import inspect
import json
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

import kiln.server as server
from tests import _read_only_reach as scan

REPO = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "audit_public_material_tables_for_doors", REPO / "scripts" / "audit_public_material_tables.py"
)
gate = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = gate
_SPEC.loader.exec_module(gate)

#: A reply carrying at least this share of a record's fields is the record.
SHARE = 0.6
#: A record smaller than this is not judged: too little to tell from chance.
MIN_FIELDS = 4
#: A reply carrying this many whole records is the table.
SET_RECORDS = 3
MATERIAL_INPUTS = ("material", "material_id", "material_a", "material_b", "filament", "filament_type",
                   "model_material", "support_material", "current_material", "from_material", "to_material")
ASKED = ("petg", "abs")


def _share(record: Any, material: str, text: str) -> float:
    """How much of *record* is in *text*: as ``"field": value``, or turned on
    its side as ``"field": {... "material": value``."""
    if not isinstance(record, dict):
        return 0.0
    pairs: list[tuple[str, Any]] = []

    def walk(node: dict) -> None:
        for key, value in node.items():
            if isinstance(value, dict):
                walk(value)
            elif value not in ("", None, [], {}):
                pairs.append((key, value))

    walk(record)
    if len(pairs) < MIN_FIELDS:
        return 0.0
    hits = 0
    for key, value in pairs:
        straight = f"{json.dumps(key)}: {json.dumps(value)}"
        sideways = f"{json.dumps(material)}: {json.dumps(value)}"
        if straight in text or (json.dumps(key) in text and sideways in text):
            hits += 1
    return hits / len(pairs)


def _material_tables() -> dict[str, dict[str, Any]]:
    """Each bundled per-material mapping: ``"<file>::<where>" -> {material: record}``."""
    names = gate.material_names()
    tables: dict[str, dict[str, Any]] = {}
    for path in sorted((gate.SRC / "data").rglob("*.json")):
        if any(part in gate.SKIP_DIRS for part in path.parts):
            continue
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            continue
        rel = path.relative_to(gate.SRC).as_posix()
        for where, _count in gate.tables_in_json(doc, names):
            node: Any = doc
            if where != "(top)":
                for part in where.split("."):
                    node = node.get(part, {}) if isinstance(node, dict) else {}
            if isinstance(node, dict):
                tables[f"{rel}::{where}"] = {k: v for k, v in node.items() if isinstance(v, dict)}
    return tables


def _askable(fn: Any) -> dict[str, str] | None:
    """Arguments that ask *fn* about a material, or None when it needs more."""
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return None
    args: dict[str, str] = {}
    material_inputs = [p for p in params if p in MATERIAL_INPUTS]
    for name, param in params.items():
        if name in MATERIAL_INPUTS:
            args[name] = ASKED[min(len(args), len(ASKED) - 1)]
        elif param.default is inspect.Parameter.empty and param.kind not in (
            inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD
        ):
            return None
    return args if material_inputs else None


def _reply(fn: Any, args: dict[str, str]) -> str:
    try:
        result = fn(**args)
        if inspect.isawaitable(result):
            result = asyncio.run(result)
        return json.dumps(result, default=str)
    except Exception:  # noqa: BLE001 -- a tool that will not answer hands out nothing
        return ""


def whole_record_doors() -> set[str]:
    """``"<tool>::<table>"`` for every tool that returns a record or the table."""
    server._ensure_internal_tool_plugins_registered()
    read_only, _changing = scan.read_only_tools(dict(server.mcp._tool_manager._tools))
    tables = _material_tables()
    found: set[str] = set()
    for name, fn in sorted(read_only.items()):
        if (getattr(fn, "__module__", "") or "").startswith("kiln_pro"):
            continue
        args = _askable(fn)
        if args is None:
            continue
        text = _reply(fn, args)
        if not text:
            continue
        for table, records in tables.items():
            whole = sum(1 for material, record in records.items() if _share(record, material, text) >= SHARE)
            one = any(_share(records.get(a), a, text) >= SHARE for a in set(args.values()))
            if one or whole >= SET_RECORDS:
                found.add(f"{name}::{table}")
    return found


def _ledger() -> dict:
    doc = yaml.safe_load(gate.LEDGER.read_text(encoding="utf-8")) or {}
    return doc.get("whole_record_doors") or {}


def test_a_record_is_recognised_whole_and_turned_on_its_side():
    record = {"a": 1, "b": {"c": 2.5, "d": "text"}, "e": [1, 2], "f": ""}
    assert _share(record, "petg", json.dumps({"x": record})) == 1.0
    sideways = {"a": {"petg": 1}, "c": {"petg": 2.5}, "d": {"petg": "text"}, "e": {"petg": [1, 2]}}
    assert _share(record, "petg", json.dumps(sideways)) == 1.0
    assert _share(record, "petg", json.dumps({"a": 1})) < SHARE
    assert _share({"a": 1}, "petg", json.dumps({"a": 1})) == 0.0


def test_a_tool_that_needs_more_than_a_material_is_not_called():
    def lookup(material: str, strict: bool = False): ...
    def needs_file(material: str, file_path: str): ...
    def no_material(limit: int = 5): ...

    assert _askable(lookup) == {"material": "petg"}
    assert _askable(needs_file) is None
    assert _askable(no_material) is None


def test_no_new_tool_hands_out_a_whole_material_record():
    ledger = _ledger()
    keep = ledger.get("keep") or {}
    baseline = list(ledger.get("baseline") or [])
    for door, row in keep.items():
        assert isinstance(row, dict) and str(row.get("why") or "").strip(), f"{door}: kept with no reason"
    assert ledger.get("baseline_count") == len(baseline), "the baseline and baseline_count disagree"
    found = whole_record_doors()
    new = sorted(found - set(keep) - set(baseline))
    assert not new, (
        "public tools that return a material's whole record, or the table (answer the one question "
        "asked, or serve the record instead):\n  " + "\n  ".join(new)
    )
    stale = sorted((set(keep) | set(baseline)) - found)
    assert not stale, "these no longer return a whole record: delete their rows:\n  " + "\n  ".join(stale)


if __name__ == "__main__":
    for door in sorted(whole_record_doors()):
        print(f"    - '{door}'")
