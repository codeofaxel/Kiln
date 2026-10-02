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

    @staticmethod
    def _record_beside(monkeypatch, lookup):
        """kiln-pro installed beside Kiln, its record door answering with
        *lookup* -- which hands back a recorded nozzle or ``None``, and never
        a catalogue default: that door reads the record and nothing else."""
        import sys
        import types

        store = types.ModuleType("kiln_pro.nozzle_intelligence.store_resolver")
        store.recorded_nozzle = lambda printer_id, *, tool_name: lookup(printer_id)
        package = types.ModuleType("kiln_pro.nozzle_intelligence")
        package.store_resolver = store
        root = types.ModuleType("kiln_pro")
        root.nozzle_intelligence = package
        monkeypatch.setitem(sys.modules, "kiln_pro", root)
        monkeypatch.setitem(sys.modules, "kiln_pro.nozzle_intelligence", package)
        monkeypatch.setitem(sys.modules, "kiln_pro.nozzle_intelligence.store_resolver", store)
        monkeypatch.setattr(bridge, "available", lambda: True)

    def test_a_recorded_nozzle_is_returned(self, monkeypatch):
        import types

        self._record_beside(monkeypatch, lambda pid: types.SimpleNamespace(diameter_mm=0.6))
        assert bridge.consult_recorded_nozzle("shop_a1") == {"diameter_mm": 0.6, "answered": True}

    def test_with_kiln_pro_beside_it_the_record_is_asked_every_time(self, monkeypatch):
        # A nozzle recorded a moment ago must be the next check's answer,
        # and a process answering for many accounts must remember none of them.
        import types

        on_record = types.SimpleNamespace(diameter_mm=0.4)
        self._record_beside(monkeypatch, lambda pid: on_record)
        assert bridge.consult_recorded_nozzle("shop_a1")["diameter_mm"] == 0.4
        on_record.diameter_mm = 0.6
        assert bridge.consult_recorded_nozzle("shop_a1")["diameter_mm"] == 0.6
        assert bridge._record_memo == {}

    def test_no_record_is_no_answer(self, monkeypatch):
        self._record_beside(monkeypatch, lambda pid: None)
        assert bridge.consult_recorded_nozzle("shop_a1") == {"diameter_mm": None, "answered": True}

    def test_the_record_is_asked_for_and_nothing_else(self, monkeypatch):
        """The lookup that also fetched a catalogue default is not called:
        a default is nobody's record, and fetching one reads printer data a
        tool that only resolves a nozzle never asked to read."""
        import types

        self._record_beside(monkeypatch, lambda pid: types.SimpleNamespace(diameter_mm=0.6))

        def summary(_pid):
            raise AssertionError("the summary lookup fetches a catalogue default")

        monkeypatch.setattr(bridge, "consult_nozzle_summary", summary)
        assert bridge.consult_recorded_nozzle("shop_a1")["diameter_mm"] == 0.6

    def test_a_record_door_that_fails_is_no_answer(self, monkeypatch):
        def broken(_pid):
            raise RuntimeError("store unreadable")

        self._record_beside(monkeypatch, broken)
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


@pytest.fixture
def block(tmp_path) -> str:
    """A 40 mm cube: thick enough that its walls are a part of its plastic
    and not all of it, so a wider line shows in the weight.  The fin is so
    thin that its shell is the whole solid at any nozzle size."""
    solid = trimesh.creation.box(extents=[40.0, 40.0, 40.0])
    solid.apply_translation([20.0, 20.0, 20.0])
    path = tmp_path / "block.stl"
    solid.export(str(path))
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

    def test_the_tool_with_no_printer_named_uses_the_only_one(self, fin, monkeypatch):
        import asyncio

        from kiln import server

        _registered(monkeypatch)
        _only_record(monkeypatch, "bambu_a1", 0.6)
        out = str(asyncio.run(server.mcp.call_tool("analyze_printability", {"file_path": fin})))
        assert "Checked for a 0.6 mm nozzle: the nozzle on record for bambu_a1, the only printer Kiln knows of." in out

    def test_the_mesh_pipeline_checks_for_the_printer_it_was_given(self, fin, monkeypatch):
        from kiln.mesh_validation_pipeline import run_validation_pipeline

        _on_record(monkeypatch, 0.6)
        for_printer = run_validation_pipeline(fin, material="PLA", auto_repair=False, printer_id="bambu_a1")
        for_nobody = run_validation_pipeline(fin, material="PLA", auto_repair=False)
        assert for_printer.printability_details["nozzle"]["source"] == "record"
        assert for_nobody.printability_details["nozzle"]["source"] == "default"
        assert for_printer.printability_score < for_nobody.printability_score


# ---------------------------------------------------------------------------
# No printer named: the only printer Kiln knows of
# ---------------------------------------------------------------------------


def _registered(monkeypatch, *names):
    class _Registry:
        def list_machines(self):
            return list(names)

    monkeypatch.setattr("kiln.registry.get_printer_registry", lambda: _Registry())


def _only_record(monkeypatch, printer_id, size, *, answered=True):
    monkeypatch.setattr(
        bridge, "consult_only_recorded_nozzle",
        lambda: {"printer_id": printer_id, "diameter_mm": size, "answered": answered},
    )


class TestTheOnlyPrinterStandsIn:
    def test_one_registered_machine_is_the_printer(self, monkeypatch):
        _registered(monkeypatch, "workshop")
        _on_record(monkeypatch, 0.6)
        answer = assumed_nozzle(None, or_only_printer=True)
        assert (answer.diameter_mm, answer.source, answer.printer_id) == (0.6, "record", "workshop")
        assert answer.inferred_printer is True
        assert "the only printer Kiln knows of" in answer.sentence()

    def test_two_registered_machines_are_never_picked_between(self, monkeypatch):
        _registered(monkeypatch, "workshop", "garage")
        _on_record(monkeypatch, 0.6)
        _only_record(monkeypatch, "workshop", 0.6)
        assert assumed_nozzle(None, or_only_printer=True).source == "default"

    def test_with_no_machine_here_the_one_nozzle_on_record_answers(self, monkeypatch):
        _registered(monkeypatch)
        _only_record(monkeypatch, "bambu_a1", 0.6)
        answer = assumed_nozzle(None, or_only_printer=True)
        assert (answer.diameter_mm, answer.source, answer.printer_id) == (0.6, "record", "bambu_a1")
        assert answer.inferred_printer is True

    def test_no_single_record_is_the_default(self, monkeypatch):
        _registered(monkeypatch)
        _only_record(monkeypatch, None, None)
        assert assumed_nozzle(None, or_only_printer=True).source == "default"

    def test_records_that_could_not_be_asked_are_said(self, monkeypatch):
        _registered(monkeypatch)
        _only_record(monkeypatch, None, None, answered=False)
        answer = assumed_nozzle(None, or_only_printer=True)
        assert answer.source == "default" and answer.record_unreachable is True

    def test_a_check_that_did_not_ask_for_it_never_infers(self, monkeypatch):
        _registered(monkeypatch, "workshop")
        _on_record(monkeypatch, 0.6)
        assert assumed_nozzle(None).source == "default"

    def test_a_named_printer_is_never_overridden(self, monkeypatch):
        _registered(monkeypatch, "workshop")
        assert assumed_nozzle("aon_m2_plus", or_only_printer=True).printer_id == "aon_m2_plus"


class TestTheOnlyRecordLookup:
    @pytest.fixture(autouse=True)
    def _real_lookup(self, monkeypatch):
        monkeypatch.undo()
        monkeypatch.setattr(bridge, "_record_memo", {})
        monkeypatch.setattr(bridge, "_service_down_until", 0.0)

    def _served(self, monkeypatch, states):
        asked: list[str] = []

        def served(tool_name, _timeout=30.0, **kwargs):
            asked.append(tool_name)
            return {"success": True, "states": states, "count": len(states)}

        monkeypatch.setattr(bridge, "available", lambda: False)
        monkeypatch.setattr("kiln.server._pro_api_call", served)
        return asked

    def test_exactly_one_stated_record_is_the_answer(self, monkeypatch):
        asked = self._served(monkeypatch, [
            {"printer_id": "bambu_a1", "diameter_mm": 0.6, "trusted_for_verdicts": True},
        ])
        assert bridge.consult_only_recorded_nozzle() == {"printer_id": "bambu_a1", "diameter_mm": 0.6, "answered": True}
        bridge.consult_only_recorded_nozzle()
        assert asked == ["list_nozzle_states"], "a served answer is remembered"

    def test_two_records_are_no_answer(self, monkeypatch):
        self._served(monkeypatch, [
            {"printer_id": "a", "diameter_mm": 0.6, "trusted_for_verdicts": True},
            {"printer_id": "b", "diameter_mm": 0.4, "trusted_for_verdicts": True},
        ])
        assert bridge.consult_only_recorded_nozzle()["printer_id"] is None

    def test_a_catalogue_default_is_not_counted(self, monkeypatch):
        self._served(monkeypatch, [
            {"printer_id": "a", "diameter_mm": 0.6, "trusted_for_verdicts": True},
            {"printer_id": "b", "diameter_mm": 0.4, "trusted_for_verdicts": False},
        ])
        assert bridge.consult_only_recorded_nozzle()["printer_id"] == "a"

    def test_no_answer_is_said_as_not_answered(self, monkeypatch):
        def served(tool_name, _timeout=30.0, **kwargs):
            raise OSError("network is unreachable")

        monkeypatch.setattr(bridge, "available", lambda: False)
        monkeypatch.setattr("kiln.server._pro_api_call", served)
        assert bridge.consult_only_recorded_nozzle()["answered"] is False


# ---------------------------------------------------------------------------
# The detail-depth floor every product's text and relief goes through
# ---------------------------------------------------------------------------


class TestTheDepthFloorFollowsTheNozzle:
    def test_a_wider_nozzle_on_record_raises_the_floor_and_says_why(self, tmp_path, monkeypatch):
        from kiln.decoration_helpers import DepthBelowLegibilityFloor, emboss_text_on_face

        _registered(monkeypatch)
        _only_record(monkeypatch, "bambu_a1", 0.6)
        body = tmp_path / "body.stl"
        body.write_text("solid body\nendsolid body\n")
        with pytest.raises(DepthBelowLegibilityFloor) as refused:
            emboss_text_on_face(str(body), "KILN", depth_mm=1.2)
        assert refused.value.floor_mm == pytest.approx(1.8)
        assert refused.value.nozzle_diameter_mm == pytest.approx(0.6)
        assert "the nozzle on record for bambu_a1, the only printer Kiln knows of" in str(refused.value)

    def test_the_same_depth_is_refused_for_several_lines_too(self, tmp_path, monkeypatch):
        from kiln.decoration_helpers import DepthBelowLegibilityFloor, emboss_text_lines_on_face

        _registered(monkeypatch)
        _only_record(monkeypatch, "bambu_a1", 0.6)
        body = tmp_path / "body.stl"
        body.write_text("solid body\nendsolid body\n")
        with pytest.raises(DepthBelowLegibilityFloor) as refused:
            emboss_text_lines_on_face(str(body), ["KILN", "2026"], depth_mm=1.2)
        assert refused.value.floor_mm == pytest.approx(1.8)

    def test_with_nothing_known_the_floor_is_the_one_it_always_was(self, monkeypatch):
        from kiln.decoration_helpers import _depth_legibility_floor_mm, _legibility_nozzle

        _registered(monkeypatch)
        _only_record(monkeypatch, None, None)
        said: list[str] = []
        nozzle, note = _legibility_nozzle(None, said)
        assert (nozzle, note, said) == (0.4, "", [])
        assert _depth_legibility_floor_mm(nozzle) == pytest.approx(1.2)

    def test_a_stated_nozzle_is_used_as_given_and_said_once(self, monkeypatch):
        from kiln.decoration_helpers import _legibility_nozzle

        _registered(monkeypatch)
        _only_record(monkeypatch, "bambu_a1", 0.6)
        assert _legibility_nozzle(0.4, [])[0] == 0.4
        said: list[str] = []
        assert _legibility_nozzle(None, said)[0] == 0.6
        _legibility_nozzle(None, said)
        assert len(said) == 1 and "0.6 mm" in said[0]


# ---------------------------------------------------------------------------
# The layer plan follows the nozzle too
# ---------------------------------------------------------------------------


class TestTheLayerPlanFollowsTheNozzle:
    def test_the_material_profile_is_for_the_only_printers_nozzle(self, monkeypatch):
        from kiln.plugins.adaptive_slicing_tools import get_material_slicing_profile

        _registered(monkeypatch)
        _only_record(monkeypatch, None, None)
        stock = get_material_slicing_profile("PLA")
        _only_record(monkeypatch, "bambu_a1", 0.6)
        wide = get_material_slicing_profile("PLA")
        told = get_material_slicing_profile("PLA", nozzle_diameter_mm=0.6)

        assert stock["nozzle"]["source"] == "default" and stock["nozzle"]["diameter_mm"] == 0.4
        assert wide["nozzle"]["source"] == "record" and wide["nozzle"]["diameter_mm"] == 0.6
        # Sensitive to the nozzle at all, and the record moves it exactly
        # the way stating the size does.
        assert told["profile"] != stock["profile"]
        assert wide["profile"] == told["profile"]

    def test_a_plan_for_a_named_printer_uses_its_nozzle(self, monkeypatch):
        from kiln.plugins.adaptive_slicing_tools import quick_adaptive_plan

        _on_record(monkeypatch, 0.6)
        plan = quick_adaptive_plan(material="PLA", model_height_mm=20.0, printer="bambu_a1")
        assert plan["success"] is True
        assert plan["nozzle"]["source"] == "record" and plan["nozzle"]["printer_id"] == "bambu_a1"


# ---------------------------------------------------------------------------
# Estimates: how much plastic a wall takes depends on how wide it is laid
# ---------------------------------------------------------------------------


def _call(name: str, **arguments):
    import asyncio
    import json

    from kiln import server

    out = asyncio.run(server.mcp.call_tool(name, arguments))
    content = out[0] if isinstance(out, tuple) else out
    return json.loads(content[0].text)


class TestTheEstimatesFollowTheNozzle:
    """Each estimate tool, through the registered tool: the only printer's
    nozzle moves the figure exactly as stating the size does, and the reply
    says which size it used."""

    @pytest.mark.parametrize(
        ("tool", "weight"),
        [
            ("estimate_material_cost", lambda out: out["weight_g"]),
            ("estimate_print_cost_from_mesh", lambda out: out["cost_breakdown"]["filament"]),
        ],
    )
    def test_a_mesh_estimate(self, block, monkeypatch, tool, weight):
        _registered(monkeypatch)
        _only_record(monkeypatch, None, None)
        stock = _call(tool, file_path=block)
        told = _call(tool, file_path=block, nozzle_mm=0.6)
        _only_record(monkeypatch, "bambu_a1", 0.6)
        wide = _call(tool, file_path=block)

        assert stock["nozzle"]["source"] == "default" and stock["nozzle"]["diameter_mm"] == 0.4
        assert told["nozzle"]["source"] == "stated"
        assert wide["nozzle"]["source"] == "record" and wide["nozzle"]["printer_id"] == "bambu_a1"
        assert weight(told) != weight(stock)
        assert weight(wide) == weight(told)

    def test_an_estimate_from_dimensions(self, monkeypatch):
        _registered(monkeypatch)
        _only_record(monkeypatch, None, None)
        size = {"width_mm": 60.0, "depth_mm": 40.0, "height_mm": 20.0}
        stock = _call("estimate_before_design", **size)
        told = _call("estimate_before_design", **size, nozzle_mm=0.6)
        _only_record(monkeypatch, "bambu_a1", 0.6)
        wide = _call("estimate_before_design", **size)

        assert stock["nozzle"]["source"] == "default"
        assert wide["nozzle"]["source"] == "record" and wide["nozzle"]["diameter_mm"] == 0.6
        assert told["estimate"] != stock["estimate"]
        assert wide["estimate"] == told["estimate"]
