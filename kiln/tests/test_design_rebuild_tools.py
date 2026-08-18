"""Tool-surface tests for rebuild_design and its engine.

Every test calls the REGISTERED TOOL through a fake MCP with REAL recipes
and REAL meshes on disk.  The predecessor of this suite lived in kiln-pro
and tested inner functions with ``load_recipe`` mocked — it mocked the
exact call that failed in production, so 40+ green tests certified a tool
that had never once succeeded.  Nothing on the path under test is mocked
here; tests that slice or compile skip honestly when the machine has no
slicer / OpenSCAD.
"""

from __future__ import annotations

import os
import shutil
import struct
from pathlib import Path
from typing import Any

import pytest
from kiln.design_recipe import (
    DesignPart,
    DesignRecipe,
    find_recipe,
    load_recipe,
    save_recipe,
)
from kiln.design_rebuild import normalize_color


def _load(design_dir: Path | str) -> DesignRecipe:
    recipe_file = find_recipe(str(design_dir))
    assert recipe_file, f"no recipe in {design_dir}"
    return load_recipe(recipe_file)


@pytest.fixture
def tools() -> dict[str, Any]:
    """Register the rebuild plugin against a fake MCP; yield {name -> fn}."""
    from kiln.plugins.design_rebuild_tools import plugin

    captured: dict[str, Any] = {}

    class _Mcp:
        def tool(self, *args, **kwargs):
            def deco(fn):
                captured[kwargs.get("name", fn.__name__)] = fn
                return fn

            return deco

    plugin.register(_Mcp())
    return captured


def _slicer_available() -> bool:
    try:
        from kiln.slicer import find_slicer

        find_slicer()
        return True
    except Exception:
        return False


def _openscad_available() -> bool:
    return shutil.which("openscad") is not None


needs_slicer = pytest.mark.skipif(
    not _slicer_available(), reason="no PrusaSlicer/OrcaSlicer installed",
)
needs_openscad = pytest.mark.skipif(
    not _openscad_available(), reason="no OpenSCAD installed",
)


def _write_box_stl(path: Path, size: float = 20.0, height: float = 10.0) -> str:
    """A real watertight box the real slicer accepts."""
    v = [
        (0, 0, 0), (size, 0, 0), (size, size, 0), (0, size, 0),
        (0, 0, height), (size, 0, height), (size, size, height), (0, size, height),
    ]
    faces = [
        (0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7),
        (0, 1, 5), (0, 5, 4), (1, 2, 6), (1, 6, 5),
        (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7),
    ]
    with open(path, "wb") as fh:
        fh.write(b"\0" * 80)
        fh.write(struct.pack("<I", len(faces)))
        for a, b, c in faces:
            fh.write(struct.pack("<3f", 0, 0, 1))
            for i in (a, b, c):
                fh.write(struct.pack("<3f", *v[i]))
            fh.write(struct.pack("<H", 0))
    return str(path)


def _make_mesh_recipe(root: Path, n_parts: int) -> Path:
    """A real n-part mesh recipe in its own directory."""
    d = root / f"design_{n_parts}p"
    d.mkdir(parents=True, exist_ok=True)
    parts = []
    for i in range(n_parts):
        name = f"part{i}"
        stl = d / f"{name}.stl"
        _write_box_stl(stl, size=10.0, height=6.0)
        parts.append(
            DesignPart(
                name=name,
                role="structural",
                stl_path=str(stl),
                color=["white", "black", "red"][i % 3],
                filament_slot=i,
            )
        )
    recipe = DesignRecipe(
        name=f"test-{n_parts}p",
        created="2026-08-17T00:00:00+00:00",
        parts=parts,
        merge_order=[p.name for p in parts],
    )
    save_recipe(recipe, str(d))
    return d


_PARAMETRIC_SCAD = """\
size = 10; // mm
wall = 3;  // mm
cube([size, size, wall]);
"""


def _make_parametric_recipe(
    root: Path, *, parameters: dict[str, Any] | None = None,
) -> Path:
    """A recipe born parametric: OpenSCAD source + parameters, no parts."""
    d = root / "design_scad"
    d.mkdir(parents=True, exist_ok=True)
    stl = d / "compiled.stl"
    _write_box_stl(stl, size=10.0, height=3.0)
    recipe = DesignRecipe(
        name="test-parametric",
        created="2026-08-17T00:00:00+00:00",
        parts=[],
        source_scad=_PARAMETRIC_SCAD,
        parameters=parameters if parameters is not None else {},
        stl_path=str(stl),
    )
    save_recipe(recipe, str(d))
    return d


def _stl_bbox(path: str) -> tuple[float, float, float]:
    """(x, y, z) extents of an STL, ASCII or binary — measured, not trusted.

    OpenSCAD emits ASCII STL; the fixtures here write binary.  A parser
    that assumes one format reads plausible garbage from the other, so
    sniff first.
    """
    xs: list[float] = []
    ys: list[float] = []
    zs: list[float] = []
    raw = Path(path).read_bytes()
    if raw[:5] == b"solid" and b"facet" in raw[:512]:
        for line in raw.decode("utf-8", errors="replace").splitlines():
            line = line.strip()
            if line.startswith("vertex"):
                _, x, y, z = line.split()
                xs.append(float(x))
                ys.append(float(y))
                zs.append(float(z))
    else:
        (count,) = struct.unpack_from("<I", raw, 80)
        offset = 84
        for _ in range(count):
            if offset + 50 > len(raw):
                break
            coords = struct.unpack_from("<12f", raw, offset)
            for j in range(3, 12, 3):
                xs.append(coords[j])
                ys.append(coords[j + 1])
                zs.append(coords[j + 2])
            offset += 50
    assert xs, f"no vertices parsed from {path}"
    return (max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs))


def _artifact_of(result: dict[str, Any]) -> str | None:
    return result.get("output_3mf") or result.get("output_gcode")


# ---------------------------------------------------------------------------


class TestNormalizeColor:
    def test_strips_whitespace(self) -> None:
        assert normalize_color("  Black ") == "black"

    def test_grey_alias(self) -> None:
        assert normalize_color("Grey") == "gray"


class TestFreeForEveryone:
    """The point of moving this tool: no tier gate stands between a user
    and rebuilding their own design.  It previously required Pro."""

    def test_no_tier_gate_in_the_tool(self, tools) -> None:
        import inspect

        src = inspect.getsource(tools["rebuild_design"])
        for gate in ("check_pro", "check_business", "check_enterprise",
                     "requires_tier", "LicenseTier"):
            assert gate not in src, f"rebuild_design regained a {gate} gate"

    def test_runs_with_kiln_pro_absent(self, tools, tmp_path, monkeypatch) -> None:
        """A free user has no kiln-pro on disk at all.  The preview wiring
        is an optional import; losing it must cost the preview, never the
        rebuild."""
        import builtins

        real_import = builtins.__import__

        def _no_pro(name, *a, **kw):
            if name.startswith("kiln_pro"):
                raise ImportError("kiln_pro not installed")
            return real_import(name, *a, **kw)

        monkeypatch.setattr(builtins, "__import__", _no_pro)
        r = tools["rebuild_design"](recipe_path=str(tmp_path / "nope"))
        assert r["code"] == "RECIPE_NOT_FOUND"


class TestRefusals:
    def test_no_recipe(self, tools, tmp_path) -> None:
        r = tools["rebuild_design"](recipe_path=str(tmp_path / "nothing"))
        assert r["status"] == "error"
        assert r["code"] == "RECIPE_NOT_FOUND"

    def test_empty_recipe_refuses_out_loud(self, tools, tmp_path) -> None:
        """No parts, no STL, no source: there is nothing to build, and the
        tool must say so rather than report a success with no artifact."""
        d = tmp_path / "empty"
        d.mkdir()
        save_recipe(
            DesignRecipe(
                name="empty", created="2026-08-17T00:00:00+00:00", parts=[],
            ),
            str(d),
        )
        r = tools["rebuild_design"](recipe_path=str(d))
        assert r["status"] == "error"
        assert r["code"] == "REBUILD_EMPTY"

    def test_recipe_file_path_also_accepted(self, tools, tmp_path) -> None:
        """Callers hand us the directory or the file; both must resolve —
        the asymmetry between them is what broke this tool historically."""
        d = tmp_path / "empty2"
        d.mkdir()
        save_recipe(
            DesignRecipe(
                name="e2", created="2026-08-17T00:00:00+00:00", parts=[],
            ),
            str(d),
        )
        r = tools["rebuild_design"](recipe_path=str(find_recipe(str(d))))
        assert r["code"] == "REBUILD_EMPTY", r


@needs_slicer
class TestMeshMode:
    def test_two_part_rebuild_slices_and_merges(self, tools, tmp_path) -> None:
        d = _make_mesh_recipe(tmp_path, 2)
        r = tools["rebuild_design"](recipe_path=str(d))
        assert r["status"] == "success", r
        assert r["mode"] == "mesh"
        assert len(r["parts"]) == 2
        assert _artifact_of(r), r

    def test_mesh_plus_source_says_parameters_do_not_reach(
        self, tools, tmp_path,
    ) -> None:
        """A recipe with BOTH parts and source: the parts came from steps
        rebuild cannot replay, so it re-slices them and must say that a
        parameter edit does not reach this build."""
        d = _make_mesh_recipe(tmp_path, 1)
        recipe = _load(d)
        recipe.source_scad = _PARAMETRIC_SCAD
        recipe.parameters = {"size": 40}
        save_recipe(recipe, str(d))
        r = tools["rebuild_design"](recipe_path=str(d))
        assert r["status"] == "success", r
        assert r["mode"] == "mesh"
        assert "note" in r and "do not reach" in r["note"]


@needs_slicer
@needs_openscad
class TestParametricMode:
    def test_parameter_change_rederives_geometry(self, tools, tmp_path) -> None:
        """The whole reason this tool exists: change the PARAMETER and the
        geometry is recompiled, so the wall stays the thickness it was
        designed to be instead of being stretched with everything else."""
        d = _make_parametric_recipe(tmp_path, parameters={"size": 30})
        r = tools["rebuild_design"](recipe_path=str(d))
        assert r["status"] == "success", r
        assert r["mode"] == "parametric"
        assert r["parameters_applied"] == {"size": 30}

        x, y, z = _stl_bbox(r["compiled_stl"])
        assert abs(x - 30.0) < 0.6, (x, y, z)
        assert abs(y - 30.0) < 0.6, (x, y, z)
        # The wall did NOT scale with the body — mesh-scaling 10->30 would
        # have tripled it to 9mm.
        assert abs(z - 3.0) < 0.3, (x, y, z)

        assert "size = 30" in _load(d).source_scad.replace(" ;", ";")

    def test_unknown_and_non_numeric_parameters_reported(
        self, tools, tmp_path,
    ) -> None:
        """An edit the rebuild cannot honor is REPORTED, never dropped."""
        d = _make_parametric_recipe(
            tmp_path, parameters={"size": 20, "nope": 5, "label": "hello"},
        )
        r = tools["rebuild_design"](recipe_path=str(d))
        assert r["status"] == "success", r
        assert r["parameters_applied"] == {"size": 20}
        assert r["parameters_not_in_source"] == ["nope"]
        assert "label" in r["parameters_skipped"]

    def test_generator_shape_parameters_values_nesting(
        self, tools, tmp_path,
    ) -> None:
        """Generator-born recipes nest values under parameters['values'] and
        carry bookkeeping beside them; bookkeeping is not a SCAD parameter."""
        d = _make_parametric_recipe(
            tmp_path,
            parameters={"product_type": "coaster", "values": {"size": 25}},
        )
        r = tools["rebuild_design"](recipe_path=str(d))
        assert r["status"] == "success", r
        assert r["parameters_applied"] == {"size": 25}
        assert "product_type" not in r["parameters_skipped"]

    def test_broken_scad_refuses_with_compile_error(
        self, tools, tmp_path,
    ) -> None:
        d = _make_parametric_recipe(tmp_path, parameters={"size": 12})
        recipe = _load(d)
        recipe.source_scad = "size = 12;\ncube([size, size,"
        save_recipe(recipe, str(d))
        r = tools["rebuild_design"](recipe_path=str(d))
        assert r["status"] == "error"
        assert r["code"] == "COMPILE_ERROR"


@needs_slicer
class TestBriefPassthrough:
    def test_brief_id_persists_on_the_recipe(self, tools, tmp_path) -> None:
        d = _make_mesh_recipe(tmp_path, 1)
        r = tools["rebuild_design"](recipe_path=str(d), brief_id="brief-42")
        assert r["status"] == "success", r
        assert _load(d).brief_id == "brief-42"

    def test_omitted_brief_id_preserves_the_existing_goal(
        self, tools, tmp_path,
    ) -> None:
        d = _make_mesh_recipe(tmp_path, 1)
        recipe = _load(d)
        recipe.brief_id = "brief-original"
        save_recipe(recipe, str(d))
        r = tools["rebuild_design"](recipe_path=str(d))
        assert r["status"] == "success", r
        assert _load(d).brief_id == "brief-original"


# ---------------------------------------------------------------------------
# Numeric fidelity — the EMITTED BYTES, not the tool's claim
#
# The measurement helpers are inlined rather than imported: public Kiln's
# suite cannot import kiln-pro, where the shared parsers live.  Keep the
# two in step when either changes.
# ---------------------------------------------------------------------------

import re
import zipfile

_G_MOVE = re.compile(r"^G[01]\b")
_X_WORD = re.compile(r"\bX(-?\d+(?:\.\d+)?)")
_Y_WORD = re.compile(r"\bY(-?\d+(?:\.\d+)?)")
_Z_WORD = re.compile(r"\bZ(-?\d+(?:\.\d+)?)")
_E_WORD = re.compile(r"\bE(-?\d+(?:\.\d+)?)")
_TOOL = re.compile(r"^T(\d+)\s*$")


def _gcode_census(text: str) -> tuple[float, int, dict[int, int]]:
    """(z_max, extruding_moves, tool_changes) of a gcode body.

    Z counts every move so lifts are visible; extrusion counts only lines
    carrying an E word, because travel visits places the print never touches.
    """
    zs: list[float] = []
    extruding = 0
    tools: dict[int, int] = {}
    for line in text.splitlines():
        t = _TOOL.match(line.strip())
        if t:
            n = int(t.group(1))
            tools[n] = tools.get(n, 0) + 1
            continue
        if not _G_MOVE.match(line):
            continue
        z = _Z_WORD.search(line)
        if z:
            zs.append(float(z.group(1)))
        if not _E_WORD.search(line):
            continue
        if _X_WORD.search(line) and _Y_WORD.search(line):
            extruding += 1
    return (max(zs) if zs else 0.0), extruding, tools


def _unwrap(path: str) -> str:
    """The plate gcode inside a wrapped .3mf, or the file itself."""
    if not path.endswith(".3mf"):
        return Path(path).read_text()
    with zipfile.ZipFile(path) as zf:
        entries = [n for n in zf.namelist() if n.endswith(".gcode")]
        if not entries:
            raise KeyError(f"no .gcode entry in {path}: {zf.namelist()}")
        return zf.read(entries[0]).decode("utf-8", errors="replace")


def _two_part_recipe(root: Path) -> Path:
    """A real two-part recipe: a 10mm body on slot 0, a 3mm lid on slot 1."""
    d = root / "twopart"
    d.mkdir(parents=True, exist_ok=True)
    body = d / "body.stl"
    lid = d / "lid.stl"
    _write_box_stl(body, size=12.0, height=10.0)
    _write_box_stl(lid, size=12.0, height=3.0)
    recipe = DesignRecipe(
        name="twopart",
        created="2026-08-17T00:00:00+00:00",
        parts=[
            DesignPart(name="body", role="structural", stl_path=str(body),
                       color="white", filament_slot=0),
            DesignPart(name="lid", role="structural", stl_path=str(lid),
                       color="black", filament_slot=1),
        ],
        merge_order=["body", "lid"],
    )
    save_recipe(recipe, str(d))
    return d


@needs_slicer
class TestRebuildArtifactFidelity:
    """The ledger's `proven` row points here: numbers out of the emitted
    bytes, never the presence of a string."""

    def test_rebuild_reslices_every_part_into_one_artifact(
        self, tools, tmp_path,
    ) -> None:
        """A rebuilt two-part design emits ONE artifact carrying BOTH
        parts: both tools selected, the tall part's Z range present, and
        real extrusion."""
        d = _two_part_recipe(tmp_path)
        r = tools["rebuild_design"](recipe_path=str(d))
        assert r.get("status") == "success", r
        assert r.get("mode") == "mesh", r
        assert len(r.get("parts") or []) == 2, r
        art = _artifact_of(r)
        assert art and Path(art).exists(), r

        z_max, extruding, tool_changes = _gcode_census(_unwrap(art))
        assert 0 in tool_changes and 1 in tool_changes, (
            f"merged artifact does not select both tools: {tool_changes}"
        )
        assert z_max >= 9.0, z_max
        assert extruding > 100, extruding

    def test_merge_order_is_an_order_not_a_filter(self, tools, tmp_path) -> None:
        """A stale merge_order that omits a part must not silently drop it
        from the print — omitted parts append in recipe order.  A vanished
        part is the quietest possible failure: the artifact still slices,
        still prints, and the user finds out at assembly."""
        import json as _json

        d = _two_part_recipe(tmp_path)
        recipe_file = Path(find_recipe(str(d)))
        data = _json.loads(recipe_file.read_text())
        data["merge_order"] = ["lid"]  # stale: body missing
        recipe_file.write_text(_json.dumps(data))

        r = tools["rebuild_design"](recipe_path=str(d))
        assert r.get("status") == "success", r
        _, _, tool_changes = _gcode_census(_unwrap(_artifact_of(r)))
        assert 0 in tool_changes and 1 in tool_changes, (
            f"the part missing from merge_order was dropped: {tool_changes}"
        )

    def test_parametric_rebuild_emits_a_real_print_body(
        self, tools, tmp_path,
    ) -> None:
        """The parametric mode's artifact is a real print, not an empty
        shell: the re-derived 30mm plate extrudes and reaches its 3mm wall."""
        if not _openscad_available():
            pytest.skip("no OpenSCAD installed")
        d = _make_parametric_recipe(tmp_path, parameters={"size": 30})
        r = tools["rebuild_design"](recipe_path=str(d))
        assert r.get("status") == "success", r
        z_max, extruding, _ = _gcode_census(_unwrap(_artifact_of(r)))
        assert extruding > 50, extruding
        assert z_max >= 2.0, z_max
