"""The printability score is what the report says it took off.

Every analysis that costs a part points states its share on its own block,
as ``score_deduction``.  Five of the eight charges used to be made inside the
score and stated nowhere: a reader could not account for them, and code that
re-scored a report from its blocks dropped them without a sign.  These pin
the sum on real parts, chosen so that every kind of charge is made at least
once.
"""

from __future__ import annotations

import sys

import pytest

trimesh = pytest.importorskip("trimesh")

from kiln.printability import analyze_printability  # noqa: E402

#: The blocks the score charges, by their key in the report.
_CHARGED = (
    "overhangs", "thin_walls", "bridging", "bed_adhesion", "supports",
    "warping", "thermal_stress", "adhesion_force",
)


def _block_kiln_pro(monkeypatch) -> None:
    """No paid package in reach, so the score is public Kiln's own.  Its
    submodules are blocked too: one already imported would still answer a
    ``from`` import with its parent blocked."""
    for name in [n for n in sys.modules if n == "kiln_pro" or n.startswith("kiln_pro.")]:
        monkeypatch.setitem(sys.modules, name, None)
    monkeypatch.setitem(sys.modules, "kiln_pro", None)


def _box(extents, at):
    mesh = trimesh.creation.box(extents=extents)
    mesh.apply_translation(at)
    return mesh


def _parts() -> dict[str, tuple[str, object]]:
    """Name -> (material, mesh).  Every part sits on the plate inside it."""
    column = trimesh.creation.cylinder(radius=3, height=60, sections=48)
    column.apply_translation([120, 120, 30])
    return {
        # An arm held out from a post: overhang, bridge, support and contact charges.
        "cantilever": ("PLA", trimesh.util.concatenate([
            _box((10, 10, 30), (100, 100, 15)), _box((70, 10, 5), (130, 100, 32.5)),
        ])),
        # A beam across two posts.
        "bridge": ("PLA", trimesh.util.concatenate([
            _box((10, 10, 20), (90, 100, 10)), _box((10, 10, 20), (140, 100, 10)),
            _box((60, 10, 4), (115, 100, 22)),
        ])),
        # The same beam made one solid with its posts: a slicer will likely
        # bridge its 30 mm, so it is charged as a bridge, by its span.
        "fused_bridge": ("PLA", trimesh.boolean.union([
            _box((10, 10, 20), (90, 100, 10)), _box((10, 10, 20), (130, 100, 10)),
            _box((50, 10, 5), (110, 100, 22.5)),
        ])),
        # A blade thinner than the nozzle.
        "fin": ("PLA", trimesh.util.concatenate([
            _box((30, 30, 3), (100, 100, 1.5)), _box((20, 0.2, 10), (100, 100, 8)),
        ])),
        # A tall, narrow column.
        "column": ("PLA", column),
        # A large flat plate in a warp-prone material.
        "abs_plate": ("ABS", _box((200, 200, 2), (128, 128, 1))),
    }


@pytest.fixture(scope="module")
def reports(tmp_path_factory) -> dict[str, dict]:
    """Each part's report, read in process: the points a check took off are
    the report's own bookkeeping and never reach a reply
    (``kiln.printability._SCORE_ARITHMETIC``), so the reconciliation reads
    the object, not ``to_dict``."""
    folder = tmp_path_factory.mktemp("charged_parts")
    out: dict[str, dict] = {}
    with pytest.MonkeyPatch.context() as mp:
        _block_kiln_pro(mp)
        for name, (material, mesh) in _parts().items():
            path = folder / f"{name}.stl"
            mesh.export(str(path))
            out[name] = _in_process(analyze_printability(str(path), material=material))
    return out


def _in_process(report) -> dict:
    """The report as a dict that still carries the score's arithmetic."""
    out = report.to_dict()
    out["stop_deduction"] = report.stop_deduction
    for key in _CHARGED:
        check = getattr(report, key, None)
        if check is not None and isinstance(out.get(key), dict):
            out[key]["score_deduction"] = check.score_deduction
    return out


def _stated(report: dict) -> dict[str, int]:
    return {key: report[key]["score_deduction"] for key in _CHARGED if isinstance(report.get(key), dict)}


@pytest.mark.parametrize("name", sorted(_parts()))
def test_the_score_is_100_less_what_its_blocks_state(reports, name):
    report = reports[name]
    assert report["placement"]["faults"] == [], "a placement fault is charged by the floor, not a block"
    stated = _stated(report)
    # A part that will not print as it is (the fin: a blade thinner than the
    # nozzle) is charged by the floor as well, and the report states that
    # charge too, so the sum still accounts for every point.
    stated["hard stops"] = report["stop_deduction"]
    assert report["score"] == max(0, min(100, 100 + sum(stated.values()))), stated


@pytest.mark.parametrize("name", sorted(_parts()))
def test_every_charged_block_states_its_share(reports, name):
    report = reports[name]
    for key in ("overhangs", "thin_walls", "bridging", "bed_adhesion", "supports"):
        deduction = report[key]["score_deduction"]
        assert isinstance(deduction, int) and deduction <= 0, (key, deduction)


def test_every_kind_of_charge_is_exercised(reports):
    """Without this the sum could hold only because nothing was charged.

    ``overhangs`` is not among them since 2026-10-24: the underside it finds
    is charged once, by area, on ``supports``, and its own block states 0.
    The fused bridge keeps ``bridging`` charged (a long bridge the support
    area did not pay for)."""
    charged = {key for report in reports.values() for key, points in _stated(report).items() if points < 0}
    assert {"thin_walls", "bridging", "bed_adhesion", "supports", "warping", "thermal_stress"} <= charged
    assert all(report["overhangs"]["score_deduction"] == 0 for report in reports.values())
