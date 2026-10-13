"""Where the printability analysis runs: here, or through kiln-pro's runner.

``analyze_printability`` hands its work to ``pro_features.run_mesh_analysis``
when kiln-pro provides one, so a server answering many people at once can run
it where one large mesh cannot take that server's memory.  The contract is
that the runner gets the work function and every argument, and the caller
gets back exactly what the work returned.
"""

from __future__ import annotations

import sys
import types

import pytest

import kiln.printability as engine

_CUBE = "solid c\n" + "".join(
    f"facet normal 0 0 0\nouter loop\nvertex {a}\nvertex {b}\nvertex {c}\nendloop\nendfacet\n"
    for a, b, c in [
        ("0 0 0", "10 10 0", "10 0 0"), ("0 0 0", "0 10 0", "10 10 0"),
        ("0 0 10", "10 0 10", "10 10 10"), ("0 0 10", "10 10 10", "0 10 10"),
        ("0 0 0", "10 0 0", "10 0 10"), ("0 0 0", "10 0 10", "0 0 10"),
        ("0 10 0", "10 10 10", "10 10 0"), ("0 10 0", "0 10 10", "10 10 10"),
        ("0 0 0", "0 0 10", "0 10 10"), ("0 0 0", "0 10 10", "0 10 0"),
        ("10 0 0", "10 10 0", "10 10 10"), ("10 0 0", "10 10 10", "10 0 10"),
    ]
) + "endsolid c\n"


@pytest.fixture
def cube(tmp_path) -> str:
    path = tmp_path / "cube.stl"
    path.write_text(_CUBE)
    return str(path)


def _with_bridge(monkeypatch, runner) -> None:
    bridge = types.ModuleType("kiln_pro.bridge")
    # Only the runner: every other kiln-pro feature reads as absent.
    bridge.pro_features = types.SimpleNamespace(
        run_mesh_analysis=runner, is_available=lambda _feature: False,
    )
    monkeypatch.setitem(sys.modules, "kiln_pro", types.ModuleType("kiln_pro"))
    monkeypatch.setitem(sys.modules, "kiln_pro.bridge", bridge)


def test_the_analysis_goes_through_the_runner_with_every_argument(cube, monkeypatch):
    seen: list = []

    def runner(fn, path, **kwargs):
        seen.append((fn, path, kwargs))
        return fn(path, **kwargs)

    _with_bridge(monkeypatch, runner)
    report = engine.analyze_printability(cube, material="petg", layer_height=0.16, printer_id=None)

    assert len(seen) == 1
    fn, path, kwargs = seen[0]
    assert fn is engine._analyze_printability_here and path == cube
    assert kwargs["material"] == "petg" and kwargs["layer_height"] == 0.16
    assert set(kwargs) == {
        "nozzle_diameter", "layer_height", "max_overhang_angle", "build_volume",
        "material", "infill_percent", "include_hole_detection", "printer_id",
        "slicer_style",
    }
    # What the runner returned is the caller's answer, unchanged.
    assert report.to_dict() == engine._analyze_printability_here(
        cube, material="petg", layer_height=0.16
    ).to_dict()


def test_a_runner_out_of_memory_reaches_the_caller(cube, monkeypatch):
    def runner(fn, path, **kwargs):
        raise MemoryError("too large to check here")

    _with_bridge(monkeypatch, runner)
    with pytest.raises(MemoryError, match="too large"):
        engine.analyze_printability(cube)


def test_without_kiln_pro_it_runs_here(cube, monkeypatch):
    monkeypatch.setitem(sys.modules, "kiln_pro", None)
    for name in [n for n in sys.modules if n.startswith("kiln_pro.")]:
        monkeypatch.setitem(sys.modules, name, None)
    assert engine._analysis_runner() is None
    assert engine.analyze_printability(cube).grade


def test_the_tool_says_a_check_too_large_for_the_machine(cube, monkeypatch):
    from kiln.plugins.printability_tools import plugin

    def runner(fn, path, **kwargs):
        raise MemoryError("This model is too large for this machine to check.")

    _with_bridge(monkeypatch, runner)
    tools: dict = {}

    class _MCP:
        def tool(self, **_k):
            def deco(fn):
                tools[fn.__name__] = fn
                return fn

            return deco

    plugin.register(_MCP())
    resp = tools["analyze_printability"](file_path=cube)
    assert resp["success"] is False
    assert resp["error"]["code"] == "MESH_TOO_LARGE"
    assert resp["error"]["message"] == "This model is too large for this machine to check."
