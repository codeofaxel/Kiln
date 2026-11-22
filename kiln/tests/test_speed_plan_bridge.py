"""Asking for the speeds Kiln chooses by height: what is measured and sent,
what comes back, and what a person is told when no answer comes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

trimesh = pytest.importorskip("trimesh")

from kiln import _pro_speed_plan_bridge as bridge  # noqa: E402
from kiln import speed_by_height as sbh  # noqa: E402
from kiln.served_answer import Miss  # noqa: E402

from tests.test_speed_by_height_kinds import INFILL, INNER, OUTER, PLAIN, _gcode  # noqa: E402


@pytest.fixture
def part(tmp_path: Path) -> str:
    box = trimesh.creation.box(extents=(30, 30, 10))
    box.apply_translation((15, 15, 5))
    path = tmp_path / "part.stl"
    box.export(path)
    return str(path)


@pytest.fixture
def plain(tmp_path: Path) -> Any:
    return sbh.read_gcode(_gcode(tmp_path / "plain.gcode", PLAIN))


class TestTheAsk:
    @pytest.mark.parametrize("wish", ["kiln", " Kiln ", {"by": "kiln"}, {"by": "KILN", "carries_load": True}])
    def test_kiln_is_an_ask_for_kilns_own_speeds(self, wish):
        assert bridge.read_ask(wish) is not None

    @pytest.mark.parametrize("wish", [None, [], [{"from_mm": 0, "percent": 50}], '[{"from_mm": 0, "percent": 50}]', {"from_mm": 0, "percent": 50}])
    def test_a_persons_own_ranges_are_not(self, wish):
        assert bridge.read_ask(wish) is None

    def test_a_door_adds_the_printer_and_material_it_knows_and_leaves_ranges_alone(self):
        assert bridge.with_context("kiln", printer="bambu_a1", material="PLA") == {"by": "kiln", "printer": "bambu_a1", "material": "PLA"}
        assert bridge.with_context({"by": "kiln", "printer": "mine"}, printer="bambu_a1")["printer"] == "mine"
        ranges = [{"from_mm": 0, "percent": 50}]
        assert bridge.with_context(ranges, printer="bambu_a1") is ranges


class TestWhatIsMeasured:
    def test_each_level_carries_a_layers_time_and_every_kind_of_line(self, part, plain):
        from kiln.part_outlines import outlines_by_height

        measured = outlines_by_height(part)
        facts = bridge.sliced_facts(measured, plain)
        assert len(facts["levels"]) == len(measured["levels"])
        level = facts["levels"][5]
        assert set(level) == {"layer_s", "kinds"}
        assert set(level["kinds"]) == {INNER.lower(), OUTER.lower(), INFILL.lower()}
        assert level["kinds"][INFILL.lower()]["fastest_mm_s"] == 100.0
        assert sum(kind["share"] for kind in level["kinds"].values()) == pytest.approx(1.0, abs=0.01)
        assert facts["flow_limit_mm3_s"] == 11.0 and facts["acceleration_mm_s2"] == 6000.0 and facts["layer_mm"] == 0.2

    def test_nothing_sent_judges_the_part(self, part, plain):
        from kiln.part_outlines import outlines_by_height

        sent = str({"part": outlines_by_height(part), "sliced": bridge.sliced_facts(outlines_by_height(part), plain)})
        for word in ("gentle", "quicker", "sway", "delicate", "sturdy", "pace", "inside"):
            assert word not in sent


class TestThePlanner:
    def _asked(self, monkeypatch, answer) -> list[dict[str, Any]]:
        asked: list[dict[str, Any]] = []

        def local(fields, several=False):
            asked.append({**fields, **({"several": True} if several else {})})
            return answer

        monkeypatch.setattr(bridge, "_local", local)
        return asked

    def test_kilns_answer_becomes_the_ranges_the_notes_and_the_question(self, monkeypatch, part, plain):
        asked = self._asked(monkeypatch, {"success": True, "status": "ok", "plans": [{
            "printer": "bambu_a1", "ranges": [{"from_mm": 4, "to_mm": 8, "max_mm_s": 50, "why": "Gentle."}],
            "said": ["One line."], "planned": True,
        }], "ask": {"about": "carries_load", "question": "Does this part have to carry load?"}})
        plan = bridge.planner(part, slicer_id="bambu_studio", ask={"printer": "bambu_a1", "material": "PLA"})(plain)
        assert plan["ranges"][0]["max_mm_s"] == 50 and plan["said"] == ["One line."] and plan["ask"]["about"] == "carries_load"
        (sent,) = asked
        assert set(sent) == {"part", "printers"}
        (printer,) = sent["printers"]
        assert (printer["printer"], printer["material"], printer["slicer"]) == ("bambu_a1", "PLA", "bambu_studio")

    def test_what_the_person_said_about_load_is_sent_and_silence_is_not(self, monkeypatch, part, plain):
        answer = {"success": True, "status": "ok", "plans": [{"printer": "", "ranges": [], "said": [], "planned": False}]}
        asked = self._asked(monkeypatch, answer)
        bridge.planner(part, slicer_id="prusaslicer", ask={"material": "PLA", "carries_load": True})(plain)
        bridge.planner(part, slicer_id="prusaslicer", ask={"material": "PLA"})(plain)
        assert asked[0]["carries_load"] == "yes" and "carries_load" not in asked[1]

    def test_a_printer_that_is_one_of_several_asks_as_one_of_several(self, monkeypatch, part, plain):
        answer = {"success": True, "status": "ok", "plans": [{"printer": "a", "ranges": [], "said": [], "planned": False}]}
        asked = self._asked(monkeypatch, answer)
        bridge.planner(part, slicer_id="prusaslicer", ask={"material": "PLA", "printer": "a", "fleet": ["a", "b"]})(plain)
        bridge.planner(part, slicer_id="prusaslicer", ask={"material": "PLA", "printer": "a", "fleet": ["a"]})(plain)
        bridge.planner(part, slicer_id="prusaslicer", ask={"material": "PLA", "printer": "a"})(plain)
        assert [one.get("several", False) for one in asked] == [True, False, False]
        assert bridge.read_ask({"by": "kiln", "fleet": ["a", "b"]}) == {"fleet": ["a", "b"]}

    def test_the_wire_asked_is_the_one_for_several_only_when_there_are_several(self, monkeypatch, part, plain):
        import kiln.server as server

        called: list[str] = []

        def api(tool, **_fields):
            called.append(tool)
            return {"success": True, "status": "ok", "plans": [{"printer": "a", "ranges": [], "said": [], "planned": False}]}

        monkeypatch.setattr(bridge, "_engine", lambda: None)
        monkeypatch.setattr(server, "_pro_api_call", api)
        bridge.planner(part, slicer_id="prusaslicer", ask={"material": "PLA", "fleet": ["a", "b"]})(plain)
        bridge.planner(part, slicer_id="prusaslicer", ask={"material": "PLA"})(plain)
        assert called == [bridge.FLEET_WIRE_TOOL, bridge.WIRE_TOOL]

    @pytest.mark.parametrize(("miss", "says"), [
        (Miss("offline"), "this computer is offline"),
        (Miss("signed_out"), "signed out"),
        (Miss("unanswered"), "didn't answer"),
        (Miss("refused", "TIER_REQUIRED", "Speeds Kiln chooses by height needs Kiln Pro."), "needs Kiln Pro"),
    ])
    def test_no_answer_refuses_the_slice_and_says_which_of_the_four_it_was(self, monkeypatch, part, plain, miss, says):
        monkeypatch.setattr(bridge, "_engine", lambda: None)
        monkeypatch.setattr(bridge, "_served", lambda fields, several=False: (None, miss))
        with pytest.raises(sbh.SpeedByHeightError) as refused:
            bridge.planner(part, slicer_id="prusaslicer", ask={"material": "PLA"})(plain)
        assert refused.value.code == bridge.NO_PLAN
        assert says in str(refused.value) and "sliced nothing" in str(refused.value)

    def test_the_engine_stopping_on_this_computer_is_a_refusal_never_a_traceback(self, monkeypatch, part, plain):
        def stopped(**_fields):
            raise RuntimeError("boom")

        monkeypatch.setattr(bridge, "_engine", lambda: stopped)
        with pytest.raises(sbh.SpeedByHeightError) as refused:
            bridge.planner(part, slicer_id="prusaslicer", ask={"material": "PLA"})(plain)
        assert refused.value.code == bridge.NO_PLAN and "boom" not in str(refused.value)

    def test_a_part_that_cannot_be_read_refuses_without_asking(self, monkeypatch, tmp_path, plain):
        asked = self._asked(monkeypatch, {})
        empty = tmp_path / "empty.stl"
        empty.write_bytes(b"")
        with pytest.raises(sbh.SpeedByHeightError) as refused:
            bridge.planner(str(empty), slicer_id="prusaslicer", ask={"material": "PLA"})(plain)
        assert refused.value.code == bridge.NO_PLAN and not asked


    def test_a_cad_file_that_cannot_be_made_into_a_mesh_refuses_without_asking(self, monkeypatch, part, plain):
        from kiln import step_import

        asked = self._asked(monkeypatch, {})
        monkeypatch.setattr(step_import, "resolve_mesh_input", lambda path: (None, None, {"error": "no converter here"}))
        with pytest.raises(sbh.SpeedByHeightError) as refused:
            bridge.planner(part, slicer_id="prusaslicer", ask={"material": "PLA"})(plain)
        assert refused.value.code == bridge.NO_PLAN and "no converter here" in str(refused.value) and not asked


class TestTheSliceDoor:
    def test_a_slice_asked_for_kilns_speeds_goes_to_the_planner_and_ranges_of_its_own_do_not(self, monkeypatch, part, tmp_path):
        from kiln import slicer

        seen: dict[str, Any] = {}

        def sliced(ranges, **kwargs):
            seen.update(ranges=ranges, planner=kwargs.get("planner"), eases=kwargs.get("eases_acceleration"))
            raise sbh.SpeedByHeightError("stopped here", code="TEST")

        monkeypatch.setattr(sbh, "sliced", sliced)
        slicer_path = "/Applications/PrusaSlicer.app/Contents/MacOS/PrusaSlicer"
        if not Path(slicer_path).exists():
            pytest.skip("PrusaSlicer not installed")
        from kiln.slicer_profiles import resolve_slicer_profile

        for wish, planned in (("kiln", True), ([{"from_mm": 2, "to_mm": 4, "max_mm_s": 30}], False)):
            with pytest.raises(sbh.SpeedByHeightError, match="stopped here"):
                slicer.slice_file(part, output_dir=str(tmp_path / "o"), profile=resolve_slicer_profile("bambu_a1"),
                                  slicer_path=slicer_path, material="PLA", speed_by_height=wish)
            assert (seen["planner"] is not None) is planned and bool(seen["ranges"]) is not planned
            assert seen["eases"] is False
