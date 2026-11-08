"""A flat underside held along one side is an overhang, said by how far it sticks out.

The failure this guards: a shelf running 10 mm past a wall answered
``bridges=13.3mm`` and "Long bridges detected (13.3 mm).  Enable supports or
reduce bridge spans."  It is held on one side, so it is not a bridge, and
13.3 mm is no dimension of the part: twice the distance from the wall to
the middle of a triangle moves with the mesh (6.7 mm for a 5 mm shelf,
40.0 mm for a 30 mm one).  A span that long read against the material's
bridge limit tells the person a shorter shelf would print.

These tests build the parts corner by corner, so every number asserted is one
a ruler gives: the shelf's own length.  A span held on two sides keeps the
bridge wording, and whether a part needs supports does not move.
"""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from kiln.printability import analyze_printability, support_verdict
from kiln.slicer_supports import decide_supports
from kiln.support_assessment import assess_support_feasibility

_Tri = tuple[tuple[float, float, float], ...]

#: The parts' depth along Y, mm.
_DEPTH = 16.0


@pytest.fixture(autouse=True)
def _public_floor(monkeypatch):
    """Judge on public Kiln's own floor, whatever is installed."""
    import kiln.design_intelligence as design_intelligence

    monkeypatch.setattr(design_intelligence, "load_pro_overlay_or_empty", lambda kind: {})


def _prism(profile: list[tuple[float, float]], caps: list[tuple[int, int, int]], x0: float = 0.0) -> list[_Tri]:
    """*profile* (x, z corners, counter-clockwise) extruded along Y: a closed
    solid whose faces use the profile's own corners and no others.  *caps*
    cuts the profile into triangles by corner index."""
    front = [(x + x0, 0.0, z) for x, z in profile]
    back = [(x + x0, _DEPTH, z) for x, z in profile]
    faces: list[_Tri] = []
    for a, b, c in caps:
        faces.append((front[a], front[b], front[c]))
        faces.append((back[a], back[c], back[b]))
    for i in range(len(profile)):
        j = (i + 1) % len(profile)
        faces.append((front[i], back[j], front[j]))
        faces.append((front[i], back[i], back[j]))
    return faces


def _shelf(sticks_out: float, x0: float = 0.0) -> list[_Tri]:
    """A 4 mm wall, 23 mm tall, whose top 3 mm runs *sticks_out* mm past it."""
    far = 4.0 + sticks_out
    return _prism([(0, 0), (4, 0), (4, 20), (far, 20), (far, 23), (0, 23)],
                  [(0, 1, 2), (0, 2, 5), (2, 3, 4), (2, 4, 5)], x0)


def _bridge(span: float, x0: float = 0.0) -> list[_Tri]:
    """Two 4 mm walls *span* mm apart under a 3 mm deck."""
    a, b = 4.0 + span, 8.0 + span
    return _prism([(0, 0), (4, 0), (4, 20), (a, 20), (a, 0), (b, 0), (b, 23), (0, 23)],
                  [(0, 1, 2), (0, 2, 7), (2, 3, 6), (2, 6, 7), (3, 4, 5), (3, 5, 6)], x0)


def _blocks(xs: list[float], ys: list[float], zs: list[float], filled) -> list[_Tri]:
    """One closed solid from the cells of a grid: *filled(i, j, k)* says
    which cells are material.  Every face is a whole cell face, so
    neighbouring faces always share their edges."""
    def solid(i: int, j: int, k: int) -> bool:
        inside = 0 <= i < len(xs) - 1 and 0 <= j < len(ys) - 1 and 0 <= k < len(zs) - 1
        return inside and bool(filled(i, j, k))

    faces: list[_Tri] = []
    for i in range(len(xs) - 1):
        for j in range(len(ys) - 1):
            for k in range(len(zs) - 1):
                if not solid(i, j, k):
                    continue
                x0, x1, y0, y1, z0, z1 = xs[i], xs[i + 1], ys[j], ys[j + 1], zs[k], zs[k + 1]
                for open_side, quad in (
                    (not solid(i - 1, j, k), [(x0, y0, z0), (x0, y0, z1), (x0, y1, z1), (x0, y1, z0)]),
                    (not solid(i + 1, j, k), [(x1, y0, z0), (x1, y1, z0), (x1, y1, z1), (x1, y0, z1)]),
                    (not solid(i, j - 1, k), [(x0, y0, z0), (x1, y0, z0), (x1, y0, z1), (x0, y0, z1)]),
                    (not solid(i, j + 1, k), [(x0, y1, z0), (x0, y1, z1), (x1, y1, z1), (x1, y1, z0)]),
                    (not solid(i, j, k - 1), [(x0, y0, z0), (x0, y1, z0), (x1, y1, z0), (x1, y0, z0)]),
                    (not solid(i, j, k + 1), [(x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)]),
                ):
                    if open_side:
                        a, b, c, d = quad
                        faces += [(a, b, c), (a, c, d)]
    return faces


def _corner_shelf() -> list[_Tri]:
    """Two 4 mm walls meeting at a corner under a 30 mm square deck: the
    deck is held along two neighbouring sides and its far corner hangs
    26 mm from the nearer wall."""
    return _blocks([0, 4, 30], [0, 4, 30], [0, 20, 23], lambda i, j, k: k == 1 or i == 0 or j == 0)


def _stl(path: Path, faces: list[_Tri]) -> str:
    with open(path, "wb") as fh:
        fh.write(b"\0" * 80 + struct.pack("<I", len(faces)))
        for tri in faces:
            fh.write(struct.pack("<12fH", 0.0, 0.0, 0.0, *(c for v in tri for c in v), 0))
    return str(path)


@pytest.mark.parametrize("sticks_out", [5.0, 10.0, 30.0])
def test_a_shelf_is_said_by_how_far_it_sticks_out(tmp_path: Path, sticks_out: float) -> None:
    report = analyze_printability(_stl(tmp_path / "shelf.stl", _shelf(sticks_out)), material="pla")
    verdict = support_verdict(report)
    assert verdict is not None and verdict.endswith(f", sticks out {sticks_out:.1f}mm"), verdict
    assert "bridges=" not in verdict
    assert report.bridging.sticks_out_mm == sticks_out
    assert report.bridging.bridge_needing_supports_mm == 0.0
    advice = [line for line in report.recommendations if "sticks out" in line]
    assert advice == [
        f"Part of this model sticks out {sticks_out:.1f} mm with nothing under it.  "
        "Turn on supports, or add a 45-degree slope or a brace under it."
    ], report.recommendations
    assert not any("ridge" in line for line in report.recommendations), report.recommendations


@pytest.mark.parametrize("sticks_out", [5.0, 10.0, 30.0])
def test_a_shelf_still_needs_supports(tmp_path: Path, sticks_out: float) -> None:
    """The words moved; the answer did not."""
    report = analyze_printability(_stl(tmp_path / "shelf.stl", _shelf(sticks_out)), material="pla")
    assert report.overhangs.needs_supports and report.bridging.needs_supports_for_bridges


def test_no_bridge_limit_is_read_against_a_shelf(tmp_path: Path) -> None:
    """The span a material's bridge limit is compared with holds bridges
    only: a 30 mm shelf is not a 40 mm bridge that 30 mm would fix."""
    shelf = analyze_printability(_stl(tmp_path / "shelf.stl", _shelf(30.0)), material="pla")
    assert shelf.bridging.max_free_air_span_mm == 0.0
    bridge = analyze_printability(_stl(tmp_path / "bridge.stl", _bridge(30.0)), material="pla")
    assert bridge.bridging.max_free_air_span_mm == 30.0


def test_a_bridge_keeps_its_words(tmp_path: Path) -> None:
    report = analyze_printability(_stl(tmp_path / "bridge.stl", _bridge(30.0)), material="pla")
    assert support_verdict(report) == "bridges=30.0mm"
    assert report.bridging.sticks_out_mm == 0.0 and report.bridging.bridge_needing_supports_mm == 30.0
    assert "Long bridges detected (30.0 mm).  Enable supports or reduce bridge spans." in report.recommendations
    assert not any("sticks out" in line for line in report.recommendations)


def test_a_part_with_both_says_both_each_with_its_own_number(tmp_path: Path) -> None:
    """The 30 mm shelf's old figure (40.0) must not be printed as the bridge's."""
    faces = _bridge(35.0) + _shelf(30.0, x0=60.0)
    report = analyze_printability(_stl(tmp_path / "both.stl", faces), material="pla")
    verdict = support_verdict(report)
    assert verdict is not None and verdict.endswith("bridges=35.0mm, sticks out 30.0mm"), verdict
    assert "Long bridges detected (35.0 mm).  Enable supports or reduce bridge spans." in report.recommendations
    assert any("sticks out 30.0 mm" in line for line in report.recommendations)


def test_the_slice_says_it_the_same_way(tmp_path: Path) -> None:
    decision = decide_supports(_stl(tmp_path / "shelf.stl", _shelf(10.0)), material="pla")
    assert decision.enabled
    assert decision.note.startswith("supports on: overhangs=") and decision.note.endswith(", sticks out 10.0mm")
    assert "bridge" not in decision.note


def test_the_pre_print_check_names_no_bridge_under_a_shelf(tmp_path: Path) -> None:
    shelf = assess_support_feasibility(stl_path=_stl(tmp_path / "shelf.stl", _shelf(10.0)), material="pla")
    assert shelf.needs_supports
    assert not any("bridge" in line for line in shelf.recommendations), shelf.recommendations
    bridge = assess_support_feasibility(stl_path=_stl(tmp_path / "bridge.stl", _bridge(30.0)), material="pla")
    assert "Supports required under the bridge: bridges=30.0mm" in bridge.recommendations


def test_the_design_check_reads_no_bridge_limit_against_a_shelf(tmp_path: Path) -> None:
    """A 30 mm shelf was failed as "Longest bridge is 40.0 mm"; it is failed
    as the overhang it is, and a real bridge is still read against the limit."""
    from kiln.design_validator import validate_design

    def checks(name: str, faces: list[_Tri]) -> dict:
        report = validate_design(_stl(tmp_path / name, faces), "a wall shelf", material="pla")
        return {check.check_name: check for check in report.checks}

    shelf = checks("shelf.stl", _shelf(30.0))
    assert shelf["bridge_length"].passed and shelf["bridge_length"].actual_value == 0.0
    assert not shelf["overhang"].passed
    bridge = checks("bridge.stl", _bridge(30.0))
    assert not bridge["bridge_length"].passed and bridge["bridge_length"].actual_value == 30.0
    assert bridge["bridge_length"].fix_suggestion.startswith("Longest bridge is 30.0 mm")


def test_a_shelf_in_a_corner_is_said_by_its_far_corner(tmp_path: Path) -> None:
    """Held along two neighbouring walls, its far corner still hangs off
    one side: it was "bridges=34.7mm", read against the bridge limit."""
    report = analyze_printability(_stl(tmp_path / "corner.stl", _corner_shelf()), material="pla")
    verdict = support_verdict(report)
    assert verdict is not None and verdict.endswith(", sticks out 26.0mm"), verdict
    assert "bridges=" not in verdict
    assert report.overhangs.needs_supports and report.bridging.needs_supports_for_bridges
    assert report.bridging.max_free_air_span_mm == 0.0
    assert any("sticks out 26.0 mm with nothing under it" in line for line in report.recommendations)
    assert not any("ridge" in line for line in report.recommendations), report.recommendations


def test_a_bridge_meshed_along_its_edge_stays_a_bridge(tmp_path: Path) -> None:
    """A corner of the deck's free edge sits 15 mm from each wall, half the
    span: as far as a bridge's edge ever gets from a hold, and not a shelf."""
    faces = _blocks([0, 4, 19, 34, 38], [0, 16], [0, 20, 23], lambda i, j, k: k == 1 or i in (0, 3))
    report = analyze_printability(_stl(tmp_path / "bridge.stl", faces), material="pla")
    assert support_verdict(report) == "bridges=30.0mm"
    assert report.bridging.sticks_out_mm == 0.0 and report.bridging.max_free_air_span_mm == 30.0
