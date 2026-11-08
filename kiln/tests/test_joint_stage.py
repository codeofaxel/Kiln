"""A composed mesh carries its moving joints to the 3D stage.

``compose_assembly`` keeps each joint declared with a motion beside the file it
writes, and every stage door reads it from there: the payload the inline panel
draws (moved by the plate step with the part) and the copy a hosted link sends
beside the file.  The checks here run through the real tool doors and read the
geometry back, so "the stage would turn the right part about the right line"
is measured, not assumed.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import manifold3d as mf
import numpy as np
import pytest
import trimesh

from kiln import joint_stage, local_stage, stage_link
from tests._served_sweep import canned_sweep, served_sweeps

pytestmark = pytest.mark.usefixtures("served_sweep")

#: Where the whole assembly sits: off the plate's centre on purpose, so the
#: plate step has to move the axis with the part.
_PLACED = (30.0, 20.0, 5.0)


def _mesh(solid: mf.Manifold) -> trimesh.Trimesh:
    out = solid.to_mesh()
    return trimesh.Trimesh(
        np.asarray(out.vert_properties)[:, :3], np.asarray(out.tri_verts), process=False,
    )


def _box(x0, x1, y0, y1, z0, z1) -> mf.Manifold:
    return mf.Manifold.cube([x1 - x0, y1 - y0, z1 - z0]).translate([x0, y0, z0])


def _cylinder_x(radius: float, x0: float, x1: float) -> mf.Manifold:
    return mf.Manifold.cylinder(x1 - x0, radius, radius, 64).rotate([0, 90, 0]).translate([x0, 0, 0])


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
    """The tool's dict, also when a preview bundle wrapped it with pictures."""
    if isinstance(result, list):
        return next(part for part in result if isinstance(part, dict))
    return result


def _compose(tools, folder: Path, motion: dict | None) -> tuple[dict, Path]:
    frame = _mesh((_box(12, 20, -6, 5, -5, 5) + _box(-20, -12, -6, 5, -5, 5)
                   + _box(-20, 20, -40, -6, -1, 1)) - _cylinder_x(2.4, -21, 21))
    leaf = _mesh(_cylinder_x(2.0, -20, 20) + _box(-10, 10, 1.5, 30, -1, 1))
    frame.export(folder / "frame.stl")
    leaf.export(folder / "leaf.stl")
    x, y, z = _PLACED
    state = tools["create_assembly"](name="lid")["data"]
    for part in ("frame", "leaf"):
        state = tools["add_assembly_part"](
            assembly_json=state, part_id=part, file_path=str(folder / f"{part}.stl"),
            position_x=x, position_y=y, position_z=z,
        )["data"]
    state = tools["add_assembly_interface"](
        assembly_json=state, part_a_id="frame", part_b_id="leaf",
        joint_type="clearance_fit", clearance_mm=0.4, motion=motion,
    )["data"]
    composed = folder / "lid.stl"
    reply = _reply(tools["compose_assembly_parts"](assembly_json=state, output_path=str(composed)))
    assert reply["success"], reply
    return reply, composed


def _hinge_motion(range_max: float = 170) -> dict:
    x, y, z = _PLACED
    return {"kind": "hinge", "axis_point_mm": [x, y, z], "axis_direction": [1, 0, 0],
            "range_min": 0, "range_max": range_max}


#: What Kiln's servers say of the hinge composed here (conftest.canned_sweep).
_SERVED = {("frame", "leaf", 170.0): canned_sweep("hinge_0_170"),
           ("frame", "leaf", 120.0): canned_sweep("hinge_0_120")}


@pytest.fixture(scope="module")
def hinged(tools, tmp_path_factory) -> tuple[dict, Path]:
    with served_sweeps(_SERVED):
        return _compose(tools, tmp_path_factory.mktemp("hinged"), _hinge_motion())


def _payload_geometry(payload: dict) -> tuple[np.ndarray, np.ndarray]:
    positions = np.frombuffer(base64.b64decode(payload["positions"]), "<f4").reshape(-1, 3)
    indices = np.frombuffer(base64.b64decode(payload["indices"]), "<u4").reshape(-1, 3)
    return positions.astype(float), indices


def _to_mesh_frame(viewer: np.ndarray) -> np.ndarray:
    """Undo the payload's turn: viewer (x, y, z) is mesh (x, z, -y)."""
    return np.column_stack([viewer[:, 0], -viewer[:, 2], viewer[:, 1]])


# ---------------------------------------------------------------------------
# Compose keeps the joints beside the file
# ---------------------------------------------------------------------------


def test_compose_keeps_the_joint_beside_the_file_and_says_what_it_does(hinged):
    reply, composed = hinged

    assert Path(joint_stage.note_path_for(str(composed))).is_file()
    said = reply["data"]["joints"]
    assert said == [{
        "part_a": "frame", "part_b": "leaf", "kind": "hinge", "binds": True,
        "says": said[0]["says"],
    }]
    assert "binds at 161.1°" in said[0]["says"]


def test_an_assembly_with_no_motion_keeps_no_note(tools, tmp_path):
    reply, composed = _compose(tools, tmp_path, None)

    assert "joints" not in reply["data"]
    assert not Path(joint_stage.note_path_for(str(composed))).exists()
    assert joint_stage.stage_block(str(composed)) is None


def test_the_note_is_for_the_bytes_it_was_written_for(tools, tmp_path):
    _reply_, composed = _compose(tools, tmp_path, _hinge_motion())
    assert joint_stage.stage_block(str(composed)) is not None

    composed.write_bytes(composed.read_bytes() + b"\0")  # other bytes, same name

    assert joint_stage.stage_block(str(composed)) is None


# ---------------------------------------------------------------------------
# The stage payload
# ---------------------------------------------------------------------------


def test_the_payload_names_the_moving_part_by_its_own_triangles(hinged):
    _reply_, composed = hinged
    payload = local_stage._payload_for_mesh(str(composed))

    block = payload["joints"]
    assert block["kind"] == joint_stage.STAGE_KIND
    joint = block["joints"][0]
    assert joint["playable"] is True
    positions, indices = _payload_geometry(payload)
    first, count = joint["moving"]["spans"][0]
    moved = _to_mesh_frame(positions[indices[first:first + count]].reshape(-1, 3))

    leaf = trimesh.load(Path(composed).parent / "leaf.stl").triangles.reshape(-1, 3) + np.array(_PLACED)
    # The payload stood the part on the plate: undo that slide, then the run of
    # triangles is the leaf's, vertex for vertex.
    raw_lo = trimesh.load(composed).bounds[0]
    slide = np.array(payload["bbox"]["min"][:2]) - raw_lo[:2]
    moved[:, :2] -= slide
    assert np.allclose(np.sort(moved, axis=0), np.sort(leaf, axis=0), atol=1e-4)


def test_the_axis_moves_with_the_part_when_the_stage_centres_it(hinged):
    _reply_, composed = hinged
    payload = local_stage._payload_for_mesh(str(composed))
    joint = payload["joints"]["joints"][0]
    positions, indices = _payload_geometry(payload)
    first, count = joint["moving"]["spans"][0]
    moved = _to_mesh_frame(positions[indices[first:first + count]].reshape(-1, 3))

    point = np.array(joint["motion"]["axis_point_mm"])
    direction = np.array(joint["motion"]["axis_direction"])
    assert point[:2].tolist() != list(_PLACED[:2])  # the plate step did move it
    # The contact point took the same slide: still where the plate's edge
    # is, 6 mm behind the axis and 1 mm above it.
    contact = np.array(joint["sweep"]["contact_point_mm"])
    assert contact - point == pytest.approx([0.0, -6.0, 1.0], abs=0.02)
    # The pin (radius 2, beyond the leaf's |x| < 10) sits centred on the axis
    # the stage was handed: the joint turns about its own pin.
    pin = moved[np.abs(moved[:, 0]) > 10.5]
    off_axis = pin - point
    radial = np.linalg.norm(off_axis - np.outer(off_axis @ direction, direction), axis=1)
    assert radial.max() == pytest.approx(2.0, abs=1e-3)


def test_a_payload_that_is_not_the_files_own_triangles_plays_nothing(hinged, monkeypatch):
    _reply_, composed = hinged

    card = local_stage._payload_for_mesh(str(composed), max_bytes=64)
    assert card.get("downgraded") is True
    assert card["joints"]["joints"][0]["playable"] is False
    assert "size card" in card["joints"]["joints"][0]["why_not"]

    real_block = joint_stage.stage_block

    def short_count(path):
        block = real_block(path)
        block["total_triangles"] += 1
        return block

    monkeypatch.setattr(joint_stage, "stage_block", short_count)
    other = local_stage._payload_for_mesh(str(composed))
    assert other["joints"]["joints"][0]["playable"] is False
    assert "does not have the triangles" in other["joints"]["joints"][0]["why_not"]


def test_a_gear_is_kept_and_said_but_not_played(tools, tmp_path):
    x, y, z = _PLACED
    gear = {"kind": "gear", "axis_point_mm": [x, y, z], "axis_direction": [1, 0, 0],
            "range_min": 0, "range_max": 360}
    reply, composed = _compose(tools, tmp_path, gear)

    joint = joint_stage.stage_block(str(composed))["joints"][0]
    assert joint["sweep"] is None
    assert "turns together with its mate" in joint["sweep_why_not"]
    assert joint["playable"] is False
    assert reply["data"]["joints"][0]["says"].startswith("Not swept:")


# ---------------------------------------------------------------------------
# The hosted link carries the block beside the file
# ---------------------------------------------------------------------------


def test_a_stage_link_upload_sends_the_joints_in_the_files_own_frame(hinged, monkeypatch):
    _reply_, composed = hinged
    sent: dict = {}

    class Refused:
        status_code = 500
        text = ""

        def json(self):
            return {}

    def capture(url, **kwargs):
        files = kwargs.get("files") or {}
        sent.update({name: part[1] for name, part in files.items() if name != "file"})
        return Refused()

    monkeypatch.setattr("httpx.post", capture)
    stage_link._upload(Path(composed), "token", None, "")

    block = json.loads(sent["joints"])
    assert block["kind"] == joint_stage.STAGE_KIND
    # Not moved by any plate step: the served file is the file.
    assert block["joints"][0]["motion"]["axis_point_mm"] == list(_PLACED)


# ---------------------------------------------------------------------------
# The profile a stage block carries
# ---------------------------------------------------------------------------


def test_the_profile_is_thinned_and_keeps_the_poses_that_carry_the_verdict():
    readings = [{"pose": round(i * 0.1, 6), "gap_mm": 1.0, "meets": False} for i in range(1000)]
    # A served sweep names its verdict poses from its own readings.
    tightest, last = readings[333]["pose"], readings[-1]["pose"]
    sweep = {"kind": "hinge", "unit": "deg", "range": [0, last], "measured": "exact",
             "readings": readings, "tightest_pose": tightest, "binding_pose": None,
             "free_range": [0.0, last]}

    profile = joint_stage._profile(sweep)

    assert len(profile) <= joint_stage.MAX_PROFILE + 3
    assert {0.0, tightest, last} <= {row[0] for row in profile}
    assert [row[0] for row in profile] == sorted(row[0] for row in profile)


def test_the_note_lists_every_part_that_moves_with_the_joint(tools, tmp_path):
    finger = _box(-3, 3, 26, 29, 1.3, 15)
    shapes = {"frame": None, "leaf": None, "finger": finger}
    frame = _mesh((_box(12, 20, -6, 5, -5, 5) + _box(-20, -12, -6, 5, -5, 5)
                   + _box(-20, 20, -40, -6, -1, 1)) - _cylinder_x(2.4, -21, 21))
    leaf = _mesh(_cylinder_x(2.0, -20, 20) + _box(-10, 10, 1.5, 30, -1, 1))
    meshes = {"frame": frame, "leaf": leaf, "finger": _mesh(shapes["finger"])}
    state = tools["create_assembly"](name="chain")["data"]
    for name, mesh in meshes.items():
        mesh.export(tmp_path / f"{name}.stl")
        state = tools["add_assembly_part"](
            assembly_json=state, part_id=name, file_path=str(tmp_path / f"{name}.stl"),
        )["data"]
    state = tools["add_assembly_interface"](
        assembly_json=state, part_a_id="frame", part_b_id="leaf", joint_type="clearance_fit",
        clearance_mm=0.4, motion={"kind": "hinge", "axis_point_mm": [0, 0, 0],
                                  "axis_direction": [1, 0, 0], "range_min": 0, "range_max": 170},
    )["data"]
    state = tools["add_assembly_interface"](
        assembly_json=state, part_a_id="leaf", part_b_id="finger", joint_type="glued", clearance_mm=0.1,
    )["data"]
    composed = tmp_path / "chain.stl"
    _reply(tools["compose_assembly_parts"](assembly_json=state, output_path=str(composed)))

    moving = joint_stage.stage_block(str(composed))["joints"][0]["moving"]

    counts = [len(meshes[name].faces) for name in ("frame", "leaf", "finger")]
    assert moving["parts"] == ["leaf", "finger"]
    assert moving["spans"] == [[counts[0], counts[1]], [counts[0] + counts[1], counts[2]]]



# ---------------------------------------------------------------------------
# A block is data: anything Kiln did not write is refused whole
# ---------------------------------------------------------------------------


def test_kilns_own_block_passes_its_check(hinged):
    _reply_, composed = hinged
    block = joint_stage.stage_block(str(composed))

    assert block is not None and joint_stage.checked_block(json.loads(json.dumps(block))) == block


def _hostile(block: dict, change) -> dict:
    copy = json.loads(json.dumps(block))
    change(copy)
    return copy


@pytest.mark.parametrize("change", [
    lambda b: b.update(kind="kiln.joints.v2"),
    lambda b: b["joints"][0]["motion"].update(range=[0.0, 1e12]),          # more than a turn
    lambda b: b["joints"][0]["motion"].update(range=[0.0, float("nan")]),
    lambda b: b["joints"][0]["motion"].update(axis_direction=[0.0, 0.0, 0.0]),
    lambda b: b["joints"][0]["motion"].update(unit="mm"),                   # a hinge turns in degrees
    lambda b: b["joints"][0]["moving"].update(spans=[[0, 10 ** 9]]),        # past the file's end
    lambda b: b["joints"][0]["moving"].update(spans=[[0, 1]] * (joint_stage.MAX_SPANS + 1)),
    lambda b: b["joints"][0]["sweep"].update(profile=[[0.0, 0.1, False]] * (joint_stage.MAX_PROFILE + 1)),
    lambda b: b["joints"][0]["sweep"].update(summary="x" * (joint_stage.MAX_TEXT + 1)),
    lambda b: b["joints"][0]["sweep"].update(contact_point_mm=[0.0, float("inf"), 0.0]),
    lambda b: b.update(joints=b["joints"] * (joint_stage.MAX_JOINTS + 1)),
    lambda b: b["joints"][0].update(playable="yes"),
], ids=["kind", "range", "nan", "axis", "unit", "past-end", "spans", "profile", "text", "inf",
        "joints", "playable"])
def test_a_block_kiln_did_not_write_is_refused_whole(hinged, change):
    _reply_, composed = hinged
    block = joint_stage.stage_block(str(composed))

    assert joint_stage.checked_block(_hostile(block, change)) is None


def test_a_slider_range_kiln_writes_passes_and_one_past_any_part_does_not(hinged):
    _reply_, composed = hinged
    block = joint_stage.stage_block(str(composed))

    def slide(span):
        def change(b):
            b["joints"][0]["motion"].update(kind="slider", unit="mm", range=span, swing_axis=None)
        return change

    assert joint_stage.checked_block(_hostile(block, slide([0.0, 25.0]))) is not None
    assert joint_stage.checked_block(_hostile(block, slide([0.0, 1e9]))) is None
