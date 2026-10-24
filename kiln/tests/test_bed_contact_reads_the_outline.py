"""A part standing on its whole outline is not graded like a part on one spot.

The bed-contact grade read the share of the bounding-box footprint the part
touches.  An open tube, a ring, a picture frame or a cookie cutter touches
only along its outline, so it read as low contact: 15 points off its score,
"Low bed contact area" in its recommendations, and a 5 or 8 mm brim that
Kiln's own slice then printed.  PrusaSlicer's stability check (supports off,
no brim) warns on none of them, at 60 to 200 mm.

Now the grade reads how much of the footprint the contact holds down, so a
patch of contact that runs around an outline holds the area inside it.  Parts
it must still caution are kept: a table on four small feet (its feet are far
apart, but each is its own small spot), a channel on two rails, and a tube
too slender for its own rim, each of which PrusaSlicer warns about too.
Every door reads the one grade.
"""

from __future__ import annotations

import contextlib
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import numpy as np
import pytest

trimesh = pytest.importorskip("trimesh")


def _box(lo, hi):
    b = trimesh.creation.box(extents=np.subtract(hi, lo))
    b.apply_translation(np.add(lo, hi) / 2.0)
    return b


def _on_the_bed(mesh):
    mesh.apply_translation([0, 0, -mesh.bounds[0][2]])
    return mesh


def _tube(radius: float, wall: float, height: float):
    outer = trimesh.creation.cylinder(radius=radius, height=height, sections=128)
    inner = trimesh.creation.cylinder(radius=radius - wall, height=height + 2, sections=128)
    return _on_the_bed(outer.difference(inner))


def _wall_along(curve, wall: float, height: float, n: int = 480):
    """A wall *wall* mm thick along the closed curve *curve(angle)*, no floor."""
    t = np.linspace(0, 2 * np.pi, n, endpoint=False)
    pts = np.array([curve(a) for a in t])
    d = np.roll(pts, -1, axis=0) - np.roll(pts, 1, axis=0)
    nrm = np.c_[d[:, 1], -d[:, 0]] / np.linalg.norm(d, axis=1)[:, None]
    if np.mean(np.sum((pts - pts.mean(0)) * nrm, axis=1)) < 0:
        nrm = -nrm
    out, inn = pts + nrm * wall / 2, pts - nrm * wall / 2
    verts = np.vstack([np.c_[out, np.zeros(n)], np.c_[out, np.full(n, height)],
                       np.c_[inn, np.zeros(n)], np.c_[inn, np.full(n, height)]])
    faces = []
    for k in range(n):
        j = (k + 1) % n
        faces += [[k, j, n + j], [k, n + j, n + k], [2 * n + k, 3 * n + j, 2 * n + j], [2 * n + k, 3 * n + k, 3 * n + j],
                  [n + k, n + j, 3 * n + j], [n + k, 3 * n + j, 3 * n + k], [k, 2 * n + j, j], [k, 2 * n + k, 2 * n + j]]
    mesh = trimesh.Trimesh(verts, faces)
    mesh.fix_normals()
    return mesh


def _export(tmp_path_factory, name: str, mesh) -> str:
    path = tmp_path_factory.mktemp(name) / f"{name}.stl"
    mesh.export(path)
    return str(path)


# Parts standing on their outline, each measured against PrusaSlicer's
# stability check: no warning at all.  (Grade, brim) before 2026-10-23 in
# the trailing comment.
_OUTLINES = {
    "open_tube_100": lambda: _tube(50.0, 2.0, 30.0),  # high, 5 mm brim
    "thin_ring_80": lambda: _tube(40.0, 3.0, 10.0),  # medium, 3 mm brim
    "frame_120x90": lambda: _on_the_bed(_box((0, 0, 0), (120, 90, 8)).difference(_box((3, 3, -1), (117, 87, 9)))),  # medium, 3 mm
    "cookie_cutter_90": lambda: _wall_along(  # high, 8 mm brim
        lambda a: (36 * (1 + 0.25 * np.cos(5 * a)) * np.cos(a), 36 * (1 + 0.25 * np.cos(5 * a)) * np.sin(a)), 1.2, 15.0,
    ),
}


@pytest.fixture(scope="module", params=sorted(_OUTLINES))
def outline_part(request, tmp_path_factory) -> str:
    return _export(tmp_path_factory, request.param, _OUTLINES[request.param]())


@pytest.fixture(scope="module")
def open_tube(tmp_path_factory) -> str:
    return _export(tmp_path_factory, "open_tube", _OUTLINES["open_tube_100"]())


@pytest.fixture(scope="module")
def four_small_feet(tmp_path_factory) -> str:
    """A 100 mm table top on four 4 mm feet, 40 mm up: the feet span the
    whole footprint, and each one is a small spot."""
    parts = [_box((0, 0, 40), (100, 100, 45))]
    parts += [_box((x, y, 0), (x + 4, y + 4, 40.01)) for x in (0, 96) for y in (0, 96)]
    return _export(tmp_path_factory, "four_small_feet", trimesh.util.concatenate(parts))


@pytest.fixture(scope="module")
def two_rails(tmp_path_factory) -> str:
    """A 150 mm channel lying open side down: it stands on two 2 mm rails."""
    channel = _box((0, 0, 0), (150, 40, 20)).difference(_box((-1, 2, -1), (151, 38, 18)))
    return _export(tmp_path_factory, "two_rails", _on_the_bed(channel))


@pytest.fixture(scope="module")
def slender_tube(tmp_path_factory) -> str:
    """20 mm across and 80 mm tall: its 1 mm rim is too little to hold it."""
    return _export(tmp_path_factory, "slender_tube", _tube(10.0, 1.0, 80.0))


@pytest.fixture(params=["free", "as_installed"])
def tier(request, monkeypatch):
    """The free floor (no curated tables) and whatever this install loads."""
    if request.param == "free":
        monkeypatch.setattr("kiln.design_intelligence.load_pro_overlay_or_empty", lambda kind: {})
    return request.param


def _call(name: str, **args) -> dict[str, Any]:
    from kiln import server
    from tests import _real_exports as rx

    return rx.call(server.mcp, name, args)


def _says_low_contact(text: str) -> bool:
    return "Low bed contact" in text or "Low contact" in text or "low bed contact" in text


# ---------------------------------------------------------------------------
# The grade
# ---------------------------------------------------------------------------


def test_a_part_on_its_outline_is_graded_as_standing_on_it(outline_part, tier):
    reply = _call("analyze_printability", file_path=outline_part)
    report = reply["report"]
    bed = report["bed_adhesion"]
    # It touches a small share of its footprint all the same.
    assert bed["contact_percentage"] < 30.0, bed
    assert bed["adhesion_risk"] == "low", bed
    # Charged nothing where the charge is served; a reply about a check Kiln
    # Pro tuned leaves the number out (kiln.printability._served), and the
    # low risk above is what charges nothing.
    assert bed.get("score_deduction", 0) == 0, bed
    assert not any(_says_low_contact(r) for r in report["recommendations"]), report["recommendations"]
    assert reply["verdict"] == "ready", reply["message"]
    assert not report["adhesion"]["use_raft"], report["adhesion"]
    if "frame" in outline_part:
        # A frame's sides are walls held at their corners, and the push test
        # judges them so: its brim is that verdict's, never the contact grade's.
        assert report["adhesion_force"]["risk_level"] == "marginal", report["adhesion_force"]
        assert report["adhesion"]["brim_width_mm"] == 5, report["adhesion"]
        assert "could come off the bed" in reply["message"], reply["message"]
    else:
        assert report["adhesion"]["brim_width_mm"] == 0, report["adhesion"]
        assert "come off the bed" not in reply["message"], reply["message"]


def test_a_part_on_four_small_feet_still_stands_on_its_feet(four_small_feet, tier):
    reply = _call("analyze_printability", file_path=four_small_feet)
    bed = reply["report"]["bed_adhesion"]
    assert bed["contact_span_percentage"] == pytest.approx(100.0, abs=1.0), bed
    assert bed["adhesion_risk"] == "high", bed
    assert reply["report"]["adhesion"]["brim_width_mm"] >= 5, reply["report"]["adhesion"]


def test_a_channel_on_two_rails_still_stands_on_its_rails(two_rails, tier):
    reply = _call("analyze_printability", file_path=two_rails)
    assert reply["report"]["bed_adhesion"]["adhesion_risk"] == "high", reply["report"]["bed_adhesion"]
    assert reply["report"]["adhesion"]["brim_width_mm"] >= 5


def test_a_tube_too_slender_for_its_rim_keeps_its_brim(slender_tube, tier):
    reply = _call("analyze_printability", file_path=slender_tube)
    bed = reply["report"]["bed_adhesion"]
    assert bed["adhesion_risk"] == "medium", bed
    assert bed["graded_contact_percentage"] == bed["contact_percentage"], bed
    assert reply["report"]["adhesion"]["brim_width_mm"] >= 3


# ---------------------------------------------------------------------------
# Every door reads the one grade
# ---------------------------------------------------------------------------


def test_every_door_grades_the_tube_the_same(open_tube):
    from click.testing import CliRunner

    from kiln.cli.main import cli
    from kiln.original_design import audit_original_design
    from kiln.plugins.validation_pipeline_tools import run_full_validation_pipeline

    said = {
        "analyze_printability": _call("analyze_printability", file_path=open_tube),
        "validate_and_prepare_mesh": _call(
            "validate_and_prepare_mesh", file_path=open_tube,
            build_volume_x=256, build_volume_y=256, build_volume_z=256,
        ),
        "validate_and_prepare": run_full_validation_pipeline(open_tube),
        "kiln validate": CliRunner().invoke(cli, ["validate", open_tube]).output,
        "recommend_adhesion_settings": _call("recommend_adhesion_settings", model_path=open_tube),
    }
    for door, reply in said.items():
        text = reply if isinstance(reply, str) else json.dumps(reply, default=str)
        assert not _says_low_contact(text), f"{door}: {text[:2000]}"
    assert said["recommend_adhesion_settings"]["recommendation"]["brim_width_mm"] == 0

    audit = audit_original_design(open_tube, "an open tube")
    requirements = next(g for g in audit.gates if g.name == "design_requirements")
    assert "bed_adhesion" not in requirements.message, requirements.message
    bed_check = next(c for c in audit.design_validation["checks"] if c["check_name"] == "bed_adhesion")
    assert bed_check["passed"], bed_check
    assert not _says_low_contact(json.dumps(audit.feedback, default=str)), audit.feedback


def test_a_failed_tube_print_is_not_put_down_to_its_contact(open_tube):
    from kiln.printability import analyze_printability, collect_failure_signals, diagnose_from_signals

    report = analyze_printability(open_tube)
    diagnosis = diagnose_from_signals(collect_failure_signals(report=report))
    assert diagnosis.failure_category != "adhesion", diagnosis


def test_the_estimate_names_no_brim_for_the_tube(open_tube):
    from kiln.slicer import find_slicer

    try:
        find_slicer()
    except Exception:  # noqa: BLE001 -- no slicer on this machine
        pytest.skip("no slicer installed")
    reply = _call("slice_and_estimate", input_path=open_tube)
    if not reply.get("success"):
        pytest.skip(f"the slice did not run here: {reply.get('error') or reply}")
    assert reply["adhesion"]["brim_width_mm"] == 0, reply["adhesion"]
    assert not _says_low_contact(reply["message"]), reply["message"]


# ---------------------------------------------------------------------------
# The brim the slice prints
# ---------------------------------------------------------------------------


class _Sliced(Exception):
    """Raised once the slice is written: the G-code is what is judged."""


def _slice_and_print_gcode(path: str) -> str:
    """The G-code ``slice_and_print`` writes for *path*, with the brim its
    own decision adds, stopped before anything is sent to a printer."""
    from kiln import slicer as _slicer
    from kiln.plugins.slicer_tools import _SlicerToolsPlugin

    tools: dict[str, Any] = {}

    class _FakeMcp:
        def tool(self, name: str | None = None, **_kwargs):
            def decorator(fn):
                tools[name or fn.__name__] = fn
                return fn

            return decorator

    _SlicerToolsPlugin().register(_FakeMcp())
    real = _slicer.slice_file
    written: dict[str, str] = {}

    def _slice_then_stop(input_path, **kwargs):
        result = real(input_path, **kwargs)
        written["gcode"] = result.output_path
        raise _Sliced

    with patch("kiln.server._check_auth", return_value=None), \
            patch("kiln.slicer.slice_file", side_effect=_slice_then_stop), contextlib.suppress(_Sliced):
        tools["slice_and_print"](input_path=path, skip_validation=True)
    if "gcode" not in written:
        pytest.skip("slice_and_print did not reach the slicer here")
    return written["gcode"]


def _brim_moves(gcode: str) -> int:
    from kiln.slicer_geometry import parse_slicer_features

    bucket = parse_slicer_features(gcode).buckets.get("brim")
    return len(bucket.segments) if bucket is not None else 0


@pytest.fixture()
def slicer_installed():
    from kiln.slicer import find_slicer

    try:
        find_slicer()
    except Exception:  # noqa: BLE001 -- no slicer on this machine
        pytest.skip("no slicer installed")


def test_the_tube_prints_with_no_brim(open_tube, slicer_installed):
    assert Path(open_tube).is_file()
    assert _brim_moves(_slice_and_print_gcode(open_tube)) == 0


def test_the_table_on_small_feet_prints_with_its_brim(four_small_feet, slicer_installed):
    assert _brim_moves(_slice_and_print_gcode(four_small_feet)) > 0
