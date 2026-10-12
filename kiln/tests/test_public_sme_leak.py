"""CI backstop for the public SME-table leak gate.

Public Kiln must not ship a curated cross-vendor capability / SME table —
a registry keyed by printer brand carrying limitations, recovery_methods,
capability matrices, failure_modes.  That belongs in the private
tier (or a pro overlay), never in public source.  A green run here
is the proof we didn't ship one.  See ``scripts/audit_public_sme_leak.py``.

The 2026-06-23 incident: a 459-line ``resume_capabilities.py`` cross-vendor
power-loss table, consumed by no live code, sat world-visible for months —
between the data gates ("where files live") and the comment gate ("the
method in prose").  The regression tests below prove this gate catches that
exact shape, and exonerates the sanctioned overlay-split loaders.

The bundled data files are also read as tables, because a per-printer
catalogue names its fields after what they hold and the text scan read them
clean.  ``TestDataTables`` proves each check of that half can fail: a new
per-printer file, a roster row that grows a limit field, a pending move past
its date, a ledger row that outlived its subject, a malformed row.
"""

from __future__ import annotations

import datetime
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "audit_public_sme_leak.py"


def _load_gate():
    spec = importlib.util.spec_from_file_location("audit_public_sme_leak", _SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["audit_public_sme_leak"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_no_public_sme_leak() -> None:
    """The whole of public source is clean (or exonerated) — the backstop."""
    assert _SCRIPT.exists(), f"gate script missing: {_SCRIPT}"
    result = subprocess.run(
        [sys.executable, str(_SCRIPT)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        "Public SME-table leak — a curated cross-vendor capability/SME table "
        "is in public source. Move it to the private tier (kiln-pro) or a pro "
        "overlay; do not allowlist it:\n\n" + result.stdout
    )


# --- regression: the gate must catch the shape that slipped through -------

# A standalone hardcoded cross-vendor capability table, NO overlay merge —
# the resume_capabilities.py shape.
_LEAK_SHAPE = '''
REGISTRY = {
    "bambu": dict(recovery_methods=["builtin"], limitations=["MQTT only"]),
    "prusa": dict(recovery_methods=["m413"], limitations=["MINI has none"]),
    "klipper": dict(recovery_methods=["probe"], limitations=["no native PLR"]),
    "elegoo": dict(recovery_methods=["sdcp"], limitations=["cancel+restart"]),
}
'''

# Same field/vendor density but routed through the design-knowledge split —
# the design_intelligence.py / printer_intelligence.py shape (safe): the
# public file holds an empty floor and pulls the curated values from the
# private overlay at runtime.
_LOADER_SHAPE = '''
# Handles bambu / prusa / klipper / elegoo printer intelligence.
_PUBLIC_FLOOR = {
    "capabilities": {},
    "failure_modes": [],
}

def load(printer):
    raw = dict(_PUBLIC_FLOOR)
    return _merge_pro_overlay_if_available(raw)
'''


def test_detector_catches_standalone_cross_vendor_table() -> None:
    gate = _load_gate()
    assert len(gate._distinct_vendors(_LEAK_SHAPE)) >= gate._VENDOR_MIN
    assert len(gate._distinct_fields(_LEAK_SHAPE)) >= gate._FIELD_MIN
    assert gate._is_overlay_loader(_LEAK_SHAPE) is False
    finding = {
        "overlay_loader": gate._is_overlay_loader(_LEAK_SHAPE),
        "allowlisted": False,
    }
    assert gate._is_leak(finding) is True, "must flag a standalone SME table"


def test_detector_exonerates_overlay_split_loader() -> None:
    gate = _load_gate()
    # Still trips vendor + field density...
    assert len(gate._distinct_vendors(_LOADER_SHAPE)) >= gate._VENDOR_MIN
    assert len(gate._distinct_fields(_LOADER_SHAPE)) >= gate._FIELD_MIN
    # ...but the overlay-split marker exonerates it (values live private).
    assert gate._is_overlay_loader(_LOADER_SHAPE) is True
    finding = {
        "overlay_loader": gate._is_overlay_loader(_LOADER_SHAPE),
        "allowlisted": False,
    }
    assert gate._is_leak(finding) is False, "loaders must not be flagged"


def test_field_match_ignores_prose() -> None:
    """A field name in prose (not an assignment/key) must not count."""
    gate = _load_gate()
    prose = (
        "This bambu / prusa / klipper helper documents the limitations and "
        "recovery_methods of each printer in words, with no data structure."
    )
    # Vendors present, but no `field=` / `\"field\":` → not an SME table.
    assert len(gate._distinct_fields(prose)) == 0


# --- the data half: every bundled JSON read as tables ------------------------

#: A fixed day, so no test depends on the wall clock.
_TODAY = datetime.date(2026, 6, 1)


def _plant(root: Path, rel: str, doc: object) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc), encoding="utf-8")


def _roster() -> dict:
    """A roster in the stripped shape.  The ids name no maker (like ``k1``);
    the display names do.  Every figure is a placeholder."""
    return {
        "_meta": {"version": 1},
        "default": {
            "display_name": "Generic printer",
            "max_hotend_temp": 1,
            "build_volume": [1, 1, 1],
        },
        "kt_one": {"display_name": "Bambu Lab Test One"},
        "kt_two": {"display_name": "Prusa Test Two"},
        "kt_three": {"display_name": "Creality Test Three"},
        "kt_four": {"display_name": "Unlisted Maker Four"},
    }


def _per_model() -> dict:
    rows = _roster()
    for key in ("kt_one", "kt_two", "kt_three"):
        rows[key].update({"max_hotend_temp": 1, "build_volume_mm": [1, 1, 1]})
    del rows["default"]
    return rows


_ROSTER_ROW = {
    "classification": "roster",
    "fields": ["display_name"],
    "full_rows": ["default"],
    "why": "test roster",
}


class TestDataTables:
    """The bundled-data half of the gate: a cross-vendor per-printer limit
    table is found by its field shape wherever it lands under the data root;
    a roster row may carry only the fields its ledger row names; a pending
    move fails once past its date; a ledger row outliving its subject is
    stale; a malformed row is refused; the shipped tree is judged."""

    def test_a_new_per_printer_file_fails_the_day_it_lands(self, tmp_path: Path) -> None:
        gate = _load_gate()
        _plant(tmp_path, "design_knowledge/new_specs.json", _per_model())
        result = gate.judge_data(tmp_path, ledger={}, today=_TODAY)
        [failure] = result["failures"]
        assert failure["kind"] == "unclassified"
        assert failure["file"] == "design_knowledge/new_specs.json"
        [table] = failure["tables"]
        assert table["vendors"] == ["bambu", "creality", "prusa"]
        assert table["limit_fields"]["temperature"] == ["max_hotend_temp"]
        assert table["limit_fields"]["build volume / bed"] == ["build_volume_mm"]

    def test_the_same_makers_without_limit_fields_pass(self, tmp_path: Path) -> None:
        gate = _load_gate()
        names_only = {k: v for k, v in _roster().items() if k != "default"}
        _plant(tmp_path, "names.json", names_only)
        assert gate.judge_data(tmp_path, ledger={}, today=_TODAY)["failures"] == []

    def test_a_table_one_level_down_is_read(self, tmp_path: Path) -> None:
        gate = _load_gate()
        _plant(tmp_path, "nested.json", {"_meta": {}, "printers": _per_model()})
        [failure] = gate.judge_data(tmp_path, ledger={}, today=_TODAY)["failures"]
        assert failure["kind"] == "unclassified"
        assert [t["table"] for t in failure["tables"]] == ["printers"]

    def test_a_limit_inside_a_sub_block_still_counts(self, tmp_path: Path) -> None:
        gate = _load_gate()
        rows = {
            key: {"display_name": row["display_name"], "spec": {"max_bed_temp": 1, "max_feedrate": 1}}
            for key, row in _roster().items()
            if key.startswith("kt_")
        }
        _plant(tmp_path, "deep.json", rows)
        [failure] = gate.judge_data(tmp_path, ledger={}, today=_TODAY)["failures"]
        limits = failure["tables"][0]["limit_fields"]
        assert limits["temperature"] == ["max_bed_temp"]
        assert limits["speed / acceleration / flow"] == ["max_feedrate"]

    def test_a_bare_number_per_printer_is_judged_outright(self, tmp_path: Path) -> None:
        gate = _load_gate()
        _plant(tmp_path, "ceilings.json", {"bambu_one": 1, "prusa_two": 2, "creality_three": 3})
        [failure] = gate.judge_data(tmp_path, ledger={}, today=_TODAY)["failures"]
        assert failure["tables"][0]["limit_fields"] == {"value per printer": ["<a number per printer>"]}

    def test_a_clean_roster_passes_and_is_reported(self, tmp_path: Path) -> None:
        gate = _load_gate()
        _plant(tmp_path, "roster.json", _roster())
        result = gate.judge_data(tmp_path, ledger={"roster.json": dict(_ROSTER_ROW)}, today=_TODAY)
        assert result["failures"] == []
        [entry] = result["classified"]
        assert entry["classification"] == "roster"
        assert entry["tables"][0]["limit_fields"]["temperature"] == ["max_hotend_temp"]

    def test_a_roster_row_that_grows_a_limit_field_fails(self, tmp_path: Path) -> None:
        gate = _load_gate()
        regrown = _roster()
        regrown["kt_two"]["max_hotend_temp"] = 1
        regrown["kt_four"]["variants"] = {}
        _plant(tmp_path, "roster.json", regrown)
        result = gate.judge_data(tmp_path, ledger={"roster.json": dict(_ROSTER_ROW)}, today=_TODAY)
        [failure] = result["failures"]
        assert failure["kind"] == "extra_fields"
        # The rule binds every row, including one whose maker the gate does
        # not know by name.
        assert failure["rows"] == {"kt_four": ["variants"], "kt_two": ["max_hotend_temp"]}

    def test_a_full_row_may_not_be_a_printer(self, tmp_path: Path) -> None:
        gate = _load_gate()
        _plant(tmp_path, "roster.json", _roster())
        row = dict(_ROSTER_ROW, full_rows=["default", "kt_one"])
        kinds = [f["kind"] for f in gate.judge_data(tmp_path, ledger={"roster.json": row}, today=_TODAY)["failures"]]
        assert kinds == ["full_row_is_a_printer"]

    def test_a_pending_move_fails_once_past_its_date(self, tmp_path: Path) -> None:
        gate = _load_gate()
        _plant(tmp_path, "specs.json", _per_model())
        ledger_path = tmp_path / "ledger.yaml"
        ledger_path.write_text(
            "files:\n"
            "  specs.json:\n"
            "    classification: pending_move\n"
            "    by: 2026-06-01\n"
            "    why: moves to the private tier\n",
            encoding="utf-8",
        )
        on_the_day = gate.judge_data(tmp_path, ledger_path=ledger_path, today=_TODAY)
        assert on_the_day["failures"] == []
        assert [(p["file"], p["days_left"]) for p in on_the_day["pending"]] == [("specs.json", 0)]
        day_after = gate.judge_data(tmp_path, ledger_path=ledger_path, today=_TODAY + datetime.timedelta(days=1))
        assert [f["kind"] for f in day_after["failures"]] == ["past_due"]
        assert day_after["pending"] == []

    def test_a_row_that_outlives_its_subject_is_stale(self, tmp_path: Path) -> None:
        gate = _load_gate()
        _plant(tmp_path, "harmless.json", {"a": {"display_name": "Bambu Lab Test One"}})
        row = {"classification": "not_printer_keyed", "why": "test"}
        result = gate.judge_data(tmp_path, ledger={"gone.json": row, "harmless.json": row}, today=_TODAY)
        stale = {f["file"]: f["detail"] for f in result["failures"] if f["kind"] == "stale"}
        assert stale == {
            "gone.json": "the file is gone",
            "harmless.json": "the file no longer carries a per-printer limit table; remove the row",
        }

    def test_a_full_row_the_file_lost_is_stale(self, tmp_path: Path) -> None:
        gate = _load_gate()
        _plant(tmp_path, "roster.json", _roster())
        row = dict(_ROSTER_ROW, full_rows=["default", "fallback"])
        [failure] = gate.judge_data(tmp_path, ledger={"roster.json": row}, today=_TODAY)["failures"]
        assert failure["kind"] == "stale" and "fallback" in failure["detail"]

    @pytest.mark.parametrize(
        "row",
        [
            {"classification": "allowed", "why": "x"},
            {"classification": "not_printer_keyed"},
            {"classification": "roster", "why": "x"},
            {"classification": "pending_move", "why": "x"},
            {"classification": "pending_move", "why": "x", "by": "soon"},
            {"classification": "not_printer_keyed", "why": "x", "feilds": ["display_name"]},
            {"classification": "not_printer_keyed", "why": "x", "fields": ["display_name"]},
        ],
    )
    def test_a_malformed_row_is_refused(self, tmp_path: Path, row: dict) -> None:
        gate = _load_gate()
        _plant(tmp_path, "specs.json", _per_model())
        [failure] = gate.judge_data(tmp_path, ledger={"specs.json": row}, today=_TODAY)["failures"]
        assert failure["kind"] == "malformed"

    def test_an_unreadable_ledger_judges_nothing_clean(self, tmp_path: Path) -> None:
        gate = _load_gate()
        _plant(tmp_path, "specs.json", _per_model())
        result = gate.judge_data(tmp_path, ledger_path=tmp_path / "missing.yaml", today=_TODAY)
        assert sorted(f["kind"] for f in result["failures"]) == ["ledger", "unclassified"]

    def test_a_disagreement_with_the_data_manifest_is_said(self, tmp_path: Path) -> None:
        gate = _load_gate()
        _plant(tmp_path, "roster.json", _roster())
        result = gate.judge_data(tmp_path, ledger={"roster.json": dict(_ROSTER_ROW)}, today=_TODAY)
        assert any(n.startswith("roster.json: classified roster") for n in result["notes"])

    def test_pending_moves_are_said_on_every_run(self) -> None:
        gate = _load_gate()
        ledger = gate.load_ledger()
        pending = sorted(rel for rel, row in ledger.items() if row.get("classification") == "pending_move")
        out = subprocess.run([sys.executable, str(_SCRIPT)], capture_output=True, text=True).stdout
        for rel in pending:
            assert f"PENDING MOVE        {rel}" in out, f"{rel} pending but not said:\n{out}"

    def test_the_intel_roster_never_allows_more_than_the_public_schema(self) -> None:
        # data_manifest.PUBLIC_PROFILE_FIELDS states the public schema of a
        # printer_intelligence row; the ledger may not allow a field it omits.
        from kiln import data_manifest

        row = _load_gate().load_ledger()["printer_intelligence.json"]
        assert set(row["fields"]) <= data_manifest.PUBLIC_PROFILE_FIELDS
