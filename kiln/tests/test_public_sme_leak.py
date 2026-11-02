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

``TestPrinterRowRules`` replays the 2026-11-02 incident: a verdict matrix
classified as a floor on the strength of a sentence, and research notes
riding under ``motion._sources`` inside a field the roster allowed.  A
printer-keyed row now names the public reader of every field it keeps, and
an underscore key inside a printer row fails unless the ledger names it.

``TestMatrixDisguises`` replays the review that followed: the same matrix
nested deeper, under an underscore key, transposed, as a list of row
objects, and packed into strings.  Each is planted and must fail; the real
shapes (the default-row-only compat file, the material floors) must not.
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
            "harmless.json": "the file no longer carries a per-printer limit table or a table of verified records; remove the row",
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


def _records(n: int = 12, *, maker: bool = False) -> dict:
    """A table of verified records keyed by material, no maker in a key and
    no sentence in a row: the shape that passed every gate until
    2026-10-16.  Every figure is a placeholder."""
    rows: dict = {"_meta": {"version": 1}}
    for i in range(n):
        row: dict = {"thermal": {"glass_c": 1, "service_c": 1, "print_c": [1, 2]}}
        if maker:
            row = {"brand": "Prusa Research", "nozzle_c": [1, 2]}
        rows[f"mat_{i}"] = row
    return rows


class TestVerifiedRecords:
    """The second shape: a compiled table of measured figures is judged
    whether or not a maker is named or a note rides beside it."""

    def test_a_table_of_measured_rows_fails_the_day_it_lands(self, tmp_path: Path) -> None:
        gate = _load_gate()
        _plant(tmp_path, "design_knowledge/figures.json", _records())
        [failure] = gate.judge_data(tmp_path, ledger={}, today=_TODAY)["failures"]
        assert failure["kind"] == "unclassified"
        [table] = failure["tables"]
        assert table["vendors"] == []
        assert table["limit_fields"]["verified records"][0].startswith("12 rows x 3")

    def test_a_brand_product_row_needs_only_one_figure(self, tmp_path: Path) -> None:
        gate = _load_gate()
        _plant(tmp_path, "brands.json", _records(maker=True))
        [failure] = gate.judge_data(tmp_path, ledger={}, today=_TODAY)["failures"]
        assert "1 makers named" in failure["tables"][0]["limit_fields"]["verified records"]

    def test_too_few_rows_is_not_a_table(self, tmp_path: Path) -> None:
        gate = _load_gate()
        _plant(tmp_path, "few.json", _records(n=9))
        assert gate.judge_data(tmp_path, ledger={}, today=_TODAY)["failures"] == []

    def test_a_parameter_schema_is_a_knob_not_a_reading(self, tmp_path: Path) -> None:
        """A template or library catalogue describes knobs a person turns."""
        gate = _load_gate()
        rows = {f"t_{i}": {"parameters": {"w": {"default": 1, "min": 0, "max": 2, "unit": "mm"},
                                          "h": {"default": 1, "min": 0, "max": 2, "unit": "mm"},
                                          "d": {"default": 1, "min": 0, "max": 2, "unit": "mm"}}}
                for i in range(12)}
        _plant(tmp_path, "templates.json", rows)
        assert gate.judge_data(tmp_path, ledger={}, today=_TODAY)["failures"] == []

    def test_a_pending_move_row_quiets_it_until_its_date(self, tmp_path: Path) -> None:
        gate = _load_gate()
        _plant(tmp_path, "figures.json", _records())
        row = {"classification": "pending_move", "by": "2026-06-30", "why": "moving"}
        result = gate.judge_data(tmp_path, ledger={"figures.json": row}, today=_TODAY)
        assert result["failures"] == [] and result["pending"][0]["file"] == "figures.json"

    def test_the_shipped_material_tables_are_classified(self) -> None:
        """The three that slipped, settled 2026-10-16: the brand figures and
        the load grids moved to kiln-pro (materials.json keeps the floor and
        a names-only brand roster; load_tables.json is a roster the gate no
        longer sees as a table), and the brand product list was retired the
        same day.  Nothing is pending."""
        gate = _load_gate()
        ledger = gate.load_ledger()
        pending = sorted(rel for rel, row in ledger.items() if row.get("classification") == "pending_move")
        assert pending == []
        materials = ledger["design_knowledge/materials.json"]
        assert materials["classification"] == "safety_floor"
        assert "brand_profiles" in materials["fields"]
        assert "material_catalog.json" not in ledger
        assert "design_knowledge/load_tables.json" not in ledger


# --- printer rows: offline doors and provenance (2026-11-02) ----------------


def _intel(sources: dict | None = None) -> dict:
    """A roster whose printer rows keep a motion block, like
    printer_intelligence.json.  Every value is a placeholder."""
    rows = _roster()
    for key in ("kt_one", "kt_two", "kt_three", "kt_four"):
        block: dict = {"z_mode": "bed"}
        if sources is not None:
            block["_sources"] = {"z_mode": dict(sources)}
        rows[key]["motion"] = block
    return rows


def _intel_row(**extra: object) -> dict:
    row = {
        "classification": "roster",
        "fields": ["display_name", "motion"],
        "full_rows": ["default"],
        "why": "test roster with a motion block",
        "offline_doors": {"motion": "kiln.motion_facts:load_motion_facts"},
        "provenance": {"motion._sources": ["class"]},
    }
    row.update(extra)
    return {k: v for k, v in row.items() if v is not None}


class TestPrinterRowRules:
    """Each rule added 2026-11-02 can fail, and the shipped tree passes it."""

    def test_a_note_under_sources_fails(self, tmp_path: Path) -> None:
        gate = _load_gate()
        _plant(tmp_path, "intel.json", _intel({"class": "vendor", "note": "quoted"}))
        [failure] = gate.judge_data(tmp_path, ledger={"intel.json": _intel_row()}, today=_TODAY)["failures"]
        assert failure["kind"] == "provenance"
        assert failure["rows"]["kt_one"] == ["motion._sources.*.note"]
        assert len(failure["rows"]) == 4

    def test_sources_with_class_only_pass_when_the_ledger_names_them(self, tmp_path: Path) -> None:
        gate = _load_gate()
        _plant(tmp_path, "intel.json", _intel({"class": "vendor"}))
        result = gate.judge_data(tmp_path, ledger={"intel.json": _intel_row()}, today=_TODAY)
        assert result["failures"] == []
        [entry] = result["classified"]
        assert entry["provenance"] == {"motion._sources": ["class"]}

    def test_an_underscore_key_the_ledger_does_not_name_fails(self, tmp_path: Path) -> None:
        gate = _load_gate()
        _plant(tmp_path, "intel.json", _intel({"class": "vendor"}))
        result = gate.judge_data(tmp_path, ledger={"intel.json": _intel_row(provenance=None)}, today=_TODAY)
        [failure] = result["failures"]
        assert failure["kind"] == "provenance"
        assert failure["rows"]["kt_two"] == ["motion._sources"]

    def test_an_underscore_key_at_the_top_of_a_row_is_provenance_not_a_field(self, tmp_path: Path) -> None:
        gate = _load_gate()
        doc = _intel({"class": "vendor"})
        doc["kt_three"]["_ref"] = "a page"
        _plant(tmp_path, "intel.json", doc)
        [failure] = gate.judge_data(tmp_path, ledger={"intel.json": _intel_row()}, today=_TODAY)["failures"]
        assert failure["kind"] == "provenance"
        assert failure["rows"] == {"kt_three": ["_ref"]}

    def test_a_named_path_no_row_carries_is_stale(self, tmp_path: Path) -> None:
        gate = _load_gate()
        _plant(tmp_path, "intel.json", _intel())
        [failure] = gate.judge_data(tmp_path, ledger={"intel.json": _intel_row()}, today=_TODAY)["failures"]
        assert failure["kind"] == "stale" and "motion._sources" in failure["detail"]

    def test_a_file_the_manifest_keys_by_printer_is_judged_stripped(self, tmp_path: Path) -> None:
        """A file cut down to its default row is still read: a note can grow
        back into it with no maker named anywhere."""
        gate = _load_gate()
        rel = "design_knowledge/printer_material_compatibility.json"
        _plant(tmp_path, rel, {"_meta": {}, "default": {"pla": {"status": "ok", "_note": "x"}}})
        [failure] = gate.judge_data(tmp_path, ledger={}, today=_TODAY)["failures"]
        assert (failure["file"], failure["kind"]) == (rel, "provenance")
        assert failure["rows"] == {"default": ["pla._note"]}

    def test_a_printer_keyed_field_with_no_offline_door_fails(self, tmp_path: Path) -> None:
        gate = _load_gate()
        _plant(tmp_path, "intel.json", _intel({"class": "vendor"}))
        result = gate.judge_data(tmp_path, ledger={"intel.json": _intel_row(offline_doors=None)}, today=_TODAY)
        [failure] = result["failures"]
        assert failure["kind"] == "no_offline_door" and "'motion'" in failure["detail"]

    def test_name_fields_owe_no_door(self, tmp_path: Path) -> None:
        gate = _load_gate()
        _plant(tmp_path, "roster.json", _roster())
        result = gate.judge_data(tmp_path, ledger={"roster.json": dict(_ROSTER_ROW)}, today=_TODAY)
        assert result["failures"] == []

    @pytest.mark.parametrize(
        ("door", "says"),
        [
            ("kiln.motion_facts:no_such_reader", "defines no function no_such_reader"),
            ("kiln.no_such_module:load", "no module kiln.no_such_module"),
            ("kiln.printers.bed_fit:_rated_build_volume", "never reads 'motion' by name"),
        ],
    )
    def test_a_door_that_does_not_read_the_field_fails(self, tmp_path: Path, door: str, says: str) -> None:
        gate = _load_gate()
        _plant(tmp_path, "intel.json", _intel({"class": "vendor"}))
        row = _intel_row(offline_doors={"motion": door})
        [failure] = gate.judge_data(tmp_path, ledger={"intel.json": row}, today=_TODAY)["failures"]
        assert failure["kind"] == "offline_door" and says in failure["detail"]

    def test_a_door_is_judged_in_the_tree_it_is_given(self, tmp_path: Path) -> None:
        """A reader that names the field passes; the same function reading
        another key fails, so the check reads code, not the door's spelling."""
        gate = _load_gate()
        data, src = tmp_path / "data", tmp_path / "src"
        _plant(data, "intel.json", _intel({"class": "vendor"}))
        (src / "readers").mkdir(parents=True)
        reader = src / "readers" / "__init__.py"
        row = _intel_row(offline_doors={"motion": "kiln.readers:read"})
        reader.write_text('def read(row):\n    return row.get("motion")\n', encoding="utf-8")
        assert gate.judge_data(data, ledger={"intel.json": row}, today=_TODAY, src=src)["failures"] == []
        reader.write_text('def read(row):\n    return row.get("other")\n', encoding="utf-8")
        [failure] = gate.judge_data(data, ledger={"intel.json": row}, today=_TODAY, src=src)["failures"]
        assert failure["kind"] == "offline_door"

    @pytest.mark.parametrize(
        "extra",
        [
            {"offline_doors": {"motion": "motion_facts.load"}},
            {"offline_doors": {"rated_bed": "kiln.motion_facts:load_motion_facts"}},
            {"provenance": {"motion._sources": []}},
            {"provenance": ["motion._sources"]},
        ],
    )
    def test_a_malformed_door_or_provenance_is_refused(self, tmp_path: Path, extra: dict) -> None:
        gate = _load_gate()
        _plant(tmp_path, "intel.json", _intel({"class": "vendor"}))
        failures = gate.judge_data(tmp_path, ledger={"intel.json": _intel_row(**extra)}, today=_TODAY)["failures"]
        assert "malformed" in [f["kind"] for f in failures]

    def test_the_matrix_shape_fails_the_day_it_lands(self, tmp_path: Path) -> None:
        """Three printers, a verdict cell for each of two materials: the
        matrix that shipped at 72 x 38, at its smallest."""
        gate = _load_gate()
        cell = {"status": "compatible", "upgrades_needed": []}
        matrix = {
            "_meta": {"version": 1},
            "kt_one": {"display_name": "Bambu Lab Test One", "pla": dict(cell), "petg": dict(cell)},
            "kt_two": {"display_name": "Prusa Test Two", "pla": dict(cell), "petg": dict(cell)},
            "kt_three": {"display_name": "Creality Test Three", "pla": dict(cell), "petg": dict(cell)},
        }
        _plant(tmp_path, "design_knowledge/verdicts.json", matrix)
        [failure] = gate.judge_data(tmp_path, ledger={}, today=_TODAY)["failures"]
        assert failure["kind"] == "unclassified"
        assert "<a table keyed by material>" in failure["tables"][0]["limit_fields"]["per-material recipe"]

    def test_the_shipped_intel_roster_names_its_doors_and_provenance(self) -> None:
        gate = _load_gate()
        ledger = gate.load_ledger()
        row = ledger["printer_intelligence.json"]
        assert row["offline_doors"] == {
            "motion": "kiln.motion_facts:load_motion_facts",
            "rated_build_volume_mm": "kiln.printers.bed_fit:_rated_build_volume",
        }
        assert row["provenance"] == {"motion._sources": ["class"]}
        assert "design_knowledge/printer_material_compatibility.json" not in ledger
        assert gate.judge_data(today=_TODAY)["failures"] == []


# --- the matrix in disguise (2026-11-02 review) -----------------------------

_COMPAT = "design_knowledge/printer_material_compatibility.json"
_DATA_ROOT = _REPO_ROOT / "kiln" / "src" / "kiln" / "data"


def _verdicts() -> dict:
    """Three printers, a verdict for each of two materials.  The ids name
    their makers, as the shipped matrix's did; every value is a placeholder."""
    cell = {"status": "compatible", "upgrades_needed": []}
    return {p: {"pla": dict(cell), "petg": dict(cell)} for p in ("bambu_t1", "prusa_t2", "creality_t3")}


def _real_compat() -> dict:
    return json.loads((_DATA_ROOT / _COMPAT).read_text(encoding="utf-8"))


def _under_compat_meta() -> dict:
    doc = _real_compat()
    doc["_meta"]["rows"] = _verdicts()
    return doc


_DISGUISES = {
    "nested_two_deep": ("design_knowledge/x.json", lambda: {"tables": {"printers": _verdicts()}}),
    "nested_four_deep": ("x.json", lambda: {"a": {"b": {"c": {"d": _verdicts()}}}}),
    "under_any_underscore_key": ("x.json", lambda: {"_meta": {"_rows": _verdicts()}}),
    "under_the_compat_files_meta": (_COMPAT, _under_compat_meta),
    "transposed_by_material": (
        "x.json",
        lambda: {m: {p: row[m]["status"] for p, row in _verdicts().items()} for m in ("pla", "petg")},
    ),
    "flat_list_of_rows": (
        "x.json",
        lambda: [{"printer": p, "material": m, **cell} for p, row in _verdicts().items() for m, cell in row.items()],
    ),
    "statuses_packed_in_a_string": (
        "x.json",
        lambda: {p: ";".join(f"{m}={c['status']}" for m, c in row.items()) for p, row in _verdicts().items()},
    ),
    "statuses_packed_in_a_list": (
        "x.json",
        lambda: {p: [f"{m}:{c['status']}" for m, c in row.items()] for p, row in _verdicts().items()},
    ),
    "a_short_code_per_material": (
        "x.json",
        lambda: {p: {m: "C" for m in row} for p, row in _verdicts().items()},
    ),
}


class TestMatrixDisguises:
    """The verdict matrix, moved out of public, put back five ways the gate
    read clean: deeper, under an underscore key, transposed, as a list of
    row objects, packed into strings.  Each fails with no ledger row; the
    real shapes still pass."""

    @pytest.mark.parametrize("name", sorted(_DISGUISES))
    def test_the_disguise_fails_unclassified(self, tmp_path: Path, name: str) -> None:
        gate = _load_gate()
        rel, build = _DISGUISES[name]
        _plant(tmp_path, rel, build())
        failures = gate.judge_data(tmp_path, ledger={}, today=_TODAY)["failures"]
        assert [(f["file"], f["kind"]) for f in failures] == [(rel, "unclassified")], failures

    def test_a_nested_table_is_named_by_its_path(self, tmp_path: Path) -> None:
        gate = _load_gate()
        _plant(tmp_path, "x.json", {"tables": {"printers": _verdicts()}})
        [failure] = gate.judge_data(tmp_path, ledger={}, today=_TODAY)["failures"]
        assert [t["table"] for t in failure["tables"]] == ["tables.printers"]

    def test_a_transposed_table_is_read_printer_side_out(self, tmp_path: Path) -> None:
        gate = _load_gate()
        _plant(tmp_path, "x.json", _DISGUISES["transposed_by_material"][1]())
        [failure] = gate.judge_data(tmp_path, ledger={}, today=_TODAY)["failures"]
        [table] = failure["tables"]
        assert table["table"] == "<file> (transposed)"
        assert table["vendors"] == ["bambu", "creality", "prusa"]
        assert table["limit_fields"] == {"per-material recipe": ["<a table keyed by material>"]}

    def test_the_disguise_inside_a_classified_roster_fails_its_fields(self, tmp_path: Path) -> None:
        """A roster's own ``_meta`` is no hiding place either."""
        gate = _load_gate()
        doc = _roster()
        doc["_meta"]["rows"] = _verdicts()
        _plant(tmp_path, "roster.json", doc)
        [failure] = gate.judge_data(tmp_path, ledger={"roster.json": dict(_ROSTER_ROW)}, today=_TODAY)["failures"]
        assert failure["kind"] == "extra_fields"
        assert failure["rows"]["prusa_t2"] == ["petg", "pla"]

    def test_the_default_row_only_compat_file_passes(self, tmp_path: Path) -> None:
        gate = _load_gate()
        _plant(tmp_path, _COMPAT, _real_compat())
        assert gate.judge_data(tmp_path, ledger={}, today=_TODAY)["failures"] == []

    @pytest.mark.parametrize(
        "rel",
        [
            "design_knowledge/materials.json",
            "design_knowledge/environment_compatibility.json",
            "design_knowledge/multi_material_pairing.json",
            "design_knowledge/skin_contact.json",
            "design_knowledge/post_processing.json",
            "design_knowledge/design_templates.json",
            "printer_intelligence.json",
            "safety_profiles.json",
            "slicer_profiles.json",
            "design_knowledge/printer_profiles.json",
        ],
    )
    def test_the_real_shapes_are_not_read_as_a_disguise(self, rel: str) -> None:
        """No shipped file holds a table that only the new rules see: the
        material floors are keyed by material with FIELD sub-keys, so none is
        turned printer-side out, and the rosters flag only their own top
        table."""
        gate = _load_gate()
        doc = json.loads((_DATA_ROOT / rel).read_text(encoding="utf-8"))
        names = [record["table"] for record, _rows in gate._flagged(doc)]
        assert not [n for n in names if "transposed" in n], names
        assert all(n == "<file>" for n in names), names

    def test_the_shipped_tree_still_passes(self) -> None:
        assert _load_gate().judge_data(today=_TODAY)["failures"] == []
