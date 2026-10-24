"""A decorated mesh says what made it, and that becomes one source.

Text is carved into or raised onto a mesh after the mesh is compiled, so the
source a design keeps used to describe the part without its lettering: a
nameplate rebuilt from its kept source came back blank.  Every emboss and
deboss now leaves its mark beside the mesh it compiled to
(:mod:`kiln.mesh_marks`), carried forward through a chain of marks, and
:func:`kiln.scad_main.with_marks` writes the marks into the source the mesh
began as.  The real-OpenSCAD tests pin that the composed source compiles, on
its own, to the part the chain delivered.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from kiln import mesh_marks
from kiln.scad_main import is_module_only, with_marks

needs_openscad = pytest.mark.skipif(
    shutil.which("openscad") is None
    and not Path("/Applications/OpenSCAD.app").exists(),
    reason="OpenSCAD required for real compiles",
)

_PLATE = """\
// A plate, kept as named parts.
$fn = 64;
module plate() { cube([80, 30, 4]); }
module main() { plate(); }
"""


def _compile(scad_text: str, out: Path) -> str:
    from kiln.emboss_generator import compile_embossed_model

    src = out.with_suffix(".scad")
    src.write_text(scad_text, encoding="utf-8")
    result = compile_embossed_model(str(src), str(out))
    assert result["success"], result.get("error")
    return result["output_path"]


def _volume_and_bounds(stl: str) -> tuple[float, list[float]]:
    trimesh = pytest.importorskip("trimesh")
    mesh = trimesh.load(stl, force="mesh")
    return float(mesh.volume), [float(v) for v in mesh.bounds.ravel()]


# ---------------------------------------------------------------------------
# The record
# ---------------------------------------------------------------------------


class TestTheRecord:
    def test_a_mark_carries_its_input_record_forward(self, tmp_path: Path) -> None:
        body, first, second = (str(tmp_path / n) for n in ("b.stl", "1.stl", "2.stl"))
        mesh_marks.note_source(body, str(tmp_path / "b.scad"))
        mesh_marks.note_mark(body, first, {"op": "difference", "scad": "cube(1);", "kind": "text"})
        mesh_marks.note_mark(first, second, {"op": "union", "scad": "cube(2);", "kind": "text"})

        record = mesh_marks.read(second)
        assert record["source"] == str((tmp_path / "b.scad").resolve())
        assert [m["op"] for m in record["marks"]] == ["difference", "union"]

    def test_a_mark_on_a_mesh_nobody_recorded_names_no_source(self, tmp_path: Path) -> None:
        out = str(tmp_path / "o.stl")
        mesh_marks.note_mark(str(tmp_path / "loose.stl"), out, {"op": "union", "scad": "cube(1);"})
        assert mesh_marks.read(out)["source"] is None

    def test_an_unreadable_record_is_absent(self, tmp_path: Path) -> None:
        mesh = tmp_path / "m.stl"
        Path(f"{mesh}{mesh_marks.SUFFIX}").write_text("not json", encoding="utf-8")
        assert mesh_marks.read(str(mesh)) is None
        assert mesh_marks.read(str(tmp_path / "missing.stl")) is None


# ---------------------------------------------------------------------------
# The composed source
# ---------------------------------------------------------------------------


class TestWithMarks:
    def test_main_keeps_its_name_and_the_marks_apply_in_order(self) -> None:
        out = with_marks(
            _PLATE,
            [
                {"op": "difference", "scad": "cube(1);", "kind": "text"},
                {"op": "difference", "scad": "cube(2);", "kind": "text"},
                {"op": "union", "scad": "cube(3);", "kind": "text"},
            ],
        )
        assert is_module_only(out)
        assert "module undecorated()" in out
        main = out[out.index("module main()") :]
        # Two cuts share one difference; the raise wraps it.
        assert main.index("union()") < main.index("difference()")
        assert main.index("text_1();") < main.index("text_2();") < main.index("text_3();")

    def test_a_name_the_source_already_uses_is_not_reused(self) -> None:
        src = "module undecorated() { cube(1); }\nmodule text_1() { cube(2); }\nmodule main() { undecorated(); text_1(); }\n"
        out = with_marks(src, [{"op": "union", "scad": "cube(3);", "kind": "text"}])
        assert "module undecorated_2()" in out
        assert "module text_1_2()" in out

    def test_a_main_in_a_comment_is_not_the_one_renamed(self) -> None:
        src = "// module main() composes the plate\n" + _PLATE
        out = with_marks(src, [{"op": "union", "scad": "cube(3);", "kind": "text"}])
        assert out.startswith("// module main() composes the plate\n")

    def test_a_mark_that_reads_a_file_is_refused(self) -> None:
        with pytest.raises(ValueError, match="reads a file"):
            with_marks(_PLATE, [{"op": "union", "scad": 'surface(file="x.dat");', "kind": "heightmap"}])

    def test_a_source_that_draws_at_the_top_level_is_refused(self) -> None:
        with pytest.raises(ValueError, match="main"):
            with_marks("cube(1);", [{"op": "union", "scad": "cube(3);"}])


# ---------------------------------------------------------------------------
# Real compiles: the composed source is the delivered part
# ---------------------------------------------------------------------------


@needs_openscad
class TestTheSourceIsThePart:
    def test_two_debossed_lines_compile_back_from_the_source(self, tmp_path: Path) -> None:
        from kiln.decoration_helpers import emboss_text_lines_on_face
        from kiln.scad_main import call_main

        source = tmp_path / "plate.scad"
        source.write_text(_PLATE, encoding="utf-8")
        body = _compile(call_main(_PLATE), tmp_path / "plate_body.stl")
        mesh_marks.note_source(body, str(source))

        decorated = emboss_text_lines_on_face(
            body,
            ["KILN", "Desk"],
            face_name="top",
            mode="deboss",
            depth_mm=1.2,
            output_dir=str(tmp_path / "decorated"),
        )
        record = mesh_marks.read(decorated)
        assert record["source"] == str(source.resolve())
        assert [m["kind"] for m in record["marks"]] == ["text", "text"]

        whole = with_marks(_PLATE, record["marks"])
        alone = _compile(call_main(whole), tmp_path / "alone.stl")

        v_delivered, b_delivered = _volume_and_bounds(decorated)
        v_alone, b_alone = _volume_and_bounds(alone)
        v_body, _ = _volume_and_bounds(body)
        assert v_delivered < v_body - 50  # the letters really were cut
        assert v_alone == pytest.approx(v_delivered, rel=1e-4)
        assert b_alone == pytest.approx(b_delivered, abs=1e-3)

    def test_raised_text_compiles_back_from_the_source(self, tmp_path: Path) -> None:
        from kiln.decoration_helpers import emboss_text_lines_on_face
        from kiln.scad_main import call_main

        source = tmp_path / "plate.scad"
        source.write_text(_PLATE, encoding="utf-8")
        body = _compile(call_main(_PLATE), tmp_path / "plate_body.stl")
        mesh_marks.note_source(body, str(source))

        decorated = emboss_text_lines_on_face(
            body, ["HI"], face_name="top", mode="emboss", depth_mm=1.2,
            output_dir=str(tmp_path / "decorated"),
        )
        whole = with_marks(_PLATE, mesh_marks.read(decorated)["marks"])
        alone = _compile(call_main(whole), tmp_path / "alone.stl")

        v_delivered, b_delivered = _volume_and_bounds(decorated)
        v_alone, b_alone = _volume_and_bounds(alone)
        assert b_delivered[5] > 5.0  # the letters stand proud of the 4 mm plate
        assert v_alone == pytest.approx(v_delivered, rel=1e-4)
        assert b_alone == pytest.approx(b_delivered, abs=1e-3)

    def test_a_rewritten_scad_leaves_no_stale_mark(self, tmp_path: Path) -> None:
        from kiln.emboss_generator import compile_embossed_model, generate_emboss_scad
        from kiln.scad_main import call_main

        body = _compile(call_main(_PLATE), tmp_path / "plate_body.stl")
        face = {"normal": [0, 0, 1], "center": [40, 15, 4], "width_mm": 80, "height_mm": 30, "face_name": "top"}
        gen = generate_emboss_scad(
            model_path=body,
            content_info={"type": "openscad_text", "text": "A"},
            face=face,
            output_dir=str(tmp_path / "e"),
            depth_mm=1.2,
            mode="deboss",
        )
        assert gen["mark"]["op"] == "difference"
        assert "text(" in gen["mark"]["scad"]

        # Something else rewrites the file before it is compiled.
        Path(gen["scad_path"]).write_text(f'import("{body}");\n', encoding="utf-8")
        out = compile_embossed_model(gen["scad_path"], str(tmp_path / "out.stl"))
        assert out["success"]
        assert mesh_marks.read(out["output_path"]) is None
        assert json.loads(Path(f"{gen['scad_path']}.mark.json").read_text())["op"] == "difference"
        assert os.path.exists(gen["scad_path"])
