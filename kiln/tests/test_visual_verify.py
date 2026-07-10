"""Tests for kiln.generation.visual_verify — the smooth-shaded render path.

VisualVerifier previously rendered its preview through OpenSCAD's
flat-shaded ``--preview`` mode; a judge (Gemini Vision) scoring "does this
match the prompt" was looking at a facet-by-facet render that makes good
organic/curved geometry look lumpy.  These tests pin the replacement:
rendering goes through ``kiln.colored_renderer`` (the same smooth-shaded
renderer ``visualize_model`` uses) via a dependency-free STL parse, with
no OpenSCAD subprocess involved at all.
"""

from __future__ import annotations

import math
import os
import struct

import pytest

from kiln.generation.base import GenerationError
from kiln.generation.visual_verify import (
    VisualVerifier,
    _load_stl_as_colored_triangles,
)


def _write_binary_stl(path: str, triangles: list) -> None:
    with open(path, "wb") as fh:
        fh.write(b"\x00" * 80)
        fh.write(struct.pack("<I", len(triangles)))
        for tri in triangles:
            fh.write(struct.pack("<3f", 0, 0, 0))  # normal, unused by the parser
            for v in tri:
                fh.write(struct.pack("<3f", *v))
            fh.write(struct.pack("<H", 0))


@pytest.fixture()
def pyramid_stl(tmp_path) -> str:
    apex = (0.0, 0.0, 10.0)
    base = [(-10.0, -10.0, 0.0), (10.0, -10.0, 0.0), (10.0, 10.0, 0.0), (-10.0, 10.0, 0.0)]
    tris = [(base[i], base[(i + 1) % 4], apex) for i in range(4)]
    tris.append((base[0], base[2], base[1]))
    tris.append((base[0], base[3], base[2]))
    path = str(tmp_path / "pyramid.stl")
    _write_binary_stl(path, tris)
    return path


@pytest.fixture()
def sphere_stl(tmp_path) -> str:
    """A coarse (low-poly) sphere — enough triangles to be a real solid,
    coarse enough that flat vs. smooth shading is visibly different."""
    r, lat_steps, lon_steps = 15.0, 8, 12

    def pt(lat, lon):
        phi = math.pi * lat / lat_steps
        theta = 2 * math.pi * lon / lon_steps
        return (
            r * math.sin(phi) * math.cos(theta),
            r * math.sin(phi) * math.sin(theta),
            r * math.cos(phi),
        )

    tris = []
    for lat in range(lat_steps):
        for lon in range(lon_steps):
            p1, p2 = pt(lat, lon), pt(lat, lon + 1)
            p3, p4 = pt(lat + 1, lon), pt(lat + 1, lon + 1)
            tris.append((p1, p3, p2))
            tris.append((p2, p3, p4))
    path = str(tmp_path / "sphere.stl")
    _write_binary_stl(path, tris)
    return path


def test_load_stl_as_colored_triangles_reads_real_geometry(pyramid_stl):
    triangles = _load_stl_as_colored_triangles(pyramid_stl)
    assert len(triangles) == 6
    assert triangles[0].color == (170, 170, 170)  # matches the old #AAAAAA


def test_load_stl_rejects_missing_file():
    with pytest.raises(Exception):  # noqa: PT011 — _parse_stl raises via errors list -> GenerationError
        _load_stl_as_colored_triangles("/no/such/file.stl")


def test_verifier_no_longer_takes_openscad_path():
    """The signature genuinely dropped openscad_path — pins the port,
    not just its behavior."""
    import inspect

    params = inspect.signature(VisualVerifier.__init__).parameters
    assert "openscad_path" not in params


def test_render_stl_to_png_produces_real_png(pyramid_stl):
    v = VisualVerifier(api_key="unused", model="unused")
    png_path = v.render_stl_to_png(pyramid_stl)
    try:
        assert os.path.isfile(png_path)
        assert os.path.getsize(png_path) > 0
        with open(png_path, "rb") as fh:
            assert fh.read(8) == b"\x89PNG\r\n\x1a\n"  # real PNG magic bytes
    finally:
        os.unlink(png_path)


def test_render_multi_angle_returns_five_paths_in_order(pyramid_stl):
    v = VisualVerifier(api_key="unused", model="unused")
    paths = v.render_multi_angle(pyramid_stl)
    try:
        assert len(paths) == 5
        for p in paths:
            assert os.path.isfile(p) and os.path.getsize(p) > 0
        # order matters: kiln.plugins.generation_tools indexes this list
        # positionally as isometric/front/right_side/top/bottom
        names = [os.path.basename(p) for p in paths]
        assert names[0].endswith("isometric.png")
        assert names[1].endswith("front.png")
        assert names[3].endswith("top.png")
        assert names[4].endswith("bottom.png")
    finally:
        for p in paths:
            if os.path.isfile(p):
                os.unlink(p)


def test_render_stl_to_png_missing_file_raises_stl_not_found():
    v = VisualVerifier(api_key="unused", model="unused")
    with pytest.raises(GenerationError) as exc_info:
        v.render_stl_to_png("/no/such/file.stl")
    assert exc_info.value.code == "STL_NOT_FOUND"


def test_render_multi_angle_missing_file_raises_stl_not_found():
    v = VisualVerifier(api_key="unused", model="unused")
    with pytest.raises(GenerationError) as exc_info:
        v.render_multi_angle("/no/such/file.stl")
    assert exc_info.value.code == "STL_NOT_FOUND"


def test_empty_stl_raises_stl_empty(tmp_path):
    path = str(tmp_path / "empty.stl")
    _write_binary_stl(path, [])
    with pytest.raises(GenerationError) as exc_info:
        _load_stl_as_colored_triangles(path)
    assert exc_info.value.code == "STL_EMPTY"


def test_smooth_shading_differs_from_flat_per_facet_on_a_curved_mesh(sphere_stl):
    """The whole point of the port: a coarse sphere must read as a smooth
    gradient, not flat facets — verified by measuring actual pixel
    luminance variation across adjacent triangle bands is LOW (smooth
    gradient) rather than the sharp per-facet jumps flat shading gives."""
    from PIL import Image

    v = VisualVerifier(api_key="unused", model="unused")
    png_path = v.render_stl_to_png(sphere_stl)
    try:
        img = Image.open(png_path).convert("L")
        w, h = img.size
        # sample a horizontal scanline across the sphere's visible face
        row = h // 2
        pixels = [img.getpixel((x, row)) for x in range(w // 4, 3 * w // 4)]
        # smooth (Gouraud-like) shading changes gradually: no single-pixel
        # jump should be huge; flat shading would show sharp facet edges
        max_jump = max(abs(pixels[i + 1] - pixels[i]) for i in range(len(pixels) - 1))
        assert max_jump < 40, f"unexpectedly sharp shading jump ({max_jump}) for a smooth sphere"
    finally:
        os.unlink(png_path)
