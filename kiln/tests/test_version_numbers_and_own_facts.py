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
