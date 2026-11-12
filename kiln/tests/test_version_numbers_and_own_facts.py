"""A version's number and the facts that are its own, at one save.

Every door that makes a version takes the version's number once and records
only that version's own facts: the parameters its own source sets, the part
it restores, and none of the story of how its parent was made.  Each case
here holds one door to both at once, with a branch record standing in for
the one a Kiln Pro install keeps, so the number and the parameters the door
files are read where they land.
"""

from __future__ import annotations

import json
import sys
import types

import pytest

pytest.importorskip("kiln.version_numbers")

from tests.test_a_version_states_its_own_facts import (  # noqa: E402
    FIRST,
    FIRST_OWN,
    SECOND,
    SECOND_OWN,
    _part,
    _recipe,
    _tools,
    _write_version,
)


class _BranchRecord:
    """The branch record of an install with Kiln Pro: what each save filed."""

    def __init__(self) -> None:
        self.filed: list[dict] = []

    def enrich_version(self, version: dict, **_kw) -> dict:
        return version

    def register_saved_version(self, *, design_id, scad_source, stl_path, prompt, parameters, notes,
                               version_number):
        self.filed.append({"design_id": design_id, "version_number": version_number,
                           "parameters": dict(parameters or {}), "scad_source": scad_source})
        return {"branch": "main", "version_number": version_number}


@pytest.fixture
def record(tmp_path, monkeypatch) -> _BranchRecord:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("KILN_DESIGNS_DIR", raising=False)
    kept = _BranchRecord()
    bridge = types.ModuleType("kiln_pro.bridge")
    bridge.pro_features = kept
    previews = types.ModuleType("kiln_pro.plugins.git_render_tools")
    previews.attach_inspect_bundle = lambda result, **_kw: result
    monkeypatch.setitem(sys.modules, "kiln_pro.bridge", bridge)
    monkeypatch.setitem(sys.modules, "kiln_pro.plugins.git_render_tools", previews)
    return kept


def test_a_first_save_beside_kept_versions_takes_the_next_number_and_its_own_parameters(record, tmp_path):
    """A folder that keeps versions 1 and 2 and no current recipe (a pulled
    design): the save is version 3, records the parameters its own source
    sets, and is filed on its branch under 3 with those parameters."""
    folder = tmp_path / ".kiln" / "designs" / "pot"
    _write_version(tmp_path, "pot", 1, source_scad=FIRST, parameters=dict(FIRST_OWN), version_id="a" * 32)
    _write_version(tmp_path, "pot", 2, source_scad=FIRST, parameters=dict(FIRST_OWN), version_id="b" * 32)
    (folder / ".kiln_recipe.json").unlink()

    out = _tools()["save_design_version"](design_id="pot", scad_source=SECOND,
                                          parameters={"pot_h": 110, "flutes": 24})

    assert out.get("ok"), out
    saved = out["version"]
    assert saved["version"] == 3, saved
    assert saved["parameters"] == {"flutes": 24}
    assert saved["parameters_not_in_source"] == ["pot_h"]
    assert _recipe(tmp_path, "pot", 3)["parameters"] == {"flutes": 24}
    assert record.filed == [{"design_id": "pot", "version_number": 3, "parameters": {"flutes": 24},
                             "scad_source": SECOND}]
    assert "warnings" not in out


def test_a_rollback_is_filed_under_its_number_and_restores_the_targets_own_facts(record, tmp_path):
    folder = tmp_path / ".kiln" / "designs" / "pot"
    _write_version(
        tmp_path, "pot", 1, source_scad=FIRST, parameters=dict(FIRST_OWN),
        parts=[_part("body", folder, "white")], merge_order=["body"],
        provenance={"material": "PETG", "change_summary": "v1: first pot", "tools_used": ["compile_scad"]},
    )
    _write_version(
        tmp_path, "pot", 2, source_scad=SECOND, parameters=dict(SECOND_OWN),
        parts=[_part("shell", folder, "black")], merge_order=["shell"],
        provenance={"material": "PLA", "change_summary": "v2: twisted flutes", "tools_used": ["make_printable"]},
    )

    out = _tools()["rollback_design_version"](design_id="pot", to_version_id="1")

    assert out.get("ok"), out
    assert out["version"]["version"] == 3
    restored = _recipe(tmp_path, "pot", 3)
    assert (restored["source_scad"], restored["parameters"]) == (FIRST, FIRST_OWN)
    assert ([p["name"] for p in restored["parts"]], restored["merge_order"]) == (["body"], ["body"])
    assert restored["provenance"] == {"material": "PETG"}, restored["provenance"]
    assert record.filed == [{"design_id": "pot", "version_number": 3, "parameters": FIRST_OWN,
                             "scad_source": FIRST}]
    assert out["version"]["branch"] == {"branch": "main", "version_number": 3}


def test_a_new_version_is_numbered_past_numbers_given_elsewhere_and_keeps_only_part_facts(record, tmp_path):
    """The design's folder records that number 12 was given to a version
    kept elsewhere: the next version is 13, and it carries the facts about
    the part, not the story of how its parent was made."""
    from kiln.design_recipe import create_new_version, load_recipe
    from kiln.version_numbers import note_number_given

    folder = _write_version(
        tmp_path, "pot", 1, source_scad=FIRST, parameters=dict(FIRST_OWN),
        provenance={"material": "PETG", "change_summary": "v1: first pot", "tools_used": ["compile_scad"],
                    "edit": {"tool": "repair_mesh"}},
    )
    note_number_given(folder, 12)
    head = folder / ".kiln_recipe.json"

    new = create_new_version(load_recipe(str(head)), str(head))

    assert new.version == 13
    assert new.provenance == {"material": "PETG"}
    assert json.loads(head.read_text(encoding="utf-8"))["provenance"]["tools_used"] == ["compile_scad"]


# ---------------------------------------------------------------------------
# Only what describes the part travels, and a piece never becomes the design
# ---------------------------------------------------------------------------

#: Facts of one version's own, as records carry them: the splits and the
#: merge it records, the branch it was filed on, what was measured and
#: stamped for it, and something its saver stated about it.
ONE_VERSIONS_OWN = {
    "kits": [{"kit_id": "k1", "pieces": 2}],
    "merge": {"of": ["a", "b"]},
    "branch": {"name": "main"},
    "sealed": {"origin": "seal"},
    "mesh_fingerprint": {"volume_mm3": 1.0},
    "spec_receipt": {"for": "this version"},
    "promoted_from": {"piece": 1},
    "a_savers_own_note": "stated when this version was saved",
}


def test_only_the_keys_that_describe_the_part_travel_to_a_new_version():
    from kiln.design_recipe import PART_FACT_KEYS, DesignRecipe, create_new_version, part_facts

    about_the_part = {"material": "PETG", "import_origin": True, "designed_hardware": [{"kind": "insert"}]}
    assert set(about_the_part) <= PART_FACT_KEYS and not set(ONE_VERSIONS_OWN) & PART_FACT_KEYS
    parent = DesignRecipe(
        name="pot", created="2026-10-01T12:00:00+00:00", design_id="pot", source_scad=FIRST,
        provenance={**about_the_part, **ONE_VERSIONS_OWN},
    )
    child = create_new_version(parent, "/nonexistent/.kiln_recipe.json")

    assert child.provenance == about_the_part, sorted(set(child.provenance) - set(about_the_part))
    assert part_facts(dict(ONE_VERSIONS_OWN)) is None
    assert child.provenance["designed_hardware"] is not parent.provenance["designed_hardware"]


def test_a_rollback_to_the_version_that_was_split_does_not_say_the_new_version_was_split(record, tmp_path):
    _write_version(
        tmp_path, "pot", 1, source_scad=FIRST, parameters=dict(FIRST_OWN),
        provenance={"material": "PETG", **ONE_VERSIONS_OWN},
    )
    _write_version(tmp_path, "pot", 2, source_scad=SECOND, parameters=dict(SECOND_OWN),
                   provenance={"material": "PLA"})

    out = _tools()["rollback_design_version"](design_id="pot", to_version_id="1")

    assert out.get("ok"), out
    assert _recipe(tmp_path, "pot", out["version"]["version"])["provenance"] == {"material": "PETG"}


def test_a_rollback_to_a_piece_of_a_split_is_refused_and_the_design_is_untouched(record, tmp_path):
    """Version 2 is one piece of a split of version 1, kept beside the
    design.  The design is always the whole part: rolling back to the piece
    is refused, and nothing is written, filed or renumbered."""
    folder = tmp_path / ".kiln" / "designs" / "pot"
    _write_version(tmp_path, "pot", 2, source_scad="cube(5);", parameters={},
                   provenance={"piece_of": {"design_id": "pot", "piece": 1, "of_pieces": 2}})
    _write_version(tmp_path, "pot", 1, source_scad=FIRST, parameters=dict(FIRST_OWN),
                   provenance={"material": "PETG", "kits": [{"kit_id": "k1"}]})
    before = {p.name: p.read_bytes() for p in folder.iterdir()}
    assert json.loads(before[".kiln_recipe.json"])["version"] == 1

    for named in ("2", "pot:2"):
        out = _tools()["rollback_design_version"](design_id="pot", to_version_id=named)
        assert out.get("ok") is False and out.get("code") == "VERSION_IS_A_PIECE", out
        assert out["changed"] is False and "whole part" in out["error"]
    assert {p.name: p.read_bytes() for p in folder.iterdir() if p.name != ".kiln_numbers.lock"} == before
    assert record.filed == [], "a refused rollback was filed on a branch"

    whole = _tools()["rollback_design_version"](design_id="pot", to_version_id="1")
    assert whole.get("ok") and whole["version"]["version"] == 3, whole
