"""Overhang faces on features that print on their own are said as that plain fact.

A hole's roof prints without supports.  Every door that says so says what
the slicer does, and no sentence in its reply speaks of a score.
"""

from __future__ import annotations

import numpy as np
import pytest

trimesh = pytest.importorskip("trimesh")
pytest.importorskip("manifold3d")

from tests import _real_exports as rx  # noqa: E402


def _holed_block(tmp_path) -> str:
    """A 20 mm block with a 4 mm hole through it, side to side."""
    block = trimesh.creation.box(extents=(20, 20, 20))
    block.apply_translation((10, 10, 10))
    hole = trimesh.creation.cylinder(radius=2.0, height=30, sections=48)
    hole.apply_transform(trimesh.transformations.rotation_matrix(np.pi / 2, (1, 0, 0)))
    hole.apply_translation((10, 10, 10))
    path = tmp_path / "holed.stl"
    trimesh.boolean.difference([block, hole], engine="manifold").export(path)
    return str(path)


def _call(name: str, **args) -> dict:
    from kiln import server

    with rx.without_kiln_pro():
        return rx.call(server.mcp, name, args)


def _said(reply: dict) -> list[str]:
    return [text for _where, text in rx._sentences(reply)]


@pytest.mark.parametrize("door", ["analyze_printability", "validate_and_prepare_mesh"])
def test_faces_that_print_on_their_own_are_said_without_a_score(tmp_path, door):
    reply = _call(door, file_path=_holed_block(tmp_path))
    said = _said(reply)
    assert any(
        "overhang face(s) sit on features that print on their own" in text
        and text.endswith("the slicer prints these without supports.")
        for text in said
    ) or door != "analyze_printability", said
    assert not any("score" in text.lower() for text in said), [text for text in said if "score" in text.lower()]
