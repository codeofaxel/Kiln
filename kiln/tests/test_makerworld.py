"""Tests for MakerWorld marketplace adapter and model source resolution."""

from __future__ import annotations

import json
import zipfile

import pytest

from kiln.marketplaces.makerworld import MakerWorldAdapter, resolve_makerworld_source
from kiln.server import resolve_model_source

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_MODEL_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<model unit="millimeter" xml:lang="en-US"
       xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">
  <metadata name="Application">BambuStudio-02.05.00.66</metadata>
  <metadata name="Title">Dog Lead Accessories</metadata>
  <metadata name="Designer">PurpleShark</metadata>
  <metadata name="DesignerUserId">3439172713</metadata>
  <metadata name="DesignModelId">US50765bb2a5bc94</metadata>
  <metadata name="DesignProfileId">187258518</metadata>
  <metadata name="License">Standard Digital File License</metadata>
  <metadata name="CreationDate">2026-03-10</metadata>
  <metadata name="Origin">original</metadata>
  <metadata name="Description">A treat holder for dogs</metadata>
  <metadata name="ProfileTitle">0.16mm layer, 2 walls</metadata>
  <metadata name="ProfileDescription">Print without supports</metadata>
  <resources/>
  <build/>
</model>
"""

_PLATE_JSON = {
    "bbox_objects": [
        {"area": 67.0, "bbox": [167, 132, 203, 168], "id": 786, "layer_height": 0.2, "name": "cylinder.stl"},
        {"area": 1136.0, "bbox": [112, 97, 150, 136], "id": 789, "layer_height": 0.2, "name": "cap.stl"},
    ],
    "bed_type": "textured_plate",
    "filament_colors": ["#898989"],
    "nozzle_diameter": 0.4,
    "is_seq_print": False,
}


@pytest.fixture
def makerworld_3mf(tmp_path):
    """Create a .gcode.3mf with MakerWorld metadata."""
    out = tmp_path / "model.gcode.3mf"
    with zipfile.ZipFile(out, "w") as zf:
        zf.writestr("3D/3dmodel.model", _MODEL_XML)
        zf.writestr("Metadata/plate_1.json", json.dumps(_PLATE_JSON))
    return str(out)


@pytest.fixture
def no_makerworld_3mf(tmp_path):
    """Create a .3mf with no MakerWorld metadata."""
    out = tmp_path / "generic.3mf"
    with zipfile.ZipFile(out, "w") as zf:
        zf.writestr(
            "3D/3dmodel.model",
            '<?xml version="1.0"?>'
            '<model xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
            "<resources/><build/></model>",
        )
    return str(out)


_GENERIC_MODEL_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<model unit="millimeter" xml:lang="en-US"
       xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">
  <metadata name="Application">PrusaSlicer-2.8.0</metadata>
  <metadata name="Title">Phone Stand</metadata>
  <metadata name="License">CC-BY-4.0</metadata>
  <resources/>
  <build/>
</model>
"""


@pytest.fixture
def generic_3mf(tmp_path):
    """Create a .3mf with generic metadata (no MakerWorld fields)."""
    out = tmp_path / "generic_with_meta.3mf"
    with zipfile.ZipFile(out, "w") as zf:
        zf.writestr("3D/3dmodel.model", _GENERIC_MODEL_XML)
    return str(out)


# ---------------------------------------------------------------------------
# resolve_makerworld_source tests
# ---------------------------------------------------------------------------


class TestResolveModelSource:
    def test_extracts_metadata(self, makerworld_3mf):
        result = resolve_makerworld_source(makerworld_3mf)
        assert result["source"] == "makerworld"
        assert result["title"] == "Dog Lead Accessories"
        assert result["designer"] == "PurpleShark"
        assert result["designer_id"] == "3439172713"
        assert result["design_model_id"] == "US50765bb2a5bc94"
        assert result["design_profile_id"] == "187258518"
        assert result["license"] == "Standard Digital File License"

    def test_constructs_model_url(self, makerworld_3mf):
        result = resolve_makerworld_source(makerworld_3mf)
        assert "makerworld.com" in result["model_url"]
        assert "187258518" in result["model_url"]

    def test_includes_plate_objects(self, makerworld_3mf):
        result = resolve_makerworld_source(makerworld_3mf)
        assert result["plate_objects"] == ["cylinder.stl", "cap.stl"]

    def test_extracts_profile_info(self, makerworld_3mf):
        result = resolve_makerworld_source(makerworld_3mf)
        assert result["profile_title"] == "0.16mm layer, 2 walls"
        assert result["profile_description"] == "Print without supports"

    def test_no_makerworld_metadata_returns_none(self, no_makerworld_3mf):
        result = resolve_makerworld_source(no_makerworld_3mf)
        assert result is None

    def test_file_not_found(self):
        with pytest.raises(FileNotFoundError):
            resolve_makerworld_source("/nonexistent/file.3mf")

    def test_not_a_zip(self, tmp_path):
        bad = tmp_path / "not_a_zip.3mf"
        bad.write_text("not a zip")
        with pytest.raises(ValueError, match="Not a valid ZIP"):
            resolve_makerworld_source(str(bad))


# ---------------------------------------------------------------------------
# MakerWorldAdapter tests
# ---------------------------------------------------------------------------


class TestMakerWorldAdapter:
    def test_name(self):
        adapter = MakerWorldAdapter()
        assert adapter.name == "makerworld"
        assert adapter.display_name == "MakerWorld"

    def test_supports_download_false(self):
        adapter = MakerWorldAdapter()
        assert adapter.supports_download is False

    def test_search_returns_url(self):
        adapter = MakerWorldAdapter()
        results = adapter.search("dog treat holder")
        assert len(results) == 1
        assert "makerworld.com" in results[0].url
        assert "dog+treat+holder" in results[0].url
        assert results[0].source == "makerworld"
        assert results[0].can_download is False

    def test_get_details(self):
        adapter = MakerWorldAdapter()
        detail = adapter.get_details("187258518")
        assert detail.id == "187258518"
        assert "makerworld.com/en/models/187258518" in detail.url
        assert detail.source == "makerworld"
        assert detail.can_download is False

    def test_get_files_empty(self):
        adapter = MakerWorldAdapter()
        files = adapter.get_files("187258518")
        assert files == []


# ---------------------------------------------------------------------------
# resolve_model_source generic fallback tests
# ---------------------------------------------------------------------------


class TestResolveModelSourceGenericFallback:
    def test_generic_3mf_returns_unknown_source(self, generic_3mf):
        result = resolve_model_source(generic_3mf)
        assert result["status"] == "success"
        assert result["source"] == "unknown"
        assert result["title"] == "Phone Stand"
        assert result["application"] == "PrusaSlicer-2.8.0"
        assert result["license"] == "CC-BY-4.0"

    def test_makerworld_3mf_returns_makerworld_source(self, makerworld_3mf):
        result = resolve_model_source(makerworld_3mf)
        assert result["status"] == "success"
        assert result["source"] == "makerworld"
        assert result["title"] == "Dog Lead Accessories"

    def test_no_metadata_at_all_returns_error(self, no_makerworld_3mf):
        result = resolve_model_source(no_makerworld_3mf)
        assert result["success"] is False
        assert result["error"]["code"] == "SOURCE_NOT_FOUND"
