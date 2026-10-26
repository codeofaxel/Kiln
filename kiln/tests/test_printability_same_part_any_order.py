"""The same part gets the same printability answer, whatever order its file
lists its triangles in.

Two compiles of one tray, the same to the last vertex and written in a
different triangle order, came back "Ready to print" and "Won't print as it
is" (2026-10-23): the analysis samples and caps what it reads, and it read
the triangles in the order the file wrote them.  It now reads them in an
order the geometry fixes, and so does the hole check.
"""

from __future__ import annotations

import json
import struct
from pathlib import Path

import numpy as np
import pytest

trimesh = pytest.importorskip("trimesh")
pytest.importorskip("manifold3d")  # trimesh's boolean backend builds the part

from kiln.generation.validation import detect_holes  # noqa: E402
from kiln.printability import analyze_printability, verdict  # noqa: E402

_RECORD = np.dtype([("normal", "<3f4"), ("corners", "<9f4"), ("attr", "<u2")])


def _part(path: Path) -> Path:
    """A plate with two holes, a small thin fin and a finely faceted dome: more
    triangles than the wall check casts from, two holes to list, and more
    large flat faces than the warping check keeps."""
    plate = trimesh.creation.box(extents=(120.0, 120.0, 3.0))
    plate.apply_translation((0, 0, 1.5))
    fin = trimesh.creation.box(extents=(0.3, 2.0, 2.0))
    fin.apply_translation((30.0, 30.0, 3.95))
    part = trimesh.boolean.union([plate, fin])
    for x, radius in ((-30.0, 3.0), (-10.0, 2.0)):
        hole = trimesh.creation.cylinder(radius=radius, height=10.0, sections=64)
        hole.apply_translation((x, -30.0, 1.5))
        part = part.difference(hole)
    dome = trimesh.creation.icosphere(subdivisions=4, radius=10.0)
    dome.apply_translation((20.0, -25.0, 1.0))
    part = part.union(dome)
    part.export(path)
    return path


def _corners(path: Path) -> np.ndarray:
    data = path.read_bytes()
    (count,) = struct.unpack_from("<I", data, 80)
    records = np.frombuffer(data, dtype=_RECORD, count=count, offset=84)
    return records["corners"].reshape(count, 3, 3).copy()


def _reordered(path: Path, seed: int) -> Path:
    """The same triangles, the same float32 corners, in another order, each
    starting at another corner (its winding kept)."""
    rng = np.random.default_rng(seed)
    tris = _corners(path)
    tris = tris[rng.permutation(len(tris))]
    turn = rng.integers(0, 3, size=len(tris))
    tris = np.stack([np.roll(tri, -k, axis=0) for tri, k in zip(tris, turn, strict=True)])
    records = np.zeros(len(tris), dtype=_RECORD)
    records["corners"] = tris.reshape(len(tris), 9)
    out = path.with_name(f"{path.stem}_order{seed}.stl")
    out.write_bytes(b"\0" * 80 + struct.pack("<I", len(tris)) + records.tobytes())
    return out


@pytest.fixture(scope="module")
def part(tmp_path_factory) -> Path:
    return _part(tmp_path_factory.mktemp("order") / "part.stl")


def _said(report) -> str:
    return json.dumps(report.to_dict(), sort_keys=True, default=str)


class TestTheAnswerIsTheParts:
    def test_every_order_gets_the_same_report(self, part):
        first = analyze_printability(str(part))
        for seed in (1, 2):
            other = analyze_printability(str(_reordered(part, seed)))
            assert verdict(other) == verdict(first)
            assert _said(other) == _said(first)

    def test_the_hole_check_lists_the_same_holes_in_the_same_order(self, part):
        first = detect_holes(str(part))
        assert len(first) == 2
        for seed in (1, 2):
            assert detect_holes(str(_reordered(part, seed))) == first


class TestTheGeometricOrder:
    def test_any_order_and_any_first_corner_give_one_list(self, part):
        from kiln.generation.validation import in_geometric_order

        tris = [tuple(map(tuple, t)) for t in _corners(part).astype(np.float64).tolist()]
        rng = np.random.default_rng(3)
        shuffled = [tris[i] for i in rng.permutation(len(tris))]
        turned = [t[1:] + t[:1] for t in shuffled]
        assert in_geometric_order(turned) == in_geometric_order(tris)

    def test_the_winding_is_kept(self):
        from kiln.generation.validation import in_geometric_order

        up = ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0))
        down = (up[0], up[2], up[1])
        for tri in (up, down):
            (out,) = in_geometric_order([tri, tri])[:1]
            normal = np.cross(np.subtract(out[1], out[0]), np.subtract(out[2], out[0]))
            want = np.cross(np.subtract(tri[1], tri[0]), np.subtract(tri[2], tri[0]))
            assert np.dot(normal, want) > 0
