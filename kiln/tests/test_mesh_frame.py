"""A glTF model stands the way it was made, at every door that reads it.

glTF (``.glb`` / ``.gltf``) defines +Y as up and says the front of an asset
faces +Z (glTF 2.0, "Coordinate System and Units"); Kiln, 3MF and the
printer bed put up on Z.  Measured 2026-10-01 on a live Tripo job: a cube
asked for "with the letter K embossed on top" arrived with the K on a side
wall, because no door turned the file, and Kiln's own GLB reader also
dropped each part's placement, piling the parts of a model into one spot.

Pinned here:

* :func:`kiln.mesh_frame.load_mesh` turns glTF up to +Z and the front to
  -Y (a turn, never a mirror), keeps every part where the scene graph puts
  it, and reads every other format exactly as trimesh does;
* every door that opens a mesh file reads the same model the same way up;
* a generated GLB arrives through the download door, and onto the stage,
  standing up;
* no module opens a mesh with trimesh directly: it goes through
  ``load_mesh``, or says on the line why it must see the file as written.
"""

from __future__ import annotations

import json
import os
import struct
from pathlib import Path

import numpy as np
import pytest

from kiln import mesh_frame
from kiln.generation import validation
from kiln.generation.base import GenerationResult
from tests.test_arrival import _PROMPT, _call, _Cloud, _generation_door, _isolated  # noqa: F401 — autouse fixture

trimesh = pytest.importorskip("trimesh")

#: The block below, read in Kiln's frame: up is +Z, the front faces -Y.
_UPRIGHT = ((-5.0, -12.0, -15.0), (5.0, 10.0, 17.0))


def _gltf_asset(path: Path) -> str:
    """A block 30 tall in glTF's frame (up = +Y), a nub on its top face and a
    nub on its front face (+Z), each nub placed by its own scene node."""
    scene = trimesh.Scene()
    scene.add_geometry(trimesh.creation.box(extents=(10, 30, 20)), node_name="body")
    nub = trimesh.creation.box(extents=(2, 2, 2))
    scene.add_geometry(nub, node_name="top", transform=trimesh.transformations.translation_matrix((0, 16, 0)))
    scene.add_geometry(nub, node_name="front", transform=trimesh.transformations.translation_matrix((0, 0, 11)))
    path.write_bytes(scene.export(file_type="glb"))
    return str(path)


def _bounds(points) -> tuple[tuple[float, ...], tuple[float, ...]]:
    pts = np.asarray(points, dtype=float).reshape(-1, 3)
    return tuple(np.round(pts.min(axis=0), 3).tolist()), tuple(np.round(pts.max(axis=0), 3).tolist())


def _box_bounds(bb: dict) -> tuple[tuple[float, ...], tuple[float, ...]]:
    return (
        tuple(round(float(bb[f"{a}_min"]), 3) for a in "xyz"),
        tuple(round(float(bb[f"{a}_max"]), 3) for a in "xyz"),
    )


# ---------------------------------------------------------------------------
# The turn
# ---------------------------------------------------------------------------


class TestTheTurn:
    def test_up_becomes_z_and_the_front_faces_the_viewer(self, tmp_path):
        # Upside down would put the top nub at -Z; a front facing +Y would
        # put the front nub at the far side.  Only the right turn gives this.
        mesh = mesh_frame.load_mesh(_gltf_asset(tmp_path / "block.glb"), force="mesh")
        assert _bounds(mesh.vertices) == _UPRIGHT

    def test_without_force_it_is_still_a_scene_and_still_upright(self, tmp_path):
        scene = mesh_frame.load_mesh(_gltf_asset(tmp_path / "block.glb"))
        assert isinstance(scene, trimesh.Scene)
        assert _bounds(scene.bounds) == _UPRIGHT

    def test_it_is_a_turn_never_a_mirror(self, tmp_path):
        mesh = mesh_frame.load_mesh(_gltf_asset(tmp_path / "block.glb"), force="mesh")
        assert np.isclose(np.linalg.det(np.array(mesh_frame.GLTF_TO_KILN)[:3, :3]), 1.0)
        assert mesh.volume == pytest.approx(10 * 30 * 20 + 2 * 8)  # a mirror turns solids inside out

    def test_a_file_that_places_none_of_its_meshes_is_read_where_written(self, tmp_path):
        # Some minimal writers leave the node list out; trimesh alone reads
        # such a file as an empty model.
        verts = [(0, 0, 0), (10, 0, 0), (0, 30, 0), (0, 0, 20)]
        tris = [(0, 2, 1), (0, 1, 3), (0, 3, 2), (1, 2, 3)]
        path = tmp_path / "bare.glb"
        path.write_bytes(_nodeless_glb(verts, tris))
        mesh = mesh_frame.load_mesh(str(path), force="mesh")
        assert len(mesh.faces) == 4
        assert _bounds(mesh.vertices) == ((0.0, -20.0, 0.0), (10.0, 0.0, 30.0))

    def test_every_other_format_is_read_as_written(self, tmp_path):
        path = tmp_path / "block.stl"
        trimesh.creation.box(extents=(10, 30, 20)).export(str(path))
        ours = mesh_frame.load_mesh(str(path), force="mesh")
        assert _bounds(ours.vertices) == _bounds(trimesh.load(str(path), force="mesh").vertices)

    @pytest.mark.parametrize(
        ("source", "file_type", "expected"),
        [
            ("model.glb", None, True),
            ("MODEL.GLTF", None, True),
            ("model.stl", None, False),
            ("model.3mf", None, False),
            ("download.bin", "glb", True),
            ("model.glb", "stl", False),
        ],
    )
    def test_which_files_put_up_on_y(self, source, file_type, expected):
        assert mesh_frame.is_y_up(source, file_type) is expected

    def test_a_file_object_with_no_type_is_not_one(self):
        import io

        assert mesh_frame.is_y_up(io.BytesIO(b"glTF")) is False


def _nodeless_glb(verts, tris) -> bytes:
    """A GLB holding one mesh and no scene nodes at all."""
    positions = b"".join(struct.pack("<3f", *v) for v in verts)
    indices = b"".join(struct.pack("<3H", *t) for t in tris)
    blob = positions + indices + b"\x00" * (-len(positions + indices) % 4)
    doc = {
        "asset": {"version": "2.0"},
        "meshes": [{"primitives": [{"attributes": {"POSITION": 0}, "indices": 1}]}],
        "accessors": [
            {"bufferView": 0, "componentType": 5126, "count": len(verts), "type": "VEC3",
             "min": [min(c) for c in zip(*verts, strict=True)], "max": [max(c) for c in zip(*verts, strict=True)]},
            {"bufferView": 1, "componentType": 5123, "count": 3 * len(tris), "type": "SCALAR"},
        ],
        "bufferViews": [
            {"buffer": 0, "byteOffset": 0, "byteLength": len(positions)},
            {"buffer": 0, "byteOffset": len(positions), "byteLength": len(indices)},
        ],
        "buffers": [{"byteLength": len(blob)}],
    }
    text = json.dumps(doc).encode()
    text += b" " * (-len(text) % 4)
    chunks = struct.pack("<I4s", len(text), b"JSON") + text + struct.pack("<I4s", len(blob), b"BIN\x00") + blob
    return struct.pack("<4sII", b"glTF", 2, 12 + len(chunks)) + chunks


# ---------------------------------------------------------------------------
# Every door reads the same model the same way up
# ---------------------------------------------------------------------------


def _stage(path):
    from kiln.mesh_payload import mesh_to_viewer_payload

    bbox = mesh_to_viewer_payload(path)["bbox"]
    return _bounds([bbox["min"], bbox["max"]])


def _converted(path):
    stl = validation._convert_glb_to_stl(Path(path), str(Path(path).with_suffix(".converted.stl")))
    return _bounds(validation.read_stl_triangles(stl))


def _bed_fit(path):
    from kiln.printers.bed_fit import compute_mesh_bbox

    return _box_bounds(compute_mesh_bbox(path))


def _diagnostics(path):
    from kiln.mesh_diagnostics import _load_mesh

    return _bounds(_load_mesh(path).vertices)


def _surfaces(path):
    from kiln.surface_intelligence import _parse_mesh

    return _bounds([t["vertices"] for t in _parse_mesh(path)])


def _decoration_faces(path):
    from kiln.decoration_faces import load_mesh_triangles

    return _bounds(load_mesh_triangles(path))


def _slicer_geometry(path):
    from kiln.slicer_geometry import mesh_geometry

    lo, hi, _, _ = mesh_geometry(path)
    return _bounds([lo, hi])


def _multicolor(path):
    from kiln.multicolor_3mf import _parse_mesh_file

    return _bounds(_parse_mesh_file(path)[0])


_DOORS = {
    "the shared reader": lambda p: _bounds(mesh_frame.load_mesh(p, force="mesh").vertices),
    "Kiln's own GLB reader": lambda p: _bounds(validation._parse_glb(Path(p), [])[1]),
    "the GLB-to-STL conversion": _converted,
    "the mesh check": lambda p: _box_bounds(validation.validate_mesh(p).bounding_box),
    "the 3D stage": _stage,
    "the bed-fit check": _bed_fit,
    "mesh diagnostics": _diagnostics,
    "the surface reader": _surfaces,
    "decoration faces": _decoration_faces,
    "the slicer's geometry": _slicer_geometry,
    "the multicolor builder": _multicolor,
}


@pytest.mark.parametrize("door", sorted(_DOORS))
def test_every_door_reads_a_glb_standing_up(door, tmp_path):
    assert _DOORS[door](_gltf_asset(tmp_path / "block.glb")) == _UPRIGHT


# ---------------------------------------------------------------------------
# A generated GLB arrives standing up
# ---------------------------------------------------------------------------


class _GlbCloud(_Cloud):
    """A generator that hands back a GLB drawn in millimetres."""

    sets_real_size = True

    def download_result(self, job_id, output_dir=""):
        self._out.mkdir(parents=True, exist_ok=True)
        path = _gltf_asset(self._out / f"{job_id}.glb")
        return GenerationResult(job_id, self.name, path, "glb", os.path.getsize(path), _PROMPT)


def test_a_generated_glb_arrives_on_the_stage_standing_up(tmp_path, monkeypatch):
    monkeypatch.setattr("kiln.server._get_generation_provider", lambda name: _GlbCloud(tmp_path / "gen"))
    sc = _call(_generation_door(), "download_generated_model", job_id="job-glb", provider="cloudgen")

    assert sc["success"] is True
    assert sc["dimensions"]["summary"] == "10.0 x 22.0 x 32.0 mm"  # the 32 is the height
    assert sc["stage_mesh_path"].endswith(".stl")
    assert _bounds(validation.read_stl_triangles(sc["stage_mesh_path"])) == _UPRIGHT


# ---------------------------------------------------------------------------
# Nothing opens a mesh around the door
# ---------------------------------------------------------------------------

_SRC = Path(mesh_frame.__file__).parent


def test_no_module_opens_a_mesh_around_the_door():
    unrouted = {
        str(path.relative_to(_SRC)): lines
        for path in sorted(_SRC.rglob("*.py"))
        if path.name != "mesh_frame.py" and (lines := mesh_frame.unrouted_loads(path.read_text()))
    }
    assert unrouted == {}, (
        "These open a mesh with trimesh directly, so a glTF file reaches them lying on its "
        "side.  Use kiln.mesh_frame.load_mesh, or say why the line must see the file as "
        f"written with a '# raw frame: <why>' comment: {unrouted}"
    )


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("import trimesh\nm = trimesh.load(p)\n", [2]),
        ("import trimesh as tm\nm = tm.load_mesh(p)\n", [2]),
        ("import trimesh\ns = trimesh.exchange.load.load(p)\n", [2]),
        ("from trimesh import load_scene\n", [1]),
        ("import trimesh\nm = trimesh.load(p)  # raw frame: hashing the bytes\n", []),
        ('"""Never call trimesh.load(p) here."""\n', []),
        ("from kiln.mesh_frame import load_mesh\nm = load_mesh(p)\n", []),
        ("import trimesh\npath = trimesh.load_path(segments)\n", []),
    ],
)
def test_the_check_sees_each_way_of_opening_a_mesh(source, expected):
    assert mesh_frame.unrouted_loads(source) == expected
