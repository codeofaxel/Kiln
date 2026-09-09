"""Regression: a carve lands on the SURFACE, never on a previous carve.

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

Two doors reach this, and they need separate fixes.  Inside ONE
multi-line call the helper can pin its own face, which is what it now
does.  But a SECOND call — a caller decorating a design that already
carries a carve on that face, which is what ``decorate_surface`` does
when a user says "now add my name too" — resolves the face fresh and
cannot be told about the first.  For that one the face resolution
itself has to be right, so the anchor along the face normal is now the
plane the material is actually on rather than the middle of the
group's thickness (see ``_dominant_plane_offset``).

An emboss has no margin to absorb the error: it is placed with
``z_offset = 0.0``, so its base sits EXACTLY on the resolved plane with
zero penetration into the body.  A deboss gets ``-depth_mm`` of bite; a
raised carve gets nothing.  Contact is a knife edge, and any outward
error in the plane is a gap.

These tests pin:

* the face a caller hands in is used VERBATIM — not re-resolved (fast);
* the resolved plane of an already-carved face is the surface, not the
  midpoint between surface and glyph tops (fast);
* adding a second line does not raise the mesh higher than one line
  does, i.e. both lines stand on the same plane (slow, OpenSCAD);
* a SECOND, independent carve on that face lands on the surface too
  (slow, OpenSCAD);
* the carve stays ONE connected solid — nothing floats free (slow,
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


def _box_tris(lo, hi):
    """Triangles of an axis-aligned box between two corners."""
    (x0, y0, z0), (x1, y1, z1) = lo, hi
    v = [
        (x0, y0, z0), (x1, y0, z0), (x0, y1, z0), (x1, y1, z0),
        (x0, y0, z1), (x1, y0, z1), (x0, y1, z1), (x1, y1, z1),
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



def test_resolved_plane_of_an_already_carved_face_is_the_surface(tmp_path):
    """The plane a face reports is where its material is.

    A plate carrying a raised pad has both surfaces facing +Z, and the
    subgrouper keeps them in one group while the relief is under its
    gap threshold.  The group is then 1.2mm thick, and its midpoint is
    0.6mm above the plate — in the air, where an emboss placed there
    cannot touch anything.  The plate is 96% of the area; that is the
    face.
    """
    from kiln.surface_intelligence import find_named_face

    mesh = tmp_path / "carved.stl"
    # 60x60x3 plate, plus a 12x12 pad standing 1.2mm proud of its top.
    _write_binary_stl(
        mesh,
        _plate_tris(60, 60, 3) + _box_tris((24, 24, 3.0), (36, 36, 4.2)),
    )

    face = find_named_face(str(mesh), "top")
    assert face["plane_min"] == pytest.approx(3.0, abs=1e-3)
    assert face["plane_max"] == pytest.approx(4.2, abs=1e-3)
    # The midpoint of that band is 3.6 and is where placement used to go.
    assert face["bbox_center"][2] == pytest.approx(3.0, abs=0.01), (
        f"top face reports its plane at z={face['bbox_center'][2]:.3f}; the "
        f"plate surface is at 3.0 and the pad tops at 4.2, so anything but "
        f"3.0 places content off the material"
    )

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


@pytest.mark.slow
@pytest.mark.skipif(not _OPENSCAD, reason="OpenSCAD not installed")
def test_a_second_independent_carve_lands_on_the_surface(plate_stl, tmp_path):
    """"Now add my name too" — a fresh call onto an already-carved face.

    The multi-line helper cannot help here: this is two separate calls,
    and the second one has no way to be told what the face looked like
    before the first.  It has to resolve a correct plane on its own.
    Pre-fix the second carve was anchored half a relief up and shipped
    as loose solids floating over the part.
    """
    first = emboss_text_on_face(
        plate_stl, "AAAA", face_name="top", mode="emboss",
        offset_y_mm=12.0, scale=0.4, output_dir=str(tmp_path / "first"),
    )
    second = emboss_text_on_face(
        first, "BBBB", face_name="top", mode="emboss",
        offset_y_mm=-12.0, scale=0.4, output_dir=str(tmp_path / "second"),
    )
    assert _z_max(second) == pytest.approx(_z_max(first), abs=0.01), (
        f"second carve tops out at {_z_max(second):.3f}mm against the "
        f"first's {_z_max(first):.3f}mm — it was placed on the first "
        f"carve's glyphs instead of on the part"
    )
    solids = _connected_solids(second)
    assert solids == 1, (
        f"second carve emitted {solids} disjoint solids — it is floating "
        f"above the part with nothing under it"
    )
