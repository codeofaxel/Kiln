"""Which files an OpenSCAD source would read (:mod:`kiln.scad_reads`).

One parser answers it everywhere: a signed-in install reads a source with it
before a build is sent to Kiln's servers, and keeps on this computer a build
whose source reads anything but Kiln's bundled libraries.

Covered: every way OpenSCAD reads a file (include/use, the reading
built-ins, a ``file`` argument), comments and strings that only mention one,
a file named by anything but one plain quoted string, the bundled-library
rule, the quick look that skips a source with no read in it, and that the
libraries named are the ones Kiln ships.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from kiln import scad_reads

READS = [
    ('import("/tmp/a.stl");', [("read", "/tmp/a.stl")]),
    ('import(file = "/tmp/a.stl", convexity = 3);', [("read", "/tmp/a.stl")]),
    ('surface(file="/tmp/h.png", center=true);', [("read", "/tmp/h.png")]),
    ('surface(center=true, file="/tmp/h.png");', [("read", "/tmp/h.png")]),
    ('linear_extrude(height=2, file="/tmp/a.dxf");', [("read", "/tmp/a.dxf")]),
    ('x = import ( "/tmp/a.json" );', [("read", "/tmp/a.json")]),
    ('import_stl("relief.stl");', [("read", "relief.stl")]),
    ("import();", []),
    ("import(convexity=3);", []),
    ('cube(10); // import("/etc/hosts");', []),
    ('/* import("/etc/hosts"); */ cube(10);', []),
    ('echo("import(\\"/etc/hosts\\")"); cube(1);', []),
    ('file = "/tmp/a.stl"; cube(1);', []),
    ("include <BOSL2/std.scad>\ncube(1);", [("include", "BOSL2/std.scad")]),
    ("use </etc/hosts>", [("include", "/etc/hosts")]),
    ("include<parts.scad>", [("include", "parts.scad")]),
    # Not a plain quoted path: nothing short of running it names the file.
    ('f = "/etc/hosts"; import(f);', [("read", None)]),
    ('import(str("/etc/", "hosts"));', [("read", None)]),
    ('module m(p) { import(p); } m("/etc/hosts");', [("read", None)]),
    ('surface(file = str("/etc/", "hosts"));', [("read", None)]),
    ('import("/tmp/a.stl" /* */ , file = f);', [("read", "/tmp/a.stl"), ("read", None)]),
    ('dxf_dim(file="/etc/a.dxf", name="x");', [("read", "/etc/a.dxf")]),
    # A positional argument names the file wherever it stands; numbers,
    # booleans and lists of them name none.
    ('import(convexity=2, "/x/a.stl");', [("read", "/x/a.stl")]),
    ('surface(center=true, invert=false, "/x/b.dat");', [("read", "/x/b.dat")]),
    ('import("/x/a.stl", 10, [0, 0]);', [("read", "/x/a.stl")]),
    ('import("/x/a.stl", -1, true, undef);', [("read", "/x/a.stl")]),
    ('surface(["/x/b.dat"][0]);', [("read", None)]),
    ('surface(true ? "/x/b.dat" : "");', [("read", None)]),
    ('let(f="/x/b.dat") surface(f);', [("read", None)]),
    ('function p()="/x/b.dat"; surface(p());', [("read", None)]),
    # A comment between the word and its target hides the target.
    ("include /* x */ </x/b.scad>", [("include", None)]),
    # `>` before the `<` does not hide the file an include names.
    ("include ></abs/t.scad>", [("include", "/abs/t.scad")]),
    ("use >>< /abs/t.scad>", [("include", "/abs/t.scad")]),
    ("include>>></abs/t.scad>", [("include", "/abs/t.scad")]),
    ("include > /* x */ </abs/t.scad>", [("include", None)]),
    ("include > <BOSL2/std.scad>", [("include", "BOSL2/std.scad")]),
    ("if (include > 2) cube(1);", []),
    # A name may start with a digit; it names a file like any other name.
    ('1x = "/x/b.stl"; import(1x);', [("read", None)]),
    ('import(convexity=2, 1e5x);', [("read", None)]),
    ('import([1e5x][0]);', [("read", None)]),
    ('0XFF = "/x/b.stl"; import(0XFF);', [("read", None)]),
    ('import("/x/a.stl", 1e5, .5, -1.5e3);', [("read", "/x/a.stl")]),
    # Hex counts as a name: a read that can only stop the source.
    ('import("/x/a.stl", 0xFF);', [("read", "/x/a.stl"), ("read", None)]),
]


@pytest.mark.parametrize(("code", "expected"), READS)
def test_every_file_a_source_reads_is_found(code, expected):
    assert scad_reads.file_reads(code) == expected


@pytest.mark.parametrize(
    "code",
    [
        "cube(10);",
        "include <BOSL2/std.scad>\ncuboid([10, 10, 10], rounding=1);",
        "use <MCAD/gears.scad>\ninclude <BOSL2/std.scad>\ncube(1);",
        '// import("relief.stl") once it is made\ncube(5);',
        'echo("surface(file=\\"x.png\\")"); cube(1);',
        'file = "/tmp/a.stl"; cube(1);',
    ],
)
def test_a_source_reading_nothing_but_the_bundled_libraries(code):
    assert not scad_reads.reads_beyond_bundled_libraries(code)


@pytest.mark.parametrize(
    "code",
    [
        'import("/Users/someone/relief.stl");',
        'surface(file="/Users/someone/height.png", center=true);',
        'import("relief.stl");',
        "include <parts.scad>\npart();",
        "use <../shared/lib.scad>",
        "use </etc/hosts>",
        "include <BOSL2/../../outside.scad>",
        "include <other/lib.scad>",
        "include <BOSL2\\std.scad>",
        'f = "/Users/someone/relief.stl"; import(f);',
        'translate([0, 0, 2]) linear_extrude(height=1, file="/Users/someone/outline.dxf");',
        'import(convexity=2, "/Users/someone/relief.stl");',
        "include /* the parts */ <BOSL2/std.scad>",
    ],
)
def test_a_source_reading_any_other_file(code):
    assert scad_reads.reads_beyond_bundled_libraries(code)


@pytest.mark.parametrize(
    "target",
    ["BOSL2/std.scad", "MCAD/involute_gears.scad", "BOSL2/beziers.scad", "MCAD/bearing.scad"],
)
def test_a_library_include_names_a_library_file(target):
    assert scad_reads.is_bundled_library(target)


@pytest.mark.parametrize(
    "target",
    [
        "BOSL2", "BOSL2/", "bosl2/std.scad", "OTHER/std.scad", "/BOSL2/std.scad",
        "BOSL2/../secrets.scad", "BOSL2/x/../../y.scad", "BOSL2\\std.scad", "BOSL2/std.scad\x00",
        "BOSL2/std .scad", "BOSL2/\t/a/b.scad", "MCAD/gears.scad\t/a/b.scad", "BOSL2/x\r/a.scad",
        "BOSL2/x\n/a.scad", "BOSL2/x\f/a.scad", "BOSL2/x\v/a.scad", "BOSL2/sté.scad",
    ],
)
def test_anything_else_between_the_brackets_is_refused(target):
    assert not scad_reads.is_bundled_library(target)
    assert scad_reads.reads_beyond_bundled_libraries(f"include <{target}>")


def test_every_file_kiln_ships_in_its_libraries_is_a_library_include():
    shipped = Path(scad_reads.__file__).parent / "data" / "scad_libraries"
    files = [p.relative_to(shipped).as_posix() for p in shipped.rglob("*.scad")]
    assert files
    assert [f for f in files if not scad_reads.is_bundled_library(f)] == []


#: Includes written with `>` before the `<`: each is a read of the file in its
#: brackets for every check that judges source.
GT_BEFORE_TARGET = ["include ></abs/t.scad>", "use >>< /abs/t.scad>", "include>>></abs/t.scad>"]


@pytest.mark.parametrize("code", GT_BEFORE_TARGET)
def test_an_include_with_gt_before_its_target_is_refused_by_every_check(code):
    from kiln.generation.openscad import _has_only_safe_includes

    assert scad_reads.file_reads(code) == [("include", "/abs/t.scad")]
    assert scad_reads.reads_beyond_bundled_libraries(code)
    assert _has_only_safe_includes(code) is False


def test_the_quick_look_never_changes_the_answer(monkeypatch):
    """A source holding none of the words a read starts with is answered
    without being read in full.  Read every source in full and every answer
    is the same."""
    sources = [code for code, _ in READS] + [
        "cube(10);",
        'linear_extrude(height=2, file="/tmp/a.dxf");',
        "include<parts.scad>",
        "polyhedron(points=[[0,0,0],[1,0,0],[0,1,0],[0,0,1]], faces=[[0,1,2],[0,1,3],[0,2,3],[1,2,3]]);",
    ]
    quick = [scad_reads.file_reads(code) for code in sources]
    monkeypatch.setattr(scad_reads, "_READ_WORDS", ("",))
    assert [scad_reads.file_reads(code) for code in sources] == quick


#: Runs that start with a digit: ``(run, OpenSCAD reads it as a name, the
#: reader reads it as a name)``.  The OpenSCAD column is checked against
#: OpenSCAD itself by the test below.  The reader agrees everywhere but hex,
#: which it reads as a name so that no read is missed.
DIGIT_LED = [
    ("1x", True, True), ("1e", True, True), ("1_a", True, True), ("1e5x", True, True),
    ("9e9e", True, True), ("0x", True, True), ("0xg", True, True), ("0x1g", True, True),
    ("1E5_", True, True), ("0XFF", True, True), ("0X1", True, True), ("0b101", True, True),
    ("1_000", True, True), ("0_1", True, True),
    ("12", False, False), ("1e5", False, False), ("1E5", False, False), ("1.5", False, False),
    ("0e0", False, False), ("00", False, False), ("01", False, False), ("1e05", False, False),
    ("0xFF", False, True), ("0xff", False, True), ("0x1F", False, True), ("0x0", False, True),
]


@pytest.mark.parametrize(("run", "openscad_name", "reader_name"), DIGIT_LED)
def test_a_run_that_starts_with_a_digit_is_read_as_a_name_whenever_openscad_reads_one(
    run, openscad_name, reader_name,
):
    # Never the other way round: a name OpenSCAD reads, taken for a number,
    # would hide the read it makes.
    assert reader_name or not openscad_name
    assert scad_reads._tokens(run) == [("id" if reader_name else "num", run)]
    reads = scad_reads.file_reads(f"import({run});")
    assert reads == ([("read", None)] if reader_name else [])


@pytest.mark.skipif(shutil.which("openscad") is None, reason="OpenSCAD not installed")
def test_the_digit_led_table_is_what_openscad_does(tmp_path):
    """Each run of :data:`DIGIT_LED` assigned a file and imported: OpenSCAD
    builds the part exactly when it reads the run as a name, so the table's
    OpenSCAD column is OpenSCAD's own answer, not a guess."""
    import subprocess

    import trimesh

    target = tmp_path / "target.stl"
    trimesh.creation.box(extents=(5, 5, 5)).export(target)
    for run, openscad_name, _reader_name in DIGIT_LED:
        source = tmp_path / "probe.scad"
        source.write_text(f'{run} = "{target}";\nimport({run});\n')
        made = tmp_path / "probe.stl"
        made.unlink(missing_ok=True)
        subprocess.run(
            [shutil.which("openscad"), "-o", str(made), str(source)],
            capture_output=True, timeout=120, check=False,
        )
        assert (made.exists() and made.stat().st_size > 0) is openscad_name, run


def test_the_libraries_named_are_the_ones_kiln_ships():
    shipped = Path(scad_reads.__file__).parent / "data" / "scad_libraries"
    folders = {path.name for path in shipped.iterdir() if path.is_dir() and not path.name.startswith(".")}
    assert set(scad_reads.LIBRARIES) == folders
