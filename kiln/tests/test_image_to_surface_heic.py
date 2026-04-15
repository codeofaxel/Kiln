"""HEIC auto-detection in prepare_image_for_emboss.

iPhones default to HEIC and commonly save images with a ``.jpg`` extension
after AirDrop / Photos export.  PIL cannot read these containers and
previously dropped them silently, leaving users with photo-less STLs.

This module exercises the magic-byte detection and the clear-error path
when sips is unavailable.  The actual sips conversion is macOS-only and
covered by a separate integration test elsewhere.
"""
from __future__ import annotations

import struct
from pathlib import Path

import pytest

from kiln import image_to_surface


def _write_heic_like(path: Path, brand: bytes = b"heic") -> None:
    """Minimal ISO-BMFF ftyp box so _is_heic_container returns True."""
    with open(path, "wb") as f:
        f.write(struct.pack(">I", 24))
        f.write(b"ftyp")
        f.write(brand)
        f.write(b"\x00\x00\x00\x00")
        f.write(b"heic")
        f.write(b"mif1")


class TestIsHeicContainer:
    def test_detects_heic(self, tmp_path: Path) -> None:
        p = tmp_path / "photo.jpg"
        _write_heic_like(p, brand=b"heic")
        assert image_to_surface._is_heic_container(str(p))

    def test_detects_mif1(self, tmp_path: Path) -> None:
        p = tmp_path / "iphone.jpg"
        _write_heic_like(p, brand=b"mif1")
        assert image_to_surface._is_heic_container(str(p))

    def test_rejects_jpeg(self, tmp_path: Path) -> None:
        p = tmp_path / "real.jpg"
        p.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 20)
        assert not image_to_surface._is_heic_container(str(p))

    def test_rejects_png(self, tmp_path: Path) -> None:
        p = tmp_path / "real.png"
        p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 24)
        assert not image_to_surface._is_heic_container(str(p))

    def test_rejects_empty(self, tmp_path: Path) -> None:
        p = tmp_path / "empty.jpg"
        p.touch()
        assert not image_to_surface._is_heic_container(str(p))

    def test_rejects_missing_file(self) -> None:
        assert not image_to_surface._is_heic_container("/nonexistent/file.jpg")


class TestAutoConvertHeic:
    def test_raises_clear_error_when_sips_missing(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """When sips isn't on PATH, raise RuntimeError with a message that
        tells the user what to do — don't silently drop their photo."""
        p = tmp_path / "iphone.jpg"
        _write_heic_like(p)

        import shutil
        monkeypatch.setattr(shutil, "which", lambda _: None)

        with pytest.raises(RuntimeError, match="HEIC"):
            image_to_surface._auto_convert_heic(str(p), str(tmp_path))


class TestPrepareImageForEmbossHeicRouting:
    def test_heic_triggers_conversion_path(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """prepare_image_for_emboss should detect HEIC at entry and route
        through _auto_convert_heic before any PIL operations run."""
        p = tmp_path / "iphone.jpg"
        _write_heic_like(p)

        import shutil
        monkeypatch.setattr(shutil, "which", lambda _: None)

        # Error must originate from the HEIC branch, not from PIL failure.
        with pytest.raises(RuntimeError, match="HEIC"):
            image_to_surface.prepare_image_for_emboss(
                str(p),
                str(tmp_path),
                style="coin",
            )
