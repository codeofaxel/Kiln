"""The nozzle size a check assumes, and where the figure came from.

A printability verdict is only true for one nozzle.  Checks used to run
with 0.4 whatever printer they were for; a part with a 0.5 mm fin scored
the same for a machine with a 0.6 mm nozzle on record as for none.  These
pin the one resolver every check asks -- the size stated, else the nozzle
on record, else the printer's own setting, else the model's stock size,
else a named default -- and that the report says which it used.
"""

from __future__ import annotations

import pytest

import kiln._pro_nozzle_bridge as bridge
import kiln.assumed_nozzle as assumed
from kiln.assumed_nozzle import assumed_nozzle

trimesh = pytest.importorskip("trimesh")


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    """No record, no readable printer, nothing remembered, until a test says so."""
    monkeypatch.setattr(assumed, "_setting_memo", {})
    monkeypatch.setattr(bridge, "_record_memo", {})
    monkeypatch.setattr(bridge, "_service_down_until", 0.0)
    monkeypatch.setattr(bridge, "consult_recorded_nozzle", lambda pid: {"diameter_mm": None, "answered": True})
    monkeypatch.setattr("kiln.printer_nozzle_reading.observe_printer_nozzle", lambda pid: None)


def _on_record(monkeypatch, size, *, answered=True):
    monkeypatch.setattr(bridge, "consult_recorded_nozzle", lambda pid: {"diameter_mm": size, "answered": answered})


def _printer_says(monkeypatch, size, *, age=1.0, budget=60.0):
    calls: list[str] = []

    def observe(pid):
        calls.append(pid)
        return {"nozzle_diameter_mm": size, "state_age_seconds": age, "stale_after_seconds": budget}

    monkeypatch.setattr("kiln.printer_nozzle_reading.observe_printer_nozzle", observe)
    return calls


# ---------------------------------------------------------------------------
# The rungs
# ---------------------------------------------------------------------------


class TestWhichSizeIsPicked:
    def test_a_stated_size_wins_over_everything(self, monkeypatch):
        _on_record(monkeypatch, 0.6)
        answer = assumed_nozzle("bambu_a1", stated=0.8)
        assert (answer.diameter_mm, answer.source) == (0.8, "stated")

    def test_the_record_comes_before_the_printer_and_the_stock_size(self, monkeypatch):
        _on_record(monkeypatch, 0.6)
        _printer_says(monkeypatch, 0.8)
        answer = assumed_nozzle("bambu_a1")
        assert (answer.diameter_mm, answer.source) == (0.6, "record")
        assert "0.6 mm" in answer.sentence() and "on record for bambu_a1" in answer.sentence()

    def test_the_printers_own_setting_answers_when_nothing_is_recorded(self, monkeypatch):
        _printer_says(monkeypatch, 0.8)
        answer = assumed_nozzle("bambu_a1")
        assert (answer.diameter_mm, answer.source) == (0.8, "printer_setting")

    def test_a_printer_gone_quiet_does_not_answer(self, monkeypatch):
        _printer_says(monkeypatch, 0.8, age=600.0, budget=60.0)
        assert assumed_nozzle("bambu_a1").source == "stock"

    def test_the_models_stock_size_is_not_always_the_default(self):
        answer = assumed_nozzle("aon_m2_plus")
        assert (answer.diameter_mm, answer.source) == (0.6, "stock")
        assert assumed_nozzle("bambu_a1").source == "stock"

    def test_a_printer_kiln_does_not_know_gets_the_default_and_says_so(self):
        answer = assumed_nozzle("a_printer_nobody_listed")
        assert (answer.diameter_mm, answer.source) == (0.4, "default")
        assert "default" in answer.sentence() and "if yours differs" in answer.sentence()

    def test_a_printer_named_default_is_not_handed_the_fallback_profile_as_its_stock(self, monkeypatch):
        # Kiln registers the first printer as "default", and the bundled
        # profiles carry a fallback entry of the same name.  A machine whose
        # model nobody declared has no stock size to read.
        monkeypatch.setattr("kiln.printer_model_resolver.resolve_printer_model_for", lambda name: None)
        assert assumed_nozzle("default").source == "default"

    def test_no_printer_named_is_the_default(self, monkeypatch):
        _on_record(monkeypatch, 0.6)
        answer = assumed_nozzle(None)
        assert (answer.diameter_mm, answer.source, answer.printer_id) == (0.4, "default", None)

    def test_a_record_that_could_not_be_asked_is_said(self, monkeypatch):
        _on_record(monkeypatch, None, answered=False)
        answer = assumed_nozzle("bambu_a1")
        assert answer.source == "stock" and answer.record_unreachable is True
        assert "could not reach its record" in answer.sentence()

    @pytest.mark.parametrize("stated", [0, -1, 40, "abc"])
    def test_a_size_that_is_not_one_is_not_a_stated_size(self, stated):
        assert assumed_nozzle(None, stated=stated).source == "default"

    def test_a_printer_that_is_off_is_asked_once_not_every_time(self, monkeypatch):
        calls = _printer_says(monkeypatch, None)
        assumed_nozzle("bambu_a1")
        assumed_nozzle("bambu_a1")
        assert calls == ["bambu_a1"]

    def test_a_record_lookup_that_breaks_costs_only_its_rung(self, monkeypatch):
        def broken(pid):
            raise RuntimeError("store unreachable")

        monkeypatch.setattr(bridge, "consult_recorded_nozzle", broken)
        assert assumed_nozzle("bambu_a1").source == "stock"


# ---------------------------------------------------------------------------
# The record, asked of kiln-pro
# ---------------------------------------------------------------------------


class TestTheRecordLookup:
    @pytest.fixture(autouse=True)
    def _real_lookup(self, monkeypatch):
        monkeypatch.undo()
        monkeypatch.setattr(bridge, "_record_memo", {})
        monkeypatch.setattr(bridge, "_service_down_until", 0.0)

    def test_a_recorded_nozzle_is_returned(self, monkeypatch):
        monkeypatch.setattr(bridge, "available", lambda: True)
        monkeypatch.setattr(
            bridge, "consult_nozzle_summary",
            lambda pid: {"diameter_mm": 0.6, "trusted_for_verdicts": True},
        )
        assert bridge.consult_recorded_nozzle("shop_a1") == {"diameter_mm": 0.6, "answered": True}

    def test_a_catalogue_default_is_nobodys_record(self, monkeypatch):
        monkeypatch.setattr(bridge, "available", lambda: True)
        monkeypatch.setattr(
            bridge, "consult_nozzle_summary",
            lambda pid: {"diameter_mm": 0.4, "trusted_for_verdicts": False},
        )
        assert bridge.consult_recorded_nozzle("shop_a1") == {"diameter_mm": None, "answered": True}

    def test_without_kiln_pro_the_served_door_is_asked(self, monkeypatch):
        asked: list[str] = []

        def served(tool_name, _timeout=30.0, **kwargs):
            asked.append(tool_name)
            return {"success": True, "found": True, "trusted_for_verdicts": True, "diameter_mm": 0.6}

        monkeypatch.setattr(bridge, "available", lambda: False)
        monkeypatch.setattr("kiln.server._pro_api_call", served)
        assert bridge.consult_recorded_nozzle("shop_a1") == {"diameter_mm": 0.6, "answered": True}
        # Remembered: a check asked again a moment later does not ask again.
        assert bridge.consult_recorded_nozzle("shop_a1")["diameter_mm"] == 0.6
        assert asked == ["get_nozzle_state"]
        # Until the record is written, when the memory is dropped.
        bridge.forget_recorded_nozzle()
        bridge.consult_recorded_nozzle("shop_a1")
        assert asked == ["get_nozzle_state", "get_nozzle_state"]

    def test_no_answer_is_said_as_not_answered(self, monkeypatch):
        def served(tool_name, _timeout=30.0, **kwargs):
            raise OSError("network is unreachable")

        monkeypatch.setattr(bridge, "available", lambda: False)
        monkeypatch.setattr("kiln.server._pro_api_call", served)
        assert bridge.consult_recorded_nozzle("shop_a1") == {"diameter_mm": None, "answered": False}

    def test_a_refusal_is_not_an_answer(self, monkeypatch):
        monkeypatch.setattr(bridge, "available", lambda: False)
        monkeypatch.setattr(
            "kiln.server._pro_api_call",
            lambda tool_name, _timeout=30.0, **kwargs: {"status": "error", "code": "KILN_ACCOUNT_NOT_PAIRED"},
        )
        assert bridge.consult_recorded_nozzle("shop_a1")["answered"] is False


# ---------------------------------------------------------------------------
# The check: a thin fin, for the nozzle that will print it
# ---------------------------------------------------------------------------


@pytest.fixture
def fin(tmp_path) -> str:
    """A 30 x 30 x 3 mm base with a 0.5 mm fin standing on it: printable
    with a 0.4 mm nozzle, too thin for a 0.6."""
    base = trimesh.creation.box(extents=[30.0, 30.0, 3.0])
    base.apply_translation([15.0, 15.0, 1.5])
    blade = trimesh.creation.box(extents=[20.0, 0.5, 10.0])
    blade.apply_translation([15.0, 15.0, 8.0])
    path = tmp_path / "fin.stl"
    trimesh.util.concatenate([base, blade]).export(str(path))
    return str(path)


class TestTheCheckRunsForTheNozzleOnRecord:
    def test_a_recorded_nozzle_changes_the_verdict_the_way_stating_it_does(self, fin, monkeypatch):
        from kiln.printability import analyze_printability

        unnamed = analyze_printability(fin, material="pla")
        stated = analyze_printability(fin, material="pla", nozzle_diameter=0.6)
        # The check is sensitive to the nozzle at all: without this, the
        # next assertion could pass by measuring nothing.
        assert stated.score < unnamed.score

        _on_record(monkeypatch, 0.6)
        recorded = analyze_printability(fin, material="pla", printer_id="bambu_a1")
        assert recorded.nozzle["source"] == "record" and recorded.nozzle["diameter_mm"] == 0.6
        assert recorded.score == stated.score and recorded.grade == stated.grade
        assert recorded.thin_walls.thin_wall_count == stated.thin_walls.thin_wall_count

    def test_the_report_always_says_which_nozzle_it_assumed(self, fin):
        from kiln.printability import analyze_printability

        unnamed = analyze_printability(fin, material="pla")
        assert unnamed.nozzle["source"] == "default" and unnamed.nozzle["diameter_mm"] == 0.4
        named = analyze_printability(fin, material="pla", printer_id="bambu_a1")
        assert named.nozzle["source"] == "stock"
        assert analyze_printability(fin, material="pla", nozzle_diameter=0.6).nozzle["source"] == "stated"
        assert "nozzle" in unnamed.to_dict()

    def test_the_tool_says_it_in_its_message(self, fin, monkeypatch):
        import asyncio

        from kiln import server

        _on_record(monkeypatch, 0.6)
        out = asyncio.run(server.mcp.call_tool("analyze_printability", {"file_path": fin, "printer_id": "bambu_a1"}))
        assert "Checked for a 0.6 mm nozzle: the nozzle on record for bambu_a1." in str(out)

    def test_the_mesh_pipeline_checks_for_the_printer_it_was_given(self, fin, monkeypatch):
        from kiln.mesh_validation_pipeline import run_validation_pipeline

        _on_record(monkeypatch, 0.6)
        for_printer = run_validation_pipeline(fin, material="PLA", auto_repair=False, printer_id="bambu_a1")
        for_nobody = run_validation_pipeline(fin, material="PLA", auto_repair=False)
        assert for_printer.printability_details["nozzle"]["source"] == "record"
        assert for_nobody.printability_details["nozzle"]["source"] == "default"
        assert for_printer.printability_score < for_nobody.printability_score
