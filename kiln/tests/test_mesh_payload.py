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
            # On the plate (positive coords), so compose_multicolor_3mf
            # keeps the parts where the fixture puts them instead of
            # baking a bed-centring group shift.
            if dense:
                m = trimesh.creation.icosphere(subdivisions=3, radius=10.0)
                m.apply_translation((100.0, 100.0, 10.0))
            else:
                m = trimesh.creation.box(extents=(10.0, 10.0, 10.0))
                m.apply_translation((100.0, 100.0, 5.0))
            return m

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
        # zones sit at z ∈ [0, 10] and z ∈ [30, 40].
        assert (rgba[pos[:, 1] < 25.0] == self.RED).all()
        assert (rgba[pos[:, 1] > 25.0] == self.BLUE).all()

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
        assert (rgba[pos[:, 1] < 25.0] == self.RED).all()
        assert (rgba[pos[:, 1] > 25.0] == self.BLUE).all()

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

        # On the plate (positive coords), so compose_painted_3mf keeps the
        # identity build transform and the transform-replace below works.
        mesh = trimesh.creation.box(extents=(10.0, 10.0, 10.0))
        mesh.apply_translation((100.0, 100.0, 5.0))
        tris = [tuple(map(tuple, mesh.vertices[f])) for f in mesh.faces]
        # top faces red (all verts at z=10), everything else blue
        colors = [
            "#F72323" if all(v[2] > 9.9 for v in t) else "#2366F7" for t in tris
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
        top = (pos[:, :, 1] > 9.9).all(axis=1)   # viewer y == mesh z
        assert top.sum() == 2
        assert {tuple(c) for f in rgba[top] for c in f.tolist()} == {self.RED}
        assert {tuple(c) for f in rgba[~top] for c in f.tolist()} == {self.BLUE}

    def test_a_translated_item_is_positioned_not_refused(self, tmp_path):
        """parse_colored_3mf ignores build transforms, but a pure
        translation is provably recoverable from the bbox delta — the
        soup is shifted to where the slicer will place it and keeps its
        colors.  This is the shape of every Kiln-painted file now that
        ``compose_painted_3mf`` bakes a bed-centring translation."""
        moved = self._painted_cube(
            tmp_path,
            transform='transform="1 0 0 0 1 0 0 0 1 50.000000 0.000000 0.000000"',
        )
        payload = mesh_to_viewer_payload(moved)
        assert payload["downgraded"] is False
        pos = _f32(payload["positions"]).reshape(-1, 3)
        # cube x 95..105 moved +50 -> 145..155 (mesh x == viewer x)
        assert pos[:, 0].min() == pytest.approx(145.0)
        assert pos[:, 0].max() == pytest.approx(155.0)
        rgba = np.frombuffer(
            base64.b64decode(payload["vertex_colors"]), dtype=np.uint8
        ).reshape(-1, 3, 4)
        assert {tuple(c) for f in rgba for c in f.tolist()} == {
            self.RED, self.BLUE,
        }

    def test_a_rotated_item_is_refused_not_mispositioned(self, tmp_path):
        """A rotation cannot be recovered from bounds alone, so the guard
        keeps the honest uncolored mesh — a wrong color is worse than
        none.  (45° about Z widens the cube's bbox, so the min and max
        deltas disagree and the check catches it.)"""
        moved = self._painted_cube(
            tmp_path,
            transform=(
                'transform="0.707107 0.707107 0 -0.707107 0.707107 0 '
                '0 0 1 0.000000 0.000000 0.000000"'
            ),
        )
        payload = mesh_to_viewer_payload(moved)
        assert payload["downgraded"] is False
        assert "vertex_colors" not in payload

    def _two_object_painted(
        self, tmp_path, *, name_b="b", item2_transform=None,
    ):
        """Two internally two-colored objects — the per-part bake (one
        color per part) rightly refuses both, so the painted soup is the
        only path to color.  Object ``a`` sits at x ∈ [0, 10] with its
        z=0 / y=0 faces RED and the rest BLUE; object ``b`` sits at
        x ∈ [20, 30] with the assignment inverted — so a soup applied in
        the wrong object order is caught by color, not luck."""
        import zipfile as z

        item2 = f'<item objectid="2" transform="{item2_transform}"/>' if (
            item2_transform
        ) else '<item objectid="2"/>'
        model = f"""<?xml version="1.0" encoding="UTF-8"?>
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
  <object id="2" type="model" name="{name_b}"><mesh>
    <vertices><vertex x="20" y="0" z="0"/><vertex x="30" y="0" z="0"/><vertex x="20" y="10" z="0"/><vertex x="20" y="0" z="10"/></vertices>
    <triangles><triangle v1="0" v2="1" v3="2" pid="9" p1="1"/><triangle v1="0" v2="1" v3="3" pid="9" p1="1"/><triangle v1="0" v2="2" v3="3" pid="9" p1="0"/><triangle v1="1" v2="2" v3="3" pid="9" p1="0"/></triangles>
  </mesh></object>
 </resources>
 <build><item objectid="1"/>{item2}</build>
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
        return p

    def test_a_multi_object_painted_file_colors_each_part_by_name(
        self, tmp_path,
    ):
        """The soup follows the file's object order and the flattened mesh
        follows the scene graph's — segments matched BY NAME prove the
        two agree per part, so multi-object painted files now reach the
        stage in color instead of neutral."""
        payload = mesh_to_viewer_payload(self._two_object_painted(tmp_path))
        assert payload["downgraded"] is False
        assert payload["counts"]["triangles"] == 8
        assert payload["counts"]["vertices"] == 24  # soup: no shared verts
        rgba = np.frombuffer(
            base64.b64decode(payload["vertex_colors"]), dtype=np.uint8
        ).reshape(-1, 3, 4)
        pos = _f32(payload["positions"]).reshape(-1, 3, 3)
        # Back to mesh space: (vx, vy, vz) = (mx, mz, -my).
        mesh_pos = np.stack(
            [pos[:, :, 0], -pos[:, :, 2], pos[:, :, 1]], axis=2,
        )
        for face_verts, face_rgba in zip(mesh_pos, rgba, strict=True):
            # Soup faces carry one color on all three vertices.
            assert (face_rgba == face_rgba[0]).all()
            a_side = face_verts[:, 0].mean() < 15.0
            flat = bool(
                np.allclose(face_verts[:, 2], 0.0, atol=1e-4)
                or np.allclose(face_verts[:, 1], 0.0, atol=1e-4)
            )
            # In a the flat faces are RED; in b the SAME faces are BLUE —
            # a swapped or misordered soup fails here.
            expected = self.RED if a_side == flat else self.BLUE
            assert tuple(face_rgba[0]) == expected

    def test_duplicate_object_names_are_refused_not_guessed(self, tmp_path):
        """Two objects sharing a name cannot be matched to their Scene
        geometry by name — the honest answer is the uncolored mesh."""
        payload = mesh_to_viewer_payload(
            self._two_object_painted(tmp_path, name_b="a")
        )
        assert payload["downgraded"] is False
        assert "vertex_colors" not in payload

    def test_a_translated_multi_object_item_is_positioned(self, tmp_path):
        """The segments sit in file coordinates; a translation-only build
        item is applied to its segment's soup — like the single-object
        case — so an arranged multi-object painted file keeps its colors
        at the placed positions."""
        payload = mesh_to_viewer_payload(
            self._two_object_painted(
                tmp_path,
                item2_transform="1 0 0 0 1 0 0 0 1 5.000000 0.000000 0.000000",
            )
        )
        assert payload["downgraded"] is False
        pos = _f32(payload["positions"]).reshape(-1, 3)
        # object b: x 20..30 moved +5 -> the whole soup spans 0..35
        assert pos[:, 0].max() == pytest.approx(35.0)
        rgba = np.frombuffer(
            base64.b64decode(payload["vertex_colors"]), dtype=np.uint8
        ).reshape(-1, 4)
        assert {tuple(c) for c in rgba.tolist()} == {self.RED, self.BLUE}

    def test_a_rotated_multi_object_item_is_refused(self, tmp_path):
        """A rotated build item's soup cannot be proven aligned, so the
        honest uncolored mesh wins."""
        payload = mesh_to_viewer_payload(
            self._two_object_painted(
                tmp_path,
                item2_transform=(
                    "0.707107 0.707107 0 -0.707107 0.707107 0 "
                    "0 0 1 0.000000 0.000000 0.000000"
                ),
            )
        )
        assert payload["downgraded"] is False
        assert "vertex_colors" not in payload

    def test_a_slicer_painted_file_reaches_the_stage_in_color(self, tmp_path):
        """The MakerWorld shape: per-triangle ``paint_color`` attributes
        and NO colorgroup, basematerials, or sidecar — the form every
        BambuStudio/OrcaSlicer-painted model ships in, which used to
        render gray everywhere.  Painted states display through the
        parser's deterministic palette."""
        import zipfile as z

        from kiln.threemf_parser import _PAINT_STATE_PALETTE

        model = """<?xml version="1.0" encoding="UTF-8"?>
<model unit="millimeter" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">
 <resources>
  <object id="1" type="model" name="painted"><mesh>
    <vertices><vertex x="0" y="0" z="0"/><vertex x="10" y="0" z="0"/><vertex x="0" y="10" z="0"/><vertex x="0" y="0" z="10"/></vertices>
    <triangles><triangle v1="0" v2="1" v3="2" paint_color="4"/><triangle v1="0" v2="1" v3="3" paint_color="8"/><triangle v1="0" v2="2" v3="3"/><triangle v1="1" v2="2" v3="3"/></triangles>
  </mesh></object>
 </resources>
 <build><item objectid="1"/></build>
</model>"""
        p = tmp_path / "bambu_painted.3mf"
        with z.ZipFile(p, "w") as zf:
            zf.writestr("3D/3dmodel.model", model)
        payload = mesh_to_viewer_payload(p)
        assert payload["downgraded"] is False
        assert payload["counts"]["vertices"] == payload["counts"]["triangles"] * 3
        state_1 = (*_PAINT_STATE_PALETTE[0], 255)
        state_2 = (*_PAINT_STATE_PALETTE[1], 255)
        grey = (170, 170, 170, 255)
        rgba = np.frombuffer(
            base64.b64decode(payload["vertex_colors"]), dtype=np.uint8
        ).reshape(-1, 3, 4)
        pos = _f32(payload["positions"]).reshape(-1, 3, 3)
        for face_verts, face_rgba in zip(pos, rgba, strict=True):
            got = tuple(face_rgba[0])
            if np.allclose(face_verts[:, 1], 0.0, atol=1e-4):
                assert got == state_1    # mesh z=0 face, painted filament 1
            elif np.allclose(face_verts[:, 2], 0.0, atol=1e-4):
                assert got == state_2    # mesh y=0 face, painted filament 2
            else:
                assert got == grey       # unpainted faces stay neutral

    @pytest.mark.skipif(
        mesh_payload._decimation_backend() is None,
        reason="fast_simplification not installed — the downgrade branch covers this",
    )
    def test_painted_decimation_keeps_the_boundary_clean(self, tmp_path):
        """A painted soup holds 2-3 coincident copies of every boundary
        vertex with DIFFERENT colors; nearest-VERTEX transfer picks among
        exact ties arbitrarily, speckling the paint boundary after
        decimation.  The centroid transfer must keep exactly two colors
        with a clean spatial split (nearest-vertex left 5 wrong-side
        vertices on this exact fixture)."""
        from kiln.multicolor_3mf import compose_painted_3mf

        # On the plate, so the composer keeps the identity transform and
        # the viewer positions below stay in fixture coordinates.
        sphere = trimesh.creation.icosphere(subdivisions=3, radius=10.0)
        sphere.apply_translation((100.0, 100.0, 10.0))
        tris = [tuple(map(tuple, sphere.vertices[f])) for f in sphere.faces]
        colors = [
            "#F72323" if cz > 10.0 else "#2366F7"
            for cz in sphere.triangles_center[:, 2]
        ]
        out = tmp_path / "painted_dense.3mf"
        compose_painted_3mf(tris, colors, output_path=str(out))
        payload = mesh_to_viewer_payload(out, max_triangles=400)
        assert payload["downgraded"] is False
        assert payload["decimated_from"] == 1280
        rgba = np.frombuffer(
            base64.b64decode(payload["vertex_colors"]), dtype=np.uint8
        ).reshape(-1, 4)
        pos = _f32(payload["positions"])
        # Both zones survive, and nothing else appears.
        assert {tuple(c) for c in rgba.tolist()} == {self.RED, self.BLUE}
        # Clean split: outside a band the width of one source face around
        # the paint boundary at z=10 (mesh z == viewer y), every vertex
        # carries its own side's color.
        band = 0.25
        upper, lower = pos[:, 1] > 10.0 + band, pos[:, 1] < 10.0 - band
        assert (rgba[upper] == self.RED).all()
        assert (rgba[lower] == self.BLUE).all()


class TestCadFactsBlock:
    """attach_cad_facts — the one place a STEP tessellation gets its label."""

    def test_facts_ride_and_format_becomes_step(self):
        from kiln.mesh_payload import attach_cad_facts

        payload = {
            "kind": "kiln.mesh.v1",
            "source": {"filename": "coaster.step", "format": "stl"},
        }
        facts = {"kind": "kiln.step_facts.v1", "available": True, "solids": 1}
        out = attach_cad_facts(payload, facts)
        assert out is payload  # in place, like attach_stage_plate
        assert out["cad"]["solids"] == 1
        # The stage labels the STEP, not the tessellation file it rode in on.
        assert out["source"]["format"] == "step"

    def test_unavailable_facts_still_ride(self):
        """Honest unavailability is part of the contract — silence is not."""
        from kiln.mesh_payload import attach_cad_facts

        payload = {"kind": "kiln.mesh.v1", "source": {"format": "stl"}}
        out = attach_cad_facts(
            payload,
            {"kind": "kiln.step_facts.v1", "available": False, "reason": "x"},
        )
        assert out["cad"]["available"] is False

    def test_no_facts_attaches_nothing(self):
        from kiln.mesh_payload import attach_cad_facts

        payload = {"kind": "kiln.mesh.v1", "source": {"format": "stl"}}
        out = attach_cad_facts(payload, None)
        assert "cad" not in out
        assert out["source"]["format"] == "stl"


class TestThreeMfWithoutTrimeshLoader:
    """A 3MF still reaches the stage when trimesh's own 3MF loader cannot run.

    That loader needs lxml and networkx, and ``pip install kiln3d`` brings
    in neither — so on every plain install, every colored 3MF Kiln makes
    (a paint, a per-part compose, a texture) was written fine and then
    refused by the stage's fetch: "preview unavailable" over a good file.
    Measured live 2026-09-01 on ``paint_mesh_regions``.  The dev extra
    installs both, which is why no test ever saw it; this class takes the
    loader away on purpose, the way ``test_mesh_payload_without_trimesh``
    takes trimesh away.
    """

    @pytest.fixture(autouse=True)
    def _no_3mf_loader(self, monkeypatch, tmp_path):
        from trimesh.exceptions import ExceptionWrapper
        from trimesh.exchange import load as _load

        missing = ModuleNotFoundError("No module named 'lxml'", name="lxml")
        monkeypatch.setitem(_load.mesh_loaders, "3mf", ExceptionWrapper(missing))
        # The pin must bite: prove trimesh itself now refuses a 3MF, so a
        # pass below is the fallback working and not the loader sneaking
        # back in through some other registry.
        probe = tmp_path / "probe.3mf"
        _write_3mf(probe, _one_object_model())
        with pytest.raises(ModuleNotFoundError):
            trimesh.load(str(probe))

    def _painted_cube(self, tmp_path):
        from kiln.multicolor_3mf import compose_painted_3mf

        mesh = trimesh.creation.box(extents=(10.0, 10.0, 10.0))
        mesh.apply_translation((100.0, 100.0, 5.0))
        tris = [tuple(map(tuple, mesh.vertices[f])) for f in mesh.faces]
        colors = [
            "#F72323" if all(v[2] > 9.9 for v in t) else "#2366F7" for t in tris
        ]
        out = tmp_path / "painted.3mf"
        compose_painted_3mf(tris, colors, output_path=str(out))
        return out

    def test_a_painted_file_keeps_its_colors(self, tmp_path):
        payload = mesh_to_viewer_payload(self._painted_cube(tmp_path))
        assert payload["downgraded"] is False
        assert payload["counts"]["triangles"] == 12
        rgba = np.frombuffer(
            base64.b64decode(payload["vertex_colors"]), dtype=np.uint8
        ).reshape(-1, 4)
        assert {tuple(c) for c in rgba.tolist()} == {
            (247, 35, 35, 255), (35, 102, 247, 255),
        }

    def test_a_per_part_multicolor_file_keeps_each_parts_color(self, tmp_path):
        from kiln.multicolor_3mf import ColorPart, compose_multicolor_3mf

        a = trimesh.creation.box(extents=(10.0, 10.0, 10.0))
        a.apply_translation((100.0, 100.0, 5.0))
        b = trimesh.creation.box(extents=(10.0, 10.0, 10.0))
        b.apply_translation((130.0, 100.0, 5.0))
        a_p, b_p = tmp_path / "a.stl", tmp_path / "b.stl"
        a.export(str(a_p))
        b.export(str(b_p))
        out = tmp_path / "parts.3mf"
        compose_multicolor_3mf(
            [
                ColorPart(stl_path=str(a_p), extruder=1, name="left", color="#FF0000"),
                ColorPart(stl_path=str(b_p), extruder=2, name="right", color="#0000FF"),
            ],
            output_path=str(out),
        )
        payload = mesh_to_viewer_payload(out)
        assert payload["downgraded"] is False
        assert payload["counts"]["triangles"] == 24
        rgba = np.frombuffer(
            base64.b64decode(payload["vertex_colors"]), dtype=np.uint8
        ).reshape(-1, 4)
        pos = _f32(payload["positions"]).reshape(-1, 3)
        left = pos[:, 0] < 115.0
        assert {tuple(c) for c in rgba[left].tolist()} == {(255, 0, 0, 255)}
        assert {tuple(c) for c in rgba[~left].tolist()} == {(0, 0, 255, 255)}

    def test_build_and_component_transforms_place_every_instance(self, tmp_path):
        """One object, referenced twice through an assembly — each instance
        lands where its composed transform puts it, exactly as trimesh's
        loader would place it."""
        out = tmp_path / "assembly.3mf"
        _write_3mf(out, _assembly_model())
        payload = mesh_to_viewer_payload(out)
        assert payload["counts"]["triangles"] == 24
        pos = _f32(payload["positions"]).reshape(-1, 3)
        # viewer x == mesh x: one cube at 0..10 shifted +100 by the build
        # item, the other at +100 +30 via the component transform.
        xs = sorted({round(float(x)) for x in pos[:, 0]})
        assert xs == [100, 110, 130, 140]

    def test_a_non_3mf_still_reports_the_real_import_error(self, tmp_path, monkeypatch):
        """The fallback is for 3MF only: an STL that trimesh cannot read
        for a missing module keeps raising, never gets misparsed as XML."""
        from trimesh.exceptions import ExceptionWrapper
        from trimesh.exchange import load as _load

        monkeypatch.setitem(
            _load.mesh_loaders, "stl",
            ExceptionWrapper(ModuleNotFoundError("No module named 'x'", name="x")),
        )
        stl = tmp_path / "cube.stl"
        trimesh.creation.box(extents=(10.0, 10.0, 10.0)).export(str(stl))
        with pytest.raises(ModuleNotFoundError):
            mesh_to_viewer_payload(stl)


def _write_3mf(path, model_xml: str) -> None:
    import zipfile as z

    with z.ZipFile(path, "w") as zf:
        zf.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>'
            "</Types>",
        )
        zf.writestr(
            "_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Target="/3D/3dmodel.model" Id="rel0" '
            'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>'
            "</Relationships>",
        )
        zf.writestr("3D/3dmodel.model", model_xml)


def _cube_mesh_xml() -> str:
    box = trimesh.creation.box(extents=(10.0, 10.0, 10.0))
    box.apply_translation((5.0, 5.0, 5.0))  # 0..10 on every axis
    verts = "".join(
        f'<vertex x="{x:.3f}" y="{y:.3f}" z="{z:.3f}"/>' for x, y, z in box.vertices
    )
    tris = "".join(
        f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in box.faces
    )
    return f"<mesh><vertices>{verts}</vertices><triangles>{tris}</triangles></mesh>"


def _one_object_model() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<model unit="millimeter" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
        f'<resources><object id="1" type="model" name="cube">{_cube_mesh_xml()}</object></resources>'
        '<build><item objectid="1"/></build></model>'
    )


def _assembly_model() -> str:
    """Object 1 is a cube; object 2 is an assembly holding the cube twice,
    the second copy shifted +30 in x; the build places the assembly at
    +100 in x."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<model unit="millimeter" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
        "<resources>"
        f'<object id="1" type="model" name="cube">{_cube_mesh_xml()}</object>'
        '<object id="2" type="model" name="pair"><components>'
        '<component objectid="1"/>'
        '<component objectid="1" transform="1 0 0 0 1 0 0 0 1 30 0 0"/>'
        "</components></object>"
        "</resources>"
        '<build><item objectid="2" transform="1 0 0 0 1 0 0 0 1 100 0 0"/></build></model>'
    )
