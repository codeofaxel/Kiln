"""Tests for kiln.model_visualizer."""

from __future__ import annotations

import os
import struct
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kiln.model_visualizer import (
    _ANGLE_ROTATIONS,
    _CAMERA_ANGLES,
    _BoundingBoxInfo,
    _compile_scad_for_bbox,
    _find_openscad,
    _get_bounding_box,
    _make_scad_wrapper,
    visualize_model,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_stl(tmp_path: Path) -> Path:
    """Create a minimal valid binary STL with 1 triangle."""
    stl = tmp_path / "test.stl"
    header = b"\x00" * 80
    num_triangles = struct.pack("<I", 1)
    # Normal + 3 vertices + attribute byte count
    triangle = struct.pack("<fff", 0, 0, 1)  # normal
    triangle += struct.pack("<fff", 0, 0, 0)  # v1
    triangle += struct.pack("<fff", 1, 0, 0)  # v2
    triangle += struct.pack("<fff", 0, 1, 0)  # v3
    triangle += struct.pack("<H", 0)  # attribute
    stl.write_bytes(header + num_triangles + triangle)
    return stl


@pytest.fixture
def tmp_scad(tmp_path: Path) -> Path:
    """Create a minimal SCAD file."""
    scad = tmp_path / "test.scad"
    scad.write_text("cube([10, 10, 10]);")
    return scad


# ---------------------------------------------------------------------------
# _find_openscad
# ---------------------------------------------------------------------------


def test_find_openscad_on_path():
    """Should find openscad if it's on PATH."""
    with patch("shutil.which", side_effect=lambda p: "/usr/bin/openscad" if p == "openscad" else None):
        assert _find_openscad() == "openscad"


def test_find_openscad_not_found():
    """Should raise FileNotFoundError when no openscad binary exists."""
    with patch("shutil.which", return_value=None), pytest.raises(FileNotFoundError, match="OpenSCAD not found"):
        _find_openscad()


# ---------------------------------------------------------------------------
# _make_scad_wrapper
# ---------------------------------------------------------------------------


def test_make_scad_wrapper_stl(tmp_stl: Path):
    """Should create a wrapper .scad that imports the STL."""
    wrapper = _make_scad_wrapper(str(tmp_stl))
    try:
        assert wrapper != str(tmp_stl)
        content = Path(wrapper).read_text()
        assert "import(" in content
        # The path is embedded in an OpenSCAD string literal, so
        # backslashes are escaped (``\`` -> ``\\``).  On POSIX the
        # path has none and the escaped form equals the raw path;
        # on Windows the check must use the escaped form.
        escaped = str(tmp_stl).replace("\\", "\\\\")
        assert escaped in content
    finally:
        os.unlink(wrapper)


def test_make_scad_wrapper_scad(tmp_scad: Path):
    """SCAD files should be returned directly, no wrapper."""
    result = _make_scad_wrapper(str(tmp_scad))
    assert result == str(tmp_scad)


# ---------------------------------------------------------------------------
# visualize_model — error cases
# ---------------------------------------------------------------------------


def test_visualize_file_not_found():
    result = visualize_model("/nonexistent/model.stl")
    assert result["success"] is False
    assert result["code"] == "FILE_NOT_FOUND"


def test_visualize_unsupported_format(tmp_path: Path):
    gcode = tmp_path / "test.gcode"
    gcode.write_text("G28\n")
    result = visualize_model(str(gcode))
    assert result["success"] is False
    assert result["code"] == "UNSUPPORTED_FORMAT"


def test_visualize_invalid_angles(tmp_stl: Path):
    with patch("kiln.model_visualizer._find_openscad", return_value="openscad"):
        result = visualize_model(str(tmp_stl), angles=["nonexistent_angle"])
    assert result["success"] is False
    assert result["code"] == "INVALID_ANGLES"


def test_visualize_openscad_not_found(tmp_stl: Path):
    with patch("kiln.model_visualizer._find_openscad", side_effect=FileNotFoundError("not found")):
        result = visualize_model(str(tmp_stl))
    assert result["success"] is False
    assert result["code"] == "OPENSCAD_NOT_FOUND"


# ---------------------------------------------------------------------------
# visualize_model — successful rendering (mocked OpenSCAD)
# ---------------------------------------------------------------------------


def test_visualize_all_angles(tmp_stl: Path, tmp_path: Path):
    """Mock OpenSCAD to produce dummy PNGs and verify all 6 angles are rendered."""
    output_dir = str(tmp_path / "output")

    def mock_run(cmd, **kwargs):
        # Find the -o argument and create a dummy PNG there
        for i, arg in enumerate(cmd):
            if arg == "-o" and i + 1 < len(cmd):
                Path(cmd[i + 1]).write_bytes(b"fake-png-data")
        mock = MagicMock()
        mock.returncode = 0
        return mock

    with patch("kiln.model_visualizer._find_openscad", return_value="openscad"), \
         patch("subprocess.run", side_effect=mock_run):
        result = visualize_model(str(tmp_stl), output_dir=output_dir)

    assert result["success"] is True
    assert result["rendered"] == 7
    assert result["failed"] == 0
    assert len(result["views"]) == 7

    angle_names = {v["angle"] for v in result["views"]}
    # `wedge_iso` is the pitched-up 3/4 view added 2026-05-03 for tilted-
    # canvas products (nameplates, awards, desk signs).  Default isometric
    # (rz=25) flattens those; wedge_iso (rz=35) shows the slope.
    assert angle_names == {
        "isometric", "wedge_iso", "front", "right", "top", "bottom", "back",
    }

    for view in result["views"]:
        assert view["path"] is not None
        assert view["description"]


def test_visualize_subset_angles(tmp_stl: Path, tmp_path: Path):
    """Should only render requested angles."""
    output_dir = str(tmp_path / "output")

    def mock_run(cmd, **kwargs):
        for i, arg in enumerate(cmd):
            if arg == "-o" and i + 1 < len(cmd):
                Path(cmd[i + 1]).write_bytes(b"fake-png-data")
        mock = MagicMock()
        mock.returncode = 0
        return mock

    with patch("kiln.model_visualizer._find_openscad", return_value="openscad"), \
         patch("subprocess.run", side_effect=mock_run):
        result = visualize_model(
            str(tmp_stl), angles=["top", "bottom"], output_dir=output_dir,
        )

    assert result["success"] is True
    assert result["rendered"] == 2
    assert len(result["views"]) == 2


def test_visualize_partial_failure(tmp_stl: Path, tmp_path: Path):
    """If some angles fail, should still return partial success."""
    output_dir = str(tmp_path / "output")
    call_count = 0

    def mock_run(cmd, **kwargs):
        nonlocal call_count
        call_count += 1
        mock = MagicMock()
        if call_count <= 3:
            # First 3 succeed
            for i, arg in enumerate(cmd):
                if arg == "-o" and i + 1 < len(cmd):
                    Path(cmd[i + 1]).write_bytes(b"fake-png-data")
            mock.returncode = 0
        else:
            # Rest fail
            mock.returncode = 1
            mock.stderr = "render error"
        return mock

    with patch("kiln.model_visualizer._find_openscad", return_value="openscad"), \
         patch("subprocess.run", side_effect=mock_run):
        result = visualize_model(str(tmp_stl), output_dir=output_dir)

    assert result["success"] is True  # partial success
    # First 3 mock-succeed, the remaining (now 4 with wedge_iso added)
    # mock-fail.  Total angles = 7.
    assert result["rendered"] == 3
    assert result["failed"] == 4


# ---------------------------------------------------------------------------
# Camera angles config
# ---------------------------------------------------------------------------


def test_camera_angles_have_seven_entries():
    """Six cardinal angles + wedge_iso (added 2026-05-03 for tilted-canvas
    products like nameplates / awards / desk signs).  Bumping this count
    is mandatory whenever a new preset rotation lands in
    :data:`_ANGLE_ROTATIONS`."""
    assert len(_CAMERA_ANGLES) == 7


def test_camera_angles_have_required_fields():
    for label, description in _CAMERA_ANGLES:
        assert isinstance(label, str)
        assert isinstance(description, str)
        assert label  # non-empty
        assert label in _ANGLE_ROTATIONS


# ---------------------------------------------------------------------------
# _get_bounding_box — pure SCAD path
# ---------------------------------------------------------------------------


class TestGetBoundingBoxPureScad:
    """_get_bounding_box compiles pure SCAD to STL for bbox measurement."""

    def test_pure_scad_uses_compile_path(self, tmp_scad: Path, tmp_stl: Path):
        """A pure parametric SCAD file (no import()) triggers compilation."""
        with patch(
            "kiln.model_visualizer._compile_scad_for_bbox",
            return_value=str(tmp_stl),
        ) as mock_compile:
            result = _get_bounding_box(str(tmp_scad))
        mock_compile.assert_called_once_with(str(tmp_scad))
        assert isinstance(result, _BoundingBoxInfo)
        assert result.distance > 0

    def test_scad_with_import_skips_compile(self, tmp_path: Path, tmp_stl: Path):
        """A SCAD file that uses import() reads the STL directly, no compile."""
        scad = tmp_path / "wrapper.scad"
        scad.write_text(f'import("{tmp_stl}");')
        with patch("kiln.model_visualizer._compile_scad_for_bbox") as mock_compile:
            result = _get_bounding_box(str(scad))
        mock_compile.assert_not_called()
        assert isinstance(result, _BoundingBoxInfo)

    def test_compile_failure_returns_default(self, tmp_scad: Path):
        """If compilation fails, fall back to default bbox (no crash)."""
        with patch(
            "kiln.model_visualizer._compile_scad_for_bbox",
            return_value=None,
        ):
            result = _get_bounding_box(str(tmp_scad))
        assert result.distance == 250  # _DEFAULT_DISTANCE


# ---------------------------------------------------------------------------
# _compile_scad_for_bbox
# ---------------------------------------------------------------------------


class TestCompileScadForBbox:
    """_compile_scad_for_bbox wraps OpenSCAD subprocess correctly."""

    def test_returns_stl_path_on_success(self, tmp_scad: Path, tmp_path: Path):
        """Returns a non-None path when OpenSCAD exits 0 and produces output."""
        fake_stl = tmp_path / "fake.stl"
        # Write > 84 bytes so size check passes.
        fake_stl.write_bytes(b"\x00" * 200)

        def _fake_mkstemp(suffix="", prefix="", **_kw):
            import os
            fd = os.open(str(fake_stl), os.O_WRONLY | os.O_CREAT)
            return fd, str(fake_stl)

        mock_result = MagicMock()
        mock_result.returncode = 0

        with patch("kiln.model_visualizer._find_openscad", return_value="/usr/bin/openscad"), \
             patch("tempfile.mkstemp", side_effect=_fake_mkstemp), \
             patch("subprocess.run", return_value=mock_result):
            path = _compile_scad_for_bbox(str(tmp_scad))

        assert path == str(fake_stl)

    def test_returns_none_when_openscad_missing(self, tmp_scad: Path):
        """Returns None if OpenSCAD binary is not found."""
        with patch(
            "kiln.model_visualizer._find_openscad",
            side_effect=FileNotFoundError("not found"),
        ):
            assert _compile_scad_for_bbox(str(tmp_scad)) is None

    def test_returns_none_on_nonzero_exit(self, tmp_scad: Path):
        """Returns None if OpenSCAD exits non-zero (compile error in SCAD)."""
        mock_result = MagicMock()
        mock_result.returncode = 1

        with patch("kiln.model_visualizer._find_openscad", return_value="/usr/bin/openscad"), \
             patch("subprocess.run", return_value=mock_result):
            assert _compile_scad_for_bbox(str(tmp_scad)) is None

    def test_returns_none_on_timeout(self, tmp_scad: Path):
        """Returns None if OpenSCAD times out."""
        with patch("kiln.model_visualizer._find_openscad", return_value="/usr/bin/openscad"), \
             patch("subprocess.run", side_effect=subprocess.TimeoutExpired("openscad", 30)):
            assert _compile_scad_for_bbox(str(tmp_scad)) is None


# ---------------------------------------------------------------------------
# Bambu-wrapped 3MF thumbnail extraction
# ---------------------------------------------------------------------------
#
# A Bambu-wrapped 3MF has ``Metadata/plate_1.gcode`` as the real payload
# and a 1-2KB placeholder in ``3D/3dmodel.model``.  OpenSCAD renders
# such files as an empty black frame, which breaks the preview gate for
# every Bambu print.  ``visualize_model`` short-circuits that path by
# surfacing the slicer's embedded thumbnails — tested here with
# synthetic fixtures because we don't want to depend on a real sliced
# 3MF bundled with the tests.


def _make_bambu_wrapped_3mf(
    path: Path,
    *,
    include_middle_thumb: bool = True,
    include_plate_png: bool = True,
    include_top_png: bool = True,
    placeholder_model_size: int = 1300,
    gcode_size: int = 2048,
) -> Path:
    """Build a synthetic Bambu-wrapped 3MF for testing."""
    import zipfile as _zipfile
    with _zipfile.ZipFile(path, "w") as zf:
        zf.writestr("3D/3dmodel.model", b"X" * placeholder_model_size)
        zf.writestr("Metadata/plate_1.gcode", b"G28\n" + b";" * gcode_size)
        # 8-byte PNG magic makes an easy "valid PNG" stub.
        png_stub = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
        if include_middle_thumb:
            zf.writestr("Auxiliaries/.thumbnails/thumbnail_middle.png", png_stub)
            zf.writestr("Auxiliaries/.thumbnails/thumbnail_small.png", png_stub)
        if include_plate_png:
            zf.writestr("Metadata/plate_1.png", png_stub)
        if include_top_png:
            zf.writestr("Metadata/top_1.png", png_stub)
    return path


@pytest.fixture
def bambu_3mf(tmp_path: Path) -> Path:
    return _make_bambu_wrapped_3mf(tmp_path / "coaster_bedcentered.3mf")


class TestBambuWrapped3MFDetection:
    def test_recognises_bambu_wrapper(self, bambu_3mf: Path):
        from kiln.model_visualizer import _is_bambu_wrapped_3mf
        assert _is_bambu_wrapped_3mf(str(bambu_3mf)) is True

    def test_plain_3mf_not_flagged(self, tmp_path: Path):
        """A 3MF without plate_1.gcode is not a Bambu wrapper."""
        import zipfile as _zipfile
        from kiln.model_visualizer import _is_bambu_wrapped_3mf
        path = tmp_path / "geom.3mf"
        with _zipfile.ZipFile(path, "w") as zf:
            zf.writestr("3D/3dmodel.model", b"<model>" + b"X" * 10000 + b"</model>")
        assert _is_bambu_wrapped_3mf(str(path)) is False

    def test_real_mesh_wrapper_not_flagged(self, tmp_path: Path):
        """An archive with a real-sized 3dmodel.model is not a wrapper
        even if it happens to contain a plate_1.gcode for some reason."""
        from kiln.model_visualizer import _is_bambu_wrapped_3mf
        path = _make_bambu_wrapped_3mf(
            tmp_path / "mesh.3mf",
            placeholder_model_size=500_000,
        )
        assert _is_bambu_wrapped_3mf(str(path)) is False

    def test_corrupt_archive_not_flagged(self, tmp_path: Path):
        from kiln.model_visualizer import _is_bambu_wrapped_3mf
        path = tmp_path / "corrupt.3mf"
        path.write_bytes(b"not a zip")
        assert _is_bambu_wrapped_3mf(str(path)) is False


class TestBambuThumbnailExtraction:
    def test_extracts_all_thumbnails(self, bambu_3mf: Path, tmp_path: Path):
        from kiln.model_visualizer import _extract_bambu_thumbnails
        out_dir = tmp_path / "out"
        views = _extract_bambu_thumbnails(str(bambu_3mf), str(out_dir))
        assert len(views) >= 3  # at least middle/top/plate
        for v in views:
            assert "path" in v
            assert os.path.isfile(v["path"])
            assert v["source"] == "bambu_3mf_thumbnail"

    def test_filter_by_angles(self, bambu_3mf: Path, tmp_path: Path):
        from kiln.model_visualizer import _extract_bambu_thumbnails
        views = _extract_bambu_thumbnails(
            str(bambu_3mf), str(tmp_path / "out"), angles=["top"],
        )
        assert len(views) == 1
        assert views[0]["angle"] == "top"

    def test_missing_thumbnail_skipped_not_faked(self, tmp_path: Path):
        """If the archive lacks a thumbnail, no faked view is produced."""
        from kiln.model_visualizer import _extract_bambu_thumbnails
        path = _make_bambu_wrapped_3mf(
            tmp_path / "no_plate.3mf",
            include_plate_png=False,
            include_top_png=False,
        )
        views = _extract_bambu_thumbnails(str(path), str(tmp_path / "out"))
        angles = {v["angle"] for v in views}
        assert "top" not in angles
        assert "front" not in angles


class TestVisualizeModelOnBambu3MF:
    def test_visualize_returns_thumbnails_without_openscad(self, bambu_3mf: Path, tmp_path: Path):
        """Bambu-wrapped 3MFs should return a success response backed
        by the slicer's own thumbnails, never falling through to an
        OpenSCAD render of the empty placeholder mesh (which would
        produce a black frame)."""
        result = visualize_model(
            str(bambu_3mf), output_dir=str(tmp_path / "out"),
        )
        assert result["success"] is True
        assert result.get("source") == "bambu_3mf_thumbnails"
        assert result["rendered"] >= 1
        for v in result["views"]:
            assert os.path.isfile(v["path"])


# ---------------------------------------------------------------------------
# Preview supersampling (SSAA)
# ---------------------------------------------------------------------------


class TestPreviewSupersample:
    """OpenSCAD preview edges are jagged at native resolution; the engine
    renders oversized and Lanczos-downscales for crisp anti-aliased output.

    Unit tests for the shared ``preview_supersample`` / ``downscale_png``
    primitives live in ``test_preview_render.py``; these assert
    ``visualize_model`` actually wires them into its OpenSCAD invocation."""

    def test_render_uses_supersampled_imgsize(self, tmp_stl: Path, tmp_path: Path,
                                              monkeypatch: pytest.MonkeyPatch):
        """With the default 2x factor, OpenSCAD is invoked at 2x the
        requested size, and the returned PNG is downscaled back to it."""
        from PIL import Image

        monkeypatch.delenv("KILN_PREVIEW_SUPERSAMPLE", raising=False)
        captured: list[list[str]] = []

        def mock_run(cmd, **kwargs):
            captured.append(cmd)
            # Honor the requested --imgsize so the real downscale runs.
            w = h = None
            for arg in cmd:
                if arg.startswith("--imgsize="):
                    w, h = (int(x) for x in arg.split("=", 1)[1].split(","))
            for i, arg in enumerate(cmd):
                if arg == "-o" and i + 1 < len(cmd):
                    Image.new("RGB", (w, h), (170, 170, 170)).save(cmd[i + 1])
            mock = MagicMock()
            mock.returncode = 0
            return mock

        with patch("kiln.model_visualizer._find_openscad", return_value="openscad"), \
             patch("subprocess.run", side_effect=mock_run):
            result = visualize_model(
                str(tmp_stl), angles=["isometric"],
                output_dir=str(tmp_path / "out"), width=800, height=600,
            )

        assert result["success"] is True
        imgsize_args = [a for c in captured for a in c if a.startswith("--imgsize=")]
        assert imgsize_args == ["--imgsize=1600,1200"], imgsize_args
        # Final artifact honors the requested size, not the oversized render.
        with Image.open(result["views"][0]["path"]) as img:
            assert img.size == (800, 600)

    def test_render_native_imgsize_when_disabled(self, tmp_stl: Path, tmp_path: Path,
                                                 monkeypatch: pytest.MonkeyPatch):
        from PIL import Image

        monkeypatch.setenv("KILN_PREVIEW_SUPERSAMPLE", "1")
        captured: list[list[str]] = []

        def mock_run(cmd, **kwargs):
            captured.append(cmd)
            for i, arg in enumerate(cmd):
                if arg == "-o" and i + 1 < len(cmd):
                    Image.new("RGB", (800, 600), (170, 170, 170)).save(cmd[i + 1])
            mock = MagicMock()
            mock.returncode = 0
            return mock

        with patch("kiln.model_visualizer._find_openscad", return_value="openscad"), \
             patch("subprocess.run", side_effect=mock_run):
            result = visualize_model(
                str(tmp_stl), angles=["isometric"],
                output_dir=str(tmp_path / "out"), width=800, height=600,
            )

        assert result["success"] is True
        imgsize_args = [a for c in captured for a in c if a.startswith("--imgsize=")]
        assert imgsize_args == ["--imgsize=800,600"], imgsize_args
