"""Where Kiln saves a result nobody named a place for.

Every such save goes through ``kiln.output_paths.kiln_temp_dir`` (or
``kiln_output_dir``), so a server running Kiln for many people can give each
request that person's own folder (``REQUEST_OUTPUT_ROOT``) and no two
people's results ever share a folder or a file name.  Until 2026-10-26 about
twenty save locations built their folder from ``tempfile.gettempdir()``
directly (``kiln_generated``, ``kiln_sliced``, ``kiln_previews``, a fixed
``kiln_multi_material.3mf``), and every person's results landed side by side.

The census below fails on a new direct ``gettempdir()`` call, so a save
location added later cannot quietly go back to the shared folder.  A call
that saves nothing (a check of where a caller-named path may point, the
terminal on a person's own computer) is listed with its reason.
"""

from __future__ import annotations

import ast
import os
import shutil
import tempfile
from pathlib import Path

import pytest

from kiln import output_paths

SRC = Path(output_paths.__file__).resolve().parent

#: ``file::function`` -> why a direct ``gettempdir()`` there is not a save
#: location for a caller's result.
NOT_A_SAVE_LOCATION = {
    "output_paths.py::kiln_temp_dir": "the one place the machine's temp folder is the default",
    "output_paths.py::_temp_root": "checks where a caller-named path may point",
    "output_paths.py::_name_reason": "checks where a caller-named path may point",
    "persistence.py::<module>": "the test run's own database",
    "plugins/gcode_validation_tools.py::register.validate_print_quality": "checks where a caller-named path may point",
    "plugins/marketplace_tools.py::register.download_model": "checks where a caller-named path may point",
    "plugins/monitoring_tools.py::register.monitor_print_vision": "checks where a caller-named path may point",
    "server.py::printer_snapshot": "checks where a caller-named path may point",
    "generation/openscad.py::validate_scad": "a throwaway compile, deleted before it returns",
    "slicer_profiles.py::resolve_slicer_profile": "the slicer's own settings file, random name, never handed out",
    "slicer_profiles.py::profile_with_overrides": "the slicer's own settings file, random name, never handed out",
    "slicer_profiles.py::resolve_multiextruder_profile": "the slicer's own settings file, random name, never handed out",
    "cli/main.py::report": "the terminal: one person's own computer",
    "cli/main.py::snapshot": "the terminal: one person's own computer",
    "cli/main.py::generate": "the terminal: one person's own computer",
    "cli/main.py::generate_download": "the terminal: one person's own computer",
    "cli/main.py::generate_and_print_cmd": "the terminal: one person's own computer",
}


def _gettempdir_calls() -> set[str]:
    found: set[str] = set()
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        where: list[str] = []
        rel = path.relative_to(SRC).as_posix()

        class Visitor(ast.NodeVisitor):
            def visit_FunctionDef(self, node: ast.AST) -> None:
                where.append(node.name)  # type: ignore[attr-defined]
                self.generic_visit(node)
                where.pop()

            visit_AsyncFunctionDef = visit_FunctionDef

            def visit_Call(self, node: ast.Call) -> None:
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
                if name == "gettempdir":
                    found.add(f"{rel}::{'.'.join(where) or '<module>'}")
                self.generic_visit(node)

        Visitor().visit(tree)
    return found


def test_every_save_location_reads_the_request_folder():
    calls = _gettempdir_calls()
    unlisted = sorted(calls - set(NOT_A_SAVE_LOCATION))
    stale = sorted(set(NOT_A_SAVE_LOCATION) - calls)
    assert not unlisted, (
        f"save in kiln.output_paths.kiln_temp_dir() / kiln_output_dir(), not gettempdir(): {unlisted}"
    )
    assert not stale, f"no longer calls gettempdir(); delete the entry: {stale}"


def test_the_census_sees_a_new_direct_call(tmp_path, monkeypatch):
    """The census can fail: a planted save location is caught by name."""
    planted = SRC / "zz_planted_for_census.py"
    planted.write_text("import tempfile\n\ndef save():\n    return tempfile.gettempdir()\n", encoding="utf-8")
    try:
        assert "zz_planted_for_census.py::save" in _gettempdir_calls()
    finally:
        planted.unlink()


@pytest.fixture
def request_root(tmp_path):
    root = tmp_path / "their-own"
    root.mkdir()
    token = output_paths.REQUEST_OUTPUT_ROOT.set(str(root))
    try:
        yield root
    finally:
        output_paths.REQUEST_OUTPUT_ROOT.reset(token)


def test_unset_it_is_the_machines_temp_folder_as_before():
    assert output_paths.REQUEST_OUTPUT_ROOT.get() is None
    assert output_paths.kiln_temp_dir() == tempfile.gettempdir()


def test_set_every_result_goes_in_the_requests_own_folder(request_root):
    from kiln import model_visualizer

    assert output_paths.kiln_temp_dir() == str(request_root)
    made = Path(output_paths.kiln_output_dir("kiln_generated"))
    assert made.parent == request_root and made.is_dir()
    assert oct(made.stat().st_mode & 0o777) == "0o700"
    assert Path(model_visualizer._default_output_dir()).parent.parent == request_root


def test_a_preview_nobody_named_a_place_for_lands_in_the_request_folder(request_root, tmp_path):
    from kiln.preview import render_multi_view_preview

    mesh = tmp_path / "part.stl"
    mesh.write_text(
        "solid p\nfacet normal 0 0 1\nouter loop\nvertex 0 0 0\nvertex 10 0 0\nvertex 0 10 0\n"
        "endloop\nendfacet\nendsolid p\n",
        encoding="utf-8",
    )
    result = render_multi_view_preview(str(mesh))
    assert Path(result.path).resolve().is_relative_to(request_root.resolve())


@pytest.mark.skipif(shutil.which("openscad") is None, reason="needs OpenSCAD")
def test_a_compiled_model_lands_in_the_request_folder(request_root):
    from kiln.parametric import compile_scad_code

    stl = compile_scad_code("cube(5);", timeout=60)
    assert Path(stl).resolve().is_relative_to(request_root.resolve())
    assert os.path.getsize(stl) > 0
