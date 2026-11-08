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


# --- bounds on the measuring builds ---


def test_more_ignored_settings_than_the_cap_are_refused_without_building():
    """A file with many ignored settings is refused before any build runs,
    so one request cannot ask for an unbounded number of measuring builds."""
    from kiln.scad_settings import _MOST_SETTINGS, resolve

    def must_not_build(code):
        raise AssertionError("no measuring build may run")

    ignored = [(f"s{i}", 1) for i in range(_MOST_SETTINGS + 1)]
    res = resolve("cube(1);\n", ignored, must_not_build, folders=[])
    assert res.source is None
    assert f"at most {_MOST_SETTINGS}" in res.refusal


def test_the_measuring_builds_share_one_time_budget(tmp_path, monkeypatch):
    """Once the budget is spent, no further measuring build starts: the
    setting is refused rather than given a fresh timeout per build."""
    import kiln.openscad_runner as runner
    from kiln.scad_settings import rebuild

    scad = tmp_path / "part.scad"
    scad.write_text("module box(width = 5) { cube([width, 5, 5]); }\nbox(widht = 8);\n")
    out = str(tmp_path / "part.stl")
    limits = []
    real = runner.run_openscad

    import subprocess
    import time as _t

    def slow(argv, *, timeout=None, output_path=None, **kw):
        # Each measuring build takes longer than the whole budget and
        # builds nothing, so only the budget decides whether a second starts.
        limits.append(timeout)
        _t.sleep(0.5)
        return subprocess.CompletedProcess(argv, 1, "", "")

    monkeypatch.setattr(runner, "run_openscad", slow)
    from kiln.generation.openscad import _find_openscad
    cmd = [_find_openscad(), "-o", out, str(scad)]
    refused = real(cmd, timeout=60, output_path=out)
    assert refused.ignored_settings
    result, res = rebuild(cmd, str(scad), refused, timeout=0.4, output_path=out)
    assert res.source is None
    assert len(limits) == 1 and limits[0] <= 0.4


# --- OpenSCAD's own shapes ------------------------------------------------------


def test_a_shape_of_openscads_own_is_read_and_measures(tmp_path):
    """``cylinder(height=10)`` built 1 mm tall and was refused with "Kiln
    could not read its definition": the shape is part of the language, so
    there was no source to read its settings from."""
    build = compile_scad_source("cylinder(height=10, d=5);\n", output_path=str(tmp_path / "c.stl"))

    assert build.corrected_source == "cylinder(h=10, d=5);\n"
    assert _extents(build.stl_path)[2] == pytest.approx(10.0, abs=0.01)
    assert build.settings_read == [
        "Kiln read height=10 on cylinder() (line 1) as h=10: cylinder() calls that size h; "
        "measured, the part is 10 mm along z, where it was 1 mm with height ignored."
    ]


def test_a_diameter_and_a_spelling_on_openscads_own_shapes(tmp_path):
    rod = compile_scad_source("cylinder(h=10, diameter=5);\n", output_path=str(tmp_path / "rod.stl"))
    assert rod.corrected_source == "cylinder(h=10, d=5);\n"
    assert _extents(rod.stl_path)[0] == pytest.approx(5.0, abs=0.01)

    box = compile_scad_source("cube([4, 6, 8], centre=true);\n", output_path=str(tmp_path / "box.stl"))
    assert box.corrected_source == "cube([4, 6, 8], center=true);\n"
    assert "cube() spells it center" in box.settings_read[0]


def test_the_radius_of_a_cylinder_is_never_one_end_of_a_cone(tmp_path):
    build = compile_scad_source("cylinder(h=10, radius=3);\n", output_path=str(tmp_path / "c.stl"))
    assert build.corrected_source == "cylinder(h=10, r=3);\n"
    assert _extents(build.stl_path)[0] == pytest.approx(6.0, abs=0.01)


def test_a_word_openscads_own_shape_cannot_mean_is_refused_with_its_settings(tmp_path):
    """A cube's ``size`` is all three sides: ``width`` names none of them."""
    with pytest.raises(Exception) as refused:
        compile_scad_source("cube(width=10);\n", output_path=str(tmp_path / "c.stl"))
    said = str(refused.value)
    assert "OpenSCAD ignored width=10 on cube() (line 1)" in said
    assert "Its settings: size (one side, or [x, y, z]), center (true puts the middle on the origin)" in said


def test_every_setting_listed_for_openscads_own_shapes_is_one_it_takes(tmp_path):
    """The list is Kiln's, the language is OpenSCAD's: each name is asked of
    the installed OpenSCAD, which warns about one it does not know."""
    from kiln.generation.openscad import _find_openscad
    from kiln.openscad_runner import ignored_settings, run_openscad
    from kiln.scad_settings import _BUILT_IN

    values = {"size": "5", "center": "true", "r": "5", "d": "10", "h": "10", "r1": "3", "r2": "2", "d1": "6",
              "d2": "4", "height": "10", "convexity": "4", "twist": "30", "slices": "10", "scale": "0.5",
              "angle": "180", "v": "[1, 2, 3]", "a": "30", "newsize": "[10, 10, 10]", "auto": "true",
              "delta": "1", "chamfer": "true", "c": '"red"', "alpha": "0.5", "text": '"a"', "font": '"Liberation Sans"',
              "halign": '"left"', "valign": '"top"', "spacing": "1", "direction": '"ltr"', "language": '"en"',
              "script": '"latin"'}
    flat = {"square", "circle", "text"}
    over_flat = {"linear_extrude": "square(2);", "rotate_extrude": "translate([5, 0]) square(2);"}
    lines = []
    for name, params in _BUILT_IN.items():
        for param in params:
            call = f"{name}({param.name}={values[param.name]})"
            if name in flat:
                lines.append(f"linear_extrude(1) {call};")
            elif name == "offset":
                lines.append(f"linear_extrude(1) {call} square(4);")
            elif name in over_flat:
                lines.append(f"{call} {over_flat[name]}")
            elif name in {"cube", "sphere", "cylinder"}:
                lines.append(f"{call};")
            else:
                lines.append(f"{call} cube(2);")
    scad = tmp_path / "all.scad"
    scad.write_text("\n".join(lines) + "\n")
    out = tmp_path / "all.stl"
    result = run_openscad([_find_openscad(), "-o", str(out), str(scad)], timeout=300, output_path=str(out))
    unknown = [(word, lines[line - 1]) for word, line in ignored_settings(result.stderr) if line]
    assert not unknown, f"OpenSCAD does not take: {unknown}"
    assert len(lines) == sum(len(params) for params in _BUILT_IN.values()) >= 40


# --- library paths a design names are confined to the library folders ---


@pytest.mark.parametrize("written", ["{outside}", "../outside/secret.scad", "lib/../../outside/secret.scad", "link.scad"])
def test_a_library_named_outside_the_folders_is_never_opened(tmp_path, written):
    """The names a design includes or uses came straight from the caller's
    code and were joined to each library folder unchecked, so on Kiln's
    server a design could make Kiln open and read any file it named: an
    absolute path, one climbing out with "..", or a link pointing out."""
    from kiln.scad_settings import find_definition

    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.scad"
    secret.write_text("module leak(width=1) { cube(width); }\n")
    library = tmp_path / "library"
    (library / "lib").mkdir(parents=True)
    (library / "link.scad").symlink_to(secret)
    source = f"use <{written.format(outside=secret)}>\nleak(widht=2);\n"
    assert find_definition(source, "leak", [str(library)]) is None


def test_a_library_inside_the_folders_is_still_read(tmp_path):
    from kiln.scad_settings import find_definition

    library = tmp_path / "library"
    (library / "parts").mkdir(parents=True)
    (library / "parts" / "rack.scad").write_text("module rack(width=10) { cube(width); }\n")
    found = find_definition("use <parts/rack.scad>\nrack(widht=5);\n", "rack", [str(library)])
    assert found is not None and [p.name for p in found.params] == ["width"]


# --- a line of thousands of calls is bounded ---


def test_a_line_of_thousands_of_calls_is_refused_before_it_is_read():
    """Every call on the line that passes the ignored setting was looked up
    in the source and its libraries, once each, re-reading the source every
    time: 4,000 calls on one line held the check for 75 s.  Past a handful
    the compile is refused, quickly, saying why."""
    import time

    from kiln.scad_settings import resolve

    calls = " ".join("m(widht=1);" for _ in range(4000))
    source = f"module m(widht=1, width=1) {{ cube(width); }}\ncube(widht=1); {calls}\n"
    started = time.monotonic()
    result = resolve(source, [("widht", 2)], lambda _code: (None, ""), folders=[])
    assert time.monotonic() - started < 5.0
    assert result.refusal and "4001 calls" in result.refusal and "at most 16" in result.refusal


def test_a_few_calls_on_a_line_are_still_read():
    from kiln.scad_settings import resolve

    source = "module m(widht=1, width=1) { cube(width); }\ncube(widht=1); m(widht=1); m(widht=1);\n"
    result = resolve(source, [("widht", 2)], lambda _code: (None, ""), folders=[])
    assert result.refusal and "calls; Kiln reads at most" not in result.refusal
