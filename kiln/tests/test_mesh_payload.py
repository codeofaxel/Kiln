"""``kiln.mesh.v1`` — the wire format the 3D stage reads.

Decodes the base64 typed arrays back and compares against the source
geometry, because a payload that merely LOOKS right and a payload that IS
the mesh are indistinguishable until something tries to draw it.  The
size-cap tests pin the downgrade branch by monkeypatching the decimation
probe to ``None``, so they cannot flip if ``fast_simplification`` lands in
the environment.
"""

from __future__ import annotations

import base64

import numpy as np
import pytest
import trimesh

from kiln import mesh_payload
from kiln.mesh_payload import VIEWER_PAYLOAD_KIND, mesh_to_viewer_payload


def _f32(b64: str) -> np.ndarray:
    return np.frombuffer(base64.b64decode(b64), dtype="<f4").reshape(-1, 3)


def _u32(b64: str) -> np.ndarray:
    return np.frombuffer(base64.b64decode(b64), dtype="<u4").reshape(-1, 3)


def _box(tmp_path, name="part.stl"):
    mesh = trimesh.creation.box(extents=(40.0, 30.0, 12.0))
    p = tmp_path / name
    mesh.export(str(p))
    return p, trimesh.load(str(p), force="mesh")


class TestTheGeometrySurvives:
    def test_counts_and_indices_round_trip(self, tmp_path):
        p, src = _box(tmp_path)
        payload = mesh_to_viewer_payload(p)
        assert payload["kind"] == VIEWER_PAYLOAD_KIND
        assert payload["downgraded"] is False
        assert payload["counts"]["triangles"] == len(src.faces)
        assert len(_u32(payload["indices"])) == len(src.faces)
        assert len(_f32(payload["positions"])) == len(src.vertices)

    def test_positions_arrive_in_viewer_space(self, tmp_path):
        """(x, y, z)_mesh -> (x, z, -y)_viewer, baked here so the viewer
        applies no transform of its own."""
        p, src = _box(tmp_path)
        got = _f32(mesh_to_viewer_payload(p)["positions"])
        want = np.column_stack(
            [src.vertices[:, 0], src.vertices[:, 2], -src.vertices[:, 1]]
        ).astype(np.float32)
        assert np.allclose(got, want, atol=1e-4)

    def test_bbox_stays_in_mesh_space(self, tmp_path):
        """It is the display truth for "40 x 30 x 12 mm" — rotating it would
        report a part's height as its depth."""
        p, src = _box(tmp_path)
        assert np.allclose(mesh_to_viewer_payload(p)["bbox"]["size"], src.extents, atol=1e-3)

    def test_normals_are_omitted_unless_asked_for(self, tmp_path):
        p, _ = _box(tmp_path)
        assert "normals" not in mesh_to_viewer_payload(p)
        assert "normals" in mesh_to_viewer_payload(p, include_normals=True)

    def test_vertex_colors_ride_along_when_the_mesh_has_them(self, tmp_path):
        mesh = trimesh.creation.box(extents=(10.0, 10.0, 10.0))
        colors = np.tile(np.array([[10, 200, 30, 255]], dtype=np.uint8), (len(mesh.vertices), 1))
        mesh.visual = trimesh.visual.ColorVisuals(mesh=mesh, vertex_colors=colors)
        p = tmp_path / "colored.ply"
        mesh.export(str(p))
        payload = mesh_to_viewer_payload(p)
        rgba = np.frombuffer(base64.b64decode(payload["vertex_colors"]), dtype=np.uint8)
        assert rgba.reshape(-1, 4)[0].tolist() == [10, 200, 30, 255]

    def test_source_carries_the_basename_only(self, tmp_path):
        """It travels into a conversation; a user's disk layout must not."""
        p, _ = _box(tmp_path, name="secret_project.stl")
        source = mesh_to_viewer_payload(p)["source"]
        assert source == {"filename": "secret_project.stl", "format": "stl"}


class TestHonestAboutSize:
    @pytest.fixture(autouse=True)
    def _no_backend(self, monkeypatch):
        monkeypatch.setattr(mesh_payload, "_decimation_backend", lambda: None)

    def _sphere(self, tmp_path):
        mesh = trimesh.creation.icosphere(subdivisions=3, radius=25.0)  # 1280 faces
        p = tmp_path / "sphere.stl"
        mesh.export(str(p))
        return p, trimesh.load(str(p), force="mesh")

    def test_too_many_triangles_downgrades_with_the_true_counts(self, tmp_path):
        p, src = self._sphere(tmp_path)
        payload = mesh_to_viewer_payload(p, max_triangles=100)
        assert payload["downgraded"] is True
        assert "no decimation backend" in payload["reason"]
        assert payload["counts"]["triangles"] == len(src.faces)
        assert np.allclose(payload["bbox"]["size"], src.extents, atol=1e-3)
        for forbidden in ("positions", "indices", "normals", "vertex_colors"):
            assert forbidden not in payload, (
                "a downgraded payload shipped geometry — the caller would "
                "render a silently mutilated mesh instead of the still image"
            )

    def test_too_many_bytes_downgrades_too(self, tmp_path):
        p, _ = self._sphere(tmp_path)
        payload = mesh_to_viewer_payload(p, max_bytes=1_000)
        assert payload["downgraded"] is True
        assert "positions" not in payload

    def test_inside_the_caps_nothing_is_touched(self, tmp_path):
        p, _ = self._sphere(tmp_path)
        payload = mesh_to_viewer_payload(p)
        assert payload["downgraded"] is False
        assert "decimated_from" not in payload


@pytest.mark.skipif(
    mesh_payload._decimation_backend() is None,
    reason="fast_simplification not installed — the downgrade branch covers this",
)
def test_a_decimated_payload_says_so(tmp_path):
    mesh = trimesh.creation.icosphere(subdivisions=5, radius=25.0)  # 20480 faces
    p = tmp_path / "dense.stl"
    mesh.export(str(p))
    payload = mesh_to_viewer_payload(p, max_triangles=4000)
    assert payload["downgraded"] is False
    assert payload["decimated_from"] == 20480, (
        "a reduced mesh must never be passed off as the original"
    )
    assert payload["counts"]["triangles"] <= 4000


class TestRefusesWhatItCannotEncode:
    def test_a_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            mesh_to_viewer_payload(tmp_path / "nope.stl")

    def test_a_file_with_no_triangles_raises(self, tmp_path):
        p = tmp_path / "empty.stl"
        p.write_text("solid empty\nendsolid empty\n")
        with pytest.raises(ValueError):
            mesh_to_viewer_payload(p)
