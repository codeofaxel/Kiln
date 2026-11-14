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


def test_the_libraries_named_are_the_ones_kiln_ships():
    shipped = Path(scad_reads.__file__).parent / "data" / "scad_libraries"
    folders = {path.name for path in shipped.iterdir() if path.is_dir() and not path.name.startswith(".")}
    assert set(scad_reads.LIBRARIES) == folders
