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


class TestCompanionSidecarsFollow:
    """A carve's face record must follow its mesh into the durable home —
    a recipe pointing at the library copy is how the enrichment stamp and
    the passport find the record after the temp workdir is pruned."""

    def test_decoration_sidecar_travels_with_the_mesh(self, home):
        src = _tmp_mesh()
        with open(src + ".decoration_faces.json", "w") as fh:
            fh.write('{"face_indices": [1, 2]}')
        out = persist_asset(src, os.path.join(kiln_root(), "designs", "d"), prefix="mesh")
        side = out + ".decoration_faces.json"
        assert os.path.isfile(side)
        with open(side) as fh:
            assert '"face_indices"' in fh.read()

    def test_sidecar_refreshes_when_the_mesh_deduplicates(self, home):
        """Same mesh saved twice, painted in between: the second persist
        dedupes the mesh but must still carry the UPDATED sidecar."""
        dest_dir = os.path.join(kiln_root(), "designs", "d")
        src = _tmp_mesh()
        with open(src + ".decoration_faces.json", "w") as fh:
            fh.write('{"face_indices": [1]}')
        out1 = persist_asset(src, dest_dir, prefix="mesh")
        with open(src + ".decoration_faces.json", "w") as fh:
            fh.write('{"face_indices": [1], "painted": {"color": "#F72323"}}')
        out2 = persist_asset(src, dest_dir, prefix="mesh")
        assert out2 == out1  # content-addressed dedupe
        with open(out2 + ".decoration_faces.json") as fh:
            assert "painted" in fh.read()

    def test_mesh_without_sidecar_persists_clean(self, home):
        out = persist_asset(_tmp_mesh(), os.path.join(kiln_root(), "designs", "d"))
        assert os.path.isfile(out)
        assert not os.path.exists(out + ".decoration_faces.json")

    def test_save_recipe_carries_the_sidecar_to_the_library_path(self, home):
        """The whole door: save_design_version's persistence step leaves the
        record beside the LIBRARY mesh the recipe actually references."""
        from kiln.design_recipe import DesignRecipe, load_recipe, save_recipe

        mesh = _tmp_mesh()
        with open(mesh + ".decoration_faces.json", "w") as fh:
            fh.write('{"face_indices": [3, 4, 5]}')
        design_dir = os.path.join(kiln_root(), "designs", "carved")
        os.makedirs(design_dir, exist_ok=True)
        r = DesignRecipe(
            name="Carved", created="2026-01-01T00:00:00Z",
            design_id="carved", stl_path=mesh,
        )
        save_recipe(r, design_dir)
        saved = load_recipe(os.path.join(design_dir, ".kiln_recipe.json"))
        assert os.path.isfile(saved.stl_path + ".decoration_faces.json")
