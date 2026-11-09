"""A mechanism declared on an assembly record is played by Kiln's servers at
compose, kept beside the composed file, and a miss is said in one sentence.

No kiln-pro here: the bridge is driven through the served door, stubbed, so
the request it builds, the note it writes and the sentence it says are the
public install's own.
"""

from __future__ import annotations

import base64
import gzip
import json
import struct
from pathlib import Path

import pytest

from kiln import _pro_mechanism_bridge as bridge
from kiln import mechanism_stage
from kiln.assembly import Assembly, AssemblyPart, compose_assembly
from kiln.served_answer import HOSTED_DOORS


def _stl(path: Path, dx: float = 0.0) -> None:
    """One triangle, binary STL."""
    tri = [(0.0 + dx, 0.0, 0.0), (10.0 + dx, 0.0, 0.0), (0.0 + dx, 10.0, 0.0)]
    body = b"\0" * 80 + struct.pack("<I", 1) + struct.pack("<12fH", 0.0, 0.0, 1.0, *[c for p in tri for c in p], 0)
    path.write_bytes(body)


@pytest.fixture
def assembly(tmp_path):
    _stl(tmp_path / "frame.stl")
    _stl(tmp_path / "crank.stl", dx=20.0)
    parts = [AssemblyPart(part_id="frame", file_path=str(tmp_path / "frame.stl")),
             AssemblyPart(part_id="crank", file_path=str(tmp_path / "crank.stl"))]
    faces = [{"part_a_id": "frame", "part_b_id": "crank", "joint_type": "clearance_fit", "clearance_mm": 0.4,
              "motion": {"kind": "hinge", "axis_point_mm": [0, 0, 0], "axis_direction": [0, 0, 1],
                         "range_min": 0, "range_max": 360, "continuous": True}}]
    return Assembly.from_dict({"assembly_id": "a", "name": "a",
                               "parts": [p.to_dict() for p in parts], "interfaces": faces,
                               "mechanism": {"driver": {"interface": 0, "continuous": True}, "couplings": []}})


def _block(playable=True):
    return {"kind": "kiln.mechanism.v1", "playable": playable, "why_not": None if playable else "locked",
            "summary": "One turn of the crank moves 1 part.", "clashes": [], "poses": [0.0, 360.0],
            "parts": {"frame": {"frames": [[0, 0, 0, 1, 0, 0, 0]] * 2, "spans": [[0, 1]], "moves": False},
                      "crank": {"frames": [[0, 0, 0, 1, 0, 0, 0]] * 2, "spans": [[1, 1]], "moves": True}}}


def test_the_request_carries_parsed_meshes_spans_interfaces_and_the_mechanism(assembly):
    request = bridge.request_for(assembly, {"frame": (0, 1), "crank": (1, 1)}, here=False)
    assert request["schema"] == "mechanism_request/1"
    assert [p["part_id"] for p in request["parts"]] == ["frame", "crank"]
    assert request["parts"][1]["spans"] == [[1, 1]]
    sent = gzip.decompress(base64.b64decode(request["parts"][0]["file"]["gz_b64"]))
    assert struct.unpack("<I", sent[80:84])[0] == 1, "the parsed triangles travel, never the file as it is"
    assert request["mechanism"] == {"driver": {"interface": 0, "continuous": True}, "couplings": []}
    assert request["interfaces"][0]["motion"]["kind"] == "hinge"
    by_path = bridge.request_for(assembly, {}, here=True)
    assert "path" in by_path["parts"][0]["file"] and "spans" not in by_path["parts"][0]


def test_compose_keeps_the_served_block_beside_the_file(assembly, tmp_path, monkeypatch):
    import kiln.server as server

    seen = {}

    def served(tool, **kwargs):
        seen["tool"] = tool
        seen["request"] = kwargs
        return {"schema": "mechanism_answer/1", "mechanism": _block()}

    monkeypatch.setattr(bridge, "_here", lambda: False)
    monkeypatch.setattr(server, "_pro_api_call", served)
    out = compose_assembly(assembly, str(tmp_path / "composed.stl"))
    assert seen["tool"] == bridge.MECHANISM_TOOL
    assert seen["request"]["parts"][1]["spans"] == [[1, 1]]
    assert out["mechanism"]["playable"] is True and out["mechanism"]["clashes"] == 0
    assert out["mechanism"]["says"] == "One turn of the crank moves 1 part."
    note = out["mechanism"]["note"]
    assert note == str(tmp_path / "composed.stl.mechanism.json") and Path(note).is_file()
    assert mechanism_stage.block_for(str(tmp_path / "composed.stl"))["poses"] == [0.0, 360.0]
    assert json.loads(mechanism_stage.sidecar_for_mesh(str(tmp_path / "composed.stl")))["kind"] == "kiln.mechanism.v1"
    # A note beside other bytes is not read.
    (tmp_path / "composed.stl").write_bytes(b"\0" * 84)
    assert mechanism_stage.block_for(str(tmp_path / "composed.stl")) is None


def test_a_miss_is_one_sentence_and_the_compose_still_lands(assembly, tmp_path, monkeypatch):
    import kiln.server as server

    monkeypatch.setattr(bridge, "_here", lambda: False)
    monkeypatch.setattr(server, "_pro_api_call", lambda tool, **kw: (_ for _ in ()).throw(ConnectionRefusedError()))
    out = compose_assembly(assembly, str(tmp_path / "composed.stl"))
    assert (tmp_path / "composed.stl").is_file()
    assert out["mechanism"]["playable"] is False and out["mechanism"]["note"] is None
    said = out["mechanism"]["says"]
    assert said.startswith("Kiln plays a mechanism's motion from Kiln's servers. Kiln can't play this mechanism right now because")
    assert "says nothing about where they meet" in said and "compose again" in said
    monkeypatch.setattr(server, "_pro_api_call", lambda tool, **kw: {"schema": "mechanism_answer/1", "mechanism": _block(False)})
    out = compose_assembly(assembly, str(tmp_path / "composed.stl"))
    assert out["mechanism"]["playable"] is False and out["mechanism"]["says"] == "locked"


def test_the_bridge_is_on_the_roster():
    assert "kiln._pro_mechanism_bridge" in HOSTED_DOORS
