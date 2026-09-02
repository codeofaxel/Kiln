"""Tests for the generic decoration "press" (apply_decoration_spec).

The engine is a mapping adapter from a preset fingerprint to
``kiln.server.decorate_surface``; these tests mock the renderer and assert
the mapping + the honest refusals, so they need no OpenSCAD.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from kiln.decoration.apply import apply_decoration_spec


@pytest.fixture
def mesh(tmp_path):
    p = tmp_path / "coaster.stl"
    p.write_text("solid x\nendsolid x\n")
    return str(p)


@pytest.fixture
def image(tmp_path):
    p = tmp_path / "logo.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n")  # just needs to exist
    return str(p)


def _ok(**over):
    base = {"success": True, "output_path": "/tmp/out.stl"}
    base.update(over)
    return base


def test_logo_deboss_maps_to_deboss(mesh, image):
    with patch("kiln.server.decorate_surface", return_value=_ok()) as ds:
        result = apply_decoration_spec(
            host_mesh_path=mesh,
            pattern_family="logo_deboss",
            image_asset_path=image,
            depth_mm=1.2,
        )
    assert result["success"] is True
    kwargs = ds.call_args.kwargs
    assert kwargs["model_path"] == mesh
    assert kwargs["content"] == image
    assert kwargs["mode"] == "deboss"
    assert kwargs["depth_mm"] == 1.2
    assert kwargs["content_type"] == "auto"
    # path normalized from output_path -> decorated_model_path
    assert result["decorated_model_path"] == "/tmp/out.stl"


def test_logo_emboss_maps_to_emboss(mesh, image):
    with patch("kiln.server.decorate_surface", return_value=_ok()) as ds:
        apply_decoration_spec(
            host_mesh_path=mesh, pattern_family="logo_emboss",
            image_asset_path=image,
        )
    assert ds.call_args.kwargs["mode"] == "emboss"


def test_posterization_tier_maps_to_known_image_style(mesh, image):
    with patch("kiln.server.decorate_surface", return_value=_ok()) as ds:
        apply_decoration_spec(
            host_mesh_path=mesh, pattern_family="photo_deboss",
            image_asset_path=image, posterization_tier="coin",
        )
    assert ds.call_args.kwargs["image_style"] == "coin"


def test_unknown_tier_falls_back_to_auto(mesh, image):
    with patch("kiln.server.decorate_surface", return_value=_ok()) as ds:
        apply_decoration_spec(
            host_mesh_path=mesh, pattern_family="photo_deboss",
            image_asset_path=image, posterization_tier="tier-7-glossy",
        )
    assert ds.call_args.kwargs["image_style"] == "auto"


def test_mark_families_always_trace_never_heightmap(mesh, image):
    # Logos/brand marks must take the traced-mark path even when the
    # preset carries a photo posterize tier — a heightmap style here
    # carves the whole tile (background + frame) around the mark.
    for family in ("logo_deboss", "logo_emboss", "brand_asset"):
        with patch("kiln.server.decorate_surface", return_value=_ok()) as ds:
            apply_decoration_spec(
                host_mesh_path=mesh, pattern_family=family,
                image_asset_path=image, posterization_tier="coin",
            )
        assert ds.call_args.kwargs["image_style"] == "stencil", family


def test_horizontal_caps_selects_top_face(mesh, image):
    with patch("kiln.server.decorate_surface", return_value=_ok()) as ds:
        apply_decoration_spec(
            host_mesh_path=mesh, pattern_family="custom_image",
            image_asset_path=image, surface_selection="horizontal_caps",
        )
    assert ds.call_args.kwargs["face"] == "top"


def _refused(code: str, message: str = "nope"):
    return {"success": False, "error": {"code": code, "message": message}}


def test_vertical_walls_asks_for_the_curved_wall(mesh, image):
    """The selection names the walls, so the carve must go there.  It used
    to resolve to "auto", which on a round body picks the largest FLAT
    face — the cap the selection explicitly excludes."""
    with patch("kiln.server.decorate_surface", return_value=_ok()) as ds:
        result = apply_decoration_spec(
            host_mesh_path=mesh, pattern_family="custom_image",
            image_asset_path=image, surface_selection="vertical_walls",
        )
    assert ds.call_args.kwargs["face"] == "wall"
    assert ds.call_count == 1
    assert not result.get("warnings")


@pytest.mark.parametrize(
    "code",
    [
        "NO_ROUND_WALL", "INVALID_CONTENT", "INVALID_MODE",
        "UNSUPPORTED_FORMAT", "ENGINE_UNAVAILABLE", "MARK_DOES_NOT_FIT",
        "TEXT_DOES_NOT_FIT",
    ],
)
def test_vertical_walls_degrades_to_the_flat_face_and_says_so(mesh, image, code):
    """A box, a photo, an emboss family, a public-only install: the wall
    cannot take the carve, so the preset lands flat rather than failing —
    the old behaviour, minus the silence."""
    with patch(
        "kiln.server.decorate_surface",
        side_effect=[_refused(code, "because reasons"), _ok()],
    ) as ds:
        result = apply_decoration_spec(
            host_mesh_path=mesh, pattern_family="custom_image",
            image_asset_path=image, surface_selection="vertical_walls",
        )
    assert [c.kwargs["face"] for c in ds.call_args_list] == ["wall", "auto"]
    assert result["success"] is True
    assert result["decorated_model_path"] == "/tmp/out.stl"
    note = " ".join(result["warnings"])
    assert "vertical_walls" in note and "flat face" in note
    assert "because reasons" in note, "the refusal's own reason must survive"


def test_vertical_walls_does_not_retry_a_malformed_request(mesh, image):
    """Only wall-capability refusals fall back.  A different failure is a
    real failure and must surface, not be retried into a flat carve."""
    with patch(
        "kiln.server.decorate_surface", return_value=_refused("PRO_REQUIRED"),
    ) as ds:
        result = apply_decoration_spec(
            host_mesh_path=mesh, pattern_family="custom_image",
            image_asset_path=image, surface_selection="vertical_walls",
        )
    assert ds.call_count == 1
    assert result["success"] is False


def test_explicit_wall_face_gets_the_honest_error(mesh, image):
    """A caller who asked for the wall by name is told the wall refused —
    landing somewhere else while reporting success is the silently-wrong
    result this whole path exists to avoid."""
    with patch(
        "kiln.server.decorate_surface", return_value=_refused("NO_ROUND_WALL"),
    ) as ds:
        result = apply_decoration_spec(
            host_mesh_path=mesh, pattern_family="custom_image",
            image_asset_path=image, surface_selection="vertical_walls",
            face="wall",
        )
    assert ds.call_count == 1
    assert result["success"] is False
    assert result["error"]["code"] == "NO_ROUND_WALL"


def test_other_selections_are_unchanged(mesh, image):
    for selection, expected in (
        ("outer_faces", "auto"), ("all_faces", "auto"), ("horizontal_caps", "top"),
    ):
        with patch("kiln.server.decorate_surface", return_value=_ok()) as ds:
            apply_decoration_spec(
                host_mesh_path=mesh, pattern_family="custom_image",
                image_asset_path=image, surface_selection=selection,
            )
        assert ds.call_args.kwargs["face"] == expected
        assert ds.call_count == 1


def test_fastmcp_list_shape_unwraps_to_payload_dict(mesh, image):
    # decorate_surface called in-process can return the FastMCP transport
    # shape [Image, payload] — the contract here is the payload DICT.
    ok = _ok()
    with patch("kiln.server.decorate_surface", return_value=[object(), ok]):
        result = apply_decoration_spec(
            host_mesh_path=mesh, pattern_family="logo_deboss",
            image_asset_path=image,
        )
    assert isinstance(result, dict)
    assert result["decorated_model_path"] == "/tmp/out.stl"


def test_procedural_is_refused_without_rendering(mesh):
    with patch("kiln.server.decorate_surface") as ds:
        result = apply_decoration_spec(
            host_mesh_path=mesh, pattern_family="procedural_texture",
        )
    assert result["success"] is False
    assert result["code"] == "PROCEDURAL_NOT_SUPPORTED"
    ds.assert_not_called()


def test_missing_host_mesh_errors():
    with patch("kiln.server.decorate_surface") as ds:
        result = apply_decoration_spec(
            host_mesh_path="/nope/missing.stl", pattern_family="logo_deboss",
            image_asset_path="/nope/logo.png",
        )
    assert result["success"] is False
    ds.assert_not_called()


def test_image_family_without_asset_errors(mesh):
    with patch("kiln.server.decorate_surface") as ds:
        result = apply_decoration_spec(
            host_mesh_path=mesh, pattern_family="logo_deboss",
        )
    assert result["success"] is False
    ds.assert_not_called()


def test_missing_image_asset_file_errors(mesh):
    with patch("kiln.server.decorate_surface") as ds:
        result = apply_decoration_spec(
            host_mesh_path=mesh, pattern_family="logo_deboss",
            image_asset_path="/nope/logo.png",
        )
    assert result["success"] is False
    ds.assert_not_called()


def test_unknown_family_errors(mesh, image):
    with patch("kiln.server.decorate_surface") as ds:
        result = apply_decoration_spec(
            host_mesh_path=mesh, pattern_family="hologram",
            image_asset_path=image,
        )
    assert result["success"] is False
    ds.assert_not_called()


def test_text_content_without_image_asset(mesh):
    """A text decoration passes content through (no image file needed)."""
    with patch("kiln.server.decorate_surface", return_value=_ok()) as ds:
        apply_decoration_spec(
            host_mesh_path=mesh, pattern_family="custom_image",
            content="text:KILN",
        )
    assert ds.call_args.kwargs["content"] == "text:KILN"


def test_caller_placement_forwards_and_wins_over_selection_mapping(mesh, image):
    """face/scale/offsets pass through; explicit face beats the
    surface_selection → face mapping; omitted placement sends NOTHING, so
    decorate_surface's own defaults stay the single authority."""
    with patch("kiln.server.decorate_surface", return_value=_ok()) as ds:
        apply_decoration_spec(
            host_mesh_path=mesh,
            pattern_family="logo_deboss",
            image_asset_path=image,
            surface_selection="horizontal_caps",  # maps to face="top"
            face="front",                          # caller wins
            scale=0.4,
            offset_x_mm=-3.0,
            offset_y_mm=2.0,
        )
    kw = ds.call_args.kwargs
    assert kw["face"] == "front"
    assert kw["scale"] == pytest.approx(0.4)
    assert kw["offset_x_mm"] == pytest.approx(-3.0)
    assert kw["offset_y_mm"] == pytest.approx(2.0)

    with patch("kiln.server.decorate_surface", return_value=_ok()) as ds:
        apply_decoration_spec(
            host_mesh_path=mesh,
            pattern_family="logo_deboss",
            image_asset_path=image,
            surface_selection="horizontal_caps",
        )
    kw = ds.call_args.kwargs
    assert kw["face"] == "top"
    assert "scale" not in kw
    assert "offset_x_mm" not in kw and "offset_y_mm" not in kw


def test_manual_offsets_clamp_to_the_face(tmp_path):
    """An offset that would push the content off the face is clamped, and
    the clamp is SAID — a successful carve with art hanging off the edge
    is a wrong result wearing a success envelope.  Drives the REAL
    generator, not a re-implementation of its math."""
    from kiln.emboss_generator import generate_emboss_scad

    dummy_stl = tmp_path / "dummy.stl"
    dummy_stl.write_bytes(b"solid dummy\nendsolid dummy\n")
    face = {
        "face": "top",
        "center": (0.0, 0.0, 10.0),
        "width_mm": 80.0,
        "height_mm": 60.0,
        "normal": (0, 0, 1),
        "area_mm2": 80.0 * 60.0,
    }
    content_info = {
        "type": "openscad_text",
        "text": "Hi",
        "font": "Liberation Sans",
    }

    result = generate_emboss_scad(
        model_path=str(dummy_stl),
        content_info=content_info,
        face=face,
        output_dir=str(tmp_path),
        scale=0.5,
        depth_mm=0.8,
        mode="deboss",
        offset_x_mm=500.0,
        offset_y_mm=-500.0,
    )

    assert any("clamped" in w for w in result.get("warnings", [])), (
        "an off-face offset must be clamped AND said, not silently applied"
    )

    # And an in-bounds offset passes through unclamped, no warning.
    quiet = generate_emboss_scad(
        model_path=str(dummy_stl),
        content_info=content_info,
        face=face,
        output_dir=str(tmp_path),
        scale=0.5,
        depth_mm=0.8,
        mode="deboss",
        offset_x_mm=5.0,
    )
    assert not any("clamped" in w and "offset" in w for w in quiet.get("warnings", []))


def test_offset_clamp_needs_no_openscad(tmp_path, monkeypatch):
    """The CI replay: the offset clamp is computed and SAID on a machine
    with no OpenSCAD.  Text measurement degrades to the heuristic fit
    (``TextMeasureError`` caught inside the generator) — a missing
    binary must never crash the carve, and must never un-say the clamp.
    """
    from kiln import emboss_generator as eg

    monkeypatch.setattr(eg, "_TEXT_METRICS_CACHE", {})

    def missing_binary():
        raise FileNotFoundError("OpenSCAD not found or not usable")

    monkeypatch.setattr(eg, "_find_openscad", missing_binary)

    dummy_stl = tmp_path / "dummy.stl"
    dummy_stl.write_bytes(b"solid dummy\nendsolid dummy\n")
    face = {
        "face": "top",
        "center": (0.0, 0.0, 10.0),
        "width_mm": 80.0,
        "height_mm": 60.0,
        "normal": (0, 0, 1),
        "area_mm2": 80.0 * 60.0,
    }
    result = eg.generate_emboss_scad(
        model_path=str(dummy_stl),
        content_info={
            "type": "openscad_text",
            "text": "Hi",
            "font": "Liberation Sans",
        },
        face=face,
        output_dir=str(tmp_path),
        scale=0.5,
        depth_mm=0.8,
        mode="deboss",
        offset_x_mm=500.0,
        offset_y_mm=-500.0,
    )
    assert any("clamped" in w for w in result.get("warnings", [])), (
        "the clamp must survive the no-OpenSCAD fallback path"
    )
