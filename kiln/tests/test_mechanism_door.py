"""A mechanism is declared, composed and sent to a stage through the tools.

The assembly tools are the door: ``add_assembly_part`` takes a part's colour,
``add_assembly_interface`` takes a joint's place in a mechanism (the driver, a
coupling, the hinge of a part that rides on another), and
``compose_assembly_parts`` asks Kiln's servers to play it.  Every check here
goes through the registered tools on an install without kiln-pro, the served
door stubbed, and reads what left this computer and what reached each stage.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import trimesh

from kiln import _pro_mechanism_bridge as bridge
from kiln import local_stage, mechanism_stage, stage_link

_CAM_AXIS = {"kind": "hinge", "axis_point_mm": [0, 0, 20], "axis_direction": [0, 1, 0],
             "range_min": 0, "range_max": 360, "continuous": True}
_LEVER_AXIS = {"kind": "hinge", "axis_point_mm": [40, 0, 30], "axis_direction": [0, 1, 0],
               "range_min": -3, "range_max": 30}


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


def _reply(result):
    if isinstance(result, list):
        return next(part for part in result if isinstance(part, dict))
    return result


def _parts(folder: Path) -> None:
    for name, size, at in (("frame", (80, 10, 4), (20, 0, 2)), ("camshaft", (6, 30, 6), (0, 0, 20)),
                           ("cam", (16, 6, 16), (0, 0, 20)), ("lever", (50, 4, 4), (20, 0, 31))):
        box = trimesh.creation.box(extents=size)
        box.apply_translation(at)
        box.export(folder / f"{name}.stl")


def _declared(tools, folder: Path) -> dict:
    """The automaton in small: a frame, a camshaft turned in it, a cam keyed
    to the shaft, and a lever that rides on the cam."""
    _parts(folder)
    state = tools["create_assembly"](name="automaton")["data"]
    for part, color in (("frame", None), ("camshaft", "#F26B21"), ("cam", "#f26b21"), ("lever", "#2e8b57")):
        state = tools["add_assembly_part"](
            assembly_json=state, part_id=part, file_path=str(folder / f"{part}.stl"), color=color,
        )["data"]
    added = tools["add_assembly_interface"](
        assembly_json=state, part_a_id="frame", part_b_id="camshaft", clearance_mm=0.4,
        motion=_CAM_AXIS, mechanism={"driver": True},
    )
    assert added["success"] and added["interface"] == 0, added
    state = tools["add_assembly_interface"](
        assembly_json=added["data"], part_a_id="camshaft", part_b_id="cam", joint_type="press_fit",
    )["data"]
    added = tools["add_assembly_interface"](
        assembly_json=state, part_a_id="frame", part_b_id="lever", clearance_mm=0.4,
        motion=_LEVER_AXIS, mechanism=json.dumps({"rests_on": "cam", "bearing_radius_mm": 2.5}),
    )
    assert added["success"] and added["interface"] == 2, added
    return added["data"]


def _served_block(request: dict) -> dict:
    """What Kiln's servers answer, built on the spans the request carried."""
    parts = {
        p["part_id"]: {"spans": p["spans"], "moves": p["part_id"] != "frame",
                       "frames": [[0, 0, 0, 1, 0, 0, 0], [0, 0, 0, 1, 0, 0, 0]],
                       **({"color": p["color"]} if p.get("color") else {})}
        for p in request["parts"]
    }
    return {"kind": "kiln.mechanism.v1", "playable": True, "why_not": None,
            "summary": "One turn of the camshaft moves 3 parts.", "headline": "Nothing meets.",
            "poses": [0.0, 360.0], "parts": parts, "clashes": [],
            "followers": [{"part": "lever", "rests_on": "cam"}]}


@pytest.fixture
def served(monkeypatch):
    """An install without kiln-pro whose served door answers the mechanism
    player and nothing else; ``seen`` is what was asked of it."""
    import kiln.server as server

    seen: dict = {}

    def door(tool, **kwargs):
        if tool != bridge.MECHANISM_TOOL:
            raise ConnectionRefusedError()
        seen["request"] = kwargs
        return {"schema": "mechanism_answer/1", "mechanism": _served_block(kwargs)}

    monkeypatch.setattr(bridge, "_here", lambda: False)
    monkeypatch.setattr(server, "_pro_api_call", door)
    return seen


def test_the_tools_record_the_driver_the_riding_part_and_the_colours(tools, tmp_path):
    state = _declared(tools, tmp_path)
    assert state["mechanism"] == {
        "driver": {"interface": 0, "continuous": True}, "couplings": [],
        "followers": [{"interface": 2, "rests_on": "cam", "bearing_radius_mm": 2.5}],
    }
    colors = {p["part_id"]: p.get("color") for p in state["parts"]}
    assert colors == {"frame": None, "camshaft": "#f26b21", "cam": "#f26b21", "lever": "#2e8b57"}
    # The state survives the round trip every later tool makes of it.
    again = tools["validate_assembly"](assembly_json=json.dumps(state))
    assert again["success"] and again["data"]["mechanism"] == state["mechanism"]
    assert not [r for r in again["data"]["recommendations"] if "mechanism cannot be played" in r]


def test_a_coupling_names_the_joint_it_turns_with(tools, tmp_path):
    _parts(tmp_path)
    state = tools["create_assembly"](name="gears")["data"]
    for part in ("frame", "camshaft", "cam"):
        state = tools["add_assembly_part"](assembly_json=state, part_id=part, file_path=str(tmp_path / f"{part}.stl"))["data"]
    state = tools["add_assembly_interface"](assembly_json=state, part_a_id="frame", part_b_id="camshaft",
                                            motion=_CAM_AXIS, mechanism={"driver": True, "range": [0, 90]})["data"]
    assert state["mechanism"]["driver"] == {"interface": 0, "range": [0.0, 90.0]}
    other = {**_CAM_AXIS, "axis_point_mm": [30, 0, 20]}
    state = tools["add_assembly_interface"](assembly_json=state, part_a_id="frame", part_b_id="cam", motion=other,
                                            mechanism={"coupled_to": 0, "kind": "gear", "ratio": -2})
    assert state["success"], state
    state = state["data"]
    assert state["mechanism"]["couplings"] == [{"kind": "gear", "a": 0, "b": 1, "ratio": -2.0}]


@pytest.mark.parametrize(("place", "motion", "said"), [
    ({"rests_on": "cam"}, _LEVER_AXIS, "Declare the mechanism's driver first"),
    ({"driver": True}, None, "must declare its motion"),
    ({"driver": True, "rests_on": "cam"}, _LEVER_AXIS, "one place for this joint"),
])
def test_a_place_that_cannot_be_is_refused_in_a_sentence(tools, tmp_path, place, motion, said):
    _parts(tmp_path)
    state = tools["create_assembly"](name="x")["data"]
    for part in ("frame", "cam", "lever"):
        state = tools["add_assembly_part"](assembly_json=state, part_id=part, file_path=str(tmp_path / f"{part}.stl"))["data"]
    out = tools["add_assembly_interface"](assembly_json=state, part_a_id="frame", part_b_id="lever",
                                          motion=motion, mechanism=place)
    assert out["success"] is False and said in out["error"]


def test_the_record_refuses_a_riding_part_that_cannot_ride(tools, tmp_path):
    state = _declared(tools, tmp_path)
    # Resting on its own base, or on a part no joint holds, is refused by name.
    for rests_on, said in (("frame", "must name the other part it rests on"), ("nothing", "which no joint holds")):
        out = tools["add_assembly_interface"](
            assembly_json={**state, "mechanism": {**state["mechanism"], "followers": []}},
            part_a_id="frame", part_b_id="lever", motion=_LEVER_AXIS, mechanism={"rests_on": rests_on})
        assert out["success"] is False and said in out["error"], out
    bad = tools["add_assembly_part"](assembly_json=state, part_id="knob", file_path="k.stl", color="orange")
    assert bad["success"] is False and "#rrggbb" in bad["error"]


def test_compose_sends_the_riders_and_colours_and_keeps_the_block(tools, tmp_path, served):
    state = _declared(tools, tmp_path)
    composed = tmp_path / "automaton.stl"
    reply = _reply(tools["compose_assembly_parts"](assembly_json=state, output_path=str(composed)))
    assert reply["success"], reply
    request = served["request"]
    assert request["mechanism"]["followers"] == [{"interface": 2, "rests_on": "cam", "bearing_radius_mm": 2.5}]
    assert request["mechanism"]["driver"] == {"interface": 0, "continuous": True}
    sent = {p["part_id"]: p.get("color") for p in request["parts"]}
    assert sent == {"frame": None, "camshaft": "#f26b21", "cam": "#f26b21", "lever": "#2e8b57"}
    said = reply["data"]["mechanism"]
    assert said["playable"] is True and said["says"] == "One turn of the camshaft moves 3 parts."
    block = mechanism_stage.block_for(str(composed))
    assert block["parts"]["lever"]["color"] == "#2e8b57" and block["followers"][0]["part"] == "lever"


def test_the_local_stage_payload_carries_the_playable_block(tools, tmp_path, served):
    state = _declared(tools, tmp_path)
    composed = tmp_path / "automaton.stl"
    _reply(tools["compose_assembly_parts"](assembly_json=state, output_path=str(composed)))
    payload = local_stage._payload_for_mesh(str(composed))
    block = payload["mechanism"]
    assert block["kind"] == mechanism_stage.KIND and block["playable"] is True
    assert block["poses"] == [0.0, 360.0] and set(block["parts"]) == {"frame", "camshaft", "cam", "lever"}
    # The parts' runs cover the payload's own triangles, part after part.
    assert sum(run[1] for part in block["parts"].values() for run in part["spans"]) == payload["counts"]["triangles"]
    # The plate step slid the part; the block says by how far, its frames untouched.
    raw_lo = trimesh.load(composed).bounds[0]
    slide = [payload["bbox"]["min"][0] - raw_lo[0], payload["bbox"]["min"][1] - raw_lo[1]]
    if any(abs(c) > 1e-6 for c in slide):
        assert block["frame"] == "plate" and block["plate_offset_mm"] == pytest.approx(slide, abs=1e-3)
    else:
        assert block["frame"] == "mesh"


def test_a_stage_that_does_not_draw_the_files_triangles_plays_nothing(tools, tmp_path, served):
    state = _declared(tools, tmp_path)
    composed = tmp_path / "automaton.stl"
    _reply(tools["compose_assembly_parts"](assembly_json=state, output_path=str(composed)))
    payload = mechanism_stage.attach_to_payload({"counts": {"triangles": 7}}, str(composed))
    block = payload["mechanism"]
    assert block["playable"] is False and "triangles" in block["why_not"]
    assert "parts" not in block and "poses" not in block
    assert block["summary"] == "One turn of the camshaft moves 3 parts."


def test_the_hosted_stage_upload_sends_the_block_as_mechanism(tools, tmp_path, served, monkeypatch):
    state = _declared(tools, tmp_path)
    composed = tmp_path / "automaton.stl"
    before = stage_link._mechanism_tag(composed)
    _reply(tools["compose_assembly_parts"](assembly_json=state, output_path=str(composed)))
    sent: dict = {}

    class Refused:
        status_code = 500
        text = ""

        def json(self):
            return {}

    def capture(url, **kwargs):
        files = kwargs.get("files") or {}
        sent.update({name: part for name, part in files.items() if name != "file"})
        return Refused()

    monkeypatch.setattr("httpx.post", capture)
    stage_link._upload(Path(composed), "token", None, "")
    name, body, kind = sent["mechanism"]
    assert name == "mechanism.json" and kind == "application/json"
    block = json.loads(body)
    assert block["kind"] == "kiln.mechanism.v1" and block["playable"] is True
    assert "plate_offset_mm" not in block  # the served file is the file
    # A link made before the mechanism was played is not handed back after.
    assert before == "" and stage_link._mechanism_tag(composed) != ""


def test_a_missed_ask_sends_no_mechanism_and_says_why(tools, tmp_path, monkeypatch):
    import kiln.server as server

    monkeypatch.setattr(bridge, "_here", lambda: False)
    monkeypatch.setattr(server, "_pro_api_call", lambda tool, **kw: (_ for _ in ()).throw(ConnectionRefusedError()))
    state = _declared(tools, tmp_path)
    composed = tmp_path / "automaton.stl"
    reply = _reply(tools["compose_assembly_parts"](assembly_json=state, output_path=str(composed)))
    said = reply["data"]["mechanism"]
    assert said["playable"] is False and said["says"].startswith("Kiln plays a mechanism's motion from Kiln's servers.")
    assert stage_link._mechanism_sidecar(composed) is None
    assert "mechanism" not in local_stage._payload_for_mesh(str(composed))
