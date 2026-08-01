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


def _resolved_via_preset(result: dict[str, Any]) -> dict[str, Any]:
    """Demote decorate_surface's managed-asset warning on the preset path.

    ``decorate_surface`` warns when it is handed a preset's stored artwork,
    because that normally means a caller could not find the preset-apply
    door and carved the raw asset with invented parameters.  Reaching it
    from HERE is the opposite case: these parameters came out of the preset.

    So the lineage is kept — it usefully records which preset produced the
    geometry — and the warning is dropped.  A warning that fires loudest on
    the one path doing it correctly is a warning people learn to ignore.
    """
    managed = result.get("managed_asset")
    if not isinstance(managed, dict):
        return result
    result = dict(result)
    result["managed_asset"] = {
        **{k: v for k, v in managed.items() if k != "warning"},
        "via_preset_apply": True,
    }
    warned = managed.get("warning")
    warnings = result.get("warnings")
    if isinstance(warnings, list) and warned in warnings:
        remaining = [w for w in warnings if w != warned]
        if remaining:
            result["warnings"] = remaining
        else:
            result.pop("warnings", None)
    return result


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
    face: str | None = None,
    scale: float | None = None,
    offset_x_mm: float = 0.0,
    offset_y_mm: float = 0.0,
) -> dict[str, Any]:
    """Carve a resolved decoration preset onto *host_mesh_path*.

    Placement (``face`` / ``scale`` / ``offset_*``) belongs to the CALLER,
    not the preset: a preset records the LOOK (art, depth, mode), but
    where that look lands depends on the host mesh being decorated this
    time.  All four are optional — omitted, the engine's own defaults
    apply (auto face, centred, decorate_surface's default coverage), so
    an unadorned call behaves exactly as before.

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

    # Caller placement wins over the surface-selection mapping; scale and
    # offsets are forwarded only when supplied, so decorate_surface's own
    # defaults stay the single authority for what "unspecified" means —
    # a copy of its 0.7 here would drift the day that default moves.
    placement: dict[str, Any] = {}
    if scale is not None:
        placement["scale"] = float(scale)
    if offset_x_mm:
        placement["offset_x_mm"] = float(offset_x_mm)
    if offset_y_mm:
        placement["offset_y_mm"] = float(offset_y_mm)

    result = decorate_surface(
        model_path=host_mesh_path,
        content=resolved_content,
        face=(face or _FACE_BY_SELECTION.get(surface_selection, "auto")),
        depth_mm=float(depth_mm or 0.0),
        mode=_mode_for_family(family),
        material=material or "PLA",
        image_style=image_style,
        content_type="auto",
        **placement,
    )

    # decorate_surface invoked in-process may return the FastMCP tool
    # shape ``[Image, payload_dict]`` (inline preview + data).  This
    # function's contract is "decorate_surface's result DICT" — unwrap
    # to the payload so callers never juggle transport shapes.
    from kiln.tool_results import unwrap_tool_result

    result = unwrap_tool_result(result)
    if isinstance(result, dict):
        result = _resolved_via_preset(result)
        path = _decorated_path(result)
        if path and "decorated_model_path" not in result:
            result = {**result, "decorated_model_path": path}
    return result
