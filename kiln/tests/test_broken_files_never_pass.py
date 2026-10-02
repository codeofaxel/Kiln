"""A broken file never reads as ready to print -- at any door.

2026-09-30: the pre-print check every print door runs called an empty file
"ready to print" at 65/100, and a garbage file and a single flat triangle
with it.  "No geometry" was logged as a note, and each check that could not
run cost only a few points, so a file nothing had measured still passed.
Three other checks gave the same flat triangle an A or "valid".  And five of
the six print doors printed anyway when the check itself crashed.

This file runs the experiment that would have caught all of it: every
verdict engine, verdict tool and print door is handed files that are empty,
unreadable, truncated, flat or degenerate, and none may say the part can
print, while a real part still passes, so a door that refuses everything
cannot hide here either.  Below that, every place in Kiln that calls a
verdict engine is accounted for, so the next door into a verdict is decided
the day it is written, and nothing may assemble the check's steps into a
private copy.
"""

from __future__ import annotations

import ast
import contextlib
import json
import struct
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "kiln"


# ---------------------------------------------------------------------------
# The files
# ---------------------------------------------------------------------------


def _facet(*vertices: tuple[float, float, float]) -> bytes:
    return struct.pack("<3f", 0, 0, 0) + b"".join(struct.pack("<3f", *v) for v in vertices) + b"\0\0"


def _binary_stl(*facets: bytes, declared: int | None = None) -> bytes:
    return b"\0" * 80 + struct.pack("<I", len(facets) if declared is None else declared) + b"".join(facets)


_TRIANGLE = _facet((0, 0, 0), (10, 0, 0), (0, 10, 0))

#: Each of these must be refused by every door, with the reason.
BROKEN = {
    "zero_bytes.stl": b"",
    "empty_ascii.stl": b"solid test\nendsolid test\n",
    "zero_triangles.stl": _binary_stl(),
    "garbage.stl": b"this is not a mesh at all " * 40,
    "truncated.stl": _binary_stl(_TRIANGLE, declared=1000),
    "single_flat_triangle.stl": _binary_stl(_TRIANGLE),
    "tilted_flat_sheet.stl": _binary_stl(
        _facet((0, 0, 0), (10, 0, 5), (0, 10, 0)), _facet((10, 0, 5), (10, 10, 5), (0, 10, 0)),
    ),
    "degenerate.stl": _binary_stl(_facet((0, 0, 0), (1, 1, 1), (2, 2, 2)), _facet((5, 5, 5), (5, 5, 5), (5, 5, 5))),
    "garbage.obj": b"this is not an obj\nhello world\n",
    "garbage.3mf": b"not a zip archive at all",
    "garbage.step": b"ISO-10303-21;\nHEADER;\nthis is not a real step file\nENDSEC;\nEND-ISO-10303-21;\n",
    "missing.stl": None,
}


def _cube(size: float = 20.0) -> bytes:
    v = [(0, 0, 0), (size, 0, 0), (size, size, 0), (0, size, 0),
         (0, 0, size), (size, 0, size), (size, size, size), (0, size, size)]
    faces = [(0, 3, 2), (0, 2, 1), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
             (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]
    return _binary_stl(*(_facet(v[a], v[b], v[c]) for a, b, c in faces))


@pytest.fixture(scope="module")
def files(tmp_path_factory) -> dict[str, str]:
    """The broken files, and one real part: a 20 mm cube."""
    d = tmp_path_factory.mktemp("files")
    out = {}
    for name, data in {**BROKEN, "cube.stl": _cube()}.items():
        path = d / name
        if data is not None:
            path.write_bytes(data)
        out[name] = str(path)
    return out


BROKEN_NAMES = sorted(BROKEN)


# ---------------------------------------------------------------------------
# The verdict engines
# ---------------------------------------------------------------------------


def _pipeline(path: str) -> bool:
    from kiln.plugins.validation_pipeline_tools import run_full_validation_pipeline

    return run_full_validation_pipeline(path, printer_id="bambu_a1", material="pla")["ready_to_print"]


def _readiness(path: str) -> bool:
    from kiln.generation.validation import can_print_now

    return can_print_now(path)["can_print"]


def _generated_mesh_pipeline(path: str) -> bool:
    from kiln.mesh_validation_pipeline import run_validation_pipeline

    return run_validation_pipeline(path).passed


def _printability(path: str) -> bool:
    from kiln.printability import analyze_printability

    return analyze_printability(path).printable


def _mesh_validation(path: str) -> bool:
    from kiln.generation.validation import validate_mesh

    return validate_mesh(path).valid


def _scorecard(path: str) -> bool:
    from kiln.generation.validation import design_scorecard

    return bool(design_scorecard(path))  # any grade at all is a verdict on the part


#: name -> (engine, whether it said the part can print).  Raising is a refusal.
ENGINES = {
    "the shared pre-print check": _pipeline,
    "can_print_now": _readiness,
    "the generated-mesh pipeline": _generated_mesh_pipeline,
    "analyze_printability": _printability,
    "validate_mesh": _mesh_validation,
    "design_scorecard": _scorecard,
}


def _verdict(engine, path: str) -> bool:
    try:
        return bool(engine(path))
    except (ValueError, FileNotFoundError):
        return False


@pytest.mark.parametrize("engine", sorted(ENGINES))
@pytest.mark.parametrize("name", BROKEN_NAMES)
def test_no_verdict_engine_passes_a_broken_file(engine, name, files):
    assert _verdict(ENGINES[engine], files[name]) is False, f"{engine} passed {name}"


@pytest.mark.parametrize("engine", sorted(ENGINES))
def test_every_verdict_engine_still_passes_a_real_part(engine, files):
    """The control: an engine that refuses everything is not a fix."""
    assert _verdict(ENGINES[engine], files["cube.stl"]) is True, engine


def test_the_shared_check_says_why_and_stops_before_judging(files):
    """A file with no part in it stops at the geometry step: nothing after it
    can measure anything, and the old steps each found nothing wrong."""
    from kiln.plugins.validation_pipeline_tools import run_full_validation_pipeline

    report = run_full_validation_pipeline(files["single_flat_triangle.stl"])

    assert report["ready_to_print"] is False and report["readiness_score"] == 0
    assert report["summary"].startswith("Not ready: This file is flat"), report["summary"]
    assert [c["name"] for c in report["checks"]] == ["format", "mesh_geometry"]


def test_a_check_that_could_not_run_is_not_a_pass(files):
    """The printability analysis failing on a real part is a refusal that says
    so -- not a skipped step that still counts toward "ready"."""
    from kiln.plugins.validation_pipeline_tools import run_full_validation_pipeline

    with patch("kiln.printability.analyze_printability", side_effect=RuntimeError("analysis fell over")):
        report = run_full_validation_pipeline(files["cube.stl"])

    check = next(c for c in report["checks"] if c["name"] == "printability")
    assert report["ready_to_print"] is False
    assert check["passed"] is False and check["severity"] == "error"
    assert "analysis fell over" in check["details"]


# ---------------------------------------------------------------------------
# The verdict tools
# ---------------------------------------------------------------------------

_VERDICT_KEYS = ("ready_to_print", "can_print", "printable", "passed", "valid", "gate_passed", "printable_after")


def _verdicts(result: dict[str, Any]) -> list[bool]:
    """Every verdict on the result and one level into it -- or, for a tool
    that gives none (a scorecard), whether it answered at all.  Lists are not
    read: a per-check ``passed`` is not a verdict on the part."""
    layers = [result, *(v for v in result.values() if isinstance(v, dict))]
    found = [layer[key] for layer in layers for key in _VERDICT_KEYS if isinstance(layer.get(key), bool)]
    return found or [result.get("success") is True]


@pytest.fixture(scope="module")
def tools() -> dict[str, Any]:
    from kiln.plugins.design_reasoning_tools import _DesignReasoningToolsPlugin
    from kiln.plugins.generation_tools import _GenerationToolsPlugin
    from kiln.plugins.mesh_tools import _MeshToolsPlugin
    from kiln.plugins.printability_tools import _PrintabilityToolsPlugin
    from kiln.plugins.validation_pipeline_tools import _ValidationPipelinePlugin

    registered: dict[str, Any] = {}

    class _FakeMcp:
        def tool(self, name: str | None = None, **_kwargs):
            def decorator(fn):
                registered[name or fn.__name__] = fn
                return fn

            return decorator

    for plugin in (_ValidationPipelinePlugin, _DesignReasoningToolsPlugin, _PrintabilityToolsPlugin,
                   _GenerationToolsPlugin, _MeshToolsPlugin):
        plugin().register(_FakeMcp())
    return registered


#: name -> how to call it on one file.
TOOLS = {
    "validate_and_prepare": lambda t, p: t["validate_and_prepare"](input_path=p),
    "prepare_ai_model_for_print": lambda t, p: t["prepare_ai_model_for_print"](input_path=p),
    "check_print_readiness": lambda t, p: t["check_print_readiness"](file_path=p),
    "analyze_printability": lambda t, p: t["analyze_printability"](file_path=p),
    "validate_and_prepare_mesh": lambda t, p: t["validate_and_prepare_mesh"](file_path=p),
    "validate_generated_mesh": lambda t, p: t["validate_generated_mesh"](file_path=p),
    "mesh_quality_scorecard": lambda t, p: t["mesh_quality_scorecard"](file_path=p),
}


@pytest.fixture
def no_auth(monkeypatch):
    import kiln.server as srv

    monkeypatch.setattr(srv, "_check_auth", lambda *_a, **_k: None)


@pytest.mark.parametrize("tool", sorted(TOOLS))
@pytest.mark.parametrize("name", BROKEN_NAMES)
def test_no_verdict_tool_passes_a_broken_file(tool, name, files, tools, no_auth):
    result = TOOLS[tool](tools, files[name])
    verdicts = _verdicts(result)

    assert not any(verdicts), f"{tool} passed {name}: {result}"


@pytest.mark.parametrize("tool", sorted(TOOLS))
def test_every_verdict_tool_still_passes_a_real_part(tool, files, tools, no_auth):
    result = TOOLS[tool](tools, files["cube.stl"])
    verdicts = _verdicts(result)

    assert verdicts and all(verdicts), f"{tool} did not pass a 20 mm cube: {result}"


# ---------------------------------------------------------------------------
# CAD: converted, then judged -- never passed on nothing
# ---------------------------------------------------------------------------


def test_a_cad_file_is_converted_and_judged(tmp_path):
    build123d = pytest.importorskip("build123d")
    from kiln.plugins.validation_pipeline_tools import run_full_validation_pipeline

    step = tmp_path / "bracket.step"
    build123d.export_step(build123d.Box(30, 20, 10), str(step))

    report = run_full_validation_pipeline(str(step))

    names = [c["name"] for c in report["checks"]]
    assert report["ready_to_print"] is True, report["summary"]
    assert "step_conversion" in names and report["printability_score"] is not None
    assert "conversion" in report


# ---------------------------------------------------------------------------
# The shared print gate, and every print door that must use it
# ---------------------------------------------------------------------------


def test_the_gate_refuses_a_broken_file_with_the_check_s_reason(files):
    """No part to print, so no "print it anyway": that would hand the
    slicer nothing."""
    from kiln.plugins.validation_pipeline_tools import gate_for_print

    gate = gate_for_print(files["garbage.stl"])

    assert gate.code == "VALIDATION_FAILED" and "could not read any geometry" in gate.reason
    assert gate.refusal == gate.reason and "skip_validation" not in gate.refusal


def test_a_real_part_that_fails_the_check_can_still_be_printed_anyway(files):
    from kiln.plugins.validation_pipeline_tools import gate_for_print

    failed = {
        "ready_to_print": False, "summary": "Not ready (readiness 35/100). 2 issues: thin walls",
        "checks": [{"name": "mesh_geometry", "passed": True}],
    }
    with patch("kiln.plugins.validation_pipeline_tools.run_full_validation_pipeline", return_value=failed):
        gate = gate_for_print(files["cube.stl"])

    assert gate.refusal.endswith("Pass skip_validation=True to bypass.")


def test_the_gate_refuses_when_the_check_cannot_run(files):
    from kiln.plugins.validation_pipeline_tools import gate_for_print

    with patch("kiln.plugins.validation_pipeline_tools.run_full_validation_pipeline", side_effect=MemoryError()):
        gate = gate_for_print(files["cube.stl"])

    assert gate.code == "VALIDATION_ERROR" and "could not check" in gate.reason
    assert gate.report is None and gate.path == files["cube.stl"]


def test_the_gate_passes_what_is_not_a_mesh_and_a_real_part(files, tmp_path):
    from kiln.plugins.validation_pipeline_tools import gate_for_print

    gcode = tmp_path / "part.gcode"
    gcode.write_text("G28\n")
    sliced = gate_for_print(str(gcode))
    part = gate_for_print(files["cube.stl"])

    assert sliced.reason is None and sliced.report is None
    assert part.reason is None and part.summary["ready_to_print"] is True


class _CheckCrashed(RuntimeError):
    pass


def _crashing_check():
    return patch(
        "kiln.plugins.validation_pipeline_tools.run_full_validation_pipeline",
        side_effect=_CheckCrashed("validator fell over"),
    )


@pytest.mark.parametrize("pipeline", ["quick_print", "reslice_and_print", "benchmark"])
@pytest.mark.parametrize("trouble", ["broken file", "check crashed"])
def test_no_print_pipeline_slices_what_failed_or_was_never_checked(pipeline, trouble, files):
    import kiln.pipelines as pipelines

    path = files["cube.stl"] if trouble == "check crashed" else files["garbage.stl"]
    crash = _crashing_check() if trouble == "check crashed" else contextlib.nullcontext()
    with crash, patch("kiln.slicer.slice_file") as slice_file:
        result = getattr(pipelines, pipeline)(model_path=path)

    step = next(s for s in result.steps if s.name == "validate_mesh")
    assert result.success is False and step.success is False, step.message
    # Printing anyway is offered for a check that crashed, never for a file with no part.
    assert ("skip_validation=True" in step.message) == (trouble == "check crashed")
    slice_file.assert_not_called()


def _slice_and_print():
    from kiln.plugins.slicer_tools import _SlicerToolsPlugin

    registered: dict[str, Any] = {}

    class _FakeMcp:
        def tool(self, name: str | None = None, **_kwargs):
            def decorator(fn):
                registered[name or fn.__name__] = fn
                return fn

            return decorator

    _SlicerToolsPlugin().register(_FakeMcp())
    return registered["slice_and_print"]


@pytest.mark.parametrize(("trouble", "code"), [("broken file", "VALIDATION_FAILED"), ("check crashed", "VALIDATION_ERROR")])
def test_slice_and_print_does_not_slice_what_failed_or_was_never_checked(trouble, code, files, no_auth):
    path = files["cube.stl"] if trouble == "check crashed" else files["garbage.stl"]
    crash = _crashing_check() if trouble == "check crashed" else contextlib.nullcontext()
    with crash, patch("kiln.slicer.slice_file") as slice_file:
        result = _slice_and_print()(input_path=path)

    assert result["success"] is False and result["error"]["code"] == code, result
    slice_file.assert_not_called()


def test_generate_and_print_does_not_slice_a_model_that_was_never_checked(files, monkeypatch, no_auth):
    import kiln.server as srv
    from kiln.generation.base import GenerationJob, GenerationResult, GenerationStatus

    provider = MagicMock()
    provider.display_name = "OpenSCAD"
    provider.generate.return_value = GenerationJob(
        id="j", provider="meshy", prompt="a cube", status=GenerationStatus.SUCCEEDED,
        progress=100, created_at=1000.0, format="stl",
    )
    provider.download_result.return_value = GenerationResult(
        job_id="j", provider="meshy", local_path=files["cube.stl"], format="stl",
        file_size_bytes=684, prompt="a cube",
    )
    monkeypatch.setattr(srv, "_get_generation_provider", lambda *_a, **_k: provider)
    monkeypatch.setattr(srv, "_get_adapter", lambda: MagicMock())
    monkeypatch.setattr(srv, "_resolve_adapter", lambda *_a, **_k: MagicMock())
    with _crashing_check(), patch("kiln.slicer.slice_file") as slice_file:
        result = srv.generate_and_print("a cube", provider="meshy")

    assert result["success"] is False and result["error"]["code"] == "VALIDATION_ERROR", result
    slice_file.assert_not_called()


class TestTheCommandLine:
    @pytest.fixture(autouse=True)
    def _no_preview_gate(self, monkeypatch):
        monkeypatch.setenv("KILN_SKIP_PREVIEW_GATE", "1")

    def _slice(self, path: str, *args: str):
        from click.testing import CliRunner

        from kiln.cli.main import cli

        with patch("kiln.cli.main._autodetect_printer_profile_id", return_value=None), \
                patch("kiln.persistence.get_db"), \
                patch("kiln.slicer.slice_file") as slice_file:
            result = CliRunner().invoke(cli, ["slice", path, "--json", *args])
        return result, slice_file

    def test_slice_print_after_refuses_a_broken_file(self, files):
        result, slice_file = self._slice(files["garbage.stl"], "--print-after")

        assert result.exit_code == 1, result.output
        error = json.loads(result.output)["error"]
        assert error["code"] == "VALIDATION_FAILED" and "--skip-validation" not in error["message"]
        slice_file.assert_not_called()

    @pytest.mark.parametrize("name", BROKEN_NAMES)
    def test_validate_fails_a_broken_file(self, name, files):
        from click.testing import CliRunner

        from kiln.cli.main import cli

        if name == "missing.stl":
            pytest.skip("click refuses a path that does not exist before validate runs")
        result = CliRunner().invoke(cli, ["validate", files[name]])
        assert result.exit_code == 1 and "PASS" not in result.output, result.output

    def test_validate_still_passes_a_real_part(self, files):
        from click.testing import CliRunner

        from kiln.cli.main import cli

        result = CliRunner().invoke(cli, ["validate", files["cube.stl"]])
        assert result.exit_code == 0 and "Printability: PASS" in result.output, result.output

    def test_slice_without_printing_is_left_to_the_slicer(self, files):
        """Making G-code is not a print: the gate is on --print-after only."""
        _, slice_file = self._slice(files["garbage.stl"])
        slice_file.assert_called_once()


# ---------------------------------------------------------------------------
# Every door into a verdict, accounted for
# ---------------------------------------------------------------------------

#: The engines whose answer decides whether a part prints.
VERDICT_ENGINES = frozenset({
    "gate_for_print",
    "run_full_validation_pipeline",
    "can_print_now",
    "run_validation_pipeline",
    "validate_mesh",
    "design_scorecard",
    "analyze_printability",
})

#: Every call to a verdict engine in Kiln, and what makes it safe.  A new
#: one fails below until someone writes down which it is: a door covered by
#: a test in this file, or a caller that reads the engine for information and
#: decides nothing about printing.
CALLERS = {
    # The print doors: one shared gate.
    "pipelines.py::_validate_mesh_step": "door: quick_print, reslice_and_print and benchmark, tested above",
    "plugins/slicer_tools.py::register.slice_and_print": "door: tested above",
    "plugins/smart_print_tools.py::register.retry_print_with_fix": (
        "door: the gate, crash-tested in test_smart_print_plugins.py; its analyze_printability "
        "call gathers failure signals for the diagnosis"
    ),
    "plugins/generation_ai_tools.py::register.generate_and_print": "door: tested above",
    "cli/main.py::_cli_print_gate": "door: kiln slice --print-after and kiln generate-and-print, tested above",
    "cli/main.py::validate": "door: kiln validate, tested above",
    "plugins/validation_pipeline_tools.py::gate_for_print": "the gate itself, tested above",
    # The verdict tools and engines.
    "plugins/validation_pipeline_tools.py::register.validate_and_prepare": "tool: tested above",
    "plugins/design_reasoning_tools.py::register.check_print_readiness": "tool: tested above",
    "plugins/printability_tools.py::register.analyze_printability": "tool: tested above",
    "plugins/generation_tools.py::register.validate_and_prepare_mesh": "tool: tested above",
    "plugins/mesh_tools.py::register.validate_generated_mesh": "tool: tested above",
    "plugins/mesh_tools.py::register.mesh_quality_scorecard": "tool: tested above",
    "generation/validation.py::can_print_now": "engine: tested above",
    "generation/validation.py::_printability_factor": "engine: the scorecard's printability factor",
    "mesh_validation_pipeline.py::run_validation_pipeline": "engine: tested above",
    "plugins/_validation_pipeline_internals.py::_step_printability": "engine: a step of the shared check",
    "plugins/_validation_pipeline_internals.py::_step_watertight_check": "engine: a step of the shared check",
    "plugins/_validation_pipeline_internals.py::_step_repair": "engine: a step of the shared check",
    # Callers that read an engine for information and decide nothing.
    "cli/main.py::_auto_support_style": "reads: picks a support style for the slice",
    "cli/main.py::generate": "reads: reports the generated mesh; prints nothing",
    "cli/main.py::generate_download": "reads: reports the downloaded mesh; prints nothing",
    "cli/main.py::generate_and_print_cmd": "reads: the triangle count it echoes; the verdict is _cli_print_gate's",
    "design_validator.py::validate_design": "reads: a design review report; prints nothing",
    "generation/validation.py::optimize_orientation": "reads: scores candidate orientations; prints nothing",
    "mesh_edit_check.py::measure_mesh": "reads: walls and holes, to judge a mesh edit; prints nothing",
    "original_design.py::audit_original_design": "reads: an originality audit; prints nothing",
    "original_design.py::generate_original_design": "reads: checks a generated design; prints nothing",
    "plugins/design_tools.py::register.analyze_warping_risk": "reads: a warping report",
    "plugins/printability_tools.py::register.recommend_adhesion_settings": "reads: an adhesion recommendation",
    "plugins/printability_tools.py::register.diagnose_print_failure_live": "reads: failure signals for a diagnosis",
    "plugins/material_tools.py::register.check_print_health": "reads: adhesion risk for a print already running",
    "plugins/estimate_tools.py::register.slice_and_estimate": "reads: the brim decision for an estimate; prints nothing",
    "arrival.py::measure": (
        "reads: reports what both download doors brought in (download_generated_model, "
        "download_model); prints nothing"
    ),
    "print_service.py::_printability_score": "reads: the quote's score, None when the file cannot be read",
    "server.py::generate_from_template": "reads: reports the generated mesh; prints nothing",
}


class _CallSites(ast.NodeVisitor):
    """``rel::enclosing.function`` for every call to *names* in one module,
    through an import alias too (``analyze_printability as _analyze``)."""

    def __init__(self, rel: str, names: frozenset[str], tree: ast.Module) -> None:
        self.rel, self.names = rel, names
        self.stack: list[str] = []
        self.found: set[str] = set()
        self.aliases = {
            alias.asname: alias.name
            for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
            for alias in node.names if alias.asname
        }

    def visit_FunctionDef(self, node) -> None:
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Call(self, node) -> None:
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if self.aliases.get(name, name) in self.names:
            self.found.add(f"{self.rel}::{'.'.join(self.stack) or '<module>'}")
        self.generic_visit(node)


def _calls(names: frozenset[str]) -> set[str]:
    """``path::enclosing.function`` for every call to *names* in Kiln."""
    found: set[str] = set()
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text())
        sites = _CallSites(path.relative_to(SRC).as_posix(), names, tree)
        sites.visit(tree)
        found |= sites.found
    return found


def test_every_call_to_a_verdict_engine_is_accounted_for():
    calls = _calls(VERDICT_ENGINES)

    unaccounted = sorted(calls - set(CALLERS))
    stale = sorted(set(CALLERS) - calls)
    assert not unaccounted, (
        "New callers of a print verdict.  Add each to CALLERS: a door, with a test "
        f"in this file that feeds it broken files, or a reader that prints nothing: {unaccounted}"
    )
    assert not stale, f"CALLERS names call sites that no longer exist: {stale}"


def test_print_doors_run_the_shared_gate_and_nothing_else_runs_the_check():
    """Each print door once carried its own copy of the gate, and the copies
    disagreed about a crash.  Only the gate and validate_and_prepare (which
    reports the check itself) may run it."""
    assert _calls(frozenset({"run_full_validation_pipeline"})) == {
        "plugins/validation_pipeline_tools.py::gate_for_print",
        "plugins/validation_pipeline_tools.py::register.validate_and_prepare",
    }


def test_nothing_assembles_the_check_s_steps_into_a_copy():
    """kiln-pro's recovery gate re-assembled these steps by hand, and the copy
    never learned the material or the CAD conversion the shared check did."""
    allowed = {"plugins/validation_pipeline_tools.py", "plugins/_validation_pipeline_internals.py"}
    copies = sorted(
        site for site in _calls(frozenset(_step_names()))
        if site.split("::")[0] not in allowed
    )
    assert not copies, f"These run the shared check's steps themselves -- call gate_for_print: {copies}"


def _step_names() -> set[str]:
    tree = ast.parse((SRC / "plugins" / "_validation_pipeline_internals.py").read_text())
    return {
        node.name for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and (node.name.startswith("_step_") or node.name == "_compute_readiness_score")
    } | {"_compute_printability_score"}
