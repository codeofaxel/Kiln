"""A setting OpenSCAD ignored is read by the module's own name when that is
certain and a measured build confirms it, and refused otherwise.

OpenSCAD ignores an argument a module has no parameter for and builds the
default size: the catalog's gear rack asked for ``height=10`` (BOSL2 calls
that size ``width``) and came out 7.16 mm tall.  Each case compiles through
:func:`kiln.parametric.compile_scad_source` (the provider every OpenSCAD door
compiles through) and measures the part.  Skipped where OpenSCAD is absent.
"""

from __future__ import annotations

import pytest

from kiln.parametric import compile_scad_source

_GEARS = "include <BOSL2/std.scad>\ninclude <BOSL2/gears.scad>\n"


def _openscad_or_skip() -> None:
    from kiln.generation.openscad import GenerationError, _find_openscad

    try:
        _find_openscad()
    except GenerationError:
        pytest.skip("OpenSCAD is not installed")


def _extents(path: str):
    import trimesh

    return trimesh.load(path).extents


@pytest.fixture(autouse=True)
def _real_openscad(monkeypatch):
    _openscad_or_skip()
    # A developer's own OPENSCADPATH must not be what makes BOSL2 resolve.
    monkeypatch.delenv("OPENSCADPATH", raising=False)


def test_the_catalog_rack_is_read_as_width_and_measures_10mm(tmp_path):
    build = compile_scad_source(_GEARS + "rack(circ_pitch=5, teeth=10, thickness=8, height=10);\n",
                                output_path=str(tmp_path / "rack.stl"))

    assert _extents(build.stl_path)[2] == pytest.approx(10.0, abs=0.01)
    assert build.corrected_source == _GEARS + "rack(circ_pitch=5, teeth=10, thickness=8, width=10);\n"
    (line,) = build.settings_read
    assert line.startswith("Kiln read height=10 on rack() (line 3) as width=10: rack() calls that size width")
    assert "the part is 10 mm along z, where it was 7.162 mm with height ignored" in line


def test_a_misspelled_setting_is_read_as_the_real_one(tmp_path):
    build = compile_scad_source(_GEARS + "rack(circ_pitch=5, teeth=10, thicknes=8);\n",
                                output_path=str(tmp_path / "rack.stl"))

    assert "thickness=8" in build.corrected_source and "thicknes=" not in build.corrected_source
    assert _extents(build.stl_path)[1] == pytest.approx(8.0, abs=0.01)
    assert "rack() spells it thickness" in build.settings_read[0]


def test_a_module_in_the_same_file_is_read_from_its_own_definition(tmp_path):
    source = "module plate(len=40, wide=20, thick=3) cube([len, wide, thick]);\nplate(length=60);\n"
    build = compile_scad_source(source, output_path=str(tmp_path / "plate.stl"))

    assert build.corrected_source.endswith("plate(len=60);\n")
    assert list(_extents(build.stl_path)) == pytest.approx([60.0, 20.0, 3.0], abs=0.01)


def test_a_size_that_could_mean_several_settings_is_refused_with_the_real_ones(tmp_path):
    out = tmp_path / "box.stl"
    source = "module box(width=10, depth=10, height=5) cube([width, depth, height]);\nbox(size=20);\n"
    with pytest.raises(ValueError) as refused:
        compile_scad_source(source, output_path=str(out))

    message = str(refused.value)
    assert "OpenSCAD ignored size=20 on box() (line 2): box() has no setting by that name" in message
    assert "Its settings: width, depth, height. Use one of these names." in message
    assert not out.exists()


def test_a_diameter_is_never_put_into_a_radius(tmp_path):
    out = tmp_path / "peg.stl"
    source = "module peg(r=2, h=10) cylinder(r=r, h=h);\npeg(diameter=6);\n"
    with pytest.raises(ValueError) as refused:
        compile_scad_source(source, output_path=str(out))

    assert "r is a radius, so diameter=6 would be r=3" in str(refused.value)
    assert not out.exists()


def test_a_name_that_measures_wrong_is_refused(tmp_path):
    """plate() has no height setting, so ``tall=7`` is refused, and nothing
    is read in its place."""
    source = "module plate(len=40, wide=20, thick=3) cube([len, wide, thick]);\nplate(tall=7);\n"
    with pytest.raises(ValueError, match="plate\\(\\) has no setting by that name"):
        compile_scad_source(source, output_path=str(tmp_path / "plate.stl"))
