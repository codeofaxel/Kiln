"""A misnamed OpenSCAD setting gets the same answer at every door that compiles.

:mod:`kiln.scad_settings` reads a setting OpenSCAD ignored by the module's own
name when a measured build confirms it.  It was reached from one compile path,
and its tests called that path's engine.  So the compile TOOL an agent calls
had no test of its own, the code checker called code "invalid" that the
compile beside it would read and build, and the Gemini provider's compile sent
the model the bare refusal.  Each door now builds through
:func:`kiln.scad_settings.run_resolving`; these tests call the doors
themselves.  Skipped where OpenSCAD is absent.
"""

from __future__ import annotations

import pytest

_GEARS = "include <BOSL2/std.scad>\ninclude <BOSL2/gears.scad>\n"
_RACK = _GEARS + "rack(circ_pitch=5, teeth=10, thickness=8, height=10);\n"
_RACK_READ = _GEARS + "rack(circ_pitch=5, teeth=10, thickness=8, width=10);\n"


@pytest.fixture(autouse=True)
def _real_openscad(monkeypatch):
    from kiln.generation.openscad import GenerationError, _find_openscad

    try:
        _find_openscad()
    except GenerationError:
        pytest.skip("OpenSCAD is not installed")
    # A developer's own OPENSCADPATH must not be what makes BOSL2 resolve.
    monkeypatch.delenv("OPENSCADPATH", raising=False)


@pytest.fixture()
def tools(monkeypatch) -> dict:
    """The design tools as an agent reaches them, signed in."""
    import kiln.server as server
    from kiln.plugins.design_tools import plugin

    monkeypatch.setattr(server, "_check_auth", lambda *a, **k: None)
    registered: dict = {}

    class _Mcp:
        def tool(self, **_kwargs):
            def keep(fn):
                registered[fn.__name__] = fn
                return fn

            return keep

    plugin.register(_Mcp())
    registered["validate_openscad_code"] = server.validate_openscad_code
    return registered


def _reply(result) -> dict:
    """A tool's reply: the dict itself, or the dict beside its picture."""
    if isinstance(result, dict):
        return result
    return next(block for block in result if isinstance(block, dict))


def _extents(path: str):
    import trimesh

    return trimesh.load(path).extents


class TestTheCompileTool:
    def test_a_library_setting_is_read_measured_and_handed_back(self, tools):
        reply = _reply(tools["compile_scad"](scad_code=_RACK))

        assert reply["success"] is True, reply
        assert reply["corrected_scad_code"] == _RACK_READ
        assert reply["settings_read"][0].startswith("Kiln read height=10 on rack() (line 3) as width=10")
        assert _extents(reply["stl_path"])[2] == pytest.approx(10.0, abs=0.01)

    def test_one_of_openscads_own_shapes_is_read_too(self, tools):
        reply = _reply(tools["compile_scad"](scad_code="cylinder(height=10, d=5);\n"))

        assert reply["success"] is True, reply
        assert reply["corrected_scad_code"] == "cylinder(h=10, d=5);\n"
        assert _extents(reply["stl_path"])[2] == pytest.approx(10.0, abs=0.01)

    def test_a_setting_that_cannot_be_read_is_refused_with_the_real_ones(self, tools):
        reply = _reply(tools["compile_scad"](scad_code="cube(width=10);\n"))

        assert reply["success"] is False
        assert "OpenSCAD ignored width=10 on cube() (line 1)" in reply["error"]
        assert "Its settings: size (" in reply["error"]

    def test_code_with_nothing_misnamed_says_nothing_about_settings(self, tools):
        reply = _reply(tools["compile_scad"](scad_code="cube(5);\n"))

        assert reply["success"] is True
        assert "settings_read" not in reply and "corrected_scad_code" not in reply


class TestTheCodeChecker:
    """The check says what the compile would do."""

    def test_code_the_compile_would_read_is_valid_and_says_what_it_reads(self, tools):
        reply = tools["validate_openscad_code"](scad_code=_RACK)

        assert reply["valid"] is True and reply["errors"] == [], reply
        assert reply["corrected_scad_code"] == _RACK_READ
        assert reply["settings_read"][0].startswith("Kiln read height=10 on rack() (line 3) as width=10")

    def test_code_the_compile_would_refuse_is_invalid_with_the_real_settings(self, tools):
        reply = tools["validate_openscad_code"](scad_code="cube(width=10);\n")

        assert reply["valid"] is False
        first = reply["errors"][0]["message"]
        assert "OpenSCAD ignored width=10 on cube() (line 1)" in first and "Its settings: size (" in first
        assert "corrected_scad_code" not in reply

    def test_clean_code_is_valid_with_nothing_added(self, tools):
        reply = tools["validate_openscad_code"](scad_code="cube(5);\n")

        assert reply["valid"] is True
        assert "settings_read" not in reply and "corrected_scad_code" not in reply


class TestTheGeminiProvidersCompile:
    """The model's code is built behind a library Kiln writes in front of it."""

    def _provider(self):
        from kiln.generation.gemini import GeminiDeepThinkProvider

        return GeminiDeepThinkProvider(api_key="not-used-here")

    def test_the_models_misnamed_setting_is_read_and_its_code_kept_corrected(self, tmp_path):
        from kiln.generation.base import GenerationStatus

        provider = self._provider()
        out = tmp_path / "part.stl"
        job = provider._compile_scad("cylinder(height=10, d=5);\n", str(out), "job-1", "a rod", "stl")

        assert job.status == GenerationStatus.SUCCEEDED, job.error
        assert job.corrected_source == "cylinder(h=10, d=5);\n"
        assert provider.get_scad_code("job-1") == "cylinder(h=10, d=5);\n"
        # The line is counted in the model's own code, not behind the library.
        assert job.settings_read[0].startswith("Kiln read height=10 on cylinder() (line 1) as h=10")
        assert _extents(str(out))[2] == pytest.approx(10.0, abs=0.01)

    def test_a_setting_it_cannot_read_fails_with_the_real_settings_for_the_retry(self, tmp_path):
        from kiln.generation.base import GenerationStatus

        job = self._provider()._compile_scad("cube(width=10);\n", str(tmp_path / "part.stl"), "job-2", "a box", "stl")

        assert job.status == GenerationStatus.FAILED
        assert "OpenSCAD ignored width=10 on cube() (line 1)" in (job.error or "")
        assert "Its settings: size (" in (job.error or "")
