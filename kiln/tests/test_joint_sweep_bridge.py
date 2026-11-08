"""Public Kiln asks Kiln's servers to sweep an assembly's moving joints.

The sweep is Kiln Pro's (:mod:`kiln._pro_assembly_bridge` asks for it, once
per check, for all of the check's moving joints).  These tests stand in for
the servers at the one place public Kiln calls them and pin what public Kiln
sends, what it does with each kind of answer, and what it works out itself
(a record that contradicts itself is never asked about).
"""

from __future__ import annotations

import base64
import gzip

import pytest

from kiln import _pro_assembly_bridge as bridge
from kiln.assembly import AssemblyPart, JointMotion, MatingInterface, validate_assembly, validate_joint
from kiln.joint_record import MAX_JOINTS_PER_CHECK


def _hinge(hi: float = 170.0) -> JointMotion:
    return JointMotion(kind="hinge", axis_point_mm=(0, 0, 0), axis_direction=(1, 0, 0),
                       range_min=0, range_max=hi)


@pytest.fixture()
def parts(tmp_path):
    names = ("frame", "leaf", "latch", "knob")
    for name in names:
        (tmp_path / f"{name}.stl").write_bytes(b"solid " + name.encode() + b"\nendsolid\n")
    return {name: AssemblyPart(part_id=name, file_path=str(tmp_path / f"{name}.stl")) for name in names}


@pytest.fixture()
def servers(monkeypatch):
    """Kiln's servers as this install reaches them, with no kiln-pro here:
    each call recorded, each answer the test's."""
    import builtins

    real_import = builtins.__import__

    def no_kiln_pro(name, *args, **kwargs):
        if name.startswith("kiln_pro"):
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_kiln_pro)
    calls = []
    state = {"answer": {"schema": "joint_sweep_answer/1", "joints": []}}

    def call(tool, **kwargs):
        calls.append((tool, kwargs))
        answer = state["answer"]
        return answer(kwargs) if callable(answer) else answer

    import kiln.server

    monkeypatch.setattr(kiln.server, "_pro_api_call", call)
    return calls, state


def test_one_request_carries_every_moving_joint_and_the_parts_bytes(parts, servers):
    calls, state = servers
    # A lid on a box, and a knob turning on a latch glued to the same box.
    faces = [MatingInterface("frame", "leaf", "clearance_fit", 0.4, motion=_hinge()),
             MatingInterface("frame", "latch", "glued", 0.1),
             MatingInterface("latch", "knob", "clearance_fit", 0.4, motion=_hinge(90))]
    state["answer"] = {"schema": "joint_sweep_answer/1", "joints": [
        {"index": 0, "sweep": {"binds": True, "summary": "The hinge binds at 161.1°."}, "why_not": None},
        {"index": 2, "sweep": None, "why_not": "part 'knob' has no triangles"},
    ]}

    with bridge.asking() as asking:
        first = asking.joint_sweep(faces[0], parts, faces)
        second = asking.joint_sweep(faces[2], parts, faces)

    assert len(calls) == 1  # one request for the whole check
    tool, sent = calls[0]
    assert tool == bridge.JOINT_SWEEP_TOOL and sent["schema"] == "joint_sweep_request/1"
    assert sent["sweep"] == [0, 2]
    for part in sent["parts"]:
        assert "path" not in part["file"]  # an install sends bytes, never a path on itself
        raw = gzip.decompress(base64.b64decode(part["file"]["gz_b64"]))
        assert raw.startswith(b"solid " + part["part_id"].encode())
    assert first == ({"binds": True, "summary": "The hinge binds at 161.1°."}, None)
    assert second == (None, "part 'knob' has no triangles")


@pytest.mark.parametrize("answer, said", [
    ({"status": "error", "why": "signed_out", "error": "Sign in to Kiln."}, "Kiln is signed out"),
    ({"status": "error", "why": "offline", "error": "No route."}, "could not reach Kiln's servers"),
    ({"status": "error", "why": "unanswered", "error": "Try again shortly."}, "did not answer"),
    ({"schema": "something else"}, "did not answer"),
])
def test_a_check_with_no_sweep_says_why_and_never_passes_the_joint_by_it(parts, servers, answer, said):
    calls, state = servers
    state["answer"] = answer
    face = MatingInterface("frame", "leaf", "clearance_fit", 0.4, motion=_hinge())

    result = validate_joint(face, parts)

    assert result.motion_sweep is None
    assert any(said in r and "was not swept through its range" in r for r in result.recommendations)
    assert not any("binds" in issue for issue in result.issues)


def test_a_part_with_no_file_is_said_without_asking(parts, servers, tmp_path):
    calls, _state = servers
    parts["leaf"] = AssemblyPart(part_id="leaf", file_path=str(tmp_path / "missing.stl"))
    face = MatingInterface("frame", "leaf", "clearance_fit", 0.4, motion=_hinge())

    result = validate_joint(face, parts)

    assert calls == []
    assert any("there is no file at" in r for r in result.recommendations)


def test_a_record_that_contradicts_itself_is_an_issue_and_never_asked(parts, servers):
    calls, _state = servers
    loop = [MatingInterface("frame", "leaf", "clearance_fit", 0.4, motion=_hinge()),
            MatingInterface("leaf", "latch", "glued", 0.1),
            MatingInterface("latch", "frame", "glued", 0.1)]
    itself = MatingInterface("leaf", "leaf", "clearance_fit", 0.4, motion=_hinge())

    looped = validate_joint(loop[0], parts, _interfaces=loop)
    selfish = validate_joint(itself, parts)

    assert calls == []
    assert any("cannot move as declared" in i and "'latch'" in i for i in looped.issues)
    assert any("names part 'leaf' on both sides" in i for i in selfish.issues)


def test_one_check_asks_about_a_bounded_number_of_moving_joints(parts, servers):
    from kiln.assembly import Assembly

    calls, _state = servers
    faces = [MatingInterface("frame", "leaf", "clearance_fit", 0.4, motion=_hinge(10 + i))
             for i in range(MAX_JOINTS_PER_CHECK + 2)]
    assembly = Assembly(assembly_id="many", name="many", parts=list(parts.values()), interfaces=faces)

    validate_assembly(assembly)

    assert len(calls) == 1 and calls[0][1]["sweep"] == list(range(MAX_JOINTS_PER_CHECK))
    last = assembly.joint_validations[-1]
    assert any(f"sweeps the first {MAX_JOINTS_PER_CHECK} joints" in r for r in last.recommendations)


def test_a_measured_gap_under_the_moving_minimum_does_not_pass(parts, servers):
    _calls, state = servers
    state["answer"] = {"schema": "joint_sweep_answer/1", "joints": [{"index": 0, "sweep": {
        "binds": False, "printed_touching": False, "printed_gap_mm": 0.1,
        "summary": "The slider moves through its whole range.",
    }, "why_not": None}]}
    face = MatingInterface("frame", "leaf", "clearance_fit", 0.4, motion=JointMotion(
        kind="slider", axis_point_mm=(0, 0, 0), axis_direction=(0, 1, 0), range_min=0, range_max=15))

    result = validate_joint(face, parts)

    assert result.valid is False
    assert any("only 0.10 mm apart (the joint declares 0.40 mm)" in i for i in result.issues)
