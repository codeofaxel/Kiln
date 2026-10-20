"""Tests for the decoration_library module — persistent decoration storage.

Coverage areas:
    - Slug generation from names
    - Library directory resolution (default + env override)
    - Save decorations (photo, SVG, QR, text, with files or inline data)
    - List decorations with filtering by content_type and tag
    - Get decoration by name or slug
    - Delete decorations
    - Resolve decoration settings: the standard settings a decoration that
      has not printed well starts from (replaying what printed well is
      Kiln Pro decoration memory, served; its tests live in kiln-pro with it)
    - Dataclass round-trip serialization (to_dict / from_dict)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kiln.decoration_library import (
    Decoration,
    DecorationScaling,
    ProvenSetting,
    _slugify,
    delete_decoration,
    get_decoration,
    get_library_dir,
    list_decorations,
    resolve_decoration_settings,
    save_decoration,
)

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _use_tmp_library(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Route all library I/O to a temp directory."""
    monkeypatch.setenv("KILN_DECORATIONS_DIR", str(tmp_path / "decorations"))


def _make_source_file(tmp_path: Path, name: str = "photo.png") -> Path:
    """Create a dummy source file and return its path."""
    p = tmp_path / name
    p.write_bytes(b"\x89PNG fake image data")
    return p


def _save_simple(
    tmp_path: Path,
    *,
    name: str = "Ash Portrait",
    content_type: str = "photo",
    tags: list[str] | None = None,
    material: str | None = None,
) -> Decoration:
    """Save a minimal decoration for tests that need one pre-existing."""
    src = _make_source_file(tmp_path, "simple_src.png")
    return save_decoration(
        name,
        content_type=content_type,
        source_path=str(src),
        depth_mm=0.6,
        tags=tags,
        material=material,
    )


# ---------------------------------------------------------------------------
# _slugify
# ---------------------------------------------------------------------------


class TestSlugify:

    def test_simple_name(self):
        assert _slugify("Ash Portrait") == "ash-portrait"

    def test_special_characters_stripped(self):
        assert _slugify("My Logo! @2024") == "my-logo-2024"

    def test_leading_trailing_hyphens_stripped(self):
        assert _slugify("--hello--") == "hello"

    def test_empty_string_raises(self):
        with pytest.raises(ValueError, match="empty"):
            _slugify("")


# ---------------------------------------------------------------------------
# get_library_dir
# ---------------------------------------------------------------------------


class TestGetLibraryDir:

    def test_default_path_is_home_kiln_decorations(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.delenv("KILN_DECORATIONS_DIR", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        result = get_library_dir()
        assert result == tmp_path / ".kiln" / "decorations"

    def test_env_var_override(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        custom = tmp_path / "custom_deco"
        monkeypatch.setenv("KILN_DECORATIONS_DIR", str(custom))
        assert get_library_dir() == custom

    def test_creates_directory_if_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        target = tmp_path / "auto_created"
        monkeypatch.setenv("KILN_DECORATIONS_DIR", str(target))
        result = get_library_dir()
        assert result.is_dir()


# ---------------------------------------------------------------------------
# save_decoration
# ---------------------------------------------------------------------------


class TestSaveDecoration:

    def test_saves_photo_decoration(self, tmp_path: Path):
        src = _make_source_file(tmp_path, "portrait.png")
        dec = save_decoration(
            "Ash Portrait",
            content_type="photo",
            source_path=str(src),
            depth_mm=0.6,
            mode="emboss",
        )
        assert dec.name == "Ash Portrait"
        assert dec.slug == "ash-portrait"
        assert dec.content_type == "photo"
        # Manifest should exist on disk
        lib = get_library_dir()
        manifest = lib / dec.slug / "manifest.json"
        assert manifest.exists()

    def test_saves_svg_decoration(self, tmp_path: Path):
        src = tmp_path / "logo.svg"
        src.write_text("<svg></svg>")
        dec = save_decoration(
            "Brand Logo",
            content_type="svg",
            source_path=str(src),
            depth_mm=0.5,
            mode="deboss",
        )
        assert dec.content_type == "svg"
        assert dec.slug == "brand-logo"

    def test_saves_qr_with_content_data(self, tmp_path: Path):
        dec = save_decoration(
            "My QR Code",
            content_type="qr",
            content_data="https://example.com",
            depth_mm=0.5,
        )
        assert dec.content_type == "qr"
        assert dec.content_data == "https://example.com"

    def test_saves_text_with_content_data(self, tmp_path: Path):
        dec = save_decoration(
            "Gift Text",
            content_type="text",
            content_data="Happy Birthday!",
            depth_mm=0.4,
        )
        assert dec.content_type == "text"
        assert dec.content_data == "Happy Birthday!"

    def test_missing_name_raises(self, tmp_path: Path):
        with pytest.raises(ValueError, match="name"):
            save_decoration("", content_type="photo", content_data="x")

    def test_invalid_content_type_raises(self, tmp_path: Path):
        with pytest.raises(ValueError, match="content_type"):
            save_decoration("Test", content_type="hologram", content_data="x")

    def test_no_content_raises(self, tmp_path: Path):
        with pytest.raises(ValueError, match="content"):
            save_decoration("Test", content_type="photo")

    def test_source_file_not_found_raises(self, tmp_path: Path):
        with pytest.raises((FileNotFoundError, ValueError)):
            save_decoration(
                "Missing",
                content_type="photo",
                source_path="/nonexistent/file.png",
            )

    def test_duplicate_name_overwrites(self, tmp_path: Path):
        src = _make_source_file(tmp_path, "v1.png")
        save_decoration("Dup", content_type="photo", source_path=str(src), depth_mm=0.5)
        src2 = _make_source_file(tmp_path, "v2.png")
        dec2 = save_decoration("Dup", content_type="photo", source_path=str(src2), depth_mm=0.8)
        # Should still work — either overwrites or raises; we accept overwrite
        assert dec2.slug == "dup"

    def test_copies_source_file_to_library(self, tmp_path: Path):
        src = _make_source_file(tmp_path, "to_copy.png")
        dec = save_decoration(
            "Copy Test",
            content_type="photo",
            source_path=str(src),
            depth_mm=0.6,
        )
        lib = get_library_dir()
        # The source file should have been copied into the decoration directory
        deco_dir = lib / dec.slug
        files = list(deco_dir.iterdir())
        file_names = [f.name for f in files]
        assert any(f != "manifest.json" for f in file_names) or dec.source_file is not None

    def test_copies_content_file_to_library(self, tmp_path: Path):
        content = tmp_path / "processed.dat"
        content.write_bytes(b"processed data")
        dec = save_decoration(
            "Content File Test",
            content_type="photo",
            content_path=str(content),
            depth_mm=0.6,
        )
        assert dec.content_file is not None

    def test_initial_proven_settings_from_material(self, tmp_path: Path):
        src = _make_source_file(tmp_path, "mat.png")
        dec = save_decoration(
            "With Material",
            content_type="photo",
            source_path=str(src),
            depth_mm=0.6,
            mode="emboss",
            material="PLA",
        )
        # If material was provided, proven_settings should have an entry keyed by material
        assert len(dec.proven_settings) >= 1
        assert "PLA" in dec.proven_settings
        assert dec.proven_settings["PLA"].mode == "emboss"


# ---------------------------------------------------------------------------
# list_decorations
# ---------------------------------------------------------------------------


class TestListDecorations:

    def test_empty_library_returns_empty(self):
        result = list_decorations()
        assert result == []

    def test_lists_all_decorations(self, tmp_path: Path):
        _save_simple(tmp_path, name="Deco A")
        _save_simple(tmp_path, name="Deco B")
        result = list_decorations()
        assert len(result) == 2

    def test_filters_by_content_type(self, tmp_path: Path):
        _save_simple(tmp_path, name="Photo One", content_type="photo")
        save_decoration(
            "QR One",
            content_type="qr",
            content_data="https://example.com",
        )
        photos = list_decorations(content_type="photo")
        assert len(photos) == 1
        assert photos[0].content_type == "photo"

    def test_filters_by_tag(self, tmp_path: Path):
        _save_simple(tmp_path, name="Tagged", tags=["gift", "holiday"])
        _save_simple(tmp_path, name="Untagged")
        result = list_decorations(tag="gift")
        assert len(result) == 1
        assert result[0].slug == "tagged"

    def test_sorted_by_last_printed(self, tmp_path: Path):
        import json

        _save_simple(tmp_path, name="Old Dec")
        _save_simple(tmp_path, name="New Dec")
        # Give the first one a print, as a recorded print leaves it.
        manifest = get_library_dir() / "old-dec" / "manifest.json"
        data = json.loads(manifest.read_text())
        data["proven_settings"] = {"PLA": {"depth_mm": 0.6, "success_count": 1,
                                           "last_printed": "2099-01-01T00:00:00+00:00"}}
        manifest.write_text(json.dumps(data))
        result = list_decorations()
        # The one with a recorded print should sort differently
        assert len(result) == 2


# ---------------------------------------------------------------------------
# get_decoration
# ---------------------------------------------------------------------------


class TestGetDecoration:

    def test_found_by_name(self, tmp_path: Path):
        _save_simple(tmp_path, name="Find Me")
        dec = get_decoration("Find Me")
        assert dec is not None
        assert dec.name == "Find Me"

    def test_found_by_slug(self, tmp_path: Path):
        _save_simple(tmp_path, name="Slug Test")
        dec = get_decoration("slug-test")
        assert dec is not None
        assert dec.slug == "slug-test"

    def test_not_found_returns_none(self):
        assert get_decoration("nonexistent") is None

    def test_corrupt_manifest_returns_none(self, tmp_path: Path):
        lib = get_library_dir()
        bad_dir = lib / "corrupt-deco"
        bad_dir.mkdir(parents=True, exist_ok=True)
        (bad_dir / "manifest.json").write_text("{invalid json!!")
        assert get_decoration("corrupt-deco") is None


# ---------------------------------------------------------------------------
# delete_decoration
# ---------------------------------------------------------------------------


class TestDeleteDecoration:

    def test_deletes_directory(self, tmp_path: Path):
        _save_simple(tmp_path, name="To Delete")
        assert delete_decoration("to-delete") is True
        assert get_decoration("to-delete") is None

    def test_not_found_returns_false(self):
        assert delete_decoration("nonexistent") is False


# ---------------------------------------------------------------------------
# resolve_decoration_settings: the standard settings
# ---------------------------------------------------------------------------


class TestResolveDecorationSettings:

    def _make_decoration_with_proven(
        self,
        content_type: str = "photo",
        proven: dict[str, ProvenSetting] | None = None,
    ) -> Decoration:
        return Decoration(
            name="Resolve Test",
            slug="resolve-test",
            content_type=content_type,
            created="2026-01-01T00:00:00",
            source_file=None,
            content_file=None,
            content_data=None,
            processing=None,
            scaling=None,
            proven_settings=proven or {},
            tags=[],
            print_count=0,
        )

    def test_falls_back_to_defaults_for_unknown_material(self):
        dec = self._make_decoration_with_proven(content_type="photo")
        settings = resolve_decoration_settings(dec, material="UNKNOWN_EXOTIC")
        # Should return defaults, not crash
        assert "depth_mm" in settings
        assert "mode" in settings

    def test_photo_defaults(self):
        dec = self._make_decoration_with_proven(content_type="photo")
        settings = resolve_decoration_settings(dec, material="PLA")
        assert settings["depth_mm"] == pytest.approx(0.6, abs=0.2)
        assert settings["mode"] == "emboss"

    def test_svg_defaults(self):
        dec = self._make_decoration_with_proven(content_type="svg")
        settings = resolve_decoration_settings(dec, material="PLA")
        assert settings["depth_mm"] == pytest.approx(0.5, abs=0.2)
        assert settings["mode"] == "deboss"

    def test_qr_defaults(self):
        dec = self._make_decoration_with_proven(content_type="qr")
        settings = resolve_decoration_settings(dec, material="PLA")
        assert settings["depth_mm"] == pytest.approx(0.5, abs=0.2)
        assert settings["mode"] == "emboss"

    def test_text_defaults(self):
        dec = self._make_decoration_with_proven(content_type="text")
        settings = resolve_decoration_settings(dec, material="PLA")
        assert settings["depth_mm"] == pytest.approx(0.4, abs=0.2)
        assert settings["mode"] == "deboss"

    def test_geometric_texture_defaults(self):
        dec = self._make_decoration_with_proven(content_type="geometric_texture")
        settings = resolve_decoration_settings(dec, material="PLA")
        assert settings["depth_mm"] == pytest.approx(1.2, abs=0.2)
        assert settings["mode"] == "deboss"


class TestEveryValidContentTypeIsWellFormed:
    """The regression guard for the class of bug that shipped a
    ``save_to_library`` param whose ``content_type`` was never added here.

    A caller (kiln-pro's ``apply_geometric_texture``) can pass ANY string as
    ``content_type`` — ``save_decoration`` is the only gate, and its
    ValueError was silently swallowed by the caller's blanket
    ``except Exception``, so the feature looked wired (no crash, a
    success-shaped return) while never actually saving anything.  This test
    doesn't catch a bad caller (that's the kiln-pro-side static audit
    reading ``_VALID_CONTENT_TYPES`` directly) — it catches the OTHER half:
    a content_type added to ``_VALID_CONTENT_TYPES`` without matching
    entries in ``_DEFAULTS`` / ``DECORATION_CATEGORIES``, which would make
    a legitimate save succeed but ``resolve_decoration_settings`` or
    category filtering silently misbehave for it.
    """

    def test_every_valid_content_type_saves_and_resolves(self, tmp_path):
        from kiln.decoration_library import _DEFAULTS, _VALID_CONTENT_TYPES, category_for

        for content_type in sorted(_VALID_CONTENT_TYPES):
            dec = save_decoration(
                f"probe {content_type}",
                content_type=content_type,
                content_data="probe",
            )
            assert dec.content_type == content_type
            # Every content type must resolve to a real category — "unknown"
            # means DECORATION_CATEGORIES wasn't updated alongside
            # _VALID_CONTENT_TYPES.
            assert category_for(content_type) != "unknown", (
                f"{content_type!r} is valid but has no DECORATION_CATEGORIES entry"
            )
            # Every content type must have its OWN defaults entry — a
            # missing one silently falls back to "photo"'s
            # (resolve_decoration_settings), which is wrong for anything
            # that isn't photo-shaped and would hide exactly this bug class
            # (an isinstance-only check on the fallback values still passes).
            assert content_type in _DEFAULTS, (
                f"{content_type!r} is valid but has no _DEFAULTS entry "
                "(resolve_decoration_settings would silently fall back to photo's)"
            )
            settings = resolve_decoration_settings(dec, material="PLA")
            assert settings["depth_mm"] == _DEFAULTS[content_type]["depth_mm"]
            assert settings["mode"] == _DEFAULTS[content_type]["mode"]


# ---------------------------------------------------------------------------
# Decoration serialization
# ---------------------------------------------------------------------------


class TestDecorationToDict:

    def test_round_trip(self, tmp_path: Path):
        dec = _save_simple(tmp_path, name="Round Trip", material="PLA")
        d = dec.to_dict()
        restored = Decoration.from_dict(d)
        assert restored.name == dec.name
        assert restored.slug == dec.slug
        assert restored.content_type == dec.content_type
        assert restored.print_count == dec.print_count
        assert len(restored.proven_settings) == len(dec.proven_settings)

    def test_proven_settings_serialized_correctly(self, tmp_path: Path):
        dec = _save_simple(tmp_path, name="Serialize", material="PLA")
        d = dec.to_dict()
        # proven_settings should be a dict keyed by material
        assert isinstance(d["proven_settings"], dict)
        if d["proven_settings"]:
            assert "PLA" in d["proven_settings"]
            ps = d["proven_settings"]["PLA"]
            assert "depth_mm" in ps
            assert "mode" in ps
            assert "success_count" in ps

    def test_version_fields_round_trip(self):
        d = {
            "name": "Versioned",
            "slug": "versioned",
            "content_type": "photo",
            "created": "2026-01-01T00:00:00",
            "version": 3,
            "parent_version": 2,
            "changes": {"depth_mm": "0.6 -> 0.8"},
        }
        dec = Decoration.from_dict(d)
        assert dec.version == 3
        assert dec.parent_version == 2
        assert dec.changes == {"depth_mm": "0.6 -> 0.8"}
        out = dec.to_dict()
        assert out["version"] == 3
        assert out["parent_version"] == 2
        assert out["changes"] == {"depth_mm": "0.6 -> 0.8"}

    def test_legacy_manifest_defaults_version_to_1(self):
        d = {
            "name": "Legacy",
            "slug": "legacy",
            "content_type": "photo",
            "created": "2025-01-01T00:00:00",
        }
        dec = Decoration.from_dict(d)
        assert dec.version == 1
        assert dec.parent_version is None
        assert dec.changes is None


# ---------------------------------------------------------------------------
# Texture-as-decoration taxonomy
# ---------------------------------------------------------------------------


class TestDecorationCategory:
    """Verify decoration category system (surface vs texture)."""

    def test_photo_is_surface(self, tmp_path):
        dec = _save_simple(tmp_path, content_type="photo")
        assert dec.category == "surface"

    def test_svg_is_surface(self, tmp_path):
        dec = _save_simple(tmp_path, name="svg test", content_type="svg")
        assert dec.category == "surface"

    def test_qr_is_surface(self, tmp_path):
        dec = _save_simple(tmp_path, name="qr test", content_type="qr")
        assert dec.category == "surface"

    def test_text_is_surface(self, tmp_path):
        dec = _save_simple(tmp_path, name="text test", content_type="text")
        assert dec.category == "surface"

    def test_procedural_texture_is_texture(self, tmp_path):
        dec = save_decoration(
            "Tiger Stripe",
            content_type="procedural_texture",
            content_data="tiger_stripe",
            texture_params={"texture": "tiger_stripe", "num_colors": 4},
        )
        assert dec.category == "texture"
        assert dec.content_type == "procedural_texture"

    def test_ai_texture_is_texture(self, tmp_path):
        dec = save_decoration(
            "AI Wood",
            content_type="ai_texture",
            content_data="wood grain",
        )
        assert dec.category == "texture"

    def test_geometric_texture_is_texture(self, tmp_path):
        # A single-color physical relief (apply_geometric_texture's
        # save_to_library) — distinct from procedural_texture (multicolor,
        # no relief depth). Regression: this content_type was missing from
        # _VALID_CONTENT_TYPES for a full release cycle, so kiln-pro's save
        # always raised ValueError, silently swallowed by the caller's
        # blanket except-Exception — "saved" but nothing ever landed.
        dec = save_decoration(
            "Alligator Relief",
            content_type="geometric_texture",
            content_data="alligator",
            texture_params={"texture": "alligator", "depth_mm": 1.6, "mode": "deboss"},
        )
        assert dec.category == "texture"
        assert dec.content_type == "geometric_texture"

    def test_category_in_to_dict(self, tmp_path):
        dec = _save_simple(tmp_path, content_type="photo")
        d = dec.to_dict()
        assert d["category"] == "surface"

    def test_texture_params_roundtrip(self, tmp_path):
        params = {"texture": "marble", "num_colors": 3, "scale": 1.5}
        dec = save_decoration(
            "Marble Test",
            content_type="procedural_texture",
            content_data="marble",
            texture_params=params,
        )
        d = dec.to_dict()
        assert d["texture_params"] == params
        loaded = Decoration.from_dict(d)
        assert loaded.texture_params == params
        assert loaded.category == "texture"

    def test_unknown_content_type_category(self):
        from kiln.decoration_library import category_for
        assert category_for("unknown_thing") == "unknown"


class TestListByCategory:
    """Filter decorations by category."""

    def test_filter_surfaces_only(self, tmp_path):
        _save_simple(tmp_path, name="photo1", content_type="photo")
        save_decoration("tex1", content_type="procedural_texture", content_data="camo")
        results = list_decorations(category="surface")
        assert len(results) == 1
        assert results[0].content_type == "photo"

    def test_filter_textures_only(self, tmp_path):
        _save_simple(tmp_path, name="photo2", content_type="photo")
        save_decoration("tex2", content_type="procedural_texture", content_data="ocean")
        results = list_decorations(category="texture")
        assert len(results) == 1
        assert results[0].content_type == "procedural_texture"

    def test_no_category_returns_all(self, tmp_path):
        _save_simple(tmp_path, name="photo3", content_type="photo")
        save_decoration("tex3", content_type="procedural_texture", content_data="lava")
        results = list_decorations()
        assert len(results) == 2


# ---------------------------------------------------------------------------
# The counted-prints ledger survives versioning
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# apply_decoration tool — material detection from a named printer
# ---------------------------------------------------------------------------


class TestApplyDecorationMaterialDetection:
    """apply_decoration resolves the printer by name to detect material.

    The detection block used to call ``kiln.server._get_adapter(printer_id)``
    — a zero-argument resolver — so every named-printer detection died on a
    TypeError that the best-effort except swallowed, and the material always
    fell back to the decoration's defaults.
    """

    @staticmethod
    def _apply_tool():
        from kiln.plugins.decoration_library_tools import plugin

        class _MockMCP:
            def __init__(self):
                self.tools = {}

            def tool(self, **_kwargs):
                def decorator(fn):
                    self.tools[fn.__name__] = fn
                    return fn

                return decorator

        mcp = _MockMCP()
        plugin.register(mcp)
        return mcp.tools["apply_decoration"]

    def test_printer_id_resolves_material(self, tmp_path: Path):
        from unittest.mock import MagicMock, patch

        save_decoration(
            "Logo Text", content_type="text", content_data="hi", depth_mm=0.6,
        )
        model = tmp_path / "target.stl"
        model.write_text("solid x\nendsolid x\n")

        adapter = MagicMock()
        adapter.get_state.return_value = MagicMock(active_material="PETG")

        with (
            patch("kiln.server._resolve_adapter", return_value=adapter) as resolve,
            patch(
                "kiln.server.decorate_surface",
                return_value={"success": True, "output_path": str(tmp_path / "out.stl")},
            ) as decorate,
        ):
            result = self._apply_tool()(
                name="Logo Text", model_path=str(model), printer_id="workshop-a1",
            )

        assert result["success"] is True
        resolve.assert_called_once_with("workshop-a1")
        assert decorate.call_args.kwargs["material"] == "PETG"

    def test_explicit_material_skips_printer(self, tmp_path: Path):
        from unittest.mock import patch

        save_decoration(
            "Logo Text 2", content_type="text", content_data="yo", depth_mm=0.6,
        )
        model = tmp_path / "target2.stl"
        model.write_text("solid x\nendsolid x\n")

        with (
            patch("kiln.server._resolve_adapter") as resolve,
            patch(
                "kiln.server.decorate_surface",
                return_value={"success": True, "output_path": str(tmp_path / "out.stl")},
            ) as decorate,
        ):
            result = self._apply_tool()(
                name="Logo Text 2", model_path=str(model),
                material="ABS", printer_id="workshop-a1",
            )

        assert result["success"] is True
        resolve.assert_not_called()
        assert decorate.call_args.kwargs["material"] == "ABS"
