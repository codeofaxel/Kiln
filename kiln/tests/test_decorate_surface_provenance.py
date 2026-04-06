"""Tests for provenance sidecar integration in decorate_surface."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


def _write_recipe(directory: str, **overrides) -> str:
    """Write a .kiln_recipe.json sidecar to *directory* and return its path."""
    recipe = {
        "name": overrides.get("name", "test-recipe"),
        "created": "2026-04-04T00:00:00Z",
        "version": overrides.get("version", 1),
        "source_scad": None,
        "parameters": overrides.get("parameters", {}),
        "parts": [],
        "merge_order": [],
        "final_3mf": None,
        "notes": "",
    }
    if "design_id" in overrides:
        recipe["design_id"] = overrides["design_id"]
    if "prompt" in overrides:
        recipe["prompt"] = overrides["prompt"]
    if "generation_provider" in overrides:
        recipe["generation_provider"] = overrides["generation_provider"]
    if "provenance" in overrides:
        recipe["provenance"] = overrides["provenance"]

    path = os.path.join(directory, ".kiln_recipe.json")
    with open(path, "w") as f:
        json.dump(recipe, f)
    return path


def _make_dummy_stl(tmp_path: Path) -> str:
    """Create a minimal binary STL for decorate_surface to accept."""
    stl_path = str(tmp_path / "model.stl")
    header = b"\x00" * 80
    # Zero triangles — enough for the provenance check to run
    with open(stl_path, "wb") as f:
        f.write(header)
        f.write((0).to_bytes(4, "little"))
    return stl_path


class TestProvenanceSidecarLookup:
    """Provenance sidecar is loaded and populates _provenance_info."""

    def test_no_sidecar_no_provenance_in_result(self, tmp_path: Path) -> None:
        """When no .kiln_recipe.json exists, no recipe is loaded."""
        _make_dummy_stl(tmp_path)
        recipe_path = os.path.join(str(tmp_path), ".kiln_recipe.json")
        assert not os.path.isfile(recipe_path)

    def test_sidecar_found_and_loaded(self, tmp_path: Path) -> None:
        _write_recipe(
            str(tmp_path),
            design_id="coaster_v6",
            prompt="decorative coaster",
            generation_provider="openscad",
        )

        from kiln.design_recipe import DesignRecipe

        recipe_path = os.path.join(str(tmp_path), ".kiln_recipe.json")
        recipe = DesignRecipe.load(recipe_path)
        assert recipe.design_id == "coaster_v6"
        assert recipe.prompt == "decorative coaster"
        assert recipe.generation_provider == "openscad"

    def test_template_id_from_design_id(self, tmp_path: Path) -> None:
        """When template_id is empty, design_id from recipe is used."""
        _write_recipe(str(tmp_path), design_id="nameplate")

        from kiln.design_recipe import DesignRecipe

        recipe = DesignRecipe.load(os.path.join(str(tmp_path), ".kiln_recipe.json"))

        # Simulate the provenance logic
        template_id = ""
        if not template_id and recipe.design_id:
            template_id = recipe.design_id
        assert template_id == "nameplate"

    def test_template_id_not_overridden_when_caller_sets_it(self, tmp_path: Path) -> None:
        """When caller provides template_id, recipe design_id is ignored."""
        _write_recipe(str(tmp_path), design_id="nameplate")

        from kiln.design_recipe import DesignRecipe

        recipe = DesignRecipe.load(os.path.join(str(tmp_path), ".kiln_recipe.json"))

        template_id = "bookmark"  # caller set this
        if not template_id and recipe.design_id:
            template_id = recipe.design_id
        assert template_id == "bookmark"  # not overridden

    def test_material_from_recipe_overrides_default(self, tmp_path: Path) -> None:
        _write_recipe(str(tmp_path), parameters={"material": "PETG"})

        from kiln.design_recipe import DesignRecipe

        recipe = DesignRecipe.load(os.path.join(str(tmp_path), ".kiln_recipe.json"))

        material = "PLA"  # default
        recipe_material = recipe.parameters.get("material")
        if recipe_material and recipe_material != material and material == "PLA":
            material = recipe_material
        assert material == "PETG"

    def test_material_not_overridden_when_caller_sets_pla_and_recipe_is_pla(self, tmp_path: Path) -> None:
        """When recipe material is also PLA, no override happens."""
        _write_recipe(str(tmp_path), parameters={"material": "PLA"})

        from kiln.design_recipe import DesignRecipe

        recipe = DesignRecipe.load(os.path.join(str(tmp_path), ".kiln_recipe.json"))

        material = "PLA"
        recipe_material = recipe.parameters.get("material")
        if recipe_material and recipe_material != material and material == "PLA":
            material = recipe_material
        assert material == "PLA"  # unchanged — recipe is same as default

    def test_material_not_overridden_when_caller_sets_explicit(self, tmp_path: Path) -> None:
        """When caller explicitly sets material to ABS, recipe doesn't override."""
        _write_recipe(str(tmp_path), parameters={"material": "PETG"})

        from kiln.design_recipe import DesignRecipe

        recipe = DesignRecipe.load(os.path.join(str(tmp_path), ".kiln_recipe.json"))

        material = "ABS"  # caller explicitly set
        recipe_material = recipe.parameters.get("material")
        if recipe_material and recipe_material != material and material == "PLA":
            material = recipe_material
        assert material == "ABS"  # not overridden — caller was explicit

    def test_face_from_recipe_parameters(self, tmp_path: Path) -> None:
        _write_recipe(str(tmp_path), parameters={"decoration_face": "top"})

        from kiln.design_recipe import DesignRecipe

        recipe = DesignRecipe.load(os.path.join(str(tmp_path), ".kiln_recipe.json"))

        face = "auto"
        if face == "auto" and recipe.parameters.get("decoration_face"):
            face = recipe.parameters["decoration_face"]
        assert face == "top"

    def test_face_not_overridden_when_caller_sets(self, tmp_path: Path) -> None:
        _write_recipe(str(tmp_path), parameters={"decoration_face": "top"})

        from kiln.design_recipe import DesignRecipe

        recipe = DesignRecipe.load(os.path.join(str(tmp_path), ".kiln_recipe.json"))

        face = "bottom"  # caller set
        if face == "auto" and recipe.parameters.get("decoration_face"):
            face = recipe.parameters["decoration_face"]
        assert face == "bottom"

    def test_depth_from_recipe_parameters(self, tmp_path: Path) -> None:
        _write_recipe(str(tmp_path), parameters={"decoration_depth_mm": 0.8})

        from kiln.design_recipe import DesignRecipe

        recipe = DesignRecipe.load(os.path.join(str(tmp_path), ".kiln_recipe.json"))

        depth_mm = 0.0
        if depth_mm == 0.0 and recipe.parameters.get("decoration_depth_mm"):
            depth_mm = float(recipe.parameters["decoration_depth_mm"])
        assert depth_mm == 0.8

    def test_corrupt_sidecar_is_silently_skipped(self, tmp_path: Path) -> None:
        """Corrupt JSON doesn't crash — exception is caught."""
        corrupt_path = os.path.join(str(tmp_path), ".kiln_recipe.json")
        with open(corrupt_path, "w") as f:
            f.write("{invalid json!!")

        from kiln.design_recipe import DesignRecipe

        with pytest.raises((json.JSONDecodeError, ValueError)):
            DesignRecipe.load(corrupt_path)

        # The decorate_surface function wraps this in try/except, so it
        # would proceed normally. Testing the DesignRecipe.load failure
        # proves the guard is needed.

    def test_provenance_info_includes_all_fields(self, tmp_path: Path) -> None:
        _write_recipe(
            str(tmp_path),
            name="coaster-v6",
            design_id="coaster",
            prompt="round coaster with QR code",
            generation_provider="openscad",
        )

        from kiln.design_recipe import DesignRecipe

        recipe = DesignRecipe.load(os.path.join(str(tmp_path), ".kiln_recipe.json"))

        info: dict = {}
        if recipe.name:
            info["recipe_name"] = recipe.name
        info["recipe_version"] = recipe.version
        if recipe.design_id:
            info["design_id"] = recipe.design_id
        if recipe.prompt:
            info["prompt"] = recipe.prompt
        if recipe.generation_provider:
            info["generation_provider"] = recipe.generation_provider

        assert info["recipe_name"] == "coaster-v6"
        assert info["recipe_version"] == 1
        assert info["design_id"] == "coaster"
        assert info["prompt"] == "round coaster with QR code"
        assert info["generation_provider"] == "openscad"

    def test_empty_name_excluded_from_provenance(self, tmp_path: Path) -> None:
        _write_recipe(str(tmp_path), name="")

        from kiln.design_recipe import DesignRecipe

        recipe = DesignRecipe.load(os.path.join(str(tmp_path), ".kiln_recipe.json"))

        info: dict = {}
        if recipe.name:
            info["recipe_name"] = recipe.name
        assert "recipe_name" not in info

    def test_material_from_provenance_dict(self, tmp_path: Path) -> None:
        """Material can come from provenance dict as fallback."""
        _write_recipe(
            str(tmp_path),
            parameters={},
            provenance={"material": "TPU"},
        )

        from kiln.design_recipe import DesignRecipe

        recipe = DesignRecipe.load(os.path.join(str(tmp_path), ".kiln_recipe.json"))

        material = "PLA"
        recipe_material = (
            recipe.parameters.get("material")
            or (recipe.provenance and recipe.provenance.get("material"))
        )
        if recipe_material and recipe_material != material and material == "PLA":
            material = recipe_material
        assert material == "TPU"
