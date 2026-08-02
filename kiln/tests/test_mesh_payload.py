"""``kiln.mesh.v1`` — the wire format the 3D stage reads.

Decodes the base64 typed arrays back and compares against the source
geometry, because a payload that merely LOOKS right and a payload that IS
the mesh are indistinguishable until something tries to draw it.  The
size-cap tests pin the downgrade branch by monkeypatching the decimation
probe to ``None``, so they cannot flip if ``fast_simplification`` lands in
the environment.

Building the fixtures needs trimesh, which ships in the ``mesh-diagnostics``
extra rather than the base install, so the module skips without it — the
same guard ``test_mesh_diagnostics`` and ``test_print_gate_enrichment``
already use.  The contract that holds with trimesh ABSENT is pinned
separately, in ``test_mesh_payload_without_trimesh``, which is where it can
still run.
"""

from __future__ import annotations

import base64

import numpy as np
import pytest

trimesh = pytest.importorskip(
    "trimesh", reason="trimesh (mesh-diagnostics extra) required to build fixture meshes"
)

from kiln import mesh_payload  # noqa: E402
from kiln.mesh_payload import (  # noqa: E402
    VIEWER_PAYLOAD_KIND,
    mesh_to_viewer_payload,
)


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


class TestScenePartColorsSurvive:
    """A multicolor 3MF loads as a multi-part Scene, and each part's color
    must arrive in the payload as per-vertex RGBA.  This is the promotion
    condition for the color tools on the stage roster: a recolor rendered
    gray reads as the tool failing.  The fixture is the real artifact —
    ``compose_multicolor_3mf`` output, whose colors live only in the slicer
    sidecar that trimesh never reads."""

    RED = [247, 35, 35, 255]     # #f72323
    BLUE = [35, 102, 247, 255]   # #2366f7

    def _multicolor_3mf(self, tmp_path, *, dense=False, colored=True):
        from kiln.multicolor_3mf import ColorPart, compose_multicolor_3mf

        def make():
            if dense:
                return trimesh.creation.icosphere(subdivisions=3, radius=10.0)
            return trimesh.creation.box(extents=(10.0, 10.0, 10.0))

        lo, hi = make(), make()
        hi.apply_translation((0.0, 0.0, 30.0))
        lo_p, hi_p = tmp_path / "lo.stl", tmp_path / "hi.stl"
        lo.export(str(lo_p))
        hi.export(str(hi_p))
        out = tmp_path / "multi.3mf"
        compose_multicolor_3mf(
            [
                ColorPart(stl_path=str(lo_p), extruder=1, name="zone_lo",
                          color="#f72323" if colored else None),
                ColorPart(stl_path=str(hi_p), extruder=2, name="zone_hi",
                          color="#2366f7" if colored else None),
            ],
            output_path=str(out),
        )
        return out

    @staticmethod
    def _rgba(payload):
        return np.frombuffer(
            base64.b64decode(payload["vertex_colors"]), dtype=np.uint8
        ).reshape(-1, 4)

    def test_each_parts_color_reaches_its_own_vertices(self, tmp_path):
        payload = mesh_to_viewer_payload(self._multicolor_3mf(tmp_path))
        assert payload["downgraded"] is False
        rgba = self._rgba(payload)
        pos = _f32(payload["positions"])
        assert len(rgba) == payload["counts"]["vertices"]
        # Distinct zone colors survive — the gray-out this class exists for
        # shipped exactly zero of them.
        assert {tuple(c) for c in rgba.tolist()} == {tuple(self.RED), tuple(self.BLUE)}
        # And each lands on ITS part: viewer-space y is mesh-space z, the
        # zones sit at z ∈ [-5, 5] and z ∈ [25, 35].
        assert (rgba[pos[:, 1] < 15.0] == self.RED).all()
        assert (rgba[pos[:, 1] > 15.0] == self.BLUE).all()

    def test_a_colorless_multipart_scene_ships_no_color_buffer(self, tmp_path):
        """No part claims a color → no buffer, same as today — the viewer's
        own neutral default applies, never a fabricated per-vertex gray."""
        payload = mesh_to_viewer_payload(
            self._multicolor_3mf(tmp_path, colored=False)
        )
        assert payload["downgraded"] is False
        assert "vertex_colors" not in payload

    @pytest.mark.skipif(
        mesh_payload._decimation_backend() is None,
        reason="fast_simplification not installed — the downgrade branch covers this",
    )
    def test_zone_colors_survive_decimation(self, tmp_path):
        """Decimation rebuilds the vertex set; the nearest-original-vertex
        transfer must carry the zones across instead of dropping them."""
        payload = mesh_to_viewer_payload(
            self._multicolor_3mf(tmp_path, dense=True), max_triangles=1000
        )
        assert payload["downgraded"] is False
        assert payload["decimated_from"] == 2560
        rgba = self._rgba(payload)
        pos = _f32(payload["positions"])
        assert {tuple(c) for c in rgba.tolist()} == {tuple(self.RED), tuple(self.BLUE)}
        assert (rgba[pos[:, 1] < 15.0] == self.RED).all()
        assert (rgba[pos[:, 1] > 15.0] == self.BLUE).all()

    def test_an_oversized_multicolor_mesh_still_downgrades_honestly(
        self, tmp_path, monkeypatch
    ):
        """No backend → the downgrade card, never a color-stripped mesh."""
        monkeypatch.setattr(mesh_payload, "_decimation_backend", lambda: None)
        payload = mesh_to_viewer_payload(
            self._multicolor_3mf(tmp_path, dense=True), max_triangles=1000
        )
        assert payload["downgraded"] is True
        assert "vertex_colors" not in payload and "positions" not in payload


class TestPartColorClaims:
    """_part_rgba judges what a Scene part actually CLAIMS — a stated
    material color counts; a texture's tint factor does not."""

    def test_a_stated_material_color_counts(self):
        mesh = trimesh.creation.box()
        mesh.visual = trimesh.visual.TextureVisuals(
            material=trimesh.visual.material.SimpleMaterial(diffuse=[10, 200, 30, 255])
        )
        rgba, explicit = mesh_payload._part_rgba(mesh, None)
        assert explicit is True
        assert rgba[0].tolist() == [10, 200, 30, 255]

    def test_a_textured_part_claims_no_color(self):
        """An image-textured material's main_color is the tint FACTOR
        (usually pure white) — painting the part with it would show a solid
        color the file never stated."""
        Image = pytest.importorskip("PIL.Image", reason="texture fixture needs PIL")
        mesh = trimesh.creation.box()
        mesh.visual = trimesh.visual.TextureVisuals(
            material=trimesh.visual.material.PBRMaterial(
                baseColorFactor=[255, 255, 255, 255],
                baseColorTexture=Image.new("RGB", (4, 4)),
            )
        )
        rgba, explicit = mesh_payload._part_rgba(mesh, None)
        assert explicit is False
        assert rgba[0].tolist() == [170, 170, 170, 255]

    def test_the_default_material_is_not_a_claim(self):
        mesh = trimesh.creation.box()
        mesh.visual = trimesh.visual.TextureVisuals(
            material=trimesh.visual.material.SimpleMaterial()
        )
        _, explicit = mesh_payload._part_rgba(mesh, None)
        assert explicit is False


class TestPaintedFileColorsSurvive:
    """A painted 3MF — one object, per-triangle colors — has no honest
    per-part color, so the encoder rebuilds it as a per-face-colored soup
    instead of showing gray."""

    RED, BLUE = (247, 35, 35, 255), (35, 102, 247, 255)

    def _painted_cube(self, tmp_path, transform=None):
        from kiln.multicolor_3mf import compose_painted_3mf

        mesh = trimesh.creation.box(extents=(10.0, 10.0, 10.0))
        tris = [tuple(map(tuple, mesh.vertices[f])) for f in mesh.faces]
        # top faces red (both verts at z=+5), everything else blue
        colors = [
            "#F72323" if all(v[2] > 4.9 for v in t) else "#2366F7" for t in tris
        ]
        out = tmp_path / "painted.3mf"
        compose_painted_3mf(tris, colors, output_path=str(out))
        if transform is not None:
            import zipfile as z

            moved = tmp_path / "moved.3mf"
            with z.ZipFile(out) as src, z.ZipFile(moved, "w") as dst:
                for n in src.namelist():
                    data = src.read(n)
                    if n == "3D/3dmodel.model":
                        data = data.replace(
                            b'transform="1 0 0 0 1 0 0 0 1 0.000000 0.000000 0.000000"',
                            transform.encode(),
                        )
                    dst.writestr(n, data)
            return moved
        return out

    def test_per_triangle_colors_reach_the_payload(self, tmp_path):
        payload = mesh_to_viewer_payload(self._painted_cube(tmp_path))
        assert payload["downgraded"] is False
        # soup: three unshared vertices per face, so paint cannot bleed
        assert payload["counts"]["vertices"] == payload["counts"]["triangles"] * 3
        rgba = np.frombuffer(
            base64.b64decode(payload["vertex_colors"]), dtype=np.uint8
        ).reshape(-1, 3, 4)
        pos = _f32(payload["positions"]).reshape(-1, 3, 3)
        top = (pos[:, :, 1] > 4.9).all(axis=1)   # viewer y == mesh z
        assert top.sum() == 2
        assert {tuple(c) for f in rgba[top] for c in f.tolist()} == {self.RED}
        assert {tuple(c) for f in rgba[~top] for c in f.tolist()} == {self.BLUE}

    def test_a_transformed_item_is_refused_not_mispositioned(self, tmp_path):
        """parse_colored_3mf ignores build transforms, so a moved item's
        soup would sit at the wrong place — the guard keeps the honest
        uncolored mesh instead."""
        moved = self._painted_cube(
            tmp_path,
            transform='transform="1 0 0 0 1 0 0 0 1 50.000000 0.000000 0.000000"',
        )
        payload = mesh_to_viewer_payload(moved)
        assert payload["downgraded"] is False
        assert "vertex_colors" not in payload

    def test_a_multi_object_painted_file_is_refused_not_misordered(self, tmp_path):
        """The soup follows the file's object order, the flattened mesh
        follows the scene graph's — with two objects nothing proves they
        agree, and colors on the wrong faces are worse than none.  Each
        object here is internally two-colored, so the per-part bake (which
        IS order-safe, keyed by name) rightly refuses and the soup is the
        only candidate — it must decline the multi-object case."""
        import zipfile as z

        model = """<?xml version="1.0" encoding="UTF-8"?>
<model unit="millimeter" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
       xmlns:m="http://schemas.microsoft.com/3dmanufacturing/material/2015/02">
 <resources>
  <m:colorgroup id="9">
    <m:color color="#F72323"/><m:color color="#2366F7"/>
  </m:colorgroup>
  <object id="1" type="model" name="a"><mesh>
    <vertices><vertex x="0" y="0" z="0"/><vertex x="10" y="0" z="0"/><vertex x="0" y="10" z="0"/><vertex x="0" y="0" z="10"/></vertices>
    <triangles><triangle v1="0" v2="1" v3="2" pid="9" p1="0"/><triangle v1="0" v2="1" v3="3" pid="9" p1="0"/><triangle v1="0" v2="2" v3="3" pid="9" p1="1"/><triangle v1="1" v2="2" v3="3" pid="9" p1="1"/></triangles>
  </mesh></object>
  <object id="2" type="model" name="b"><mesh>
    <vertices><vertex x="20" y="0" z="0"/><vertex x="30" y="0" z="0"/><vertex x="20" y="10" z="0"/><vertex x="20" y="0" z="10"/></vertices>
    <triangles><triangle v1="0" v2="1" v3="2" pid="9" p1="1"/><triangle v1="0" v2="1" v3="3" pid="9" p1="1"/><triangle v1="0" v2="2" v3="3" pid="9" p1="0"/><triangle v1="1" v2="2" v3="3" pid="9" p1="0"/></triangles>
  </mesh></object>
 </resources>
 <build><item objectid="1"/><item objectid="2"/></build>
</model>"""
        ct = """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>
</Types>"""
        rels = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Target="/3D/3dmodel.model" Id="rel-1" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>
</Relationships>"""
        p = tmp_path / "two_painted.3mf"
        with z.ZipFile(p, "w") as zf:
            zf.writestr("[Content_Types].xml", ct)
            zf.writestr("_rels/.rels", rels)
            zf.writestr("3D/3dmodel.model", model)
        payload = mesh_to_viewer_payload(p)
        assert payload["downgraded"] is False
        assert "vertex_colors" not in payload
