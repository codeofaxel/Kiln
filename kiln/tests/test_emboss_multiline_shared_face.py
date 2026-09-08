"""Regression: every line of a multi-line carve shares ONE reference face.

:func:`kiln.decoration_helpers.emboss_text_lines_on_face` applies its
lines by CHAINING — line 2's input mesh is line 1's output.  It resolved
the face once for the layout math but let each per-line
:func:`~kiln.decoration_helpers.emboss_text_on_face` call re-resolve its
own, from that chained mesh.  So line 2 was placed against a face
measured partly off line 1's glyphs.

The plane subgrouper keeps a carve's glyph tops in the same plane group
as the surface they sit on whenever the relief is under its 1.5mm gap
threshold, and the legibility floor puts a default emboss at 1.2mm —
under it, always.  So the merged group's extent spanned the surface AND
the glyph tops, and the placement anchor (``bbox_center``, the middle of
that extent) sat half a relief above the real surface.  Each line landed
further out than the last.

Measured 2026-09-08 on a 3mm pet tag, 1.2mm floor emboss: line 1 fused
to the tag at z 3.0..4.3; line 2 was placed at 3.65 and printed as a
separate solid floating 0.65mm above the face — glyphs in mid-air, with
nothing under them.  The preview render showed a normal-looking tag from
every stock angle.  Only the emitted STL's own numbers said otherwise.

The anchor change is what made it big enough to trip a bound.  The
drift itself is older and came from the chaining: on the tree before
that change the same tag put line 2 at 0.063mm off the surface — still
detached, still nine separate solids, just too small for anything to
notice.  So these tests pin the contract, not the magnitude.

These tests pin:

* the face a caller hands in is used VERBATIM — not re-resolved (fast);
* adding a second line does not raise the mesh higher than one line
  does, i.e. both lines stand on the same plane (slow, OpenSCAD);
* the carve stays ONE connected solid — no line floats free (slow,
  OpenSCAD).
"""
from __future__ import annotations

import struct

import pytest

from kiln.decoration_helpers import emboss_text_lines_on_face, emboss_text_on_face


# ---------------------------------------------------------------------------
# Mesh helpers (binary STL, no deps)
# ---------------------------------------------------------------------------


def _write_binary_stl(path, tris):
    with open(path, "wb") as f:
        f.write(b"\0" * 80)
        f.write(struct.pack("<I", len(tris)))
        for a, b, c in tris:
            f.write(struct.pack("<fff", 0.0, 0.0, 0.0))
            for v in (a, b, c):
                f.write(struct.pack("<fff", *v))
            f.write(struct.pack("<H", 0))


def _plate_tris(x, y, z):
    """Axis-aligned box from the origin — a decoratable canvas."""
    v = [
        (0, 0, 0), (x, 0, 0), (0, y, 0), (x, y, 0),
        (0, 0, z), (x, 0, z), (0, y, z), (x, y, z),
    ]
    quads = [
        [0, 2, 3, 1], [4, 5, 7, 6], [0, 1, 5, 4],
        [2, 6, 7, 3], [0, 4, 6, 2], [1, 3, 7, 5],
    ]
    tris = []
    for q in quads:
        a, b, c, d = (v[i] for i in q)
        tris += [(a, b, c), (a, c, d)]
    return tris


def _read_tris(path):
    """Triangles from an STL, binary or ASCII — OpenSCAD emits ASCII."""
    with open(path, "rb") as f:
        head = f.read(80)
        ascii_stl = head[:5] == b"solid" and b"facet" in f.read(2048)
    if ascii_stl:
        out, cur = [], []
        with open(path, encoding="ascii", errors="ignore") as f:
            for line in f:
                parts = line.split()
                if parts and parts[0] == "vertex":
                    cur.append(tuple(float(x) for x in parts[1:4]))
                    if len(cur) == 3:
                        out.append(tuple(cur))
                        cur = []
        return out
    with open(path, "rb") as f:
        f.seek(80)
        (count,) = struct.unpack("<I", f.read(4))
        out = []
        for _ in range(count):
            f.read(12)
            out.append(tuple(struct.unpack("<fff", f.read(12)) for _ in range(3)))
            f.read(2)
        return out


def _z_max(path):
    return max(v[2] for tri in _read_tris(path) for v in tri)


def _connected_solids(path):
    """Count disjoint solids by welding vertices and union-finding faces.

    A carve that fuses to the body is ONE solid; a line placed off the
    surface is a second one, hanging in space.  Deliberately not a mesh
    library: the count has to be readable here, on the bytes the tool
    actually emitted.
    """
    tris = _read_tris(path)
    parent: dict[int, int] = {}

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i, j):
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[ri] = rj

    # Weld at 1µm — OpenSCAD emits shared vertices for a CSG union, and
    # the rounding only absorbs float32 print/parse noise.
    vert_id: dict[tuple, int] = {}
    for t, tri in enumerate(tris):
        parent.setdefault(t, t)
        for v in tri:
            key = (round(v[0], 3), round(v[1], 3), round(v[2], 3))
            if key in vert_id:
                union(t, vert_id[key])
            else:
                vert_id[key] = t
    return len({find(t) for t in parent})


# ---------------------------------------------------------------------------
# Fast: a handed-in face is used verbatim
# ---------------------------------------------------------------------------


def test_passed_face_is_used_verbatim_not_re_resolved(tmp_path, monkeypatch):
    """``face=`` bypasses resolution — the chained caller's whole point.

    Without this, the multi-line helper cannot pin a reference plane: it
    can resolve the face all it likes, the per-line call would go and
    measure its own off the mesh the previous line just changed.
    """
    plate = tmp_path / "plate.stl"
    _write_binary_stl(plate, _plate_tris(40, 40, 3))

    import kiln.decoration_helpers as dh

    called: list[str] = []

    def _boom(*a, **k):  # pragma: no cover — must never run
        called.append("resolved")
        raise AssertionError("face was re-resolved despite being passed in")

    monkeypatch.setattr(
        "kiln.surface_intelligence.resolve_decoratable_face", _boom
    )

    captured: dict[str, object] = {}

    def _fake_scad(*, face, **kwargs):
        captured["face"] = face
        raise RuntimeError("stop after placement")

    monkeypatch.setattr("kiln.emboss_generator.generate_emboss_scad", _fake_scad)

    pinned = {
        "normal": (0.0, 0.0, 1.0),
        "center": (20.0, 20.0, 3.0),
        "bbox_center": (20.0, 20.0, 3.0),
        "width_mm": 40.0,
        "height_mm": 40.0,
        "face_name": "top",
        "area_mm2": 1600.0,
    }
    with pytest.raises(RuntimeError, match="stop after placement"):
        emboss_text_on_face(
            str(plate), "AA", face_name="top", face=pinned,
            output_dir=str(tmp_path),
        )

    assert not called, "resolve_decoratable_face ran even though face= was given"
    assert captured["face"] is pinned, (
        "the engine was handed a different face than the caller pinned"
    )
    assert dh  # module import is the surface under test


# ---------------------------------------------------------------------------
# Slow (OpenSCAD): the lines actually land on one plane
# ---------------------------------------------------------------------------

try:
    from kiln.emboss_generator import _find_openscad

    _find_openscad()
    _OPENSCAD = True
except Exception:  # pragma: no cover — OpenSCAD not installed
    _OPENSCAD = False


@pytest.fixture
def plate_stl(tmp_path):
    p = tmp_path / "canvas.stl"
    _write_binary_stl(p, _plate_tris(60, 60, 3))
    return str(p)


@pytest.mark.slow
@pytest.mark.skipif(not _OPENSCAD, reason="OpenSCAD not installed")
def test_second_line_does_not_stand_taller_than_the_first(plate_stl, tmp_path):
    """A second line adds text, not height.

    Both lines carve the same face at the same depth, so the mesh a
    two-line block produces can be no taller than the one-line block —
    the extra line is beside the first, not stacked on it.  Pre-fix the
    two-line mesh stood a further half-relief proud, because line 2 was
    anchored to a face that already included line 1's glyph tops.
    """
    one = emboss_text_lines_on_face(
        plate_stl, ["AAAA"], face_name="top", mode="emboss",
        output_dir=str(tmp_path / "one"),
    )
    two = emboss_text_lines_on_face(
        plate_stl, ["AAAA", "BBBB"], face_name="top", mode="emboss",
        output_dir=str(tmp_path / "two"),
    )
    z_one, z_two = _z_max(one), _z_max(two)
    assert z_two == pytest.approx(z_one, abs=0.01), (
        f"two-line carve tops out at {z_two:.3f}mm but one line reaches "
        f"{z_one:.3f}mm — line 2 is standing {z_two - z_one:.3f}mm higher "
        f"than line 1 on the same face"
    )


@pytest.mark.slow
@pytest.mark.skipif(not _OPENSCAD, reason="OpenSCAD not installed")
def test_every_line_stays_attached_to_the_body(plate_stl, tmp_path):
    """No line floats.

    The height check above says the lines agree with each other; this
    says they agree with the PART.  A line placed off the surface unions
    with nothing and ships as a separate solid — geometry the slicer
    would print in mid-air.

    It is the assertion that states the physical consequence, and it
    holds at any magnitude: the drift was already there before the
    placement anchor moved, just small (measured on the tree before
    that change: 0.047mm on this plate, 0.063mm on the pet tag) — nine
    disjoint solids either way.  A gap does not have to be big to be a
    gap.
    """
    three = emboss_text_lines_on_face(
        plate_stl, ["AAAA", "BBBB", "CCCC"], face_name="top", mode="emboss",
        output_dir=str(tmp_path / "three"),
    )
    solids = _connected_solids(three)
    assert solids == 1, (
        f"carve emitted {solids} disjoint solids — a line was placed off "
        f"the face and is floating above the part with nothing under it"
    )
