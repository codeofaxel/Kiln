"""A moving joint is declared once on the interface: its axis and its range.

``MatingInterface`` recorded a joint type and a clearance and nothing about
how the joint moves, so a check could only judge the pose as modelled.
``JointMotion`` puts the axis and the range on the record, through every
door that builds or reads an interface: the dataclass, the assembly's
round trip, the ``add_assembly_interface`` tool, and the joint check,
which reads the gap class off the motion.
"""

from __future__ import annotations

import json
import math

import pytest

from kiln.assembly import (
    Assembly,
    AssemblyPart,
    JointMotion,
    MatingInterface,
    create_assembly,
    validate_joint,
)

pytestmark = pytest.mark.usefixtures("served_sweep")


def _two_part_assembly() -> Assembly:
    asm = create_assembly("hinged_lid")
    asm.add_part(AssemblyPart(part_id="box", file_path="box.stl", material="PLA"))
    asm.add_part(AssemblyPart(part_id="lid", file_path="lid.stl", material="PLA"))
    return asm


def _hinge(**overrides) -> JointMotion:
    fields = dict(
        kind="hinge",
        axis_point_mm=(0.0, 40.0, 12.0),
        axis_direction=(2.0, 0.0, 0.0),
        range_min=0.0,
        range_max=110.0,
    )
    fields.update(overrides)
    return JointMotion(**fields)


# ---------------------------------------------------------------------------
# The record
# ---------------------------------------------------------------------------


def test_motion_round_trips_through_the_assembly_record():
    asm = _two_part_assembly()
    asm.add_interface(
        MatingInterface(
            part_a_id="box", part_b_id="lid", joint_type="clearance_fit",
            clearance_mm=0.4, motion=_hinge(),
        )
    )

    restored = Assembly.from_dict(json.loads(json.dumps(asm.to_dict())))

    motion = restored.interfaces[0].motion
    assert motion is not None
    assert motion.kind == "hinge"
    assert motion.axis_point_mm == (0.0, 40.0, 12.0)
    assert motion.axis_direction == (1.0, 0.0, 0.0)  # normalised on the way in
    assert (motion.range_min, motion.range_max) == (0.0, 110.0)
    assert motion.range_unit == "deg"
    assert motion.mating == "pin_in_bore"
    assert restored.interfaces[0].to_dict()["motion"]["range_unit"] == "deg"


def test_an_interface_recorded_before_motion_existed_reads_as_no_motion():
    data = {
        "assembly_id": "a", "name": "old",
        "parts": [
            {"part_id": "p", "file_path": "p.stl", "position_mm": [0, 0, 0], "rotation_deg": [0, 0, 0]},
            {"part_id": "q", "file_path": "q.stl", "position_mm": [0, 0, 0], "rotation_deg": [0, 0, 0]},
        ],
        "interfaces": [{"part_a_id": "p", "part_b_id": "q", "joint_type": "glued"}],
    }
    restored = Assembly.from_dict(data)
    assert restored.interfaces[0].motion is None
    assert restored.interfaces[0].to_dict()["motion"] is None


@pytest.mark.parametrize(
    ("kind", "mating", "unit"),
    [
        ("hinge", "pin_in_bore", "deg"),
        ("gear", "gear_flank", "deg"),
        ("slider", "slot", "mm"),
        ("ball", None, "deg"),
    ],
)
def test_the_gap_class_and_the_unit_follow_the_kind(kind, mating, unit):
    motion = _hinge(kind=kind, range_min=0.0, range_max=30.0)
    assert motion.mating == mating
    assert motion.range_unit == unit


def test_a_named_gap_class_is_kept():
    assert _hinge(mating="planar_face").mating == "planar_face"
    assert _hinge(mating="  ").mating == "pin_in_bore"


def test_the_direction_is_normalised_to_unit_length():
    motion = _hinge(axis_direction=(3.0, 4.0, 0.0))
    assert motion.axis_direction == pytest.approx((0.6, 0.8, 0.0))
    assert math.isclose(math.hypot(*motion.axis_direction), 1.0)


@pytest.mark.parametrize(
    ("overrides", "said"),
    [
        ({"kind": "lever"}, "kind must be one of"),
        ({"axis_direction": (0.0, 0.0, 0.0)}, "zero vector"),
        ({"axis_point_mm": (1.0, 2.0)}, "three numbers"),
        ({"axis_point_mm": (1.0, float("nan"), 0.0)}, "finite"),
        ({"range_min": 90.0, "range_max": 10.0}, "range_min must be below range_max"),
        ({"range_min": 45.0, "range_max": 45.0}, "range_min must be below range_max"),
        ({"range_min": -200.0, "range_max": 200.0}, "full turn"),
        ({"kind": "ball", "range_min": -10.0, "range_max": 20.0}, "ball joint"),
        ({"kind": "ball", "range_min": 0.0, "range_max": 190.0}, "ball joint"),
        ({"range_min": "wide"}, "must be a number"),
    ],
)
def test_an_invalid_motion_is_refused_by_name(overrides, said):
    with pytest.raises(ValueError, match=said):
        _hinge(**overrides)


def test_from_dict_names_the_missing_fields():
    with pytest.raises(ValueError, match="missing axis_direction, range_max"):
        JointMotion.from_dict({"kind": "hinge", "axis_point_mm": [0, 0, 0], "range_min": 0})
    assert JointMotion.from_dict(None) is None


# ---------------------------------------------------------------------------
# The tool door
# ---------------------------------------------------------------------------


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


def _assembly_json() -> str:
    return json.dumps(_two_part_assembly().to_dict())


_SLIDER = {
    "kind": "slider",
    "axis_point_mm": [0, 0, 5],
    "axis_direction": [0, 1, 0],
    "range_min": 0,
    "range_max": 25,
}


def test_the_tool_records_a_motion_given_as_an_object(tools):
    result = tools["add_assembly_interface"](
        assembly_json=_assembly_json(), part_a_id="box", part_b_id="lid",
        joint_type="clearance_fit", clearance_mm=0.3, motion=_SLIDER,
    )
    assert result["success"] is True, result
    motion = result["data"]["interfaces"][0]["motion"]
    assert motion["kind"] == "slider"
    assert motion["range_unit"] == "mm"
    assert motion["mating"] == "slot"
    assert motion["axis_direction"] == [0.0, 1.0, 0.0]


def test_the_tool_records_a_motion_given_as_json_text(tools):
    result = tools["add_assembly_interface"](
        assembly_json=_assembly_json(), part_a_id="box", part_b_id="lid",
        joint_type="clearance_fit", motion=json.dumps(_SLIDER),
    )
    assert result["success"] is True, result
    assert result["data"]["interfaces"][0]["motion"]["range_max"] == 25.0


def test_the_tool_leaves_motion_unset_when_none_is_given(tools):
    result = tools["add_assembly_interface"](
        assembly_json=_assembly_json(), part_a_id="box", part_b_id="lid",
    )
    assert result["success"] is True
    assert result["data"]["interfaces"][0]["motion"] is None


def test_the_tool_reports_a_bad_motion_instead_of_recording_it(tools):
    broken_json = tools["add_assembly_interface"](
        assembly_json=_assembly_json(), part_a_id="box", part_b_id="lid",
        motion="{not json",
    )
    assert broken_json["success"] is False
    assert "Invalid motion JSON" in broken_json["error"]

    bad_kind = tools["add_assembly_interface"](
        assembly_json=_assembly_json(), part_a_id="box", part_b_id="lid",
        motion={**_SLIDER, "kind": "lever"},
    )
    assert bad_kind["success"] is False
    assert "kind must be one of" in bad_kind["error"]

    # The fastener door kept its own wording.
    bad_fastener = tools["add_assembly_interface"](
        assembly_json=_assembly_json(), part_a_id="box", part_b_id="lid",
        fastener="{not json",
    )
    assert "Invalid fastener JSON" in bad_fastener["error"]


# ---------------------------------------------------------------------------
# The check reads the record
# ---------------------------------------------------------------------------


def _capture_mating(monkeypatch) -> list:
    import kiln.assembly as assembly_mod

    seen: list = []

    def fake_view(joint_type, *, material, printer_id, mating=None, cache=None):
        seen.append(mating)
        return None

    monkeypatch.setattr(assembly_mod, "_running_clearance_view", fake_view)
    return seen


def test_a_declared_motion_picks_the_gap_class_the_clearance_is_judged_in(monkeypatch):
    seen = _capture_mating(monkeypatch)
    asm = _two_part_assembly()
    slider = MatingInterface(
        part_a_id="box", part_b_id="lid", joint_type="clearance_fit",
        clearance_mm=0.3, motion=JointMotion.from_dict(_SLIDER),
    )
    validate_joint(slider, asm.parts, printer_id="bambu_a1")
    assert seen == ["slot"]


def test_a_gap_class_the_caller_names_outranks_the_motion(monkeypatch):
    seen = _capture_mating(monkeypatch)
    asm = _two_part_assembly()
    slider = MatingInterface(
        part_a_id="box", part_b_id="lid", joint_type="clearance_fit",
        motion=JointMotion.from_dict(_SLIDER),
    )
    validate_joint(slider, asm.parts, printer_id="bambu_a1", mating="planar_face")
    assert seen == ["planar_face"]


def test_no_motion_leaves_the_gap_class_to_the_check(monkeypatch):
    seen = _capture_mating(monkeypatch)
    asm = _two_part_assembly()
    plain = MatingInterface(part_a_id="box", part_b_id="lid", joint_type="clearance_fit")
    validate_joint(plain, asm.parts, printer_id="bambu_a1")
    assert seen == [None]


@pytest.mark.parametrize("fixed", ["press_fit", "glued"])
def test_a_motion_declared_on_a_fixed_joint_is_an_issue(fixed):
    asm = _two_part_assembly()
    iface = MatingInterface(
        part_a_id="box", part_b_id="lid", joint_type=fixed,
        clearance_mm=-0.1 if fixed == "press_fit" else 0.1, motion=_hinge(),
    )
    result = validate_joint(iface, asm.parts)
    assert result.valid is False
    assert "motion_on_fixed_joint" in result.design_rules_checked
    assert any("holds its parts fixed" in issue for issue in result.issues)


def test_a_motion_on_a_moving_joint_raises_no_such_issue():
    asm = _two_part_assembly()
    iface = MatingInterface(
        part_a_id="box", part_b_id="lid", joint_type="clearance_fit",
        clearance_mm=0.4, motion=_hinge(),
    )
    result = validate_joint(iface, asm.parts)
    assert "motion_on_fixed_joint" not in result.design_rules_checked
    assert not any("holds its parts fixed" in issue for issue in result.issues)
