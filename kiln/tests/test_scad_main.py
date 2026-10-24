"""A source written as named modules composed by ``main()`` compiles to ``main()``.

Kiln keeps a generated design's source as definitions only: one module per
named part and a ``module main()`` composing them, with nothing drawn at the
top level.  OpenSCAD compiles such a file to nothing.  These tests pin that
every door handing OpenSCAD a source gets the design instead: the compile
tool, a rebuild of kept source (compiled as written), the preview's bounding
box, and syntax validation, all through :func:`kiln.openscad_runner.run_openscad`.

Each behavioural test here fails at ad7eabba, before the runner learned the
shape: the compile raised "top level object is empty", the bounding-box
compile returned None.
"""

from __future__ import annotations

import os
import shutil
import struct
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from kiln.scad_main import call_main, is_module_only, runnable, runnable_inputs

needs_openscad = pytest.mark.skipif(
    shutil.which("openscad") is None
    and not Path("/Applications/OpenSCAD.app").exists(),
    reason="OpenSCAD required for real compiles",
)

_MODULE_ONLY = """\
// A plate with a peg, kept as named parts.
size = 20;
$fn = 32;
function half(x) = x / 2;
module body() { cube([size, size, 2]); }
module peg() { translate([half(size), half(size), 2]) cylinder(h = 6, d = 4); }
module main() { union() { body(); peg(); } }
"""


def _triangles(stl: str | os.PathLike[str]) -> int:
    data = Path(stl).read_bytes()
    if data[:5].lower() == b"solid" and b"facet" in data[:400]:
        return data.count(b"facet normal")
    return struct.unpack("<I", data[80:84])[0]


# ---------------------------------------------------------------------------
# The shape
# ---------------------------------------------------------------------------

class TestTheShape:
    def test_definitions_only_is_module_only(self) -> None:
        assert is_module_only(_MODULE_ONLY)

    def test_include_and_use_lines_are_definitions(self) -> None:
        src = "include <BOSL2/std.scad>\nuse <a.scad>\nmodule main() { cube(1); }\n"
        assert is_module_only(src)

    @pytest.mark.parametrize(
        "src",
        [
            "module main() { cube(1); }\nmain();\n",
            "module main() { cube(1); }\ncube(2);\n",
            "module main() { cube(1); }\necho(\"hi\");\n",
            "module main() { cube(1); }\ntranslate([1,0,0]) main();\n",
            "cube(1);\n",
            "// module main() { }\ncube(1);\n",
            'label = "module main()";\nmodule m() { cube(1); }\n',
            "",
        ],
    )
    def test_anything_drawn_at_the_top_level_compiles_as_written(self, src: str) -> None:
        assert not is_module_only(src)
        assert runnable(src) == src

    def test_runnable_calls_main_once(self) -> None:
        assert runnable(_MODULE_ONLY) == call_main(_MODULE_ONLY)
        assert not is_module_only(runnable(_MODULE_ONLY))


# ---------------------------------------------------------------------------
# The swap at the runner
# ---------------------------------------------------------------------------

class TestTheSwap:
    def test_a_written_source_passes_through_untouched(self, tmp_path: Path) -> None:
        f = tmp_path / "plain.scad"
        f.write_text("cube(1);\n")
        argv = ["openscad", "-o", str(tmp_path / "out.stl"), str(f)]
        with runnable_inputs(argv) as out:
            assert out == argv

    def test_the_output_and_options_are_never_taken_for_inputs(self, tmp_path: Path) -> None:
        f = tmp_path / "parts.scad"
        f.write_text(_MODULE_ONLY)
        out_scad = tmp_path / "out.scad"
        out_scad.write_text(_MODULE_ONLY)
        argv = ["openscad", "-o", str(out_scad), "-D", "size=10", str(f)]
        with runnable_inputs(argv) as out:
            assert out[:5] == argv[:5]
            assert out[5] != argv[5]

    def test_the_file_and_its_folder_are_left_as_they_were(self, tmp_path: Path) -> None:
        f = tmp_path / "parts.scad"
        f.write_text(_MODULE_ONLY)
        before = sorted(os.listdir(tmp_path))
        with runnable_inputs(["openscad", str(f)]) as out:
            wrapper = Path(out[1])
            assert wrapper.parent != tmp_path
            assert sorted(os.listdir(tmp_path)) == before
        assert f.read_text() == _MODULE_ONLY
        assert not wrapper.exists()
        assert not wrapper.parent.exists()

    def test_a_relative_input_is_read_from_the_working_folder(self, tmp_path: Path) -> None:
        (tmp_path / "parts.scad").write_text(_MODULE_ONLY)
        with runnable_inputs(["openscad", "parts.scad"], cwd=str(tmp_path)) as out:
            assert out[1] != "parts.scad"


# ---------------------------------------------------------------------------
# Every door, with a real OpenSCAD
# ---------------------------------------------------------------------------

@needs_openscad
class TestEveryDoorCompilesMain:
    def test_compile_scad_code_makes_the_design(self, tmp_path: Path) -> None:
        from kiln.parametric import compile_scad_code

        stl = compile_scad_code(_MODULE_ONLY, output_path=str(tmp_path / "a.stl"))
        assert _triangles(stl) > 12

    def test_same_part_as_calling_main_by_hand(self, tmp_path: Path) -> None:
        import trimesh

        from kiln.parametric import compile_scad_code

        alone = compile_scad_code(_MODULE_ONLY, output_path=str(tmp_path / "a.stl"))
        called = compile_scad_code(call_main(_MODULE_ONLY), output_path=str(tmp_path / "b.stl"))
        a, b = trimesh.load(alone), trimesh.load(called)
        assert len(a.faces) == len(b.faces)
        assert float(a.volume) == pytest.approx(float(b.volume), rel=1e-9)
        # 20 x 20 x 2 plate plus a d4 h6 peg: the peg is really there.
        assert float(a.volume) > 800.0

    def test_kept_source_rebuilds_as_written(self, tmp_path: Path) -> None:
        from kiln.parametric import compile_scad_code

        stl = compile_scad_code(
            _MODULE_ONLY, output_path=str(tmp_path / "r.stl"), as_written=True
        )
        assert _triangles(stl) > 12

    def test_the_compile_tool_returns_the_design(self, tmp_path: Path) -> None:
        tools = _design_tools()
        src = tmp_path / "kept.scad"
        src.write_text(_MODULE_ONLY)
        with patch("kiln.server._check_auth", return_value=None), _no_bundle():
            result = tools["compile_scad"](scad_path=str(src))
        assert result.get("success") is True, result
        assert _triangles(result["stl_path"]) > 12

    def test_the_preview_measures_the_design(self, tmp_path: Path) -> None:
        from kiln.model_visualizer import _compile_scad_for_bbox

        f = tmp_path / "kept.scad"
        f.write_text(_MODULE_ONLY)
        stl = _compile_scad_for_bbox(str(f))
        assert stl is not None
        assert _triangles(stl) > 12
        os.unlink(stl)

    def test_validation_finds_nothing_wrong(self) -> None:
        from kiln.generation.openscad import OpenSCADProvider

        report = OpenSCADProvider().validate_scad(_MODULE_ONLY)
        assert report["valid"] is True, report

    def test_its_own_relative_include_and_import_still_resolve(self, tmp_path: Path) -> None:
        from kiln.model_visualizer import _compile_scad_for_bbox

        (tmp_path / "lib").mkdir()
        (tmp_path / "lib" / "part.scad").write_text(
            "module peg() { cylinder(h = 10, d = 4, $fn = 24); }\n"
        )
        (tmp_path / "c.scad").write_text("cube(5);\n")
        from kiln.openscad_runner import run_openscad
        from kiln.generation.openscad import _find_openscad

        run_openscad(
            [_find_openscad(), "-o", str(tmp_path / "cube.stl"), str(tmp_path / "c.scad")],
            timeout=60,
            output_path=str(tmp_path / "cube.stl"),
        )
        (tmp_path / "design.scad").write_text(
            "include <lib/part.scad>\n"
            "module main() { cube([20, 20, 2]); translate([5, 5, 2]) peg();"
            ' translate([12, 12, 2]) import("cube.stl"); }\n'
        )
        stl = _compile_scad_for_bbox(str(tmp_path / "design.scad"))
        assert stl is not None
        # plate 12 + peg 92 + imported cube 12 triangles: all three present.
        assert _triangles(stl) >= 12 + 12 + 24
        os.unlink(stl)


def _design_tools() -> dict[str, Any]:
    from kiln.plugins.design_tools import plugin

    class _Mcp:
        def __init__(self) -> None:
            self.tools: dict[str, Any] = {}

        def tool(self, *args: Any, **kwargs: Any):
            def deco(fn: Any) -> Any:
                self.tools[kwargs.get("name") or fn.__name__] = fn
                return fn

            return deco

    mcp = _Mcp()
    plugin.register(mcp)
    return mcp.tools


class _no_bundle:
    """Keep kiln-pro's preview bundle out of a public compile test."""

    def __enter__(self) -> None:
        try:
            self._p = patch(
                "kiln_pro.plugins.git_render_tools.attach_inspect_bundle",
                side_effect=lambda result, *a, **k: result,
            )
            self._p.start()
        except (ImportError, ModuleNotFoundError):
            self._p = None

    def __exit__(self, *exc: object) -> None:
        if self._p is not None:
            self._p.stop()
