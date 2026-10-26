"""Reinforcing a part never hands back one that carries more structural risk.

Until 2026-10-23 ``apply_design_reinforcements`` read worse after it ran on
the parts it is for.  Two of its edits were the cause:

- the gusset was a flat triangle 2 mm thick laid on the bed at the
  cantilever finding's x and y, whatever height the overhang was at, so it
  braced nothing: on Blender's donut five of them lay loose in the hole and
  their overlaps read as three new sharp inside corners; beside a post with
  an arm, each lay loose on the bed under the arm as a second solid;
- the base plate was a third wider than the part, whatever the part's
  height, so a 5 mm post 60 mm tall still tipped (9.5 to 1, critical) and
  gained a sharp inside corner where it met the plate.

Since then a cantilever is listed with where to add a rib, the base plate is
as wide as the tipping risk it answers asks, and every edit is kept only if
the part it makes carries no more structural risk than the part before it,
in no more solids and as sealed.

Each part here is measured before and after by the structural analysis and
by trimesh, independently of the tool's own check, and the reply's sentence
must agree with what was measured.
"""

from __future__ import annotations

import numpy as np
import pytest

trimesh = pytest.importorskip("trimesh")

from tests import _real_exports as rx  # noqa: E402

_MORE = "carries more structural risk than the original"


def _box(lo, hi):
    b = trimesh.creation.box(extents=np.subtract(hi, lo))
    b.apply_translation(np.add(lo, hi) / 2.0)
    return b


def _part(path, *boxes) -> str:
    if len(boxes) > 1:
        pytest.importorskip("manifold3d", reason="the part is joined with a boolean union")
        trimesh.boolean.union(list(boxes), engine="manifold").export(path)
    else:
        boxes[0].export(path)
    return str(path)


def _needs_openscad() -> None:
    try:
        from kiln.generation.openscad import _find_openscad

        _find_openscad()
    except Exception:  # noqa: BLE001 -- any failure to find it means it cannot run here
        pytest.skip("OpenSCAD is not installed")


def _shape(path) -> tuple[int, bool]:
    """Separate solids and whether the surface is closed, read by trimesh."""
    mesh = trimesh.load(str(path), force="mesh")
    return len(mesh.split(only_watertight=False)), bool(mesh.is_watertight)


def _reinforce(part: str, tmp_path):
    """The real tool on *part*, as a plain install runs it; with the plans
    of the part given and of the part handed back."""
    from kiln import server
    from kiln.design_reasoning import generate_improvement_plan

    out = tmp_path / "reinforced.stl"
    with rx.without_kiln_pro():
        reply = rx.call(server.mcp, "apply_design_reinforcements", {"file_path": part, "output_path": str(out)})
        assert reply.get("success"), reply
        before, after = generate_improvement_plan(part), generate_improvement_plan(str(out))
    worse = (after.critical_count, after.warning_count) > (before.critical_count, before.warning_count)
    assert (_MORE in reply["summary"]) == worse, reply["summary"]
    assert not worse, reply["summary"]
    return reply, before, after, out


def test_the_donut_comes_back_as_it_was(tmp_path):
    """Nothing Kiln can do to the donut makes it stronger, so it is handed
    back unchanged, every cantilever listed with where to brace it."""
    donut = rx.stage("donut_mm.stl", tmp_path / "donut")
    reply, before, after, out = _reinforce(str(donut), tmp_path)

    assert reply["applied"] == [], reply["applied"]
    assert "The part is as it was." in reply["summary"], reply["summary"]
    assert out.read_bytes() == donut.read_bytes()
    assert (after.critical_count, after.warning_count) == (before.critical_count, before.warning_count)
    cantilevers = [r for r in before.risks if r.risk_type == "cantilever"]
    gussets = [s for s in reply["skipped"] if s["type"] == "gusset"]
    assert len(gussets) == (1 if cantilevers else 0), gussets
    if cantilevers:
        assert len(gussets[0]["locations_mm"]) == len(cantilevers)


def test_a_cantilever_is_told_where_to_brace_not_given_a_loose_rib(tmp_path):
    """A post with an arm at its top: no rib lies loose on the bed under the
    arm, and the reply says where a rib would go."""
    part = _part(tmp_path / "arm.stl", _box((0, 0, 0), (10, 10, 40)), _box((0, 0, 35), (40, 10, 40)))
    reply, before, _after, out = _reinforce(part, tmp_path)

    cantilevers = [r for r in before.risks if r.risk_type == "cantilever"]
    assert cantilevers, "the arm reads as a cantilever"
    assert "gusset" not in [a["type"] for a in reply["applied"]]
    assert _shape(out) == _shape(part) == (1, True)
    (gusset,) = [s for s in reply["skipped"] if s["type"] == "gusset"]
    assert gusset["locations_mm"] == [[round(c, 1) for c in r.location_mm] for r in cantilevers]
    assert gusset["reason"] and gusset["guidance"]


def test_a_tall_post_stops_tipping_on_its_base_plate(tmp_path):
    """A 5 mm post 60 mm tall tips (12 to 1, critical); the plate under it is
    wide enough that it no longer does, and it stays one closed solid."""
    _needs_openscad()
    post = _part(tmp_path / "post.stl", _box((-2.5, -2.5, 0), (2.5, 2.5, 60)))
    reply, before, after, out = _reinforce(post, tmp_path)

    (tipping,) = [r for r in before.risks if r.risk_type == "insufficient_base"]
    assert tipping.severity == "critical"
    assert [a["type"] for a in reply["applied"]] == ["add_base"]
    assert after.critical_count == 0, reply["summary"]
    assert not [r for r in after.risks if r.risk_type == "insufficient_base"], reply["summary"]
    assert _shape(out) == (1, True)
    lo, hi = trimesh.load(str(out), force="mesh").bounds
    assert (hi[2] - lo[2]) / min(hi[:2] - lo[:2]) < tipping.metric_threshold


def test_an_edit_that_adds_risk_is_left_out_and_says_so(tmp_path):
    """An arch on two legs tips a little (6.3 to 1, a warning); a plate under
    it would add a sharp inside corner at each leg, so it is left out, the
    reply says why, and the arch comes back as it was."""
    _needs_openscad()
    arch = _part(
        tmp_path / "arch.stl",
        _box((0, 0, 0), (3, 10, 60)), _box((27, 0, 0), (30, 10, 60)), _box((0, 0, 60), (30, 10, 63)),
    )
    reply, before, after, out = _reinforce(arch, tmp_path)

    assert [r.severity for r in before.risks if r.risk_type == "insufficient_base"] == ["warning"]
    assert reply["applied"] == [], reply["applied"]
    (base,) = [s for s in reply["skipped"] if s["type"] == "add_base"]
    assert base["reason"].startswith("Left out: the part it made carried more structural risk"), base
    assert "The part is as it was." in reply["summary"], reply["summary"]
    assert (after.critical_count, after.warning_count) == (before.critical_count, before.warning_count)
    assert _shape(out) == _shape(arch) == (1, True)
