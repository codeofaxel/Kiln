"""An assembly record carries what a kit's guide needs, through the tools.

The record holds the build order and the way a part goes in, and no assembly
tool drops them; the validator knows every joint type on the record's list;
a hand-fitted slip fit is judged as a fit to assemble; a screw beside its
insert is not sized a thread in the print; and the load-bearing caution
carries its offer only to a caller whose plan lacks the tool.  Each test
calls the registered tools.
"""

from __future__ import annotations

import struct

import pytest

import kiln.licensing as licensing
from kiln.assembly import INSERT_JOINT_TYPES, JOINT_TYPES


@pytest.fixture(scope="module")
def tools() -> dict:
    from kiln.plugins.assembly_tools import plugin

    captured: dict = {}

    class FakeMCP:
        def tool(self, **_kwargs):
            def decorator(fn):
                captured[fn.__name__] = fn
                return fn

            return decorator

    plugin.register(FakeMCP())
    return captured


def _box_stl(path, lo, hi) -> str:
    """A binary STL of one axis-aligned box."""
    (x0, y0, z0), (x1, y1, z1) = lo, hi
    v = [(x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0),
         (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)]
    faces = [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
             (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]
    with open(path, "wb") as fh:
        fh.write(b"\0" * 80 + struct.pack("<I", len(faces)))
        for a, b, c in faces:
            fh.write(struct.pack("<12fH", 0.0, 0.0, 0.0, *v[a], *v[b], *v[c], 0))
    return str(path)


@pytest.fixture(scope="module")
def meshes(tmp_path_factory) -> dict:
    root = tmp_path_factory.mktemp("kit")
    return {
        "box": _box_stl(root / "box.stl", (0, 0, 0), (20, 20, 20)),
        "lid": _box_stl(root / "lid.stl", (0, 0, 21), (20, 20, 25)),
        "pin": _box_stl(root / "pin.stl", (40, 0, 0), (44, 4, 20)),
    }


@pytest.fixture()
def _kit(tools, meshes):
    def build(**part_b):
        state = tools["create_assembly"]("kit")["data"]
        state = tools["add_assembly_part"](state, "box", meshes["box"], role="base")["data"]
        reply = tools["add_assembly_part"](state, "lid", meshes["lid"], **part_b)
        assert reply["success"], reply
        return reply["data"]

    return build


class TestBuildOrderIsSaidAndKept:
    def test_a_part_says_what_it_goes_on_after_and_which_way(self, tools, _kit):
        state = _kit(install_after=["box"], insertion_axis=[0, 0, -2])
        lid = state["parts"][1]
        assert lid["install_after"] == ["box"]
        assert lid["insertion_axis"] == [0.0, 0.0, -2.0]

    def test_no_later_tool_drops_it(self, tools, _kit, meshes):
        state = _kit(install_after=["box"], insertion_axis=[0, 1, 0])
        state = tools["add_assembly_part"](state, "pin", meshes["pin"])["data"]
        state = tools["add_assembly_interface"](state, "box", "lid", "clearance_fit", 0.4)["data"]
        state = tools["validate_assembly"](state)["data"]
        lid = next(p for p in state["parts"] if p["part_id"] == "lid")
        assert lid["install_after"] == ["box"]
        assert lid["insertion_axis"] == [0.0, 1.0, 0.0]

    def test_an_order_that_names_no_part_is_said(self, tools, _kit):
        state = _kit(install_after=["ghost"])
        checked = tools["validate_assembly"](state)["data"]
        assert checked["overall_valid"] is False
        assert any("'ghost'" in line for line in checked["recommendations"])

    def test_an_order_in_a_ring_is_said(self, tools, meshes):
        state = tools["create_assembly"]("ring")["data"]
        state = tools["add_assembly_part"](state, "a", meshes["box"], install_after=["b"])["data"]
        state = tools["add_assembly_part"](state, "b", meshes["lid"], install_after=["a"])["data"]
        checked = tools["validate_assembly"](state)["data"]
        assert checked["overall_valid"] is False
        assert any("ring" in line for line in checked["recommendations"])

    def test_a_part_cannot_wait_for_itself(self, tools):
        state = tools["create_assembly"]("self")["data"]
        reply = tools["add_assembly_part"](state, "a", "a.stl", install_after=["a"])
        assert reply["success"] is False


class TestEveryJointTypeIsKnown:
    @pytest.mark.parametrize("joint_type", JOINT_TYPES)
    def test_the_validator_knows_it(self, tools, _kit, joint_type):
        state = _kit()
        clearance = -0.1 if joint_type in ("press_fit", "interference_fit") else 0.2
        state = tools["add_assembly_interface"](state, "box", "lid", joint_type, clearance)["data"]
        checked = tools["validate_assembly"](state)["data"]
        said = " ".join(checked["joint_validations"][0]["issues"])
        assert "Unknown joint type" not in said

    def test_an_unlisted_word_is_still_refused_and_told_the_list(self, tools, _kit):
        state = _kit()
        state = tools["add_assembly_interface"](state, "box", "lid", "welded", 0.2)["data"]
        issue = tools["validate_assembly"](state)["data"]["joint_validations"][0]["issues"][0]
        assert "Unknown joint type" in issue and "heat_set_insert" in issue

    def test_an_interference_fit_drawn_loose_is_said(self, tools, _kit):
        state = _kit()
        state = tools["add_assembly_interface"](state, "box", "lid", "interference_fit", 0.1)["data"]
        validation = tools["validate_assembly"](state)["data"]["joint_validations"][0]
        assert validation["valid"] is False

    @pytest.mark.parametrize("joint_type", sorted(INSERT_JOINT_TYPES))
    def test_an_insert_is_given_no_made_up_clearance(self, tools, _kit, joint_type):
        answer = tools["get_joint_recommendation"](joint_type)["data"]
        assert answer["recommended_clearance_mm"] is None
        assert "recommend_hole" in answer["rationale"]


class TestAScrewIntoAnInsert:
    def _screwed(self, tools, _kit, insert_fastener=None):
        state = _kit()
        state = tools["add_assembly_interface"](
            state, "lid", "box", "threaded", 0.2,
            fastener={"size": "M3", "length_mm": 10, "surface_type": "printed"},
        )["data"]
        return tools["add_assembly_interface"](
            state, "lid", "box", "heat_set_insert", 0.0, fastener=insert_fastener,
        )["data"]

    def test_the_screw_is_not_told_to_cut_a_thread_in_the_print(self, tools, _kit, monkeypatch):
        import kiln.assembly as assembly

        asked: list = []
        monkeypatch.setattr(
            assembly, "_screw_hole_detail_for_joint",
            lambda *a, **k: asked.append(a) or {"fastener_size": "M3", "tier": "generic"},
        )
        checked = tools["validate_assembly"](self._screwed(tools, _kit))["data"]
        screw = checked["joint_validations"][0]
        assert asked == [], "a screw beside its insert was sized a self-threading hole"
        assert not any("fine threads" in line for line in screw["recommendations"])

    def test_a_declared_pocket_and_iron_temperature_are_kept(self, tools, _kit):
        state = self._screwed(tools, _kit, insert_fastener={
            "size": "M3", "family": "heat_set_insert",
            "hole_diameter_mm": 4.1, "hole_depth_mm": 6.7, "install_temp_c": [215, 225],
        })
        checked = tools["validate_assembly"](state)["data"]
        spec = checked["interfaces"][1]["fastener_spec"]
        assert (spec["hole_diameter_mm"], spec["hole_depth_mm"]) == (4.1, 6.7)
        assert spec["install_temp_c"] == [215.0, 225.0]
        assert any("4.1 mm wide, 6.7 mm deep" in line for line in checked["joint_validations"][1]["recommendations"])

    def test_an_undeclared_pocket_is_pointed_at_the_door_that_sizes_it(self, tools, _kit):
        checked = tools["validate_assembly"](self._screwed(tools, _kit))["data"]
        said = " ".join(checked["joint_validations"][1]["recommendations"])
        assert "recommend_hole(screw_name='M3 heat-set insert')" in said


class TestPartsPrintedApart:
    def _slip(self, tools, _kit, monkeypatch, **joint):
        import kiln.assembly as assembly

        seen: list = []

        def view(jtype, *, material, printer_id, mating=None, cache=None, same_print=True):
            seen.append(same_print)
            return (0.5 if same_print else 0.8), {}

        monkeypatch.setattr(assembly, "_running_clearance_view", view)
        state = _kit()
        state = tools["add_assembly_interface"](state, "box", "lid", "clearance_fit", 0.3, **joint)["data"]
        assert state["interfaces"][0]["prints_separately"] is bool(joint.get("prints_separately"))
        return tools["validate_assembly"](state)["data"]["joint_validations"][0], seen

    def test_a_print_in_place_gap_is_still_judged_as_one(self, tools, _kit, monkeypatch):
        validation, seen = self._slip(tools, _kit, monkeypatch)
        assert seen == [True]
        assert any("fused together" in issue for issue in validation["issues"])

    def test_a_hand_fitted_slip_fit_is_not_failed_as_fused(self, tools, _kit, monkeypatch):
        validation, seen = self._slip(tools, _kit, monkeypatch, prints_separately=True)
        assert seen == [], "a fit that does not move was judged as a running gap"
        assert validation["valid"] is True, validation["issues"]


class TestTheOfferReadsTheCallersPlan:
    def _checked(self, tools, _kit, monkeypatch, paid):
        monkeypatch.setattr(licensing, "check_tier", lambda *a, **k: (paid, None if paid else "needs Kiln Pro"))
        state = _kit()
        state = tools["add_assembly_interface"](state, "box", "lid", "threaded", 0.2)["data"]
        return tools["validate_assembly"](state)

    def test_a_free_caller_reads_the_caution_and_the_offer(self, tools, _kit, monkeypatch):
        reply = self._checked(tools, _kit, monkeypatch, paid=False)
        assert "upgrade_recommendation" in reply
        assert "kiln3d.com/pricing" in reply["load_bearing_note"]

    def test_a_paying_caller_reads_the_caution_and_no_offer(self, tools, _kit, monkeypatch):
        reply = self._checked(tools, _kit, monkeypatch, paid=True)
        assert "upgrade_recommendation" not in reply
        note = reply["load_bearing_note"]
        assert "load-test it" in note
        assert "pricing" not in note and "Kiln Pro" not in note

    def test_the_joint_recommendation_door_reads_it_too(self, tools, _kit, monkeypatch):
        monkeypatch.setattr(licensing, "check_tier", lambda *a, **k: (True, None))
        reply = tools["get_joint_recommendation"]("threaded", "PETG", "PETG")
        assert "upgrade_recommendation" not in reply


class TestADeclaredMagnet:
    def test_its_size_and_pocket_are_kept_and_it_is_not_sized_as_a_screw(self, tools, _kit, monkeypatch):
        import kiln.assembly as assembly

        asked: list = []
        monkeypatch.setattr(assembly, "_screw_hole_detail_for_joint", lambda *a, **k: asked.append(a) or None)
        state = tools["add_assembly_interface"](
            _kit(), "box", "lid", "magnetic", 0.1, magnet_polarity_aligned=True,
            fastener={"size": "8 x 3 mm", "family": "magnet", "quantity_per_interface": 4,
                      "hole_diameter_mm": 8.2, "hole_depth_mm": 3.1},
        )["data"]
        checked = tools["validate_assembly"](state)["data"]
        spec = checked["interfaces"][0]["fastener_spec"]
        assert (spec["size"], spec["family"], spec["hole_diameter_mm"], spec["hole_depth_mm"]) == ("8 x 3 mm", "magnet", 8.2, 3.1)
        assert asked == [], "a magnet was sized a screw hole"


class TestAPartsOwnName:
    def test_a_name_is_kept_as_written_by_every_tool(self, tools, _kit, meshes):
        state = _kit(name="  hare   lever ")
        state = tools["add_assembly_part"](state, "pin", meshes["pin"])["data"]
        state = tools["validate_assembly"](state)["data"]
        named = {p["part_id"]: p.get("name") for p in state["parts"]}
        assert named == {"box": None, "lid": "hare lever", "pin": None}


class TestATurningJointIsJudgedByWhatItIs:
    """A shaft carried in two bearings is one joint with two supports, and a
    part declared as riding on another touches it on purpose."""

    TURN = {"kind": "hinge", "axis_point_mm": [10, 0, 10], "axis_direction": [0, 1, 0],
            "range_min": 0, "range_max": 360, "continuous": True}

    def _frame(self, tools, meshes, second_joint):
        add, join = tools["add_assembly_part"], tools["add_assembly_interface"]
        state = tools["create_assembly"]("shaft in two walls")["data"]
        for name, mesh in (("wall_a", "box"), ("wall_b", "lid"), ("shaft", "pin")):
            state = add(state, name, meshes[mesh])["data"]
        state = join(state, "wall_a", "wall_b", "threaded", 0.2)["data"]
        state = join(state, "wall_a", "shaft", "clearance_fit", 0.8, motion=self.TURN, prints_separately=True)["data"]
        reply = join(state, "wall_b", "shaft", **second_joint)
        assert reply["success"], reply
        return tools["validate_assembly"](reply["data"])["data"]

    def test_a_second_bearing_on_the_same_line_is_not_a_loop(self, tools, meshes):
        checked = self._frame(tools, meshes, {
            "joint_type": "clearance_fit", "clearance_mm": 0.8, "motion": self.TURN, "prints_separately": True,
        })
        said = " ".join(issue for joint in checked["joint_validations"] for issue in joint["issues"])
        assert "cannot move on this joint alone" not in said

    def test_a_shaft_also_glued_to_the_frame_is_still_a_loop(self, tools, meshes):
        checked = self._frame(tools, meshes, {"joint_type": "glued", "clearance_mm": 0.1})
        assert "cannot move on this joint alone" in " ".join(checked["joint_validations"][1]["issues"])

    def test_the_parts_that_ride_are_sent_with_the_sweep(self, tools, meshes, monkeypatch):
        import kiln._pro_assembly_bridge as bridge

        sent: list = []
        monkeypatch.setattr(bridge, "_sent", lambda request, **_k: sent.append(request) or (None, bridge.Miss("offline")))
        add, join = tools["add_assembly_part"], tools["add_assembly_interface"]
        state = tools["create_assembly"]("cam and lever")["data"]
        for name, mesh in (("frame", "box"), ("cam", "lid"), ("lever", "pin")):
            state = add(state, name, meshes[mesh])["data"]
        state = join(state, "frame", "cam", "clearance_fit", 0.8, motion=self.TURN, mechanism={"driver": True})["data"]
        swing = {**self.TURN, "axis_point_mm": [42, 0, 10], "range_max": 30, "continuous": False}
        reply = join(state, "frame", "lever", "clearance_fit", 0.8, motion=swing, mechanism={"rests_on": "cam"})
        assert reply["success"], reply
        tools["validate_assembly"](reply["data"])
        assert sent and sent[0]["rides"] == [["lever", "cam"]]

    def test_a_riding_part_is_not_among_the_parts_swept_against(self):
        from kiln.joint_record import standing_parts

        parts = ["frame", "shaft", "cam", "lever"]
        rides = [("lever", "cam")]
        assert standing_parts(["shaft", "cam"], parts, rides) == ["frame"]
        assert standing_parts(["lever"], parts, rides) == ["frame", "shaft"]
        assert standing_parts(["lever"], parts) == ["frame", "shaft", "cam"]

    def test_a_measured_gap_is_held_to_half_of_a_rule_stated_across_the_joint(self):
        from kiln.assembly import _judge_measured_gap

        across = (0.8, {"modeled_quantity": "diametral clearance (bore diameter minus pin diameter)"})
        faces = (0.8, {"modeled_quantity": "gap between the faces"})

        def judged(gap, view, **kw):
            issues: list[str] = []
            _judge_measured_gap({"printed_gap_mm": gap}, 0.8, issues, view, "PLA", **kw)
            return issues

        assert judged(0.45, across) == []
        assert "0.40 mm a side" in judged(0.35, across)[0]
        assert judged(0.45, faces) and "a side" not in judged(0.45, faces)[0]
        assert "fused" in judged(0.35, across)[0]
        apart = judged(0.35, across, separately=True)[0]
        assert "fused" not in apart and "may not go together" in apart
