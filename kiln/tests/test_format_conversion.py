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
