"""A mark is cut where the part's outside is, never sealed inside it.

The face a mark is placed on is not always the outside of the part.  A
frisbee's flat plate has a shallow dome rising out of it, and the text,
centred on the plate's outline, was cut 1.4 mm below the dome's top: a
hollow sealed inside the disc, invisible from either side.  A wall plaque's
caption landed under its photo relief the same way.  The dome, straddle and
hollow tests fail at 0a7ba121, where the engine only checked for part UNDER
a mark; the flat-face test passes on both sides and pins that a face which
is the outside of the part is cut exactly as before.

:func:`kiln.emboss_generator.footprint_surface_rise` casts from outside the
part onto the content's footprint.  A gentle cover over the whole footprint
moves the mark up to meet it and deepens it; part on the face and part under
something, or a cover that varies by more than the mark is deep, is refused
with :class:`~kiln.emboss_generator.ContentBuriedError`.  The compile refuses
any mark that leaves a sealed hollow behind.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

trimesh = pytest.importorskip("trimesh")

pytestmark = pytest.mark.skipif(
    shutil.which("openscad") is None
    and not Path("/Applications/OpenSCAD.app").exists(),
    reason="OpenSCAD required for real compiles",
)

#: A 120 mm plate, 3 mm thick, with a shallow dome rising out of its middle
#: to 4.6 mm (the frisbee's shape in small).
_DOMED_PLATE = """\
$fn = 120;
cylinder(h = 3, r = 60);
scale([1, 1, 0.04]) sphere(r = 115);
"""
#: An 80 x 40 mm slab with a 1 mm block standing on its upper half.
_SLAB_WITH_BLOCK = """\
cube([80, 40, 4]);
translate([10, 22, 4]) cube([60, 14, 1]);
"""
_FLAT_SLAB = "cube([80, 40, 4]);\n"


def _compile(scad: str, out: Path) -> str:
    from kiln.emboss_generator import compile_embossed_model

    src = out.with_suffix(".scad")
    src.write_text(scad, encoding="utf-8")
    result = compile_embossed_model(str(src), str(out))
    assert result["success"], result.get("error")
    return result["output_path"]


def _pieces(stl: str) -> list:
    return trimesh.load(stl, force="mesh").split(only_watertight=False)


def test_text_on_a_domed_plate_is_cut_into_the_dome(tmp_path: Path) -> None:
    from kiln.decoration_helpers import emboss_text_lines_on_face

    body = _compile(_DOMED_PLATE, tmp_path / "plate.stl")
    notes: list[str] = []
    out = emboss_text_lines_on_face(
        body, ["KILN"], face_name="top", mode="deboss", depth_mm=1.2,
        output_dir=str(tmp_path / "out"), collect_warnings=notes,
    )
    pieces = _pieces(out)
    assert len(pieces) == 1, [round(float(p.volume), 2) for p in pieces]
    assert trimesh.load(out, force="mesh").is_watertight
    assert float(pieces[0].volume) < float(trimesh.load(body, force="mesh").volume) - 50
    assert any("moved up to meet the surface" in n for n in notes), notes


def test_text_straddling_a_raised_block_is_refused(tmp_path: Path) -> None:
    from kiln.decoration_helpers import emboss_text_on_face
    from kiln.emboss_generator import ContentBuriedError

    body = _compile(_SLAB_WITH_BLOCK, tmp_path / "slab.stl")
    face = {
        "normal": (0, 0, 1), "center": (40, 20, 4), "bbox_center": (40, 20, 4),
        "width_mm": 80, "height_mm": 40, "face_name": "top",
    }
    with pytest.raises(ContentBuriedError, match="part of it lands on that face"):
        emboss_text_on_face(
            body, "KILN", face=face, mode="deboss", depth_mm=1.2, font_size_mm=12,
            output_dir=str(tmp_path / "out"),
        )


def test_a_flat_face_is_cut_as_it_always_was(tmp_path: Path) -> None:
    from kiln.decoration_helpers import emboss_text_lines_on_face

    body = _compile(_FLAT_SLAB, tmp_path / "slab.stl")
    notes: list[str] = []
    out = emboss_text_lines_on_face(
        body, ["KILN"], face_name="top", mode="deboss", depth_mm=1.2,
        output_dir=str(tmp_path / "out"), collect_warnings=notes,
    )
    assert not any("moved up" in n for n in notes), notes
    mesh = trimesh.load(out, force="mesh")
    assert mesh.bounds[1][2] == pytest.approx(4.0, abs=1e-6)
    floor = sorted({round(float(z), 3) for z in mesh.vertices[:, 2]})
    assert floor[0] == 0.0 and 2.8 in floor  # 4 mm slab, 1.2 mm deep letters


def test_a_mark_that_seals_a_hollow_fails_its_compile(tmp_path: Path) -> None:
    from kiln.emboss_generator import compile_embossed_model

    body = _compile(_FLAT_SLAB, tmp_path / "slab.stl")
    scad = tmp_path / "sealed.scad"
    scad.write_text(
        f'difference() {{\n    import("{body}");\n'
        f"    translate([30, 15, 1]) cube([20, 10, 1]);\n}}\n",
        encoding="utf-8",
    )
    result = compile_embossed_model(str(scad), str(tmp_path / "sealed.stl"))
    assert not result["success"]
    assert "sealed inside" in result["error"]
