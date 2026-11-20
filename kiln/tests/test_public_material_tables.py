"""Backstop for ``scripts/audit_public_material_tables.py``.

Proves the gate finds a per-material table by its shape in a data file and in
Python source, under names it is handed at run time as well as the
catalogue's own, that each ledger check can fail, and that the real tree is
clean against the real ledger.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "audit_public_material_tables", REPO / "scripts" / "audit_public_material_tables.py"
)
gate = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = gate
_SPEC.loader.exec_module(gate)

NAMES = {"pla", "petg", "abs", "nylon"}


def _tree(tmp_path: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return tmp_path


def test_a_material_keyed_data_table_is_found(tmp_path):
    doc = {"limits": {"pla": {"max_c": 50}, "petg": {"max_c": 65}, "abs": {"max_c": 85}}}
    src = _tree(tmp_path, {"data/heat.json": json.dumps(doc)})
    assert gate.discover(src, NAMES) == {"data/heat.json::limits": 3}


def test_a_list_of_rows_that_each_name_a_material_is_found(tmp_path):
    rows = [{"material": m, "score": 1} for m in ("pla", "petg", "abs")]
    src = _tree(tmp_path, {"data/rows.json": json.dumps({"rules": rows})})
    assert gate.discover(src, NAMES) == {"data/rows.json::rules[]": 3}


def test_a_material_keyed_table_in_source_is_found(tmp_path):
    source = '_SHRINK = {"PLA": 0.3, "PETG": 0.4, "ABS": 0.7}\n'
    src = _tree(tmp_path, {"shrink.py": source})
    assert gate.discover(src, NAMES) == {"shrink.py::_SHRINK": 3}


def test_a_table_written_as_repeated_calls_is_found(tmp_path):
    source = (
        "def build(self):\n"
        '    self.add("pla", "petg", score=0.8)\n'
        '    self.add("petg", "abs", score=0.6)\n'
        '    self.add("abs", "nylon", score=0.4)\n'
    )
    src = _tree(tmp_path, {"swaps.py": source})
    assert gate.discover(src, NAMES) == {"swaps.py::build:self.add()": 3}


def test_a_table_written_as_a_list_of_rows_in_source_is_found(tmp_path):
    source = '_RULES = [("pla", 0.3), ("petg", 0.4), ("abs", 0.7)]\n'
    src = _tree(tmp_path, {"rules.py": source})
    assert gate.discover(src, NAMES) == {"rules.py::_RULES[]": 3}


def test_a_plain_list_of_material_names_is_not_a_table(tmp_path):
    src = _tree(tmp_path, {"names.py": '_KNOWN = ["pla", "petg", "abs", "nylon"]\n'})
    assert gate.discover(src, NAMES) == {}


def test_calls_that_only_name_a_material_are_not_a_table(tmp_path):
    source = 'def f(log):\n    log("pla")\n    log("petg")\n    log("abs")\n'
    src = _tree(tmp_path, {"logs.py": source})
    assert gate.discover(src, NAMES) == {}


def test_a_pair_key_names_its_materials(tmp_path):
    doc = {"pairs": {"pla/petg": "poor", "pla/abs": "poor", "petg/abs": "fair"}}
    src = _tree(tmp_path, {"data/pairs.json": json.dumps(doc)})
    assert list(gate.discover(src, NAMES)) == ["data/pairs.json::pairs"]


@pytest.mark.parametrize(
    "key",
    ['frozenset({{"{a}", "{b}"}})', 'frozenset(["{a}", "{b}"])', '("{a}", "{b}")', 'frozenset(("{a}", "{b}"))'],
)
def test_a_table_keyed_by_material_pairs_written_as_sets_is_found(tmp_path, key):
    rows = [("PLA", "ABS"), ("PLA", "NYLON"), ("PETG", "ABS")]
    body = ",\n".join(f'    {key.format(a=a, b=b)}: {{"level": "bad", "warning": "do not pair these two"}}'
                      for a, b in rows)
    src = _tree(tmp_path, {"pairs.py": f"_PAIRS = {{\n{body},\n}}\n"})
    assert gate.discover(src, NAMES) == {"pairs.py::_PAIRS": 3}


def test_a_set_keyed_mapping_of_names_with_no_cells_is_not_a_table(tmp_path):
    source = '_SEEN = {frozenset({"PLA", "ABS"}): None, frozenset({"PLA", "PETG"}): None, frozenset({"ABS", "PETG"}): None}\n'
    src = _tree(tmp_path, {"seen.py": source})
    assert gate.discover(src, NAMES) == {}


def test_the_refused_pairs_are_kept_public_with_their_reason():
    """The pairs the multi-colour composer refuses with no server at all are
    found by the gate, listed by ``--list``, and kept with a recorded door."""
    found = gate.discover()
    table = "material_safety.py::_REFUSED_PAIRS"
    assert found.get(table, 0) >= 3, sorted(found)
    ledger = yaml.safe_load(gate.LEDGER.read_text(encoding="utf-8"))
    row = ledger["keep"].get(table)
    assert row and row["door"].strip() and row["why"].strip()
    assert table not in ledger["baseline"]


def test_list_shows_the_refused_pairs(capsys):
    assert gate.main(["--list"]) == 0
    assert "material_safety.py::_REFUSED_PAIRS" in capsys.readouterr().out


def test_two_materials_are_not_a_table(tmp_path):
    src = _tree(tmp_path, {"two.py": '_X = {"PLA": 1, "PETG": 2, "other": 3}\n'})
    assert gate.discover(src, NAMES) == {}


def test_a_mapping_of_names_with_no_cells_is_not_a_table(tmp_path):
    src = _tree(tmp_path, {"data/empty.json": json.dumps({"pla": "", "petg": "", "abs": ""})})
    assert gate.discover(src, NAMES) == {}


def test_names_handed_in_at_run_time_are_read_as_materials(tmp_path, monkeypatch):
    extra = tmp_path / "names.txt"
    extra.write_text("alloy_one\nalloy_two\nalloy_three\n", encoding="utf-8")
    monkeypatch.setenv("KILN_EXTRA_MATERIAL_NAMES", str(extra))
    names = gate.material_names(tmp_path / "no_catalogue.json")
    assert {"alloy_one", "alloy_two", "alloy_three"} <= names
    src = _tree(tmp_path / "src", {"feeds.py": '_FEED = {"alloy_one": 1, "alloy_two": 2, "alloy_three": 3}\n'})
    assert gate.discover(src, names) == {"feeds.py::_FEED": 3}


def test_vendored_libraries_are_not_read(tmp_path):
    src = _tree(tmp_path, {"data/scad_libraries/x.py": '_T = {"PLA": 1, "PETG": 2, "ABS": 3}\n'})
    assert gate.discover(src, NAMES) == {}


FOUND = {"a.py::_T": 3, "data/b.json::rows": 5}


def _ledger(**change):
    ledger = {
        "baseline_count": 1,
        "keep": {"a.py::_T": {"door": "the start gate", "why": "must refuse offline"}},
        "baseline": ["data/b.json::rows"],
    }
    ledger.update(change)
    return ledger


def test_a_fully_recorded_tree_is_clean():
    assert gate.audit(_ledger(), FOUND) == []


def test_a_new_table_with_no_row_fails():
    found = dict(FOUND, **{"c.py::_NEW": 4})
    assert any("c.py::_NEW" in f and "no recorded reason" in f for f in gate.audit(_ledger(), found))


@pytest.mark.parametrize(
    "change, expect",
    [
        ({"keep": {"a.py::_T": {"door": "", "why": "x"}}}, "no door or no reason"),
        ({"keep": {"a.py::_T": {"door": "x", "why": ""}}}, "no door or no reason"),
        ({"baseline": ["data/b.json::rows", "gone.py::_T"], "baseline_count": 2}, "no longer exists"),
        ({"baseline_count": 3}, "baseline_count says 3"),
        ({"baseline": ["data/b.json::rows", "a.py::_T"], "baseline_count": 2}, "keep and baseline"),
    ],
)
def test_each_ledger_check_can_fail(change, expect):
    assert any(expect in f for f in gate.audit(_ledger(**change), FOUND))


def test_a_kept_table_that_is_gone_is_stale():
    assert any("kept public and no longer exists" in f for f in gate.audit(_ledger(), {"data/b.json::rows": 5}))


def test_the_real_tree_is_clean_against_the_real_ledger():
    ledger = yaml.safe_load(gate.LEDGER.read_text(encoding="utf-8"))
    assert gate.audit(ledger, gate.discover()) == []
