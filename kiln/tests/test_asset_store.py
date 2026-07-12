"""Asset durability — a saved design's mesh lives under ~/.kiln, never temp.

Root-cause backstop: ``save_recipe`` recorded ``stl_path`` as-is, so a mesh
compiled to a temp dir stayed there and a cleanup orphaned the saved design.
``save_recipe`` now copies the mesh into the design directory and rewrites the
reference.  These go red if that persistence is removed.
"""

import os
import tempfile

import pytest

from kiln.asset_store import is_durable, kiln_root, persist_asset


@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(
        os.path, "expanduser", lambda p: p.replace("~", str(tmp_path), 1)
    )
    return tmp_path


def _tmp_mesh(data=b"solid s\nendsolid s\n"):
    p = os.path.join(tempfile.mkdtemp(), "openscad-scratch.stl")
    with open(p, "wb") as fh:
        fh.write(data)
    return p


class TestPersistPrimitive:
    def test_temp_becomes_durable(self, home):
        src = _tmp_mesh()
        assert not is_durable(src)
        out = persist_asset(src, os.path.join(kiln_root(), "designs", "d"), prefix="mesh")
        assert is_durable(out) and os.path.isfile(out)
        assert open(out, "rb").read() == b"solid s\nendsolid s\n"

    def test_already_durable_noop(self, home):
        dest = os.path.join(kiln_root(), "designs", "d")
        out = persist_asset(_tmp_mesh(), dest)
        assert persist_asset(out, dest) == out

    def test_missing_source_unchanged(self, home):
        assert persist_asset("/tmp/nope-xyz.stl", kiln_root()) == "/tmp/nope-xyz.stl"

    def test_is_durable_rejects_temp(self):
        assert not is_durable("/tmp/x.stl")
        assert not is_durable("/var/folders/ab/cd/T/x.stl")


class TestSaveRecipeIsDurable:
    def test_save_recipe_persists_temp_mesh(self, home):
        from kiln.design_recipe import DesignRecipe, load_recipe, save_recipe

        mesh = _tmp_mesh()
        design_dir = os.path.join(kiln_root(), "designs", "smoke")
        os.makedirs(design_dir, exist_ok=True)
        r = DesignRecipe(
            name="Smoke", created="2026-01-01T00:00:00Z",
            design_id="smoke", stl_path=mesh,
        )
        assert not is_durable(r.stl_path)
        save_recipe(r, design_dir)
        # current recipe + versioned snapshot must both point into ~/.kiln
        saved = load_recipe(os.path.join(design_dir, ".kiln_recipe.json"))
        assert is_durable(saved.stl_path) and os.path.isfile(saved.stl_path)
        import json
        v = json.load(open(os.path.join(design_dir, f".kiln_recipe.v{saved.version}.json")))
        assert is_durable(v["stl_path"])
