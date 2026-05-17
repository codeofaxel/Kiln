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
    """Full recipe describing a multi-part design and its build pipeline.

    Recipes are version-aware: each modification creates a new version
    rather than overwriting the original.  ``version`` is an incrementing
    integer, ``parent_version`` links back to the previous version, and
    ``changes`` records a delta of what changed (e.g.,
    ``{"portrait.color": "white -> black"}``).
    """

    name: str
    created: str  # ISO-8601 timestamp
    parts: list[DesignPart] = field(default_factory=list)
    source_scad: str | None = None
    parameters: dict[str, Any] = field(default_factory=dict)
    merge_order: list[str] = field(default_factory=list)
    final_3mf: str | None = None
    notes: str = ""
    version: int = 1
    parent_version: str | None = None  # path to the parent recipe file
    changes: dict[str, str] | None = None  # delta from parent
    # Provenance fields (absorbed from DesignVersion system)
    design_id: str | None = None  # unique identifier grouping versions of the same design
    prompt: str | None = None  # natural-language prompt that produced this design
    generation_provider: str | None = None  # e.g. "gemini", "openscad", "manual"
    provenance: dict[str, Any] | None = None  # freeform context (tools_used, change_summary, source_files)
    stl_path: str | None = None  # path to the primary output STL
    # Saved-goal provenance: when the recipe was produced via the
    # kiln-pro design_session lifecycle, ``brief_id`` is the saved-goal
    # identifier (kiln_pro.design_brief.DesignBrief.brief_id) and
    # ``intent_hash`` is the content hash of the goal's derived intent
    # payload.  Both fields are optional — recipes produced outside the
    # design_session flow leave them unset.  Recipes that DO carry
    # these fields promise the caller that the recipe was generated
    # against that saved goal at that intent hash, so downstream
    # audits can verify the mesh against the same goal without
    # re-resolving it.
    brief_id: str | None = None
    intent_hash: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "name": self.name,
            "created": self.created,
            "version": self.version,
            "source_scad": self.source_scad,
            "parameters": self.parameters,
            "parts": [p.to_dict() for p in self.parts],
            "merge_order": self.merge_order,
            "final_3mf": self.final_3mf,
            "notes": self.notes,
        }
        if self.parent_version is not None:
            d["parent_version"] = self.parent_version
        if self.changes is not None:
            d["changes"] = self.changes
        if self.design_id is not None:
            d["design_id"] = self.design_id
        if self.prompt is not None:
            d["prompt"] = self.prompt
        if self.generation_provider is not None:
            d["generation_provider"] = self.generation_provider
        if self.provenance is not None:
            d["provenance"] = self.provenance
        if self.stl_path is not None:
            d["stl_path"] = self.stl_path
        if self.brief_id is not None:
            d["brief_id"] = self.brief_id
        if self.intent_hash is not None:
            d["intent_hash"] = self.intent_hash
        return d

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
            version=data.get("version", 1),
            parent_version=data.get("parent_version"),
            changes=data.get("changes"),
            design_id=data.get("design_id"),
            prompt=data.get("prompt"),
            generation_provider=data.get("generation_provider"),
            provenance=data.get("provenance"),
            stl_path=data.get("stl_path"),
            brief_id=data.get("brief_id"),
            intent_hash=data.get("intent_hash"),
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

    Also writes a versioned copy (e.g., ``.kiln_recipe.v2.json``) so that
    every version is preserved on disk alongside the "current" recipe.

    :returns: The absolute path of the written file.
    :raises FileNotFoundError: If *directory* does not exist.
    """
    if not os.path.isdir(directory):
        raise FileNotFoundError(f"Directory does not exist: {directory}")
    # Always write the canonical "current" recipe
    path = os.path.join(directory, _RECIPE_FILENAME)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(recipe.to_dict(), fh, indent=2)
    # Write a versioned snapshot (v1, v2, ...) for history
    versioned = os.path.join(directory, f".kiln_recipe.v{recipe.version}.json")
    with open(versioned, "w", encoding="utf-8") as fh:
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


def create_new_version(
    parent: DesignRecipe,
    parent_path: str,
    *,
    changes: dict[str, str] | None = None,
    notes: str = "",
) -> DesignRecipe:
    """Create a new version of *parent*, incrementing the version number.

    Returns a deep copy with ``version`` incremented, ``parent_version``
    set to *parent_path*, ``changes`` recording the delta, and a fresh
    timestamp.  The caller should mutate the returned recipe (e.g., update
    part colors) before saving.

    :param parent: The recipe to derive from.
    :param parent_path: Absolute path to the parent recipe file.
    :param changes: Dict of human-readable change descriptions,
        e.g. ``{"portrait.color": "white -> black"}``.
    :param notes: Free-text notes for this version.
    """
    new = copy.deepcopy(parent)
    new.version = parent.version + 1
    new.parent_version = os.path.abspath(parent_path)
    new.changes = changes
    new.created = datetime.now(timezone.utc).isoformat()
    new.notes = notes
    new.final_3mf = None  # invalidate — must be re-merged
    new.stl_path = None  # invalidate — output path changes per version
    # design_id, prompt, generation_provider, and provenance are carried
    # forward from the deep copy — they describe the design lineage
    return new


def list_recipe_versions(directory: str) -> list[dict[str, Any]]:
    """List all versioned recipe snapshots in *directory*, sorted by version.

    Returns a list of dicts with ``version``, ``path``, ``created``, and
    ``name`` for each snapshot found.
    """
    results: list[dict[str, Any]] = []
    if not os.path.isdir(directory):
        return results
    for fname in os.listdir(directory):
        if fname.startswith(".kiln_recipe.v") and fname.endswith(".json"):
            path = os.path.abspath(os.path.join(directory, fname))
            try:
                recipe = load_recipe(path)
                results.append({
                    "version": recipe.version,
                    "path": path,
                    "created": recipe.created,
                    "name": recipe.name,
                    "notes": recipe.notes,
                    "changes": recipe.changes,
                })
            except Exception:  # noqa: BLE001
                results.append({"path": path, "error": "failed to parse"})
    results.sort(key=lambda r: r.get("version", 0))
    return results
