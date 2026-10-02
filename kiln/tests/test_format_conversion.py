"""The format-conversion receipt — every silent conversion now says so.

Until 2026-08-28 an AI provider's GLB/OBJ became an STL with only a log
line, and a decorated OBJ came back as an STL with nothing at all.  These
pin the record's shape, its honesty rules (capability, not measurement;
no invented losses for unknown pairs), and that the recorded original
really is still on disk after the conversion that named it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kiln.format_conversion import (
    FORMAT_CONVERSION_KIND,
    convert_to_stl_recorded,
    format_conversion_record,
    lost_capabilities,
)

# A minimal but real tetrahedron — enough geometry for a genuine convert.
_OBJ = (
    "v 0 0 0\nv 10 0 0\nv 10 10 0\nv 0 0 10\n"
    "f 1 2 3\nf 1 2 4\nf 2 3 4\nf 1 3 4\n"
)


class TestRecordShape:
    def test_names_both_formats_and_the_tool(self, tmp_path):
        rec = format_conversion_record(
            from_path=str(tmp_path / "scan.glb"),
            to_path=str(tmp_path / "scan.stl"),
            tool="download_generated_model",
            reason="converted to STL for slicer compatibility",
        )
        assert rec["kind"] == FORMAT_CONVERSION_KIND
        assert rec["from_format"] == "glb"
        assert rec["to_format"] == "stl"
        assert rec["tool"] == "download_generated_model"
        assert rec["converted_at"]

    def test_glb_to_stl_names_the_one_way_doors(self, tmp_path):
        rec = format_conversion_record(
            from_path=str(tmp_path / "a.glb"),
            to_path=str(tmp_path / "a.stl"),
            tool="t",
            reason="r",
        )
        assert "textures" in rec["lost_capabilities"]
        assert "materials" in rec["lost_capabilities"]

    def test_unknown_pair_claims_no_losses(self):
        # Honesty rule: the record never asserts a loss nobody established.
        assert lost_capabilities("xyz", "stl") == []
        assert lost_capabilities("glb", "xyz") == []

    def test_original_path_present_only_when_retained(self, tmp_path):
        kept = format_conversion_record(
            from_path=str(tmp_path / "a.obj"), to_path=str(tmp_path / "a.stl"),
            tool="t", reason="r", original_retained=True,
        )
        gone = format_conversion_record(
            from_path=str(tmp_path / "a.obj"), to_path=str(tmp_path / "a.stl"),
            tool="t", reason="r", original_retained=False,
        )
        assert kept["original_path"].endswith("a.obj")
        assert "original_path" not in gone


class TestConvertToStlRecorded:
    def test_converts_and_the_named_original_still_exists(self, tmp_path):
        src = tmp_path / "gen.obj"
        src.write_text(_OBJ)

        stl_path, rec = convert_to_stl_recorded(
            str(src), tool="download_generated_model",
        )

        # A real STL was written…
        out = Path(stl_path)
        assert out.suffix == ".stl" and out.stat().st_size > 0
        # …the receipt tells the story…
        assert rec["from_format"] == "obj" and rec["to_format"] == "stl"
        # …and the original it names is genuinely still on disk — the
        # whole point of naming it is that nothing deleted it.
        assert Path(rec["original_path"]) == src
        assert src.is_file()

    def test_a_bad_source_still_raises_and_leaves_no_record(self, tmp_path):
        src = tmp_path / "junk.obj"
        src.write_text("not an obj at all")
        with pytest.raises(ValueError):
            convert_to_stl_recorded(str(src), tool="t")
        # Nor a half-written STL: the folder holds what it held before.
        assert [p.name for p in tmp_path.iterdir()] == ["junk.obj"]


def _glb(path: Path, extents: tuple[float, float, float] = (40.0, 10.0, 20.0)) -> str:
    """A box written as glTF, whose own frame puts up on +Y."""
    trimesh = pytest.importorskip("trimesh")
    trimesh.Scene(trimesh.creation.box(extents=extents)).export(str(path))
    return str(path)


def _extents(path: str) -> tuple[float, ...]:
    from kiln.mesh_frame import load_mesh

    return tuple(round(float(v), 3) for v in load_mesh(path).extents)


class TestTheConversionNeverReplacesAFile:
    """A listing can ship its designer's own ``part.stl`` beside ``part.glb``.

    Until 2026-10-02 the conversion wrote ``part.stl`` whatever stood there,
    so converting the GLB destroyed the file the person had just downloaded.
    """

    def test_a_listings_own_stl_beside_the_glb_is_kept(self, tmp_path):
        trimesh = pytest.importorskip("trimesh")
        own = tmp_path / "part.stl"
        trimesh.creation.box(extents=(30.0, 30.0, 30.0)).export(str(own))
        before = own.read_bytes()
        glb = _glb(tmp_path / "part.glb")

        stl_path, rec = convert_to_stl_recorded(glb, tool="download_model")

        assert own.read_bytes() == before
        assert Path(stl_path).name == "part.glb.stl"
        assert rec["original_path"] == glb
        # The copy is the GLB's model, standing the way it was made.
        assert _extents(stl_path) == (40.0, 20.0, 10.0)

    def test_the_same_conversion_again_keeps_its_one_stl(self, tmp_path):
        glb = _glb(tmp_path / "part.glb")
        first, _ = convert_to_stl_recorded(glb, tool="t")
        again, _ = convert_to_stl_recorded(glb, tool="t")
        assert first == again == str(tmp_path / "part.stl")
        assert sorted(p.name for p in tmp_path.iterdir() if p.suffix == ".stl") == ["part.stl"]


class TestConvertOnArrival:
    """The one helper every door a file arrives through calls."""

    def test_a_glb_is_handed_on_as_the_stl_beside_it(self, tmp_path):
        from kiln.format_conversion import convert_on_arrival

        glb = _glb(tmp_path / "model.glb")
        handed_on, rec = convert_on_arrival(glb, tool="download_model")
        assert handed_on == str(tmp_path / "model.stl")
        assert (rec["from_format"], rec["tool"], rec["original_path"]) == ("glb", "download_model", glb)
        assert Path(glb).is_file()

    def test_any_other_file_is_handed_on_as_it_is(self, tmp_path):
        from kiln.format_conversion import convert_on_arrival

        obj = tmp_path / "model.obj"
        obj.write_text(_OBJ)
        assert convert_on_arrival(str(obj), tool="download_model") == (str(obj), None)
        assert not (tmp_path / "model.stl").exists()

    def test_a_generators_obj_becomes_an_stl_as_it_always_has(self, tmp_path):
        from kiln.format_conversion import convert_generated_result
        from kiln.generation.base import GenerationResult

        obj = tmp_path / "job-1.obj"
        obj.write_text(_OBJ)
        given = GenerationResult("job-1", "cloudgen", str(obj), "obj", obj.stat().st_size, "a part")

        result, rec = convert_generated_result(given, tool="download_generated_model")

        assert (result.local_path, result.format) == (str(tmp_path / "job-1.stl"), "stl")
        assert result.file_size_bytes == (tmp_path / "job-1.stl").stat().st_size
        assert rec["from_format"] == "obj"

    def test_a_generators_file_that_cannot_be_read_is_kept_as_it_came(self, tmp_path):
        from kiln.format_conversion import convert_generated_result
        from kiln.generation.base import GenerationResult

        bad = tmp_path / "job-2.glb"
        bad.write_bytes(b"glTF\x02\x00\x00\x00not a model")
        given = GenerationResult("job-2", "cloudgen", str(bad), "glb", bad.stat().st_size, "a part")

        assert convert_generated_result(given, tool="t") == (given, None)
        assert [p.name for p in tmp_path.iterdir()] == ["job-2.glb"]
