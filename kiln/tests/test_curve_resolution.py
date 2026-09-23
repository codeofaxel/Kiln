"""Curve resolution: one rule decides how finely every curve is faceted.

Covers the rule (``kiln.curve_resolution``) against the chord floor STEP
geometry is tessellated to; the OpenSCAD it states, against OpenSCAD's own
facet count; the threaded jar built at that rule -- its wall, its thread
and its fit, measured on the compiled mesh; and the template doors, which
must all render through ``kiln.parametric.render_template_scad``.

Background (2026-09-22): the threaded jar hard-coded ``$fn = 60``.  A 45 mm
jar printed as 60 flats 2.4 mm wide, each 0.03 mm inside the circle, and
the slicer traced every one -- 60 straight moves per layer, 6 degrees at
each corner -- so the corners stacked into vertical lines up the jar.  Its
thread stepped 10 degrees a station, coarser still.
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

_TEMPLATES = Path(__file__).parent.parent / "src" / "kiln" / "data" / "design_templates.json"


def _template(template_id: str) -> dict:
    return json.loads(_TEMPLATES.read_text(encoding="utf-8"))[template_id]


def _defaults(template: dict) -> dict:
    return {k: v["default"] for k, v in template["parameters"].items()}


def _openscad_or_skip() -> None:
    try:
        from kiln.generation.openscad import _find_openscad

        _find_openscad()
    except Exception:  # noqa: BLE001
        pytest.skip("needs OpenSCAD")


# ---------------------------------------------------------------------------
# Mesh measurements
# ---------------------------------------------------------------------------


def _loop_facets(loop: np.ndarray) -> np.ndarray:
    """A section loop's corners: repeats and collinear points dropped."""
    pts: list[np.ndarray] = []
    for p in loop:
        if not pts or np.linalg.norm(p - pts[-1]) > 1e-5:
            pts.append(p)
    if len(pts) > 1 and np.linalg.norm(pts[0] - pts[-1]) <= 1e-5:
        pts.pop()
    changed = True
    while changed:
        changed = False
        for i in range(len(pts)):
            a, b, c = pts[i - 1], pts[i], pts[(i + 1) % len(pts)]
            v1, v2 = b - a, c - b
            turn = math.atan2(v1[0] * v2[1] - v1[1] * v2[0], float(np.dot(v1, v2)))
            if abs(math.degrees(turn)) < 0.01:
                del pts[i]
                changed = True
                break
    return np.asarray(pts)


def _outer_wall_chord_depth(body, z: float) -> tuple[int, float]:
    """(facets, deepest facet in mm) of the outermost loop at height z.

    A facet's depth is how far its middle sits inside the circle through
    its two corners -- what the slicer traces as a flat.
    """
    section = body.section(plane_origin=[0, 0, z], plane_normal=[0, 0, 1])
    planar, to_3d = section.to_2D()
    centre = body.bounds.mean(axis=0)[:2]
    best = None
    for loop in planar.discrete:
        xyz = np.column_stack([loop, np.zeros(len(loop))])
        xy = (to_3d[:3, :3] @ xyz.T).T[:, :2] + to_3d[:2, 3] - centre
        corners = _loop_facets(xy)
        radius = np.linalg.norm(corners, axis=1)
        if best is None or radius.mean() > best[0].mean():
            best = (radius, corners)
    radius, corners = best
    mids = np.linalg.norm((corners + np.roll(corners, -1, axis=0)) / 2, axis=1)
    depth = (radius + np.roll(radius, -1)) / 2 - mids
    return len(corners), float(depth.max())


def _thread_crest_chord_depth(body, pitch: float) -> float:
    """Deepest chord along the thread crest, in mm.

    Crest vertices are the body's outermost; each sits on the helix, so
    its height gives its unwrapped angle and sorting by that separates
    the turns.  The widest step between stations is the longest chord.
    """
    v = body.vertices
    centre = body.bounds.mean(axis=0)[:2]
    r = np.linalg.norm(v[:, :2] - centre, axis=1)
    crest = v[r > r.max() - 0.02]
    crest_r = float(r.max())
    turn = np.sort((crest[:, 2] - crest[:, 2].min()) / pitch * 360.0)
    steps = np.diff(turn)
    widest = float(steps[steps > 0.05].max())
    return crest_r * (1 - math.cos(math.radians(widest / 2)))


# ---------------------------------------------------------------------------
# The rule
# ---------------------------------------------------------------------------


class TestRule:
    """The two bounds, and how they sit against the STEP chord floor."""

    def test_angle_bound_keeps_large_curves_under_the_step_chord_floor(self):
        from kiln import curve_resolution as cr
        from kiln.step_import import _OCP_LINEAR_DEFLECTION

        by_angle = round(360 / cr.FACET_ANGLE_DEG)
        for r in np.linspace(1.0, 131.0, 400):
            n = cr.fragments(float(r))
            if n == by_angle:
                assert r * (1 - math.cos(math.pi / n)) <= _OCP_LINEAR_DEFLECTION, r

    def test_width_bound_caps_small_curves_at_a_nozzle(self):
        from kiln import curve_resolution as cr

        for r in np.linspace(0.3, 20.0, 400):
            n = cr.fragments(float(r))
            assert 2 * r * math.sin(math.pi / n) <= cr.FACET_WIDTH_MM + 1e-9, r

    def test_python_scad_and_openscad_agree_on_the_count(self, tmp_path):
        _openscad_or_skip()
        import trimesh

        from kiln import curve_resolution as cr
        from kiln.parametric import compile_scad_code

        # OpenSCAD exits 0 when a top-level assert() fails, so the counts
        # are compared in geometry: one ring cut by OpenSCAD's own rule,
        # one by curve_fragments(), both against the Python count.
        for r in (0.3, 1.6, 5.0, 22.5, 50.0):
            stl = compile_scad_code(
                f"cylinder(r = {r}, h = 1);\n"
                f"translate([0, 0, 5]) cylinder(r = {r}, h = 1, $fn = curve_fragments({r}));\n" + cr.SCAD_TRAILER,
                output_path=str(tmp_path / f"c{r}.stl"),
            )
            v = trimesh.load(stl).vertices
            by_openscad = int((np.abs(v[:, 2]) < 1e-6).sum())
            by_scad_function = int((np.abs(v[:, 2] - 5) < 1e-6).sum())
            assert by_openscad == by_scad_function == cr.fragments(r), r


# ---------------------------------------------------------------------------
# The threaded jar, built the way a user gets it
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def jar_45(tmp_path_factory):
    """The 45 mm jar printed 2026-09-22, through generate_from_template."""
    _openscad_or_skip()
    import tempfile

    import trimesh

    import kiln.daily_stats as stats
    import kiln.server as srv

    params = {"diameter": 45, "height": 45}
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(tempfile, "tempdir", str(tmp_path_factory.mktemp("jar")))
        mp.setattr(srv, "_check_auth", lambda *_: None)
        mp.setattr(stats, "record_template_use", lambda *_: None)
        result = srv.generate_from_template("threaded_jar", params)
    assert result.get("success"), result
    mesh = trimesh.load(result["result"]["local_path"])
    parts = sorted(
        mesh.split(only_watertight=False),
        key=lambda b: b.bounds[0][0],
    )
    return SimpleNamespace(
        parts=parts,
        params={**_defaults(_template("threaded_jar")), **params},
    )


class TestThreadedJar:
    """Wall, thread and fit of the 45 mm jar, measured on its mesh."""

    def test_two_parts_and_nothing_else(self, jar_45):
        assert len(jar_45.parts) == 2
        assert all(p.is_watertight for p in jar_45.parts)

    def test_wall_facets_sit_inside_the_chord_floor(self, jar_45):
        from kiln.step_import import _OCP_LINEAR_DEFLECTION

        _, deepest = _outer_wall_chord_depth(jar_45.parts[0], z=8.0)
        assert deepest <= _OCP_LINEAR_DEFLECTION

    def test_thread_steps_as_finely_as_the_wall(self, jar_45):
        from kiln.step_import import _OCP_LINEAR_DEFLECTION

        pitch = jar_45.params["thread_pitch"]
        assert _thread_crest_chord_depth(jar_45.parts[0], pitch) <= _OCP_LINEAR_DEFLECTION

    def test_lid_screws_on_with_its_designed_play(self, jar_45):
        """Seat the lid cap-up on the rim, then lift it through one pitch:
        the free window is the axial play.  Facets cut into the radial
        clearance, and the old 60-facet jar lost a quarter of it."""
        mf = pytest.importorskip("manifold3d")
        import trimesh

        p = jar_45.params
        scad = _template("threaded_jar")["scad_template"]

        def constant(name: str) -> float:
            return float(re.search(rf"^{name} = ([\d.]+);", scad, re.M).group(1))

        # The play the template designs in: both flanks' axial gap, from
        # its own clearance and thread depth.
        pitch = p["thread_pitch"]
        half = pitch * float(re.search(r"^half = pitch \* ([\d.]+);", scad, re.M).group(1))
        depth = min(constant("thread_depth"), half)
        radial = constant("clearance") + constant("thread_depth") - depth
        designed = 2 * (pitch / 2 - half + half * radial / depth)

        jar, lid = jar_45.parts
        lid = lid.copy()
        lid.apply_translation([*-lid.bounds.mean(axis=0)[:2], 0])
        lid.apply_transform(trimesh.transformations.rotation_matrix(math.pi, [1, 0, 0]))
        lid.apply_translation([0, 0, p["height"] + p["wall"]])

        def solid(mesh):
            return mf.Manifold(
                mf.Mesh(
                    vert_properties=np.asarray(mesh.vertices, np.float32),
                    tri_verts=np.asarray(mesh.faces, np.uint32),
                )
            )

        jar_solid = solid(jar)

        def clear(lift: float) -> bool:
            moved = lid.copy()
            moved.apply_translation([0, 0, lift])
            return (jar_solid ^ solid(moved)).volume() < 1e-6

        # Coarse pass over one pitch, then the longest free run's edges
        # to 0.005 mm.  A run touching the first lift is cut short by the
        # rim, not by a flank, so the scan starts clear of it.
        lifts = [0.02 + 0.1 * i for i in range(int(pitch / 0.1) + 1)]
        runs: list[list[float]] = []
        for z in lifts:
            if clear(z):
                if runs and abs(z - runs[-1][-1] - 0.1) < 1e-9:
                    runs[-1].append(z)
                else:
                    runs.append([z])
        assert runs, "the lid cannot sit on the jar anywhere in a turn"
        run = max(runs, key=len)
        assert run[0] > lifts[0] and run[-1] < lifts[-1], "window cut by the scan"

        def edge(inside: float, outside: float) -> float:
            while abs(outside - inside) > 0.005:
                mid = (inside + outside) / 2
                inside, outside = (mid, outside) if clear(mid) else (inside, mid)
            return inside

        play = edge(run[-1], run[-1] + 0.1) - edge(run[0], run[0] - 0.1)
        assert play >= designed - 0.05


# ---------------------------------------------------------------------------
# Every door that builds a template renders it the one way
# ---------------------------------------------------------------------------


def _failed_provider() -> MagicMock:
    provider = MagicMock()
    provider.name = "openscad"
    provider.generate.return_value = SimpleNamespace(
        status=SimpleNamespace(value="failed"),
        error="stopped by test",
        id="x",
        to_dict=dict,
    )
    return provider


class TestTemplateDoorsShareOneRenderer:
    """generate_from_template, generate_template_variations,
    optimize_template_params and design_to_gcode each compile exactly
    what render_template_scad returns."""

    @patch("kiln.server._check_auth", return_value=None)
    def test_generate_from_template(self, _auth):
        import kiln.server as srv
        from kiln.parametric import render_template_scad

        provider = _failed_provider()
        with patch.object(srv, "_get_generation_provider", return_value=provider):
            srv.generate_from_template("threaded_jar", {"diameter": 45})

        tpl = _template("threaded_jar")
        expected = render_template_scad(tpl, {**_defaults(tpl), "diameter": 45})
        assert provider.generate.call_args[0][0] == expected

    @patch("kiln.server._check_auth", return_value=None)
    def test_generate_template_variations(self, _auth):
        import kiln.server as srv
        from kiln.parametric import render_template_scad
        from kiln.plugins import generation_ai_tools

        tools: dict = {}
        generation_ai_tools.plugin.register(
            SimpleNamespace(
                tool=lambda *a, **k: lambda fn: tools.setdefault(fn.__name__, fn),
            )
        )
        provider = _failed_provider()
        with (
            patch.object(srv, "_get_generation_provider", return_value=provider),
            patch("kiln.daily_stats.record_template_use"),
        ):
            result = tools["generate_template_variations"]("threaded_jar", variation_count=2)

        tpl = _template("threaded_jar")
        compiled = [c[0][0] for c in provider.generate.call_args_list]
        assert compiled == [render_template_scad(tpl, v["parameters"]) for v in result["variations"]]

    def test_optimize_template_params(self, tmp_path):
        from kiln.design_reasoning import optimize_template_params
        from kiln.parametric import render_template_scad

        compiled: list[str] = []

        def fake_run(cmd, **_):
            compiled.append(Path(cmd[-1]).read_text())
            return SimpleNamespace(returncode=1)

        with (
            patch("kiln.generation.openscad._find_openscad", return_value="/x/openscad"),
            patch("kiln.openscad_runner.run_openscad", side_effect=fake_run),
            pytest.raises(ValueError, match="No valid variants"),
        ):
            optimize_template_params(
                "threaded_jar",
                samples_per_param=1,
                max_variants=1,
                output_dir=str(tmp_path),
            )

        tpl = _template("threaded_jar")
        assert compiled == [render_template_scad(tpl, _defaults(tpl))]

    @patch("kiln.design_reasoning.search_templates")
    def test_design_to_gcode(self, mock_search, tmp_path):
        from kiln.design_reasoning import TemplateSearchResult, design_to_gcode
        from kiln.parametric import render_template_scad

        mock_search.return_value = TemplateSearchResult(
            query="jar",
            matches=[{"template_id": "threaded_jar", "score": 1.0}],
        )
        with patch("kiln.parametric.compile_scad_code", side_effect=ValueError("stop")):
            result = design_to_gcode("a jar with a lid", output_dir=str(tmp_path))

        tpl = _template("threaded_jar")
        assert Path(result.scad_file).read_text() == render_template_scad(tpl, _defaults(tpl))
