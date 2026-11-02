"""An edit of a design's newest version, on a plain install, is kept as its next version.

On a computer without kiln-pro (every customer's), repair, rescale, mirror and
the other edit tools wrote their result and nothing landed in the design's
history -- and several write over the file they were handed, so the version
the person started from could simply stop existing.  Now the tool dispatch
keeps an edit of the NEWEST version of one design saved here as that design's
next version, tagged as made by an edit and by whom; every other edit says in
one line why it was not kept and the call that keeps it.

These tests call the registered tools through the MCP door, as a host does,
with kiln-pro removed from the import system.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

import kiln.server as server
from kiln import edit_lineage

trimesh = pytest.importorskip("trimesh")


class _NoKilnPro:
    def find_spec(self, name, path=None, target=None):
        if name == "kiln_pro" or name.startswith("kiln_pro."):
            raise ImportError("kiln_pro is not installed")
        return None


@pytest.fixture(autouse=True)
def plain_install(monkeypatch, tmp_path):
    for key in [k for k in sys.modules if k == "kiln_pro" or k.startswith("kiln_pro.")]:
        monkeypatch.delitem(sys.modules, key)
    monkeypatch.setattr(sys, "meta_path", [_NoKilnPro(), *sys.meta_path])
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("KILN_HOME", str(home / ".kiln"))
    monkeypatch.setattr(server, "_check_auth", lambda *_a, **_k: None)
    monkeypatch.setattr(server, "_served_bearer", lambda: (None, None))
    monkeypatch.setattr("kiln.local_stage.host_renders_apps", lambda *a, **k: False)
    manifest = json.loads((Path(server.__file__).parent / "pro_tool_manifest.json").read_text())
    load = getattr(edit_lineage, "load", None)
    if load is not None:
        load(manifest.get("edit_lineage"))
    yield


def _client():
    try:
        from mcp import Client
    except ImportError:
        from mcp.shared.memory import create_connected_server_and_client_session

        return create_connected_server_and_client_session(server.mcp)
    return Client(server.mcp)


def _call(tool: str, **arguments) -> dict:
    async def _one():
        async with _client() as client:
            return await client.call_tool(tool, arguments)

    result = asyncio.run(_one())
    structured = getattr(result, "structured_content", None) or getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        return structured
    return json.loads("".join(b.text for b in result.content if getattr(b, "type", None) == "text"))


def _box(path: Path, size=(10, 10, 10)) -> str:
    trimesh.creation.box(extents=size).export(str(path))
    return str(path)


def _save(design_id: str, stl: str) -> dict:
    out = _call("save_design_version", design_id=design_id, scad_source="", stl_path=stl)
    assert out.get("ok") is True, out
    return out["version"]


def _library(design_id: str) -> Path:
    return Path.home() / ".kiln" / "designs" / design_id


def _bytes_of(folder: Path, version: int) -> bytes:
    recipe = json.loads((folder / f".kiln_recipe.v{version}.json").read_text())
    return Path(recipe["stl_path"]).read_bytes()


def test_an_edit_of_the_newest_version_is_kept_as_the_next(tmp_path):
    work = _box(tmp_path / "mug.stl")
    original = Path(work).read_bytes()
    _save("mug", work)
    out = _call("rescale_model", file_path=work, scale_factor=2.0)
    assert out.get("success") is not False, out
    kept = out["saved_version"]
    assert kept["recorded"] is True
    assert kept["design_id"] == "mug" and kept["version"] == 2 and kept["parent_version"] == 1
    assert kept["by"] == "agent"
    assert "kept as version 2" in kept["note"]
    folder = _library("mug")
    v2 = json.loads((folder / ".kiln_recipe.v2.json").read_text())
    assert v2["provenance"]["edit"]["tool"] == "rescale_model"
    assert v2["provenance"]["edit"]["by"] == "agent"
    assert v2["provenance"]["edit"]["derived_from"]["version"] == 1
    # The rescale wrote over the file it was handed; version 1 still holds
    # the part as it was, and version 2 holds the edit.
    assert Path(work).read_bytes() != original
    assert _bytes_of(folder, 1) == original
    assert _bytes_of(folder, 2) == Path(work).read_bytes()


def test_the_next_edit_continues_the_line(tmp_path):
    work = _box(tmp_path / "mug.stl")
    _save("mug", work)
    _call("rescale_model", file_path=work, scale_factor=2.0)
    out = _call("rescale_model", file_path=work, scale_factor=0.5)
    assert out["saved_version"]["version"] == 3 and out["saved_version"]["parent_version"] == 2


def test_an_edit_of_an_older_version_says_how_to_keep_it(tmp_path):
    first = _box(tmp_path / "v1.stl")
    _save("mug", first)
    _save("mug", _box(tmp_path / "v2.stl", (20, 10, 10)))
    copy = tmp_path / "old.stl"
    copy.write_bytes(Path(first).read_bytes())
    out = _call("rescale_model", file_path=str(copy), scale_factor=2.0)
    block = out["saved_version"]
    assert block["recorded"] is False and block["reason"] == "older"
    assert "create_design_branch" in block["note"] and "version 1" in block["note"]
    assert not (_library("mug") / ".kiln_recipe.v3.json").exists()


def test_a_part_two_designs_hold_is_not_guessed(tmp_path):
    part = _box(tmp_path / "p.stl")
    _save("mug", part)
    _save("cup", part)
    out = _call("rescale_model", file_path=part, scale_factor=2.0)
    block = out["saved_version"]
    assert block["recorded"] is False and block["reason"] == "ambiguous"
    assert "mug" in block["note"] and "cup" in block["note"]
    assert "save_design_version" in block["note"]


def test_a_file_no_design_holds_says_how_to_keep_it(tmp_path):
    out = _call("rescale_model", file_path=_box(tmp_path / "loose.stl"), scale_factor=2.0)
    block = out["saved_version"]
    assert block["recorded"] is False and block["reason"] == "none"
    assert "save_design_version" in block["note"]


def test_a_person_at_the_terminal_is_recorded_as_the_person(tmp_path):
    work = _box(tmp_path / "mug.stl")
    _save("mug", work)
    with edit_lineage.around("kiln repair", work) as edit:
        _box(Path(work), (12, 10, 10))
        result = edit.finish({"success": True, "path": work})
    assert result["saved_version"]["recorded"] is True
    assert result["saved_version"]["by"] == "person"
    v2 = json.loads((_library("mug") / ".kiln_recipe.v2.json").read_text())
    assert v2["provenance"]["edit"]["by"] == "person"


def test_an_edit_that_changed_nothing_keeps_nothing(tmp_path):
    work = _box(tmp_path / "mug.stl")
    _save("mug", work)
    with edit_lineage.around("kiln repair", work) as edit:
        result = edit.finish({"success": True, "path": work})
    assert result["saved_version"]["recorded"] is False and result["saved_version"]["unchanged"] is True
    assert not (_library("mug") / ".kiln_recipe.v2.json").exists()


def test_a_door_told_its_design_keeps_it_itself():
    assert edit_lineage.begin_here("rescale_model", {"file_path": __file__, "design_id": "mug"}) is None


def test_nothing_is_read_where_kiln_pro_keeps_edits(monkeypatch, tmp_path):
    import types

    monkeypatch.setitem(sys.modules, "kiln_pro", types.ModuleType("kiln_pro"))
    assert edit_lineage.begin_here("rescale_model", {"file_path": _box(tmp_path / "x.stl")}) is None
