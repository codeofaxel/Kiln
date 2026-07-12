"""Tests for kiln.preview_render — the shared preview supersampling knob.

Every OpenSCAD preview surface (model_visualizer, decoration previews,
multicolor thumbnails, generation previews) routes through these two
helpers, so this is the one place their contract is pinned.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kiln.preview_render import (
    PREVIEW_SUPERSAMPLE_DEFAULT,
    PREVIEW_SUPERSAMPLE_MAX,
    downscale_png,
    effective_supersample,
    preview_supersample,
)


class TestPreviewSupersampleFactor:
    def test_default_is_two(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("KILN_PREVIEW_SUPERSAMPLE", raising=False)
        assert preview_supersample() == PREVIEW_SUPERSAMPLE_DEFAULT == 2

    def test_env_override(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("KILN_PREVIEW_SUPERSAMPLE", "3")
        assert preview_supersample() == 3

    def test_one_disables(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("KILN_PREVIEW_SUPERSAMPLE", "1")
        assert preview_supersample() == 1

    def test_clamped_low(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("KILN_PREVIEW_SUPERSAMPLE", "0")
        assert preview_supersample() == 1
        monkeypatch.setenv("KILN_PREVIEW_SUPERSAMPLE", "-5")
        assert preview_supersample() == 1

    def test_clamped_high(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("KILN_PREVIEW_SUPERSAMPLE", "99")
        assert preview_supersample() == PREVIEW_SUPERSAMPLE_MAX

    def test_garbage_falls_back_to_default(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("KILN_PREVIEW_SUPERSAMPLE", "abc")
        assert preview_supersample() == PREVIEW_SUPERSAMPLE_DEFAULT
        monkeypatch.setenv("KILN_PREVIEW_SUPERSAMPLE", "")
        assert preview_supersample() == PREVIEW_SUPERSAMPLE_DEFAULT


class TestEffectiveSupersample:
    def test_matches_factor_when_pillow_present(self, monkeypatch: pytest.MonkeyPatch):
        pytest.importorskip("PIL")
        monkeypatch.setenv("KILN_PREVIEW_SUPERSAMPLE", "2")
        assert effective_supersample() == 2

    def test_degrades_to_one_without_pillow(self, monkeypatch: pytest.MonkeyPatch):
        import builtins

        monkeypatch.setenv("KILN_PREVIEW_SUPERSAMPLE", "2")
        real_import = builtins.__import__

        def _no_pil(name, *a, **k):
            if name == "PIL" or name.startswith("PIL."):
                raise ImportError("simulated: Pillow unavailable")
            return real_import(name, *a, **k)

        monkeypatch.setattr(builtins, "__import__", _no_pil)
        assert effective_supersample() == 1


class TestDownscalePng:
    def test_resizes_in_place(self, tmp_path: Path):
        from PIL import Image

        p = tmp_path / "big.png"
        Image.new("RGB", (200, 100), (128, 128, 128)).save(p)
        assert downscale_png(str(p), 50, 25) is True
        with Image.open(p) as img:
            assert img.size == (50, 25)

    def test_bad_path_returns_false(self, tmp_path: Path):
        assert downscale_png(str(tmp_path / "nope.png"), 50, 25) is False

    def test_leaves_no_temp_file_on_failure(self, tmp_path: Path):
        # A non-image file makes Image.open raise; the temp must be cleaned.
        p = tmp_path / "notpng.png"
        p.write_bytes(b"not a real png")
        assert downscale_png(str(p), 10, 10) is False
        assert not (tmp_path / "notpng.png.ss.tmp").exists()
