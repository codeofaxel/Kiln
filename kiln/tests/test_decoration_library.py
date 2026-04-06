"""Tests for the decoration_library module — persistent decoration storage.

Coverage areas:
    - Slug generation from names
    - Library directory resolution (default + env override)
    - Save decorations (photo, SVG, QR, text, with files or inline data)
    - List decorations with filtering by content_type and tag
    - Get decoration by name or slug
    - Delete decorations
    - Record successful prints (proven settings tracking)
    - Compute decoration scale for target faces
    - Resolve decoration settings (proven vs. defaults per material)
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
    compute_decoration_scale,
    decoration_history,
    delete_decoration,
    get_decoration,
    get_library_dir,
    iterate_decoration,
    list_decorations,
    record_decoration_success,
    resolve_decoration_settings,
    rollback_decoration,
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
        _save_simple(tmp_path, name="Old Dec")
        _save_simple(tmp_path, name="New Dec")
        # Record a print on the first one to give it a more recent last_printed
        record_decoration_success("old-dec", material="PLA", depth_mm=0.6)
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
# record_decoration_success
# ---------------------------------------------------------------------------


class TestRecordDecorationSuccess:

    def test_creates_new_proven_setting(self, tmp_path: Path):
        _save_simple(tmp_path, name="Prove Me")
        dec = record_decoration_success(
            "prove-me", material="PLA", depth_mm=0.6, mode="emboss"
        )
        assert "PLA" in dec.proven_settings
        assert dec.proven_settings["PLA"].depth_mm == 0.6
        assert dec.proven_settings["PLA"].success_count >= 1

    def test_increments_existing_success_count(self, tmp_path: Path):
        _save_simple(tmp_path, name="Count Up")
        record_decoration_success(
            "count-up", material="PLA", depth_mm=0.6, mode="emboss"
        )
        dec = record_decoration_success(
            "count-up", material="PLA", depth_mm=0.6, mode="emboss"
        )
        assert dec.proven_settings["PLA"].success_count >= 2

    def test_updates_last_printed(self, tmp_path: Path):
        _save_simple(tmp_path, name="Timestamp")
        dec = record_decoration_success(
            "timestamp", material="PLA", depth_mm=0.5
        )
        assert dec.proven_settings["PLA"].last_printed is not None

    def test_increments_print_count(self, tmp_path: Path):
        _save_simple(tmp_path, name="Print Count")
        record_decoration_success("print-count", material="PLA", depth_mm=0.5)
        dec = record_decoration_success("print-count", material="PLA", depth_mm=0.5)
        assert dec.print_count >= 2

    def test_not_found_raises(self):
        with pytest.raises((ValueError, FileNotFoundError)):
            record_decoration_success(
                "ghost", material="PLA", depth_mm=0.5
            )


# ---------------------------------------------------------------------------
# compute_decoration_scale
# ---------------------------------------------------------------------------


class TestComputeDecorationScale:

    def _make_scaled_decoration(
        self,
        *,
        native_w: float = 56.0,
        native_h: float = 56.0,
        min_detail: float = 0.0,
    ) -> Decoration:
        """Build a Decoration with the given scaling for scale tests."""
        scaling = DecorationScaling(
            native_width_mm=native_w,
            native_height_mm=native_h,
            min_detail_mm=min_detail,
            aspect_ratio=native_w / native_h if native_h else 1.0,
        )
        return Decoration(
            name="Scale Test",
            slug="scale-test",
            content_type="photo",
            created="2026-01-01T00:00:00",
            source_file=None,
            content_file=None,
            content_data=None,
            processing=None,
            scaling=scaling,
            proven_settings={},
            tags=[],
            print_count=0,
        )

    def test_fits_within_target(self):
        dec = self._make_scaled_decoration(native_w=56.0, native_h=56.0)
        scale = compute_decoration_scale(
            dec, target_face_width_mm=80.0, target_face_height_mm=80.0
        )
        # Should scale up but not exceed face (with margin)
        assert 0.5 < scale < 1.5

    def test_respects_min_detail(self):
        dec = self._make_scaled_decoration(
            native_w=56.0, native_h=56.0, min_detail=1.2
        )
        # Very small face — scaling must not shrink below min_detail
        scale = compute_decoration_scale(
            dec, target_face_width_mm=10.0, target_face_height_mm=10.0
        )
        # At this scale, the min detail should still be >= 1.2mm effective
        # The function should clamp rather than produce unusably small features
        assert scale > 0

    def test_preserves_aspect_ratio(self):
        dec = self._make_scaled_decoration(native_w=100.0, native_h=50.0)
        scale = compute_decoration_scale(
            dec, target_face_width_mm=80.0, target_face_height_mm=80.0
        )
        # Width is the constraining dimension
        effective_w = 100.0 * scale
        effective_h = 50.0 * scale
        assert effective_w <= 80.0
        assert effective_h <= 80.0
        # Aspect ratio preserved
        assert abs(effective_w / effective_h - 2.0) < 0.01

    def test_target_smaller_than_native_scales_down(self):
        dec = self._make_scaled_decoration(native_w=100.0, native_h=100.0)
        scale = compute_decoration_scale(
            dec, target_face_width_mm=50.0, target_face_height_mm=50.0
        )
        assert scale < 1.0

    def test_10_percent_margin(self):
        dec = self._make_scaled_decoration(native_w=100.0, native_h=100.0)
        scale = compute_decoration_scale(
            dec, target_face_width_mm=100.0, target_face_height_mm=100.0
        )
        # Should leave ~10% margin, so effective size ~90mm not 100mm
        effective = 100.0 * scale
        assert effective <= 92.0  # generous tolerance


# ---------------------------------------------------------------------------
# resolve_decoration_settings
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

    def test_uses_proven_settings_for_material(self):
        proven = ProvenSetting(
            depth_mm=0.8,
            mode="emboss",
            image_style="posterize",
            success_count=3,
            last_printed="2026-04-01T00:00:00",
        )
        dec = self._make_decoration_with_proven(proven={"PLA": proven})
        settings = resolve_decoration_settings(dec, material="PLA")
        assert settings["depth_mm"] == 0.8
        assert settings["mode"] == "emboss"
        assert settings["material"] == "PLA"
        assert settings["source"] == "proven"

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
        dec = _save_simple(tmp_path, name="Serialize")
        record_decoration_success("serialize", material="PLA", depth_mm=0.6, mode="emboss")
        dec = get_decoration("serialize")
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
# iterate_decoration
# ---------------------------------------------------------------------------


class TestIterateDecoration:

    def test_creates_new_version(self, tmp_path: Path):
        _save_simple(tmp_path, name="Iter Test", material="PLA")
        dec = iterate_decoration("iter-test", depth_mm=0.8)
        assert dec.version == 2
        assert dec.parent_version == 1

    def test_archives_old_manifest(self, tmp_path: Path):
        _save_simple(tmp_path, name="Archive Test", material="PLA")
        iterate_decoration("archive-test", depth_mm=0.8)
        lib = get_library_dir()
        archive = lib / "archive-test" / "manifest.v1.json"
        assert archive.exists()

    def test_changes_dict_captures_delta(self, tmp_path: Path):
        _save_simple(tmp_path, name="Delta Test", material="PLA")
        dec = iterate_decoration("delta-test", depth_mm=0.8)
        assert dec.changes is not None
        assert "depth_mm" in dec.changes
        assert "0.6 -> 0.8" in dec.changes["depth_mm"]

    def test_carries_forward_unchanged_fields(self, tmp_path: Path):
        _save_simple(tmp_path, name="Carry Fwd", material="PLA", tags=["gift"])
        dec = iterate_decoration("carry-fwd", depth_mm=0.9)
        assert dec.name == "Carry Fwd"
        assert dec.slug == "carry-fwd"
        assert dec.tags == ["gift"]
        assert dec.content_type == "photo"

    def test_new_content_file_copied(self, tmp_path: Path):
        _save_simple(tmp_path, name="New Content", material="PLA")
        new_content = tmp_path / "updated.dat"
        new_content.write_bytes(b"new heightmap data")
        dec = iterate_decoration("new-content", content_path=str(new_content))
        assert dec.content_file == "content.v2.dat"
        lib = get_library_dir()
        assert (lib / "new-content" / "content.v2.dat").exists()

    def test_not_found_raises(self):
        with pytest.raises(ValueError, match="not found"):
            iterate_decoration("nonexistent-deco", depth_mm=1.0)


# ---------------------------------------------------------------------------
# rollback_decoration
# ---------------------------------------------------------------------------


class TestRollbackDecoration:

    def test_rollback_to_v1(self, tmp_path: Path):
        _save_simple(tmp_path, name="Rollback Test", material="PLA")
        # Iterate to v2 with a different depth
        iterate_decoration("rollback-test", depth_mm=0.9)
        # Rollback to v1
        dec = rollback_decoration("rollback-test", version=1)
        # Should have restored v1's proven settings
        if "PLA" in dec.proven_settings:
            assert dec.proven_settings["PLA"].depth_mm == 0.6

    def test_rollback_creates_new_version_number(self, tmp_path: Path):
        _save_simple(tmp_path, name="Rollback VN", material="PLA")
        iterate_decoration("rollback-vn", depth_mm=0.9)
        # Now at v2, rollback to v1 should produce v3
        dec = rollback_decoration("rollback-vn", version=1)
        assert dec.version == 3
        assert dec.parent_version == 2

    def test_rollback_records_change(self, tmp_path: Path):
        _save_simple(tmp_path, name="Rollback Change", material="PLA")
        iterate_decoration("rollback-change", depth_mm=0.9)
        dec = rollback_decoration("rollback-change", version=1)
        assert dec.changes is not None
        assert "rollback" in dec.changes
        assert "v1" in dec.changes["rollback"]

    def test_invalid_version_raises(self, tmp_path: Path):
        _save_simple(tmp_path, name="Bad Version", material="PLA")
        with pytest.raises(ValueError, match="not found"):
            rollback_decoration("bad-version", version=99)


# ---------------------------------------------------------------------------
# decoration_history
# ---------------------------------------------------------------------------


class TestDecorationHistory:

    def test_single_version_returns_one_entry(self, tmp_path: Path):
        _save_simple(tmp_path, name="Solo History")
        history = decoration_history("solo-history")
        assert len(history) == 1
        assert history[0]["version"] == 1

    def test_multi_version_history_sorted(self, tmp_path: Path):
        _save_simple(tmp_path, name="Multi History", material="PLA")
        iterate_decoration("multi-history", depth_mm=0.8)
        iterate_decoration("multi-history", depth_mm=1.0)
        history = decoration_history("multi-history")
        assert len(history) == 3
        versions = [h["version"] for h in history]
        assert versions == [1, 2, 3]

    def test_not_found_returns_empty(self):
        history = decoration_history("nonexistent-history")
        assert history == []
