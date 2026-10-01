"""Two CAD revisions compare as they are.

2026-10-01: ``compare_mesh_versions`` handed two ``.step`` paths answered
"Unsupported format: .step" — the door accepted the path and the parser
several layers down had never heard of it.  The comparison now converts
through the shared STEP door first.
"""

from __future__ import annotations

import struct

from kiln import step_import
from kiln.generation.validation import compare_meshes


def _box_stl(path, x: float, y: float, z: float) -> str:
    """A closed binary-STL box, corner at the origin."""
    v = [(0, 0, 0), (x, 0, 0), (x, y, 0), (0, y, 0), (0, 0, z), (x, 0, z), (x, y, z), (0, y, z)]
    faces = [
        (0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
        (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7),
    ]
    data = bytearray(b"\x00" * 80) + struct.pack("<I", len(faces))
    for a, b, c in faces:
        data += struct.pack("<12fH", 0, 0, 0, *v[a], *v[b], *v[c], 0)
    path.write_bytes(bytes(data))
    return str(path)


def test_two_step_revisions_are_converted_then_compared(tmp_path, monkeypatch):
    rev_a, rev_b = tmp_path / "rev_a.step", tmp_path / "rev_b.STEP"
    rev_a.write_text("ISO-10303-21; a")
    rev_b.write_text("ISO-10303-21; b")
    made = {
        str(rev_a): _box_stl(tmp_path / "a.stl", 10, 10, 10),
        str(rev_b): _box_stl(tmp_path / "b.stl", 10, 10, 15),
    }
    monkeypatch.setattr(
        step_import, "ensure_mesh_path", lambda path, **kw: (made.get(path, path), None)
    )
    got = compare_meshes(str(rev_a), str(rev_b))
    assert got["meshes_identical"] is False
    assert got["volume_change_pct"] > 40


def test_a_mesh_still_passes_straight_through(tmp_path):
    a = _box_stl(tmp_path / "a.stl", 10, 10, 10)
    assert compare_meshes(a, a)["meshes_identical"] is True
