"""Render a resolved decoration-preset fingerprint onto a host mesh.

This is the public, free, mechanical "press": it maps a preset's flat
fingerprint (pattern family, depth, surface, image asset) to the existing
:func:`kiln.server.decorate_surface` renderer.  Saving / versioning /
branching / signing a decoration preset is the kiln-pro feature; turning a
resolved preset into actual carved geometry is this.

Brand-geometry boundary
-----------------------
This engine is intentionally *unaware* of any reserved brand.  It renders
whatever image/content it is handed, exactly like decorating with your own
logo.  The official Kiln mark is a reserved asset whose geometry lives only
in kiln-pro: the kiln-pro preset-apply path resolves and renders the
reserved mark through its own owner-gated private path and never calls this
function for it.  So no brand artwork or reserved-asset list lives here, and
public Kiln keeps zero kiln-pro dependency.

Quota & auth are enforced downstream inside ``decorate_surface`` (free tier
gets 3 decorations/month), so this adapter does not re-check them.
"""
from __future__ import annotations

import os
from typing import Any

# Plain-string mirror of kiln_pro's PatternFamily — duplicated as literals so
# public Kiln stays dependency-free.  Keep in sync with
# kiln_pro/design_versions/decoration_presets.py::PatternFamily.
_EMBOSS_FAMILIES = frozenset({"photo_emboss", "logo_emboss"})
_DEBOSS_FAMILIES = frozenset({"photo_deboss", "logo_deboss"})
_IMAGE_FAMILIES = _EMBOSS_FAMILIES | _DEBOSS_FAMILIES | frozenset(
    {"brand_asset", "custom_image"}
)
# Families that are marks BY DEFINITION (logos, wordmarks, brand art):
# always carved via the traced-mark path, never a photo heightmap.
_MARK_FAMILIES = frozenset({"logo_emboss", "logo_deboss", "brand_asset"})
_PROCEDURAL_FAMILY = "procedural_texture"

# decorate_surface's accepted image styles; an unrecognised preset tier maps
# to "auto" rather than passing an invalid style through.
_VALID_IMAGE_STYLES = frozenset(
    {
        "auto", "coin", "portrait", "composite",
        "medallion", "photo", "stencil", "lithophane",
    }
)

# Best-effort map from a preset's surface_selection to decorate_surface's
# single-face `face`.  decorate_surface places one-off content on one face;
# the richer multi-face selection (vertical_walls / selected_face_ids) is a
# texture-path concern, so anything but the horizontal cap falls back to
# "auto" (largest flat face).
_FACE_BY_SELECTION = {
    "horizontal_caps": "top",
    "all_faces": "auto",
    "outer_faces": "auto",
    "vertical_walls": "auto",
}

# Keys decorate_surface may use for the produced mesh, most-specific first.
_PATH_KEYS = (
    "decorated_model_path", "output_path", "stl_path",
    "output_stl", "model_path", "path",
)
_MESH_SUFFIXES = (".stl", ".obj", ".3mf")


def _mode_for_family(family: str) -> str:
    """Emboss (raised) for *_emboss; deboss (cut, coin-relief) otherwise."""
    return "emboss" if family in _EMBOSS_FAMILIES else "deboss"


def _decorated_path(result: dict[str, Any]) -> str | None:
    """Pull the produced mesh path out of decorate_surface's result dict."""
    for key in _PATH_KEYS:
        value = result.get(key)
        if isinstance(value, str) and value.lower().endswith(_MESH_SUFFIXES):
            return value
    for value in result.values():
        if isinstance(value, str) and value.lower().endswith(_MESH_SUFFIXES):
            return value
    return None


def apply_decoration_spec(
    *,
    host_mesh_path: str,
    pattern_family: str,
    pattern_id: str | None = None,
    depth_mm: float = 1.2,
    surface_selection: str = "outer_faces",
    selected_face_ids: list[int] | None = None,
    posterization_tier: str | None = None,
    image_asset_sha256: str | None = None,
    image_asset_path: str | None = None,
    content: str | None = None,
    material: str = "PLA",
) -> dict[str, Any]:
    """Carve a resolved decoration preset onto *host_mesh_path*.

    :param host_mesh_path: STL/OBJ to decorate.
    :param pattern_family: One of the seven preset families (see
        :data:`_IMAGE_FAMILIES` / ``procedural_texture``).
    :param image_asset_path: Concrete path to the preset's image asset (the
        caller resolves this from the preset; the sha256 is for integrity).
    :param content: Alternative content string (e.g. ``"text:KILN"``) when
        the family is not image-backed by a file.
    :param posterization_tier: Maps to ``decorate_surface``'s ``image_style``
        when it names a known style, else ``"auto"``.
    :returns: ``decorate_surface``'s result dict, augmented with a
        ``decorated_model_path`` key; or ``{"success": False, "error": ...}``.
    """
    if not host_mesh_path or not os.path.isfile(host_mesh_path):
        return {"success": False, "error": f"host mesh not found: {host_mesh_path!r}"}

    family = (pattern_family or "").strip().lower()

    if family == _PROCEDURAL_FAMILY:
        # Tiling textures render through a different path; bridging the preset
        # fingerprint to apply_procedural_texture is not done yet.  Fail
        # honestly rather than carve the wrong thing.
        return {
            "success": False,
            "error": (
                "procedural-texture presets are not yet rendered via "
                "apply_decoration_spec — use apply_procedural_texture"
            ),
            "code": "PROCEDURAL_NOT_SUPPORTED",
        }

    if family not in _IMAGE_FAMILIES:
        return {"success": False, "error": f"unknown pattern_family: {family!r}"}

    resolved_content = content or image_asset_path
    if not resolved_content:
        return {
            "success": False,
            "error": (
                f"image-based pattern {family!r} requires image_asset_path "
                "or content"
            ),
        }
    if image_asset_path and not os.path.isfile(image_asset_path):
        return {"success": False, "error": f"image asset not found: {image_asset_path!r}"}

    tier = (posterization_tier or "").strip().lower()
    if family in _MARK_FAMILIES:
        # Posterize tiers are a photo concept — honoring one for a logo
        # would route the mark through a whole-tile heightmap (background
        # carve + perimeter frame).  Marks always trace.
        image_style = "stencil"
    else:
        image_style = tier if tier in _VALID_IMAGE_STYLES else "auto"

    from kiln.server import decorate_surface

    result = decorate_surface(
        model_path=host_mesh_path,
        content=resolved_content,
        face=_FACE_BY_SELECTION.get(surface_selection, "auto"),
        depth_mm=float(depth_mm or 0.0),
        mode=_mode_for_family(family),
        material=material or "PLA",
        image_style=image_style,
        content_type="auto",
    )

    if isinstance(result, dict):
        path = _decorated_path(result)
        if path and "decorated_model_path" not in result:
            result = {**result, "decorated_model_path": path}
    return result
