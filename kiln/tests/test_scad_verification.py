"""Tests for the pre-print SCAD verifier.

Locks in the invariants the original Fig-the-dog session surfaced:

* Bottom-face text() without mirror([1, 0, 0]) is an ERROR — the
  carve prints reversed when the part is flipped for reading.
* Bottom-face text() with extrude depth < 1mm is a WARNING — first-
  layer squish partially fills shallow recesses on FDM PLA.
* Non-bottom-face text (interior cavity, top of coaster, etc.) is
  unaffected — no false positives.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from kiln.scad_verification import verify_flip_readability


class TestFlipReadabilityBottomTextDetection:
    def test_bottom_text_without_mirror_is_error(self, tmp_path: Path) -> None:
        scad = tmp_path / "buggy.scad"
        scad.write_text(
            "difference() {\n"
            "  cube([100, 100, 5]);\n"
            "  translate([0, 0, -0.01])\n"
            "    linear_extrude(height=0.9)\n"
            '      text("HELLO", size=10);\n'
            "}\n"
        )
        report = verify_flip_readability(str(scad))
        assert report["ok"] is False
        errors = [i for i in report["issues"] if i["severity"] == "error"]
        assert errors
        assert errors[0]["code"] == "BOTTOM_TEXT_NOT_MIRRORED"
        assert "HELLO" in errors[0]["message"]
        assert "mirror([1, 0, 0])" in errors[0]["suggested_fix"]

    def test_bottom_text_with_mirror_is_ok(self, tmp_path: Path) -> None:
        scad = tmp_path / "correct.scad"
        scad.write_text(
            "difference() {\n"
            "  cube([100, 100, 5]);\n"
            "  translate([0, 0, -0.01])\n"
            "    mirror([1, 0, 0])\n"
            "    linear_extrude(height=1.21)\n"
            '      text("Happy birthday", size=8);\n'
            "}\n"
        )
        report = verify_flip_readability(str(scad))
        errors = [i for i in report["issues"] if i["severity"] == "error"]
        assert not errors, report["issues"]
        assert report["ok"] is True

    def test_text_on_top_face_is_unaffected(self, tmp_path: Path) -> None:
        scad = tmp_path / "top.scad"
        scad.write_text(
            "difference() {\n"
            "  cube([100, 100, 7]);\n"
            "  translate([0, 0, 6.2])\n"       # Z well above 0 → top face
            "    linear_extrude(height=0.9)\n"
            '      text("KILN", size=10);\n'
            "}\n"
        )
        report = verify_flip_readability(str(scad))
        assert report["ok"] is True
        assert not report["issues"]


class TestFlipReadabilityDepthWarnings:
    def test_shallow_bottom_text_warns(self, tmp_path: Path) -> None:
        scad = tmp_path / "shallow.scad"
        scad.write_text(
            "difference() {\n"
            "  cube([100, 100, 5]);\n"
            "  translate([0, 0, -0.01])\n"
            "    mirror([1, 0, 0])\n"
            "    linear_extrude(height=0.5)\n"   # 0.48mm effective — too shallow
            '      text("Dim", size=10);\n'
            "}\n"
        )
        report = verify_flip_readability(str(scad))
        warnings = [i for i in report["issues"] if i["severity"] == "warning"]
        assert warnings
        assert warnings[0]["code"] == "BOTTOM_TEXT_SHALLOW"
        assert "1.0mm" in warnings[0]["message"]

    def test_deep_bottom_text_passes_depth_check(self, tmp_path: Path) -> None:
        scad = tmp_path / "deep.scad"
        scad.write_text(
            "difference() {\n"
            "  cube([100, 100, 5]);\n"
            "  translate([0, 0, -0.01])\n"
            "    mirror([1, 0, 0])\n"
            "    linear_extrude(height=1.22)\n"  # 1.2mm effective → ok
            '      text("Bold", size=10);\n'
            "}\n"
        )
        report = verify_flip_readability(str(scad))
        depth_warnings = [
            i for i in report["issues"]
            if i["code"] == "BOTTOM_TEXT_SHALLOW"
        ]
        assert not depth_warnings


class TestFlipReadabilityMultipleEntries:
    def test_counts_every_text_call(self, tmp_path: Path) -> None:
        scad = tmp_path / "multi.scad"
        scad.write_text(
            "difference() {\n"
            "  cube([100, 100, 5]);\n"
            "  translate([0, 0, -0.01])\n"
            "    mirror([1, 0, 0])\n"
            "    linear_extrude(height=1.21)\n"
            '      text("Line1", size=8);\n'
            "  translate([0, 20, -0.01])\n"
            "    mirror([1, 0, 0])\n"
            "    linear_extrude(height=1.21)\n"
            '      text("Line2", size=8);\n'
            "  translate([0, 0, 4.5])\n"
            "    linear_extrude(height=0.5)\n"
            '      text("TopOK", size=6);\n'
            "}\n"
        )
        report = verify_flip_readability(str(scad))
        # 3 text() calls total
        assert report["text_entries_checked"] == 3
        # No errors (both bottom-face entries properly mirrored)
        assert report["ok"] is True


class TestFlipReadabilityFileHandling:
    def test_missing_file_returns_error(self) -> None:
        report = verify_flip_readability("/nonexistent/file.scad")
        assert report["ok"] is False
        assert any(i["code"] == "SCAD_NOT_FOUND" for i in report["issues"])

    def test_empty_scad_is_ok(self, tmp_path: Path) -> None:
        scad = tmp_path / "empty.scad"
        scad.write_text("// empty\n")
        report = verify_flip_readability(str(scad))
        assert report["ok"] is True
        assert report["text_entries_checked"] == 0


class TestIntegrationWithRealJewelryTraySCAD:
    """Run the verifier against the SCAD that generate_jewelry_tray
    emits to make sure our fixed generator produces clean reports.
    """

    def test_post_fix_jewelry_tray_scad_is_clean(self, tmp_path: Path) -> None:
        scad = tmp_path / "tray.scad"
        # Synthetic snapshot of the post-fix jewelry_tray_tools output.
        scad.write_text(
            "$fn=48;\n"
            "difference() {\n"
            "  hull() {\n"
            "    translate([72, 72, 0]) cylinder(r=8, h=18);\n"
            "  }\n"
            "  translate([0, 0, 2.4]) hull() { cylinder(r=5.6, h=15.6); }\n"
            "  // Exterior bottom text deboss (mirrored X for flip-reading)\n"
            "  translate([0, 0, -0.01])\n"
            "    mirror([1, 0, 0])\n"
            "    linear_extrude(height=1.21)\n"
            '      text("Happy birthday Damian 4/14/26", size=7.5,\n'
            '           halign="center", valign="center",\n'
            '           font="Liberation Sans:style=Bold");\n'
            "}\n"
        )
        report = verify_flip_readability(str(scad))
        assert report["ok"] is True
        assert not report["errors"]
        assert not report["warnings"]
        assert report["text_entries_checked"] == 1

    def test_pre_fix_jewelry_tray_scad_flagged(self, tmp_path: Path) -> None:
        # The OLD (buggy) output — no mirror, 0.81mm depth
        scad = tmp_path / "tray_buggy.scad"
        scad.write_text(
            "difference() {\n"
            "  cube([160, 160, 18]);\n"
            "  translate([0, 0, -0.01])\n"
            "    linear_extrude(height=0.81)\n"
            '      text("Happy birthday Damian 4/14/26", size=7.5,\n'
            '           halign="center", valign="center");\n'
            "}\n"
        )
        report = verify_flip_readability(str(scad))
        assert report["ok"] is False
        codes = {i["code"] for i in report["issues"]}
        assert "BOTTOM_TEXT_NOT_MIRRORED" in codes
        assert "BOTTOM_TEXT_SHALLOW" in codes
