#!/usr/bin/env python3
"""Public Kiln ships no compiled per-material table without a recorded reason.

A table whose rows are materials and whose cells are numbers or judgements is
compiled knowledge.  Published in the open-source package it is read by
anyone, whether or not it is free to use: where a table lives and who may use
it are separate questions.  A per-material table belongs in the public
package only when a public code path must work with no server at all and
cannot refuse something harmful without it.

The gate recognises a table by its SHAPE, not by a list of today's
materials: a mapping with several keys that are material names, or a list of
rows that each name a material, in a bundled data file or in Python source.
Material names are the bundled catalogue's own ids and short forms, plus any
names in the file ``KILN_EXTRA_MATERIAL_NAMES`` points at (one per line),
so names that are not public are never written here.

Every table found is one of, in ``scripts/public_material_tables.yaml``:

``keep``      stays public: names the offline door that needs it, and why
``baseline``  already present when the gate was added: listed, not judged;
              the list only shrinks, and a new table may not join it

A table in neither list fails.  So does a stale entry, a ``keep`` with no
door or reason, and a baseline that grew.

Bounds: it finds tables by material names in keys and in a row's
``material`` field, so a table keyed another way is unseen; a table split
across several small mappings (fewer names each than the floor) is unseen;
it reads ``kiln/src/kiln`` only, and skips vendored libraries.

Usage::

    python3 scripts/audit_public_material_tables.py            # exit 2 on findings
    python3 scripts/audit_public_material_tables.py --list     # every table found
    python3 scripts/audit_public_material_tables.py --seed     # print baseline rows
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Iterator

import yaml

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "kiln" / "src" / "kiln"
LEDGER = REPO / "scripts" / "public_material_tables.yaml"
CATALOGUE = SRC / "data" / "design_knowledge" / "materials.json"
SKIP_DIRS = ("scad_libraries", "__pycache__")
#: A mapping with at least this many material-named keys is a table.
MIN_MATERIALS = 3
#: Short forms people write for catalogued materials.
SHORT_FORMS = (
    "pa", "pa6", "pa12", "pa66", "pc", "pei", "pet", "pom", "hips", "tpe", "pmma",
    "pla+", "nylon", "ultem", "polypropylene", "polyamide", "resin",
)


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9+]+", "_", str(name).strip().lower()).strip("_")


def material_names(catalogue: Path = CATALOGUE) -> set[str]:
    """Every name the gate reads as a material."""
    names = {_norm(n) for n in SHORT_FORMS}
    if catalogue.is_file():
        rows = json.loads(catalogue.read_text(encoding="utf-8"))
        rows = rows.get("materials", rows)
        names |= {_norm(k) for k in rows if not str(k).startswith("_")}
    extra = os.environ.get("KILN_EXTRA_MATERIAL_NAMES", "")
    if extra and Path(extra).is_file():
        names |= {_norm(line) for line in Path(extra).read_text(encoding="utf-8").splitlines() if line.strip()}
    names.discard("")
    return names


def _is_material(key: Any, names: set[str]) -> bool:
    if not isinstance(key, str):
        return False
    folded = _norm(key)
    # A pair such as "pla/petg" or "pla+tpu" names materials too.
    return folded in names or any(part in names for part in re.split(r"[/|>]", key.lower()) if part.strip())


def _carries_knowledge(value: Any) -> bool:
    """A cell that is a number, a sentence, or a structure of them."""
    if isinstance(value, bool):
        return True
    if isinstance(value, (int, float)):
        return True
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, dict):
        return any(_carries_knowledge(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return any(_carries_knowledge(v) for v in value)
    return False


def tables_in_json(doc: Any, names: set[str], path: str = "") -> Iterator[tuple[str, int]]:
    """``(where, materials)`` for each material-keyed mapping or row list."""
    if isinstance(doc, dict):
        hits = [k for k, v in doc.items() if _is_material(k, names) and _carries_knowledge(v)]
        if len(hits) >= MIN_MATERIALS:
            yield path or "(top)", len(hits)
            return
        for key, value in doc.items():
            yield from tables_in_json(value, names, f"{path}.{key}" if path else str(key))
    elif isinstance(doc, list):
        rows = [r for r in doc if isinstance(r, dict) and any(
            _is_material(r.get(f), names) for f in ("material", "material_id", "filament", "model_material"))]
        if len(rows) >= MIN_MATERIALS:
            yield f"{path}[]", len(rows)
            return
        for index, value in enumerate(doc):
            if isinstance(value, (dict, list)):
                yield from tables_in_json(value, names, f"{path}[{index}]")


def _literal_key(node: ast.AST) -> Any:
    return node.value if isinstance(node, ast.Constant) else None


def _has_literal(node: ast.AST) -> bool:
    return any(isinstance(n, ast.Constant) and isinstance(n.value, (int, float, str)) for n in ast.walk(node))


def tables_in_python(source: str, names: set[str]) -> Iterator[tuple[str, int]]:
    """``(name, materials)`` for each material-keyed dict literal."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return
    targets: dict[int, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and isinstance(node.value, ast.Dict):
            target = node.targets[0] if isinstance(node, ast.Assign) else node.target
            targets[id(node.value)] = ast.unparse(target)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        hits = [k for k, v in zip(node.keys, node.values)
                if k is not None and _is_material(_literal_key(k), names) and _has_literal(v)]
        if len(hits) >= MIN_MATERIALS:
            yield targets.get(id(node), f"dict@{node.lineno}"), len(hits)


def discover(src: Path = SRC, names: set[str] | None = None) -> dict[str, int]:
    """Every table under *src*: ``"<relative path>::<where>" -> materials``."""
    names = names if names is not None else material_names()
    found: dict[str, int] = {}
    for path in sorted(src.rglob("*")):
        if any(part in SKIP_DIRS for part in path.parts) or not path.is_file():
            continue
        rel = path.relative_to(src).as_posix()
        if path.suffix == ".json":
            try:
                doc = json.loads(path.read_text(encoding="utf-8"))
            except (ValueError, UnicodeDecodeError):
                continue
            for where, count in tables_in_json(doc, names):
                found[f"{rel}::{where}"] = max(count, found.get(f"{rel}::{where}", 0))
        elif path.suffix == ".py":
            for where, count in tables_in_python(path.read_text(encoding="utf-8"), names):
                key = f"{rel}::{where}"
                found[key] = max(count, found.get(key, 0))
    return found


def audit(ledger: dict, found: dict[str, int]) -> list[str]:
    findings: list[str] = []
    keep = ledger.get("keep") or {}
    baseline = list(ledger.get("baseline") or [])
    for table, row in keep.items():
        if not isinstance(row, dict) or not str(row.get("door") or "").strip() or not str(row.get("why") or "").strip():
            findings.append(f"{table}: kept public with no door or no reason")
        if table not in found:
            findings.append(f"STALE: {table} is kept public and no longer exists")
    for table in baseline:
        if table in keep:
            findings.append(f"{table}: listed under keep and baseline")
        if table not in found:
            findings.append(f"STALE: {table} is in the baseline and no longer exists: delete its row")
    pinned = ledger.get("baseline_count")
    if not isinstance(pinned, int):
        findings.append("baseline_count is missing or not a whole number")
    elif len(baseline) != pinned:
        findings.append(f"the baseline holds {len(baseline)} tables and baseline_count says {pinned}")
    listed = set(keep) | set(baseline)
    for table in sorted(set(found) - listed):
        findings.append(
            f"{table}: a per-material table ({found[table]} materials) with no recorded reason to be "
            f"public. Serve it instead, or record under keep the offline door that needs it."
        )
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--list", action="store_true", help="print every table found")
    parser.add_argument("--seed", action="store_true", help="print baseline rows for unlisted tables")
    args = parser.parse_args(argv)
    found = discover()
    ledger = yaml.safe_load(LEDGER.read_text(encoding="utf-8")) if LEDGER.is_file() else {}
    ledger = ledger or {}
    if args.list:
        for table, count in sorted(found.items()):
            print(f"{count:4d}  {table}")
        return 0
    if args.seed:
        listed = set(ledger.get("keep") or {}) | set(ledger.get("baseline") or [])
        for table in sorted(set(found) - listed):
            print(f"  - '{table}'")
        return 0
    findings = audit(ledger, found)
    print(f"[public-material-tables] {len(found)} per-material table(s) in public source; "
          f"{len(ledger.get('keep') or {})} kept with a reason, {len(ledger.get('baseline') or [])} in the baseline")
    for finding in findings:
        print(f"[public-material-tables] FINDING: {finding}")
    if not findings:
        print("[public-material-tables] clean")
    return 2 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
