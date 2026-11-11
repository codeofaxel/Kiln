"""A version states its own facts, never another version's.

A design's history may travel from version to version: its name, its goal,
the prompt it started from.  What a version IS may not: the parameters its
source makes the part with, the part it restores, and the story of how it
was made.  A version showing another version's reality as its own is the
failure this file guards against, at every door that makes a version.

Two censuses keep it whole: every door that calls
``design_recipe.create_new_version`` must have a case here, and every
``DesignRecipe`` field must be classified as the version's own, the
design's history, or mixed (with the rule for each half).
"""

from __future__ import annotations

import ast
import dataclasses
import json
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "kiln"

#: A source whose parameter block opens with a render-settings line.
FIRST = (
    "$fa = 2; $fs = 0.4;\n\n"
    "pot_h = 110;   // pot height\n"
    "flutes = 20;\n"
    "wall = 2;\n\n"
    "cylinder(r=30, h=pot_h);\n"
)
SECOND = (
    "$fa = 2; $fs = 0.4;\n\n"
    "r_base = 50;\n"
    "flutes = 24;\n"
    "wall = 2.2;\n\n"
    "cylinder(r=r_base, h=112);\n"
)
FIRST_OWN = {"pot_h": 110, "flutes": 20, "wall": 2}
SECOND_OWN = {"r_base": 50, "flutes": 24, "wall": 2.2}


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("KILN_DESIGNS_DIR", raising=False)
    # The tools' kiln-pro steps (fingerprints, previews) are not what is
    # judged here; keep them out so the run is the same everywhere.
    for name in ("kiln_pro.bridge", "kiln_pro.plugins.git_render_tools"):
        monkeypatch.setitem(sys.modules, name, None)
    return tmp_path


def _tools() -> dict:
    from kiln.plugins import version_tools

    tools: dict = {}

    class _MCP:
        def tool(self, *_a, **_k):
            def deco(fn):
                tools[fn.__name__] = fn
                return fn

            return deco

    version_tools.plugin.register(_MCP())
    return tools


def _save(design_id: str, scad: str, **kwargs) -> dict:
    out = _tools()["save_design_version"](design_id=design_id, scad_source=scad, **kwargs)
    assert out.get("ok"), out
    return out["version"]


def _recipe(home: Path, design_id: str, version: int) -> dict:
    path = home / ".kiln" / "designs" / design_id / f".kiln_recipe.v{version}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _write_version(home: Path, design_id: str, version: int, **fields) -> Path:
    folder = home / ".kiln" / "designs" / design_id
    folder.mkdir(parents=True, exist_ok=True)
    stl = folder / f"v{version}.stl"
    stl.write_bytes(f"solid v{version}\nendsolid v{version}\n".encode())
    recipe = {
        "name": design_id,
        "created": f"2026-10-0{version}T12:00:00+00:00",
        "version": version,
        "design_id": design_id,
        "stl_path": str(stl),
        "parts": [],
        **fields,
    }
    text = json.dumps(recipe)
    (folder / f".kiln_recipe.v{version}.json").write_text(text, encoding="utf-8")
    (folder / ".kiln_recipe.json").write_text(text, encoding="utf-8")
    return folder


def _part(name: str, folder: Path, color: str) -> dict:
    return {"name": name, "role": "structural", "stl_path": str(folder / f"{name}.stl"),
            "color": color, "filament_slot": None, "slicer_profile": None, "gcode_path": None}


# ---------------------------------------------------------------------------
# The reader
# ---------------------------------------------------------------------------


def test_the_reader_reads_past_a_render_settings_line() -> None:
    from kiln.design_recipe import own_parameters

    assert own_parameters(FIRST) == FIRST_OWN
    assert own_parameters(SECOND) == SECOND_OWN


# ---------------------------------------------------------------------------
# Saving a version
# ---------------------------------------------------------------------------


def test_a_new_source_records_its_own_parameters(home) -> None:
    """The planter's case: v2 saved with a new source and no parameters
    recorded v1's, so v2 claimed 20 flutes while its source made 24."""
    _save("pot", FIRST, parameters=dict(FIRST_OWN))
    second = _save("pot", SECOND)

    assert second["parameters"] == SECOND_OWN, second["parameters"]
    assert _recipe(home, "pot", second["version"])["parameters"] == SECOND_OWN


def test_an_unchanged_source_keeps_its_parameters(home) -> None:
    _save("pot", FIRST, parameters={"flutes": 22})
    again = _save("pot", FIRST, notes="same part, a note")

    assert again["parameters"] == {"flutes": 22}


def test_a_stated_name_the_source_does_not_set_is_left_out_and_named(home) -> None:
    _save("pot", FIRST)
    second = _save("pot", SECOND, parameters={"pot_h": 110, "flutes": 24})

    assert second["parameters"] == {"flutes": 24}
    assert second["parameters_not_in_source"] == ["pot_h"]


def test_a_first_save_records_its_source_parameters(home) -> None:
    first = _save("pot", FIRST)

    assert first["parameters"] == FIRST_OWN


def test_a_first_save_into_an_empty_slot_records_its_source_parameters(home) -> None:
    """A slot made before anything was saved holds no version, so whatever
    it was made with is not a version's parameters to keep."""
    folder = home / ".kiln" / "designs" / "pot"
    folder.mkdir(parents=True)
    slot = {"name": "pot", "created": "2026-10-01T12:00:00+00:00", "design_id": "pot",
            "source_scad": FIRST, "parameters": {"pot_h": 90}, "parts": []}
    (folder / ".kiln_recipe.json").write_text(json.dumps(slot), encoding="utf-8")

    first = _save("pot", FIRST)

    assert first["parameters"] == FIRST_OWN


# ---------------------------------------------------------------------------
# Rolling back
# ---------------------------------------------------------------------------


def test_a_rollback_restores_the_target_versions_own_facts(home) -> None:
    folder = home / ".kiln" / "designs" / "pot"
    _write_version(
        home, "pot", 1, source_scad=FIRST, parameters=dict(FIRST_OWN),
        parts=[_part("body", folder, "white")], merge_order=["body"],
        provenance={"material": "PETG",
                    "change_summary": "v1: first pot", "tools_used": ["compile_scad"]},
    )
    _write_version(
        home, "pot", 2, source_scad=SECOND, parameters=dict(SECOND_OWN),
        parts=[_part("shell", folder, "black")], merge_order=["shell"],
        provenance={"material": "PLA",
                    "change_summary": "v2: twisted flutes", "tools_used": ["make_printable"]},
    )

    out = _tools()["rollback_design_version"](design_id="pot", to_version_id="1")
    assert out.get("ok"), out
    restored = _recipe(home, "pot", out["version"]["version"])

    assert restored["source_scad"] == FIRST
    assert restored["parameters"] == FIRST_OWN
    assert [p["name"] for p in restored["parts"]] == ["body"]
    assert restored["merge_order"] == ["body"]
    assert restored["provenance"] == {"material": "PETG"}, restored["provenance"]


# ---------------------------------------------------------------------------
# Keeping an edit
# ---------------------------------------------------------------------------


def test_an_edit_keeps_only_its_own_story(home, tmp_path) -> None:
    from kiln import edit_lineage

    folder = _write_version(
        home, "pot", 1, source_scad=FIRST, parameters=dict(FIRST_OWN), version_id="a" * 32,
        provenance={"material": "PETG",
                    "change_summary": "v1: first pot", "tools_used": ["compile_scad"]},
    )
    edited = tmp_path / "edited.stl"
    edited.write_bytes(b"solid edited\nendsolid edited\n")
    parent = edit_lineage._Held(
        design_id="pot", version=1, version_id="a" * 32, name="pot",
        recipe_path=folder / ".kiln_recipe.v1.json",
    )
    pending = edit_lineage.Pending(
        tool="repair_mesh", says="repaired", by="agent", input_path=str(folder / "v1.stl"),
        sha="0" * 64, decision="keep", parent=parent,
    )

    block = edit_lineage._keep(pending, str(edited))
    kept = _recipe(home, "pot", block["version"])

    assert set(kept["provenance"]) == {"material", "edit"}, kept["provenance"]
    assert kept["parameters"] == FIRST_OWN  # the source is the parent's own


# ---------------------------------------------------------------------------
# The censuses
# ---------------------------------------------------------------------------

#: Every door that makes a new version, and the case that holds it to its
#: own facts.
DOORS = {
    ("plugins/version_tools.py", "save_design_version"): "test_a_new_source_records_its_own_parameters",
    ("plugins/version_tools.py", "rollback_design_version"): "test_a_rollback_restores_the_target_versions_own_facts",
    ("edit_lineage.py", "_keep"): "test_an_edit_keeps_only_its_own_story",
}

#: What each recipe field is.  OWN: describes the version being written and
#: never comes from another version.  HISTORY: the design's history, which
#: may travel from version to version.  MIXED: both halves, with the rule.
OWN = {
    "created", "version", "parent_version", "changes", "notes", "source_scad",
    "parameters", "merge_order", "final_3mf", "stl_path",
}
HISTORY = {"name", "design_id", "prompt", "generation_provider", "brief_id", "intent_hash"}
MIXED = {
    # The keys in design_recipe.VERSION_STORY_KEYS tell how one version was
    # made and are the version's own; the rest follows the part.
    "provenance",
    # A part's name, role, colour and filament slot are the design's
    # choices and travel; its files are the version's own.
    "parts",
}


def _doors_in_source() -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(fn):
                if isinstance(node, ast.Call):
                    name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
                    if name == "create_new_version":
                        found.add((path.relative_to(SRC).as_posix(), fn.name))
    # A call inside a nested function is also inside its outer one; keep the
    # innermost, which is the door.
    return {
        (f, name) for f, name in found
        if not any(f == f2 and name != n2 and _encloses(f, name, n2) for f2, n2 in found)
    }


def _encloses(file: str, outer: str, inner: str) -> bool:
    tree = ast.parse((SRC / file).read_text(encoding="utf-8"))
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and fn.name == outer:
            return any(
                isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == inner
                for n in ast.walk(fn) if n is not fn
            )
    return False


def test_every_door_that_makes_a_version_is_held_here() -> None:
    found = _doors_in_source()

    new = found - set(DOORS)
    gone = set(DOORS) - found
    assert not new, (
        f"New doors make versions with no case holding them to their own facts: {sorted(new)}. "
        "Add a case here and name it in DOORS."
    )
    assert not gone, f"DOORS names doors that no longer exist: {sorted(gone)}"
    for case in DOORS.values():
        assert case in globals(), f"DOORS names a case that does not exist: {case}"


def test_every_recipe_field_is_classified() -> None:
    from kiln.design_recipe import DesignRecipe

    fields = {f.name for f in dataclasses.fields(DesignRecipe)}

    assert not (OWN & HISTORY) and not (OWN & MIXED) and not (HISTORY & MIXED)
    unclassified = fields - (OWN | HISTORY | MIXED)
    stale = (OWN | HISTORY | MIXED) - fields
    assert not unclassified, (
        f"DesignRecipe fields nobody has classified: {sorted(unclassified)}. Decide whether each "
        "is the version's own, the design's history, or mixed, before any door writes it."
    )
    assert not stale, f"Classified fields that DesignRecipe no longer has: {sorted(stale)}"


def test_a_new_version_never_inherits_the_story_of_how_its_parent_was_made() -> None:
    from kiln.design_recipe import VERSION_STORY_KEYS, DesignRecipe, create_new_version

    parent = DesignRecipe(
        name="pot", created="2026-10-01T12:00:00+00:00", design_id="pot", source_scad=FIRST,
        provenance={"material": "PETG",
                    **{k: f"the parent's {k}" for k in VERSION_STORY_KEYS}},
    )
    child = create_new_version(parent, "/nonexistent/.kiln_recipe.json")

    assert child.provenance == {"material": "PETG"}
