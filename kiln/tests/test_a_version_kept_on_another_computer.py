"""A version whose model stayed on the computer that made it reads as that.

A model larger than the person's Kiln account keeps is pushed as a record
without the model, and the account notes why.  On a second computer that
pulls or clones the design, the version's record is here and its model is
not.  Every door on this computer that reads the version says the model is
kept on the computer it was made on, never that it is missing, empty or
broken, and never crashes:

* ``get_design_version`` says so beside the record;
* ``rollback_design_version`` to a version that has nothing here to restore
  (no model, no source to make it from) is refused, with nothing changed;
  one with its source restores the source and says where the model is;
* ``rebuild_design`` of such a version says why there is nothing to rebuild.

A model that is on this computer is never said to be elsewhere.  Each door is
walked through the registered tools, after a real clone from a stand-in for
the account.
"""

from __future__ import annotations

import base64
import builtins
import json
import sys

import pytest

from kiln import history_courier as courier
from tests.test_history_vault import home  # noqa: F401 -- fixture

MB = 1024 * 1024
SMALL = (
    b"solid one\nfacet normal 0 0 1\n outer loop\n vertex 0 0 0\n vertex 1 0 0\n"
    b" vertex 0 1 0\n endloop\nendfacet\nendsolid one\n"
)
#: The account's note on version 2, as it hands it back with the recipe.
NOTE = {"sha256": "ab" * 32, "bytes": 53 * MB + 1, "limit_bytes": 50 * MB}
KEPT_ELSEWHERE = "kept on the computer it was made on"


def _clone(monkeypatch, *, source: str | None) -> None:
    """Clone "lamp" from a stand-in account: version 1 with its model,
    version 2 a record without its model (with *source*, or a mesh import)."""
    second = {"name": "lamp", "version": 2, "version_id": "v2", "model_not_kept": dict(NOTE)}
    if source is None:
        second["stl_path"] = "lamp-big.stl"
    else:
        second["source_scad"] = source
    branch = {"branch_id": "lamp-main", "design_id": "lamp", "name": "main", "head_version_id": "v2",
              "parent_branch_id": None, "base_version_id": None, "created_at": 1.0, "status": "active",
              "annotation": "", "metadata": {}}
    answer = {
        "design_id": "lamp", "branches": [branch], "releases": [],
        "versions": [{"version_id": "v1", "parent_version_id": None, "created_at": 1.0},
                     {"version_id": "v2", "parent_version_id": "v1", "created_at": 2.0}],
        "memberships": [{"branch_id": "lamp-main", "version_id": vid, "position": i, "added_at": 1.0}
                        for i, vid in enumerate(["v1", "v2"])],
        "provenance_sidecars": [
            {"version_id": "v1", "recipe_dict": {"name": "lamp", "version": 1, "version_id": "v1",
                                                 "source_scad": "cube(1);",
                                                 "mesh_bytes_b64": base64.b64encode(SMALL).decode()}},
            {"version_id": "v2", "recipe_dict": second},
        ],
    }

    def account(tool, method, path, body=None, **_kw):
        return {"recipes": []} if path.endswith("/proven") else json.loads(json.dumps(answer))

    monkeypatch.setattr(courier, "_request", account)
    cloned = courier.clone("cloud_clone_design", "lamp")
    assert cloned["status"] == "success", cloned
    assert KEPT_ELSEWHERE in cloned["said_models"]


@pytest.fixture
def tools(home, monkeypatch):  # noqa: F811
    """The registered version and rebuild tools, on a plain install."""
    real_import = builtins.__import__

    def no_pro(name, *args, **kwargs):
        if name.split(".")[0] == "kiln_pro":
            raise ImportError("No module named 'kiln_pro'", name="kiln_pro")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_pro)
    for loaded in [m for m in sys.modules if m.split(".")[0] == "kiln_pro"]:
        monkeypatch.delitem(sys.modules, loaded)

    from kiln.plugins.design_rebuild_tools import _DesignRebuildPlugin
    from kiln.plugins.version_tools import _VersionToolsPlugin

    found: dict = {}

    class _MCP:
        def tool(self, **_kw):
            def deco(fn):
                found[fn.__name__] = fn
                return fn
            return deco

    _VersionToolsPlugin().register(_MCP())
    _DesignRebuildPlugin().register(_MCP())
    return found


def _kept_numbers(home) -> list[int]:  # noqa: F811
    folder = home / ".kiln" / "designs" / "lamp"
    return sorted(int(p.name.split(".v")[1].split(".")[0]) for p in folder.glob(".kiln_recipe.v*.json"))


@pytest.mark.parametrize("source", [None, "cube(2);"])
def test_the_version_reads_as_kept_on_the_computer_it_was_made_on(tools, monkeypatch, source):
    _clone(monkeypatch, source=source)

    got = tools["get_design_version"](version_id="lamp:2")

    assert got["ok"] is True, got
    said = got["version"]["model_kept_elsewhere"]
    assert "version 2" in said and "53.1 MB" in said and KEPT_ELSEWHERE in said


def test_a_model_on_this_computer_is_never_said_to_be_elsewhere(tools, monkeypatch):
    _clone(monkeypatch, source=None)

    got = tools["get_design_version"](version_id="lamp:1")

    assert got["ok"] is True and got["version"]["stl_path"], got
    assert "model_kept_elsewhere" not in got["version"]


def test_rolling_back_to_a_version_with_nothing_here_changes_nothing(tools, home, monkeypatch):  # noqa: F811
    _clone(monkeypatch, source=None)
    before = _kept_numbers(home)

    got = tools["rollback_design_version"](design_id="lamp", to_version_id="2")

    assert got["ok"] is False and got["code"] == "MODEL_KEPT_ELSEWHERE", got
    assert KEPT_ELSEWHERE in got["error"] and got["changed"] is False
    assert _kept_numbers(home) == before, "a version with nothing in it was saved"


def test_rolling_back_to_a_version_with_its_source_restores_it_and_says_where_the_model_is(
    tools, home, monkeypatch,  # noqa: F811
):
    _clone(monkeypatch, source="cube(2);")

    got = tools["rollback_design_version"](design_id="lamp", to_version_id="2")

    assert got["ok"] is True, got
    assert got["version"]["restored_from_version"] == 2
    assert KEPT_ELSEWHERE in got["version"]["model_kept_elsewhere"]
    assert _kept_numbers(home) == [1, 2, 3]


def test_rebuilding_it_says_why_there_is_nothing_to_rebuild(tools, home, monkeypatch):  # noqa: F811
    _clone(monkeypatch, source=None)

    got = tools["rebuild_design"](recipe_path=str(home / ".kiln" / "designs" / "lamp"))

    assert got["status"] == "error" and got["code"] == "MODEL_KEPT_ELSEWHERE", got
    assert KEPT_ELSEWHERE in got["error"]
    assert "Nothing to rebuild" not in got["error"]
