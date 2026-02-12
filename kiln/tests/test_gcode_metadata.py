"""Tests for the G-code metadata extraction module (kiln.gcode_metadata).

Covers:
    - PrusaSlicer / OrcaSlicer / BambuStudio comment patterns
    - Cura comment patterns
    - Simplify3D comment patterns
    - Time string parsing (all formats)
    - Material name normalisation
    - Temperature fallback from M-commands
    - PrinterFile enrichment
    - Edge cases (empty, binary, no metadata, huge header)
"""

from __future__ import annotations

import pytest

from kiln.gcode_metadata import (
    GCodeMetadata,
    extract_metadata,
    extract_metadata_from_content,
    enrich_printer_file,
    _parse_time_string,
    _normalize_material,
)
from kiln.printers.base import PrinterFile


# ===================================================================
# GCodeMetadata dataclass
# ===================================================================

class TestGCodeMetadataDataclass:
    """Verify the metadata dataclass defaults and to_dict behaviour."""

    def test_defaults_are_none(self) -> None:
        meta = GCodeMetadata()
        assert meta.material is None
        assert meta.estimated_time_seconds is None
        assert meta.tool_temp is None
        assert meta.bed_temp is None
        assert meta.slicer is None
        assert meta.layer_height is None
        assert meta.filament_used_mm is None
        assert meta.printer_model is None

    def test_to_dict_omits_none(self) -> None:
        meta = GCodeMetadata(material="PLA", tool_temp=210.0)
        d = meta.to_dict()
        assert d == {"material": "PLA", "tool_temp": 210.0}
        assert "bed_temp" not in d
        assert "slicer" not in d

    def test_to_dict_full(self) -> None:
        meta = GCodeMetadata(
            material="PETG",
            estimated_time_seconds=3600,
            tool_temp=240.0,
            bed_temp=80.0,
            slicer="PrusaSlicer 2.7.0",
            layer_height=0.2,
            filament_used_mm=4523.45,
            printer_model="Ender 3",
        )
        d = meta.to_dict()
        assert len(d) == 8
        assert d["material"] == "PETG"
        assert d["estimated_time_seconds"] == 3600

    def test_independent_instances(self) -> None:
        m1 = GCodeMetadata()
        m2 = GCodeMetadata()
        m1.material = "PLA"
        assert m2.material is None


# ===================================================================
# Time string parsing
# ===================================================================

class TestTimeStringParsing:
    """Test all time format variants."""

    def test_prusa_format_hms(self) -> None:
        assert _parse_time_string("1h 42m 30s") == 6150

    def test_prusa_format_hm(self) -> None:
        assert _parse_time_string("2h 30m") == 9000

    def test_prusa_format_ms(self) -> None:
        assert _parse_time_string("42m 30s") == 2550

    def test_prusa_format_seconds_only(self) -> None:
        assert _parse_time_string("30s") == 30

    def test_prusa_format_hours_only(self) -> None:
        assert _parse_time_string("2h") == 7200

    def test_prusa_format_with_days(self) -> None:
        assert _parse_time_string("1d 2h 30m 15s") == 95415

    def test_cura_raw_seconds(self) -> None:
        assert _parse_time_string("6150") == 6150

    def test_cura_raw_seconds_zero(self) -> None:
        assert _parse_time_string("0") == 0

    def test_simplify3d_format(self) -> None:
        assert _parse_time_string("1 hours 42 minutes") == 6120

    def test_simplify3d_singular(self) -> None:
        assert _parse_time_string("1 hour 1 minute") == 3660

    def test_empty_string(self) -> None:
        assert _parse_time_string("") is None

    def test_whitespace_only(self) -> None:
        assert _parse_time_string("   ") is None

    def test_garbage_string(self) -> None:
        assert _parse_time_string("not a time") is None

    def test_large_value(self) -> None:
        assert _parse_time_string("100h") == 360000

    def test_leading_trailing_whitespace(self) -> None:
        assert _parse_time_string("  1h 30m  ") == 5400


# ===================================================================
# Material normalisation
# ===================================================================

class TestMaterialExtraction:
    """Test material name normalisation."""

    def test_simple_uppercase(self) -> None:
        assert _normalize_material("PLA") == "PLA"

    def test_lowercase_normalised(self) -> None:
        assert _normalize_material("pla") == "PLA"

    def test_mixed_case(self) -> None:
        assert _normalize_material("Petg") == "PETG"

    def test_whitespace_stripped(self) -> None:
        assert _normalize_material("  ABS  ") == "ABS"

    def test_full_name_alias(self) -> None:
        assert _normalize_material("Polylactic Acid") == "PLA"

    def test_tpu_alias(self) -> None:
        assert _normalize_material("Thermoplastic Polyurethane") == "TPU"

    def test_unknown_material_passthrough(self) -> None:
        assert _normalize_material("Nylon-CF") == "NYLON-CF"


# ===================================================================
# PrusaSlicer / OrcaSlicer / BambuStudio parsing
# ===================================================================

class TestExtractMetadataPrusaSlicer:
    """Test parsing from PrusaSlicer-style G-code headers."""

    def test_full_prusa_header(self) -> None:
        content = (
            "; generated by PrusaSlicer 2.7.0\n"
            "; filament_type = PLA\n"
            "; estimated printing time (normal mode) = 1h 42m 30s\n"
            "; temperature = 210\n"
            "; bed_temperature = 60\n"
            "; layer_height = 0.2\n"
            "; filament used [mm] = 4523.45\n"
            "; printer_model = Original Prusa i3 MK3S\n"
            "G28\n"
            "M104 S210\n"
        )
        meta = extract_metadata_from_content(content)
        assert meta.material == "PLA"
        assert meta.estimated_time_seconds == 6150
        assert meta.tool_temp == 210.0
        assert meta.bed_temp == 60.0
        assert meta.slicer == "PrusaSlicer 2.7.0"
        assert meta.layer_height == pytest.approx(0.2)
        assert meta.filament_used_mm == pytest.approx(4523.45)
        assert meta.printer_model == "Original Prusa i3 MK3S"

    def test_orcaslicer_header(self) -> None:
        content = (
            "; generated by OrcaSlicer 1.8.0\n"
            "; filament_type = PETG\n"
            "; estimated printing time (normal mode) = 3h 15m\n"
            "; nozzle_temperature = 240\n"
            "; bed_temperature = 80\n"
            "; layer_height = 0.15\n"
        )
        meta = extract_metadata_from_content(content)
        assert meta.material == "PETG"
        assert meta.estimated_time_seconds == 11700
        assert meta.tool_temp == 240.0
        assert meta.bed_temp == 80.0
        assert meta.slicer == "OrcaSlicer 1.8.0"
        assert meta.layer_height == pytest.approx(0.15)

    def test_partial_prusa_header(self) -> None:
        content = (
            "; filament_type = ABS\n"
            "; layer_height = 0.3\n"
        )
        meta = extract_metadata_from_content(content)
        assert meta.material == "ABS"
        assert meta.layer_height == pytest.approx(0.3)
        assert meta.estimated_time_seconds is None
        assert meta.tool_temp is None


# ===================================================================
# Cura parsing
# ===================================================================

class TestExtractMetadataCura:
    """Test parsing from Cura-style G-code headers."""

    def test_full_cura_header(self) -> None:
        content = (
            ";Generated with Cura_SteamEngine 5.5.0\n"
            ";MATERIAL:PLA\n"
            ";TIME:6150\n"
            ";Filament used: 4.523m\n"
            ";Layer height: 0.2\n"
            ";MACHINE_TYPE:Ender-3 V2\n"
        )
        meta = extract_metadata_from_content(content)
        assert meta.material == "PLA"
        assert meta.estimated_time_seconds == 6150
        assert meta.filament_used_mm == pytest.approx(4523.0)
        assert meta.layer_height == pytest.approx(0.2)
        assert meta.slicer == "Cura_SteamEngine 5.5.0"
        assert meta.printer_model == "Ender-3 V2"

    def test_cura_filament_in_mm(self) -> None:
        content = ";Filament used: 4523.0mm\n"
        meta = extract_metadata_from_content(content)
        assert meta.filament_used_mm == pytest.approx(4523.0)

    def test_cura_filament_in_metres(self) -> None:
        content = ";Filament used: 4.523m\n"
        meta = extract_metadata_from_content(content)
        assert meta.filament_used_mm == pytest.approx(4523.0)


# ===================================================================
# Simplify3D parsing
# ===================================================================

class TestExtractMetadataSimplify3D:
    """Test parsing from Simplify3D-style G-code headers."""

    def test_full_s3d_header(self) -> None:
        content = (
            "; Simplify3D(R) Version 5.0\n"
            "; extruder0Temp,210\n"
            "; platformTemp,60\n"
            "; Build time: 1 hours 42 minutes\n"
            "; Filament length: 4523.4 mm\n"
            "; layerHeight,0.20\n"
        )
        meta = extract_metadata_from_content(content)
        assert meta.tool_temp == 210.0
        assert meta.bed_temp == 60.0
        assert meta.estimated_time_seconds == 6120
        assert meta.filament_used_mm == pytest.approx(4523.4)
        assert meta.layer_height == pytest.approx(0.2)
        assert meta.slicer == "5.0"

    def test_s3d_filament_in_metres(self) -> None:
        content = "; Filament length: 4.523 m\n"
        meta = extract_metadata_from_content(content)
        assert meta.filament_used_mm == pytest.approx(4523.0)


# ===================================================================
# Temperature fallback from M-commands
# ===================================================================

class TestTemperatureFallback:
    """Test M-command fallback when no slicer comments provide temps."""

    def test_m104_fallback(self) -> None:
        content = "G28\nM104 S215\nM140 S65\nG1 X10\n"
        meta = extract_metadata_from_content(content)
        assert meta.tool_temp == 215.0
        assert meta.bed_temp == 65.0

    def test_m109_fallback(self) -> None:
        content = "G28\nM109 S200\n"
        meta = extract_metadata_from_content(content)
        assert meta.tool_temp == 200.0

    def test_m190_fallback(self) -> None:
        content = "G28\nM190 S70\n"
        meta = extract_metadata_from_content(content)
        assert meta.bed_temp == 70.0

    def test_comment_temps_override_mcommands(self) -> None:
        content = (
            "; temperature = 210\n"
            "; bed_temperature = 60\n"
            "M104 S220\n"
            "M140 S70\n"
        )
        meta = extract_metadata_from_content(content)
        # Comment temps take priority
        assert meta.tool_temp == 210.0
        assert meta.bed_temp == 60.0

    def test_m104_s0_ignored(self) -> None:
        """M104 S0 (heater off) should not be used as fallback."""
        content = "M104 S0\nM104 S200\n"
        meta = extract_metadata_from_content(content)
        assert meta.tool_temp == 200.0

    def test_m140_s0_ignored(self) -> None:
        """M140 S0 (bed off) should not be used as fallback."""
        content = "M140 S0\nM140 S60\n"
        meta = extract_metadata_from_content(content)
        assert meta.bed_temp == 60.0

    def test_no_temps_at_all(self) -> None:
        content = "G28\nG1 X10 Y10\n"
        meta = extract_metadata_from_content(content)
        assert meta.tool_temp is None
        assert meta.bed_temp is None


# ===================================================================
# enrich_printer_file
# ===================================================================

class TestEnrichPrinterFile:
    """Test the enrichment function that mutates a PrinterFile."""

    def test_basic_enrichment(self) -> None:
        pf = PrinterFile(name="test.gcode", path="/test.gcode")
        content = (
            "; filament_type = PLA\n"
            "; temperature = 210\n"
            "; bed_temperature = 60\n"
            "; layer_height = 0.2\n"
        )
        enrich_printer_file(pf, file_content=content)
        assert pf.material == "PLA"
        assert pf.tool_temp == 210.0
        assert pf.bed_temp == 60.0
        assert pf.layer_height == pytest.approx(0.2)

    def test_no_content_is_noop(self) -> None:
        pf = PrinterFile(name="test.gcode", path="/test.gcode")
        enrich_printer_file(pf, file_content=None)
        assert pf.material is None
        assert pf.tool_temp is None

    def test_does_not_overwrite_existing(self) -> None:
        pf = PrinterFile(
            name="test.gcode", path="/test.gcode",
            material="PETG", tool_temp=240.0,
        )
        content = "; filament_type = PLA\n; temperature = 210\n"
        enrich_printer_file(pf, file_content=content)
        # Existing values should NOT be overwritten
        assert pf.material == "PETG"
        assert pf.tool_temp == 240.0

    def test_empty_content_is_safe(self) -> None:
        pf = PrinterFile(name="test.gcode", path="/test.gcode")
        enrich_printer_file(pf, file_content="")
        assert pf.material is None

    def test_enrichment_from_mcommands(self) -> None:
        pf = PrinterFile(name="test.gcode", path="/test.gcode")
        content = "G28\nM104 S195\nM140 S55\n"
        enrich_printer_file(pf, file_content=content)
        assert pf.tool_temp == 195.0
        assert pf.bed_temp == 55.0


# ===================================================================
# File-based extraction
# ===================================================================

class TestExtractMetadataFromFile:
    """Test file-based extraction using tmpfile."""

    def test_from_file(self, tmp_path) -> None:
        gcode = tmp_path / "test.gcode"
        gcode.write_text(
            "; filament_type = PETG\n"
            "; estimated printing time (normal mode) = 2h 15m\n"
            "; temperature = 240\n"
            "; bed_temperature = 80\n"
            "G28\n"
        )
        meta = extract_metadata(str(gcode))
        assert meta.material == "PETG"
        assert meta.estimated_time_seconds == 8100
        assert meta.tool_temp == 240.0
        assert meta.bed_temp == 80.0

    def test_nonexistent_file(self) -> None:
        meta = extract_metadata("/nonexistent/path/test.gcode")
        # Should not raise -- returns empty metadata
        assert meta.material is None
        assert meta.estimated_time_seconds is None


# ===================================================================
# Edge cases
# ===================================================================

class TestEdgeCases:
    """Edge cases: empty file, binary file, no metadata, huge header."""

    def test_empty_content(self) -> None:
        meta = extract_metadata_from_content("")
        assert meta.material is None
        assert meta.to_dict() == {}

    def test_no_metadata_in_content(self) -> None:
        content = "G28\nG1 X10 Y10 Z0.2 F1200\nG1 X20 Y20 E1.5\n"
        meta = extract_metadata_from_content(content)
        assert meta.material is None
        assert meta.slicer is None
        assert meta.layer_height is None

    def test_binary_content(self) -> None:
        """Binary content should not crash the parser."""
        content = "\x00\x01\x02\xff\xfe\xfd"
        meta = extract_metadata_from_content(content)
        assert meta.material is None

    def test_only_comments_no_metadata(self) -> None:
        content = (
            "; This is a custom G-code file\n"
            "; Created by hand\n"
            "; No slicer metadata here\n"
        )
        meta = extract_metadata_from_content(content)
        assert meta.material is None
        assert meta.slicer is None

    def test_metadata_beyond_max_lines(self) -> None:
        """Metadata after _MAX_HEADER_LINES should not be picked up."""
        lines = ["; unrelated comment\n"] * 250
        lines.append("; filament_type = PLA\n")
        content = "".join(lines)
        meta = extract_metadata_from_content(content)
        # PLA is at line 251, beyond the 200-line limit
        assert meta.material is None

    def test_metadata_within_max_lines(self) -> None:
        lines = ["; unrelated comment\n"] * 150
        lines.append("; filament_type = ABS\n")
        content = "".join(lines)
        meta = extract_metadata_from_content(content)
        assert meta.material == "ABS"

    def test_empty_file_on_disk(self, tmp_path) -> None:
        gcode = tmp_path / "empty.gcode"
        gcode.write_text("")
        meta = extract_metadata(str(gcode))
        assert meta.to_dict() == {}

    def test_mixed_slicer_comments(self) -> None:
        """First match wins when multiple slicer patterns appear."""
        content = (
            "; filament_type = PLA\n"
            ";MATERIAL:PETG\n"
        )
        meta = extract_metadata_from_content(content)
        # PrusaSlicer pattern matches first
        assert meta.material == "PLA"

    def test_case_insensitive_comments(self) -> None:
        content = "; FILAMENT_TYPE = pla\n; TEMPERATURE = 200\n"
        meta = extract_metadata_from_content(content)
        assert meta.material == "PLA"
        assert meta.tool_temp == 200.0


# ===================================================================
# PrinterFile.to_dict with metadata
# ===================================================================

class TestPrinterFileToDict:
    """Verify PrinterFile.to_dict strips None metadata fields."""

    def test_no_metadata(self) -> None:
        pf = PrinterFile(name="test.gcode", path="/test.gcode", size_bytes=1024)
        d = pf.to_dict()
        assert d["name"] == "test.gcode"
        assert d["size_bytes"] == 1024
        assert "material" not in d
        assert "tool_temp" not in d

    def test_with_metadata(self) -> None:
        pf = PrinterFile(
            name="test.gcode", path="/test.gcode",
            material="PLA", tool_temp=210.0,
        )
        d = pf.to_dict()
        assert d["material"] == "PLA"
        assert d["tool_temp"] == 210.0
        assert "bed_temp" not in d  # None, should be stripped

    def test_partial_metadata(self) -> None:
        pf = PrinterFile(
            name="test.gcode", path="/test.gcode",
            material="PETG", estimated_time_seconds=3600,
        )
        d = pf.to_dict()
        assert d["material"] == "PETG"
        assert d["estimated_time_seconds"] == 3600
        assert "slicer" not in d
        assert "layer_height" not in d
