"""An answer that a part prints says how to hold it down when it stands on one
small spot.

The verdict (``kiln.printability.verdict``) checked supports before anything
else and stopped there, so an 80 mm cap on a 3 mm stem read "Prints with
supports (...)." and nothing more, while the report under it called the bed
contact high risk and its own brim decision said an 8 mm brim was mandatory.
A person reads the sentence; the caution that keeps the part on the bed was
only in the list below it.

Measured 2026-10-23 with PrusaSlicer's own stability check (supports off) as
the second opinion: every part this sentence names (the stem-and-cap parts,
spheres and a knife edge resting on the bed, a cone frustum on a 6 mm base)
carries its "Low bed adhesion" warning, and the brim Kiln names clears it on
all but the spheres.  Open tubes and rings read as low contact too, by the
share of their footprint they touch, yet none of six carried that warning:
their contact runs around their outline, so the sentence leaves them alone.
The verdict key does not move, so no gate passes or fails differently.
"""

from __future__ import annotations

import numpy as np
import pytest

trimesh = pytest.importorskip("trimesh")


def _box(lo, hi):
    b = trimesh.creation.box(extents=np.subtract(hi, lo))
    b.apply_translation(np.add(lo, hi) / 2.0)
    return b


@pytest.fixture()
def mushroom(tmp_path) -> str:
    """An 80 mm cap on a 3 mm stem: prints with supports, on 9 mm2 of bed."""
    cap = _box((-40, -40, 25), (40, 40, 27))
    for _ in range(4):
        cap = cap.subdivide()
    path = tmp_path / "mushroom.stl"
    trimesh.util.concatenate([_box((-1.5, -1.5, 0), (1.5, 1.5, 25)), cap,
                              _box((-40, -40, 27), (-36, 40, 40))]).export(path)
    return str(path)


@pytest.fixture()
def small_base(tmp_path) -> str:
    """A frustum 40 mm across on a 6 mm base: walls 40 degrees off vertical,
    so it needs no supports and prints as it is, on 28 mm2 of bed."""
    n = 96
    ang = np.linspace(0, 2 * np.pi, n, endpoint=False)
    bottom = np.c_[3 * np.cos(ang), 3 * np.sin(ang), np.zeros(n)]
    top = np.c_[20 * np.cos(ang), 20 * np.sin(ang), np.full(n, 20.0)]
    verts = np.vstack([bottom, top, [[0, 0, 0], [0, 0, 20]]])
    faces = []
    for i in range(n):
        j = (i + 1) % n
        faces += [[i, j, n + j], [i, n + j, n + i], [2 * n, j, i], [2 * n + 1, n + i, n + j]]
    mesh = trimesh.Trimesh(verts, faces)
    mesh.fix_normals()
    path = tmp_path / "small_base.stl"
    mesh.export(path)
    return str(path)


@pytest.fixture()
def open_tube(tmp_path) -> str:
    """A 100 mm tube with 2 mm walls and no floor: low contact by its share of
    the footprint, standing on a ring as wide as itself."""
    outer = trimesh.creation.cylinder(radius=50.0, height=30.0, sections=128)
    inner = trimesh.creation.cylinder(radius=48.0, height=32.0, sections=128)
    tube = outer.difference(inner)
    tube.apply_translation([0, 0, -tube.bounds[0][2]])
    path = tmp_path / "tube.stl"
    tube.export(path)
    return str(path)


@pytest.fixture(params=["free", "as_installed"])
def tier(request, monkeypatch):
    """The free floor (no curated tables) and whatever this install loads."""
    if request.param == "free":
        monkeypatch.setattr("kiln.design_intelligence.load_pro_overlay_or_empty", lambda kind: {})
    return request.param


def _call(name: str, **args):
    from kiln import server
    from tests import _real_exports as rx

    return rx.call(server.mcp, name, args)


def _brim_named(reply: dict) -> str:
    """The fix the report's own brim decision names, in the sentence's words."""
    rec = reply["report"]["adhesion"]
    if rec["use_raft"]:
        return "on a raft"
    return f"{rec['brim_width_mm']} mm brim"


def test_a_part_on_one_small_spot_is_told_how_to_hold_it_down(mushroom, tier):
    reply = _call("analyze_printability", file_path=mushroom)
    assert reply["verdict"] == "prints_with_supports"
    first, _, rest = reply["message"].partition(". ")
    assert first.startswith("Prints with supports ("), reply["message"]
    assert "9 mm2 of the bed" in rest and _brim_named(reply) in rest, reply["message"]


def test_the_fix_is_the_report_s_own_decision(mushroom):
    # A warping filament on a tiny contact: the decision is a raft, and the
    # sentence says the raft, not a brim it did not choose.
    reply = _call("analyze_printability", file_path=mushroom, material="abs")
    assert reply["report"]["adhesion"]["use_raft"] is True
    assert "print it on a raft" in reply["message"] and "brim" not in reply["message"], reply["message"]


def test_a_part_that_prints_as_it_is_on_a_small_base_says_so(small_base, tier):
    reply = _call("analyze_printability", file_path=small_base)
    assert reply["verdict"] == "ready"
    assert reply["message"].startswith("Ready to print. It stands on only 28 mm2"), reply["message"]
    assert _brim_named(reply) in reply["message"]


def test_a_tube_standing_on_its_rim_is_not_told_to_add_a_brim(open_tube, tier):
    from kiln.printability import analyze_printability, verdict

    report = analyze_printability(open_tube)
    # The share of the footprint it touches is in the high band, but since
    # 2026-10-23 the grade reads the outline it stands on, so it is low.
    assert report.bed_adhesion.contact_percentage <= 10.0
    assert report.bed_adhesion.adhesion_risk == "low"
    assert verdict(report) == ("ready", "Ready to print.")


def test_every_door_says_it(mushroom):
    from click.testing import CliRunner

    from kiln.cli.main import cli
    from kiln.original_design import audit_original_design
    from kiln.plugins.validation_pipeline_tools import run_full_validation_pipeline

    said = {
        "validate_and_prepare_mesh": _call(
            "validate_and_prepare_mesh", file_path=mushroom,
            build_volume_x=256, build_volume_y=256, build_volume_z=256,
        )["message"],
        "validate_and_prepare": run_full_validation_pipeline(mushroom)["summary"],
        "design audit": next(
            g.message for g in audit_original_design(mushroom, "a mushroom").gates
            if g.name == "printability"
        ),
        "kiln validate": CliRunner().invoke(cli, ["validate", mushroom]).output.splitlines()[0],
    }
    for door, text in said.items():
        assert "It stands on only 9 mm2 of the bed" in text and "8 mm brim" in text, f"{door}: {text}"
    assert said["validate_and_prepare"].startswith("Print-ready with supports ("), said


def test_the_estimate_says_the_brim_once(mushroom):
    from kiln.slicer import find_slicer

    try:
        find_slicer()
    except Exception:  # noqa: BLE001 -- no slicer on this machine
        pytest.skip("no slicer installed")
    reply = _call("slice_and_estimate", input_path=mushroom)
    if not reply.get("success"):
        pytest.skip(f"the slice did not run here: {reply.get('error') or reply}")
    message = reply["message"]
    assert "It stands on only 9 mm2 of the bed" in message, message
    assert message.count("brim") == 1, message
