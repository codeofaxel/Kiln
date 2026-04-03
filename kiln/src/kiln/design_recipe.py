"""Design recipe system for tracking multi-part 3D printing designs.

A design recipe is a JSON sidecar (``.kiln_recipe.json``) that lives next to a
design's output files.  It records every part, color, filament slot, slicer
profile, OpenSCAD source, parameters, and merge pipeline so that future edits
("change the color", "make it bigger") can be applied without re-doing the
entire pipeline from scratch.
"""

from __future__ import annotations

import copy
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

_RECIPE_FILENAME = ".kiln_recipe.json"


@dataclass
class DesignPart:
    """A single part in a multi-part design."""

    name: str
    role: str  # "structural", "decoration", "functional"
    stl_path: str
    color: str  # e.g. "white", "black", "#FF0000"
    filament_slot: int | None = None  # AMS slot index (0-3 for Bambu AMS Lite)
    slicer_profile: str | None = None
    gcode_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "role": self.role,
            "stl_path": self.stl_path,
            "color": self.color,
            "filament_slot": self.filament_slot,
            "slicer_profile": self.slicer_profile,
            "gcode_path": self.gcode_path,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DesignPart:
        return cls(
            name=data["name"],
            role=data["role"],
            stl_path=data["stl_path"],
            color=data["color"],
            filament_slot=data.get("filament_slot"),
            slicer_profile=data.get("slicer_profile"),
            gcode_path=data.get("gcode_path"),
        )


@dataclass
class DesignRecipe:
    """Full recipe describing a multi-part design and its build pipeline."""

    name: str
    created: str  # ISO-8601 timestamp
    parts: list[DesignPart] = field(default_factory=list)
    source_scad: str | None = None
    parameters: dict[str, Any] = field(default_factory=dict)
    merge_order: list[str] = field(default_factory=list)
    final_3mf: str | None = None
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "created": self.created,
            "source_scad": self.source_scad,
            "parameters": self.parameters,
            "parts": [p.to_dict() for p in self.parts],
            "merge_order": self.merge_order,
            "final_3mf": self.final_3mf,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DesignRecipe:
        parts = [DesignPart.from_dict(p) for p in data.get("parts", [])]
        return cls(
            name=data["name"],
            created=data["created"],
            parts=parts,
            source_scad=data.get("source_scad"),
            parameters=data.get("parameters", {}),
            merge_order=data.get("merge_order", []),
            final_3mf=data.get("final_3mf"),
            notes=data.get("notes", ""),
        )

    def save(self, directory: str) -> str:
        """Write recipe to ``<directory>/.kiln_recipe.json`` and return the path."""
        return save_recipe(self, directory)

    @classmethod
    def load(cls, path: str) -> DesignRecipe:
        """Load a recipe from a ``.kiln_recipe.json`` file."""
        return load_recipe(path)


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def save_recipe(recipe: DesignRecipe, directory: str) -> str:
    """Serialize *recipe* to ``<directory>/.kiln_recipe.json``.

    :returns: The absolute path of the written file.
    :raises FileNotFoundError: If *directory* does not exist.
    """
    if not os.path.isdir(directory):
        raise FileNotFoundError(f"Directory does not exist: {directory}")
    path = os.path.join(directory, _RECIPE_FILENAME)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(recipe.to_dict(), fh, indent=2)
    return os.path.abspath(path)


def load_recipe(path: str) -> DesignRecipe:
    """Deserialize a recipe from *path*.

    :raises FileNotFoundError: If *path* does not exist.
    :raises json.JSONDecodeError: If the file is not valid JSON.
    :raises KeyError: If required fields are missing.
    """
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    return DesignRecipe.from_dict(data)


def find_recipe(directory: str) -> str | None:
    """Return the path to ``.kiln_recipe.json`` inside *directory*, or ``None``."""
    if not os.path.isdir(directory):
        return None
    candidate = os.path.join(directory, _RECIPE_FILENAME)
    if os.path.isfile(candidate):
        return os.path.abspath(candidate)
    return None


def find_recipes_recursive(directory: str) -> list[str]:
    """Return all ``.kiln_recipe.json`` paths under *directory* (recursive)."""
    results: list[str] = []
    if not os.path.isdir(directory):
        return results
    for root, _dirs, files in os.walk(directory):
        if _RECIPE_FILENAME in files:
            results.append(os.path.abspath(os.path.join(root, _RECIPE_FILENAME)))
    return sorted(results)


def update_part_color(
    recipe: DesignRecipe,
    part_name: str,
    new_color: str,
    *,
    new_slot: int | None = None,
) -> DesignRecipe:
    """Return a copy of *recipe* with the named part's color (and optionally slot) updated.

    :raises ValueError: If no part with *part_name* exists.
    """
    updated = copy.deepcopy(recipe)
    for part in updated.parts:
        if part.name == part_name:
            part.color = new_color
            if new_slot is not None:
                part.filament_slot = new_slot
            return updated
    raise ValueError(f"Part {part_name!r} not found in recipe {recipe.name!r}")


def update_parameter(
    recipe: DesignRecipe,
    param_name: str,
    value: Any,
) -> DesignRecipe:
    """Return a copy of *recipe* with the given OpenSCAD parameter changed.

    :raises ValueError: If *param_name* is empty.
    """
    if not param_name:
        raise ValueError("param_name must not be empty")
    updated = copy.deepcopy(recipe)
    updated.parameters[param_name] = value
    return updated


def create_recipe(
    name: str,
    parts: list[dict[str, Any]],
    *,
    source_scad: str | None = None,
    parameters: dict[str, Any] | None = None,
    merge_order: list[str] | None = None,
    final_3mf: str | None = None,
    notes: str = "",
) -> DesignRecipe:
    """Convenience factory to build a :class:`DesignRecipe` from raw dicts."""
    design_parts = [DesignPart.from_dict(p) for p in parts]
    return DesignRecipe(
        name=name,
        created=datetime.now(timezone.utc).isoformat(),
        parts=design_parts,
        source_scad=source_scad,
        parameters=parameters or {},
        merge_order=merge_order or [p.name for p in design_parts],
        final_3mf=final_3mf,
        notes=notes,
    )
