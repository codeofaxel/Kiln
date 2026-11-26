"""Every failed OpenSCAD compile says why in a code an agent can act on.

``compile_scad`` and ``tweak_and_compile_scad`` answer a failure with a
sentence for the person and a ``code`` for the agent: the file is missing,
nothing was given, OpenSCAD could not build the model, or something else
went wrong.  A failure with no code leaves the agent guessing from words.
"""

from __future__ import annotations

import re

import pytest

from kiln.generation.base import GenerationError

_CODE = re.compile(r"^[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+$")


@pytest.fixture()
def tools():
    captured: dict = {}

    class _MCP:
        def tool(self, **_kwargs):
            def decorator(fn):
                captured[fn.__name__] = fn
                return fn

            return decorator

    from kiln.plugins.design_tools import plugin

    plugin.register(_MCP())
    return captured


def _failed_with(result: dict, code: str) -> None:
    assert result["success"] is False
    assert result["error"]
    assert result.get("code") == code
    assert _CODE.match(result["code"])


def test_a_missing_file_says_so(tools, tmp_path):
    _failed_with(tools["compile_scad"](scad_path=str(tmp_path / "nowhere.scad")), "FILE_NOT_FOUND")


def test_nothing_given_says_so(tools):
    _failed_with(tools["compile_scad"](), "INVALID_INPUT")


def test_code_openscad_cannot_build_says_so(tools, monkeypatch):
    def _refuse(*_a, **_k):
        raise ValueError("OpenSCAD compilation failed: syntax error in line 1")

    monkeypatch.setattr("kiln.parametric.compile_scad_source", _refuse)
    _failed_with(tools["compile_scad"](scad_code="cube(10"), "OPENSCAD_COMPILE_ERROR")


def test_a_failure_that_names_its_own_code_keeps_it(tools, monkeypatch):
    def _refuse(*_a, **_k):
        raise GenerationError("OpenSCAD is not installed", code="OPENSCAD_NOT_FOUND")

    monkeypatch.setattr("kiln.parametric.compile_scad_source", _refuse)
    _failed_with(tools["compile_scad"](scad_code="cube(10);"), "OPENSCAD_NOT_FOUND")


def test_anything_else_is_an_internal_error(tools, monkeypatch):
    def _refuse(*_a, **_k):
        raise RuntimeError("disk full")

    monkeypatch.setattr("kiln.parametric.compile_scad_source", _refuse)
    _failed_with(tools["compile_scad"](scad_code="cube(10);"), "INTERNAL_ERROR")


@pytest.mark.parametrize(
    ("raised", "code"),
    [
        (ValueError("OpenSCAD compilation failed: syntax error"), "OPENSCAD_COMPILE_ERROR"),
        (GenerationError("OpenSCAD is not installed", code="OPENSCAD_NOT_FOUND"), "OPENSCAD_NOT_FOUND"),
        (RuntimeError("disk full"), "INTERNAL_ERROR"),
    ],
)
def test_a_tweak_that_fails_says_why_the_same_way(tools, monkeypatch, raised, code):
    def _refuse(*_a, **_k):
        raise raised

    monkeypatch.setattr("kiln.parametric.tweak_and_compile", _refuse)
    _failed_with(tools["tweak_and_compile_scad"](scad_code="w = 10; cube(w);", parameter_name="w", new_value=12), code)
