"""A mechanism declared on the assembly record: one driver, and the joints
that turn together.  The record is judged here; whether the parts really
move as declared is the sweep's question, asked of Kiln's servers."""

from __future__ import annotations

import json

import pytest

from kiln.assembly import (
    Assembly,
    AssemblyPart,
    Coupling,
    JointMotion,
    MatingInterface,
    Mechanism,
    create_assembly,
    validate_assembly,
)

pytestmark = pytest.mark.usefixtures("served_sweep")


def _turning(**overrides) -> JointMotion:
    fields = dict(kind="hinge", axis_point_mm=(0.0, 0.0, 0.0), axis_direction=(0.0, 0.0, 1.0),
                  range_min=0.0, range_max=360.0, continuous=True)
    fields.update(overrides)
    return JointMotion(**fields)


def _slide() -> JointMotion:
    return JointMotion(kind="slider", axis_point_mm=(0.0, 0.0, 0.0),
                       axis_direction=(1.0, 0.0, 0.0), range_min=0.0, range_max=30.0)


def _geared_pair() -> Assembly:
    """A frame carrying two gears and a rack: interfaces 0 and 1 turn,
    interface 2 slides."""
    asm = create_assembly("gear_train")
    for part in ("frame", "drive", "driven", "rack"):
        asm.add_part(AssemblyPart(part_id=part, file_path=f"{part}.stl"))
    asm.add_interface(MatingInterface("frame", "drive", "clearance_fit", motion=_turning()))
    asm.add_interface(MatingInterface("frame", "driven", "clearance_fit",
                                      motion=_turning(axis_point_mm=(30.0, 0.0, 0.0))))
    asm.add_interface(MatingInterface("frame", "rack", "clearance_fit", motion=_slide()))
    return asm


def test_the_mechanism_round_trips_in_the_words_the_record_travels_in():
    asm = _geared_pair()
    asm.mechanism = Mechanism(driver=0, continuous=True, couplings=[
        Coupling("gear", a=0, b=1, ratio=-0.5),
        Coupling("rack", a=1, b=2, pitch_radius_mm=12.0),
    ])

    record = json.loads(json.dumps(asm.to_dict()))

    assert record["mechanism"] == {
        "driver": {"interface": 0, "continuous": True},
        "couplings": [{"kind": "gear", "a": 0, "b": 1, "ratio": -0.5},
                      {"kind": "rack", "a": 1, "b": 2, "pitch_radius_mm": 12.0}],
    }
    restored = Assembly.from_dict(record)
    assert restored.mechanism == asm.mechanism
    assert restored.interfaces[0].motion.continuous is True
    assert restored.interfaces[2].motion.continuous is False


def test_a_bounded_driver_travels_with_its_range():
    asm = _geared_pair()
    asm.mechanism = Mechanism(driver=2, driver_range=(0.0, 25.0))
    assert asm.to_dict()["mechanism"]["driver"] == {"interface": 2, "range": [0.0, 25.0]}
    assert Assembly.from_dict(asm.to_dict()).mechanism.driver_range == (0.0, 25.0)


def test_a_record_written_before_mechanisms_existed_reads_as_none():
    asm = _geared_pair()
    record = asm.to_dict()
    assert "mechanism" not in record
    assert Assembly.from_dict(record).mechanism is None


def test_a_joint_that_does_not_turn_without_end_keeps_its_old_record():
    # Records written before this field existed stay byte-for-byte the same.
    assert "continuous" not in _slide().to_dict()
    assert "continuous" not in _turning(continuous=False, range_max=110.0).to_dict()
    assert _turning().to_dict()["continuous"] is True


@pytest.mark.parametrize("overrides, said", [
    (dict(kind="slider", range_max=30.0), "a joint that turns"),
    (dict(range_max=180.0), "one full turn"),
])
def test_a_continuous_joint_is_one_that_turns_a_full_turn(overrides, said):
    with pytest.raises(ValueError, match=said):
        _turning(**overrides)


@pytest.mark.parametrize("mechanism, said", [
    (Mechanism(driver=7, continuous=True), "joint 7, which is not one of the assembly's interfaces"),
    (Mechanism(driver=2, continuous=True), "a continuous driver must be a joint declared continuous"),
    (Mechanism(driver=2), "needs a range"),
    (Mechanism(driver=2, driver_range=(25.0, 5.0)), "its low end below its high end"),
    (Mechanism(driver=0, continuous=True, couplings=[Coupling("gear", a=0, b=2, ratio=2.0)]),
     "a gear coupling joins two joints that turn"),
    (Mechanism(driver=0, continuous=True, couplings=[Coupling("gear", a=0, b=1, ratio=0.0)]),
     "a ratio that is a number other than zero"),
    (Mechanism(driver=0, continuous=True, couplings=[Coupling("rack", a=0, b=1, pitch_radius_mm=5.0)]),
     "one joint that turns and one that slides"),
    (Mechanism(driver=0, continuous=True, couplings=[Coupling("screw", a=0, b=2)]),
     "needs its lead"),
    (Mechanism(driver=0, continuous=True, couplings=[Coupling("gear", a=1, b=1, ratio=1.0)]),
     "names joint 1 on both sides"),
    (Mechanism(driver=0, continuous=True, couplings=[Coupling("belt", a=0, b=1)]),
     "must be one of gear, rack, screw"),
])
def test_a_declaration_that_cannot_be_is_named(mechanism, said):
    problems = mechanism.problems(_geared_pair().interfaces)
    assert any(said in problem for problem in problems), problems


def test_a_driver_with_no_motion_is_named():
    asm = _geared_pair()
    asm.add_part(AssemblyPart(part_id="cover", file_path="cover.stl"))
    asm.add_interface(MatingInterface("frame", "cover", "glued"))
    problems = Mechanism(driver=3, continuous=True).problems(asm.interfaces)
    assert problems == ["the driver names joint 3, which declares no motion"]


def test_a_sound_declaration_has_no_problems():
    mechanism = Mechanism(driver=0, continuous=True, couplings=[
        Coupling("gear", a=0, b=1, ratio=-0.5),
        Coupling("rack", a=1, b=2, pitch_radius_mm=12.0),
        Coupling("screw", a=2, b=0, lead_mm=2.0),
    ])
    assert mechanism.problems(_geared_pair().interfaces) == []


def test_validation_says_what_cannot_be_played_and_still_lets_the_parts_print(monkeypatch):
    import kiln.assembly as assembly_mod

    monkeypatch.setattr(assembly_mod, "check_all_clearances", lambda asm: [])
    asm = _geared_pair()
    asm.mechanism = Mechanism(driver=2)

    validate_assembly(asm)

    said = [r for r in asm.recommendations if r.startswith("The mechanism cannot be played")]
    assert said == ["The mechanism cannot be played as declared: a driver that is not "
                    "continuous needs a range, its low end below its high end."]
    assert not any("mechanism" in i for j in asm.joint_validations for i in j.issues)
