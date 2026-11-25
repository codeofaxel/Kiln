"""One reply gives one answer about supports.

A deck 21 mm across between two posts is anchored on both sides, yet longer
than a bridge a slicer spans: the reply asks for supports for it, so it
never also says the slicer will bridge it with no action needed.  An 8 mm
deck is bridged, and is said so.
"""

from __future__ import annotations

import pytest

trimesh = pytest.importorskip("trimesh")
pytest.importorskip("manifold3d")

from tests import _real_exports as rx  # noqa: E402

NO_ACTION = "no action"


def _box(size: tuple[float, float, float], at: tuple[float, float, float]):
    mesh = trimesh.creation.box(extents=size)
    mesh.apply_translation([a + s / 2.0 for a, s in zip(at, size, strict=True)])
    return mesh


def _deck_on_posts(tmp_path, span: float) -> str:
    """A 3 mm deck on two 15 mm posts, *span* mm between them."""
    parts = [_box((3, 12, 15), (0, 0, 0)), _box((3, 12, 15), (3 + span, 0, 0)), _box((6 + span, 12, 3), (0, 0, 15))]
    path = tmp_path / f"deck_{span:g}.stl"
    trimesh.boolean.union(parts, engine="manifold").export(path)
    return str(path)


def _call(name: str, **args) -> dict:
    from kiln import server

    with rx.without_kiln_pro():
        return rx.call(server.mcp, name, args)


def _said(reply: dict) -> list[str]:
    return [text for _where, text in rx._sentences(reply)]


def test_a_bridge_too_long_to_span_is_never_also_called_bridged(tmp_path):
    reply = _call("analyze_printability", file_path=_deck_on_posts(tmp_path, 21.0))
    report = reply["report"]
    # Control: the deck is two-sided and too long to bridge.
    assert report["bridging"]["needs_supports_for_bridges"] is True, report["bridging"]
    said = _said(reply)
    assert any("Long bridges detected (21.0 mm).  Enable supports" in text for text in said), said
    assert not any("will likely bridge" in text for text in said), said
    assert not any(NO_ACTION in text for text in said), said
    assert "needs supports (bridges=21.0mm)" in reply["message"], reply["message"]


def test_a_bridge_the_slicer_spans_is_said_so(tmp_path):
    reply = _call("analyze_printability", file_path=_deck_on_posts(tmp_path, 8.0))
    assert reply["report"]["bridging"]["needs_supports_for_bridges"] is False
    recommendations = reply["report"]["recommendations"]
    assert any(
        text.startswith("Slicer will likely bridge the") and NO_ACTION in text for text in recommendations
    ), recommendations
    assert not any("Long bridges detected" in text for text in recommendations), recommendations
