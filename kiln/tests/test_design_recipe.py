"""Tests for kiln.design_recipe — design recipe system."""

from __future__ import annotations

import json
import os

import pytest

from kiln.design_recipe import (
    DesignPart,
    DesignRecipe,
    create_recipe,
    find_recipe,
    find_recipes_recursive,
    load_recipe,
    save_recipe,
    update_parameter,
    update_part_color,
)


def _sample_parts() -> list[dict]:
    return [
        {
            "name": "body",
            "role": "structural",
            "stl_path": "/tmp/body.stl",
            "color": "white",
            "filament_slot": 0,
        },
        {
            "name": "portrait",
            "role": "decoration",
            "stl_path": "/tmp/portrait.stl",
            "color": "black",
            "filament_slot": 1,
            "slicer_profile": "0.20mm_standard",
            "gcode_path": "/tmp/portrait.gcode",
        },
    ]


def _sample_recipe() -> DesignRecipe:
    return create_recipe(
        "test-coaster",
        _sample_parts(),
        source_scad="/tmp/coaster.scad",
        parameters={"diameter": 80, "depth": 1.5},
        notes="test recipe",
    )


class TestDesignPart:
    """DesignPart dataclass: construction, serialization."""

    def test_to_dict(self):
        part = DesignPart(
            name="body",
            role="structural",
            stl_path="/tmp/body.stl",
            color="white",
        )
        d = part.to_dict()
        assert d["name"] == "body"
        assert d["role"] == "structural"
        assert d["filament_slot"] is None
        assert d["slicer_profile"] is None
        assert d["gcode_path"] is None

    def test_from_dict_minimal(self):
        part = DesignPart.from_dict(
            {"name": "qr", "role": "functional", "stl_path": "/tmp/qr.stl", "color": "#000000"}
        )
        assert part.name == "qr"
        assert part.filament_slot is None

    def test_from_dict_full(self):
        part = DesignPart.from_dict(_sample_parts()[1])
        assert part.filament_slot == 1
        assert part.slicer_profile == "0.20mm_standard"
        assert part.gcode_path == "/tmp/portrait.gcode"

    def test_round_trip(self):
        original = DesignPart(
            name="cap",
            role="functional",
            stl_path="/tmp/cap.stl",
            color="red",
            filament_slot=2,
            slicer_profile="fast",
            gcode_path="/tmp/cap.gcode",
        )
        restored = DesignPart.from_dict(original.to_dict())
        assert restored.name == original.name
        assert restored.filament_slot == original.filament_slot
        assert restored.gcode_path == original.gcode_path


class TestDesignRecipe:
    """DesignRecipe dataclass: construction, serialization, round-trip."""

    def test_to_dict_includes_all_fields(self):
        recipe = _sample_recipe()
        d = recipe.to_dict()
        assert d["name"] == "test-coaster"
        assert len(d["parts"]) == 2
        assert d["source_scad"] == "/tmp/coaster.scad"
        assert d["parameters"]["diameter"] == 80
        assert d["merge_order"] == ["body", "portrait"]
        assert d["notes"] == "test recipe"

    def test_from_dict_round_trip(self):
        original = _sample_recipe()
        restored = DesignRecipe.from_dict(original.to_dict())
        assert restored.name == original.name
        assert len(restored.parts) == len(original.parts)
        assert restored.parts[0].color == "white"
        assert restored.parameters == original.parameters
        assert restored.merge_order == original.merge_order

    def test_from_dict_missing_optional_fields(self):
        data = {"name": "bare", "created": "2026-01-01T00:00:00Z"}
        recipe = DesignRecipe.from_dict(data)
        assert recipe.parts == []
        assert recipe.parameters == {}
        assert recipe.merge_order == []
        assert recipe.final_3mf is None
        assert recipe.notes == ""

    def test_from_dict_missing_name_raises(self):
        with pytest.raises(KeyError):
            DesignRecipe.from_dict({"created": "2026-01-01T00:00:00Z"})


class TestSaveLoad:
    """Filesystem save/load operations."""

    def test_save_and_load_round_trip(self, tmp_path):
        recipe = _sample_recipe()
        path = save_recipe(recipe, str(tmp_path))
        assert path.endswith(".kiln_recipe.json")
        assert os.path.isfile(path)

        loaded = load_recipe(path)
        assert loaded.name == recipe.name
        assert len(loaded.parts) == 2
        assert loaded.parameters["depth"] == 1.5

    def test_save_writes_valid_json(self, tmp_path):
        recipe = _sample_recipe()
        path = save_recipe(recipe, str(tmp_path))
        with open(path) as fh:
            data = json.load(fh)
        assert data["name"] == "test-coaster"

    def test_save_nonexistent_directory_raises(self):
        recipe = _sample_recipe()
        with pytest.raises(FileNotFoundError, match="does not exist"):
            save_recipe(recipe, "/nonexistent/directory/xyz")

    def test_load_nonexistent_file_raises(self):
        with pytest.raises(FileNotFoundError):
            load_recipe("/nonexistent/file.json")

    def test_load_invalid_json_raises(self, tmp_path):
        bad_file = tmp_path / ".kiln_recipe.json"
        bad_file.write_text("not json{{{")
        with pytest.raises(json.JSONDecodeError):
            load_recipe(str(bad_file))

    def test_instance_save_method(self, tmp_path):
        recipe = _sample_recipe()
        path = recipe.save(str(tmp_path))
        assert os.path.isfile(path)

    def test_instance_load_classmethod(self, tmp_path):
        recipe = _sample_recipe()
        path = save_recipe(recipe, str(tmp_path))
        loaded = DesignRecipe.load(path)
        assert loaded.name == recipe.name


class TestFindRecipe:
    """find_recipe and find_recipes_recursive."""

    def test_find_recipe_exists(self, tmp_path):
        recipe = _sample_recipe()
        save_recipe(recipe, str(tmp_path))
        assert find_recipe(str(tmp_path)) is not None

    def test_find_recipe_missing(self, tmp_path):
        assert find_recipe(str(tmp_path)) is None

    def test_find_recipe_nonexistent_dir(self):
        assert find_recipe("/nonexistent/path") is None

    def test_find_recipes_recursive(self, tmp_path):
        sub1 = tmp_path / "design_a"
        sub2 = tmp_path / "design_b"
        sub1.mkdir()
        sub2.mkdir()
        save_recipe(_sample_recipe(), str(sub1))
        save_recipe(_sample_recipe(), str(sub2))
        results = find_recipes_recursive(str(tmp_path))
        assert len(results) == 2

    def test_find_recipes_recursive_empty(self, tmp_path):
        assert find_recipes_recursive(str(tmp_path)) == []

    def test_find_recipes_recursive_nonexistent_dir(self):
        assert find_recipes_recursive("/nonexistent") == []


class TestUpdatePartColor:
    """update_part_color: color and slot changes."""

    def test_update_color(self):
        recipe = _sample_recipe()
        updated = update_part_color(recipe, "body", "grey")
        assert updated.parts[0].color == "grey"
        # Original unchanged (deep copy)
        assert recipe.parts[0].color == "white"

    def test_update_color_and_slot(self):
        recipe = _sample_recipe()
        updated = update_part_color(recipe, "portrait", "red", new_slot=3)
        assert updated.parts[1].color == "red"
        assert updated.parts[1].filament_slot == 3

    def test_update_color_unknown_part_raises(self):
        recipe = _sample_recipe()
        with pytest.raises(ValueError, match="not found"):
            update_part_color(recipe, "nonexistent", "blue")


class TestUpdateParameter:
    """update_parameter: OpenSCAD parameter changes."""

    def test_update_existing_param(self):
        recipe = _sample_recipe()
        updated = update_parameter(recipe, "diameter", 100)
        assert updated.parameters["diameter"] == 100
        # Original unchanged
        assert recipe.parameters["diameter"] == 80

    def test_add_new_param(self):
        recipe = _sample_recipe()
        updated = update_parameter(recipe, "border_width", 2.0)
        assert updated.parameters["border_width"] == 2.0
        assert "border_width" not in recipe.parameters

    def test_empty_param_name_raises(self):
        recipe = _sample_recipe()
        with pytest.raises(ValueError, match="must not be empty"):
            update_parameter(recipe, "", 42)


class TestCreateRecipe:
    """create_recipe convenience factory."""

    def test_creates_with_defaults(self):
        recipe = create_recipe("minimal", [_sample_parts()[0]])
        assert recipe.name == "minimal"
        assert recipe.created  # non-empty ISO timestamp
        assert recipe.merge_order == ["body"]
        assert recipe.notes == ""
        assert recipe.parameters == {}

    def test_empty_parts_list(self):
        recipe = create_recipe("empty", [])
        assert recipe.parts == []
        assert recipe.merge_order == []
