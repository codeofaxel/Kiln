"""A mesh file says how much mesh it holds, and the door reads that first.

Kiln's own 3MF reader already refuses a model too large to hold whole
(``threemf_parser``: more than six million vertices and triangles, about
three times the largest real model measured).  ``trimesh`` has no such
limit, and ``load_mesh`` is the door every mesh read in Kiln goes through
-- so the same file one reader refused, the other read.

Measured 2026-10-18, on a machine with 2 GB to its name:

* a 2.4 MB 3MF declaring 14 million triangles -> 26.5 GB, 31 s
* a 64 MB OBJ of nothing but face lines -> 1.5 GB through this door, and
  4.0 GB through the one the 3D stage uses

Both are inside every upload cap, so neither door refused them.  This is
the census that does, before any of that memory is spent.
"""

from __future__ import annotations

import io
import struct
import zipfile

import pytest

from kiln import mesh_frame

_CT = (
    b'<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/'
    b'package/2006/content-types"><Default Extension="model" ContentType='
    b'"application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/></Types>'
)
_MODEL_HEAD = (
    b'<?xml version="1.0"?><model unit="millimeter" xmlns="http://schemas.'
    b'microsoft.com/3dmanufacturing/core/2015/02"><resources>'
    b'<object id="1" type="model"><mesh><vertices>'
)
_MODEL_TAIL = (
    b"</triangles></mesh></object></resources><build>"
    b'<item objectid="1"/></build></model>'
)


def _three_mf(path, vertices: int, triangles: int) -> str:
    """A 3MF declaring *vertices* and *triangles*, written without holding
    them: the point of the census is that a small file can declare a huge
    mesh."""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        z.writestr("[Content_Types].xml", _CT)
        with z.open("3D/3dmodel.model", "w", force_zip64=True) as fh:
            fh.write(_MODEL_HEAD)
            one = b'<vertex x="1.5" y="2.5" z="3.5"/>'
            for _ in range(vertices // 1000):
                fh.write(one * 1000)
            fh.write(one * (vertices % 1000))
            fh.write(b"</vertices><triangles>")
            tri = b'<triangle v1="0" v2="1" v3="2"/>'
            for _ in range(triangles // 1000):
                fh.write(tri * 1000)
            fh.write(tri * (triangles % 1000))
            fh.write(_MODEL_TAIL)
    return str(path)


def _binary_stl(path, declared: int, real: int = 1) -> str:
    tri = struct.pack("<12fH", 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0)
    path.write_bytes(b"\x00" * 80 + struct.pack("<I", declared) + tri * real)
    return str(path)


def _obj(path, faces: int) -> str:
    path.write_bytes(b"v 0 0 0\nv 1 0 0\nv 0 1 0\n" + b"f 1 2 3\n" * faces)
    return str(path)


# ---------------------------------------------------------------------------
# The budget is Kiln's own, read from the reader that already had one
# ---------------------------------------------------------------------------


def test_the_budget_is_the_one_kilns_own_3mf_reader_uses():
    """Read, never copied: one number, in the module that reasoned about it
    against real files."""
    from kiln import threemf_parser

    assert mesh_frame.MAX_MESH_ENTRIES == threemf_parser._MAX_KEPT_MESH_ENTRIES


# ---------------------------------------------------------------------------
# Refused, before the memory is spent
# ---------------------------------------------------------------------------


def test_a_small_3mf_declaring_millions_of_triangles_is_refused(tmp_path):
    """The 26.5 GB case.  Refused in a moment, from a file of kilobytes."""
    path = _three_mf(tmp_path / "huge.3mf", vertices=3, triangles=14_000_000)
    with pytest.raises(mesh_frame.MeshTooLargeError):
        mesh_frame.load_mesh(path, force="mesh")


def test_an_obj_of_nothing_but_faces_is_refused(tmp_path):
    """The 1.5 GB / 4.0 GB case."""
    path = _obj(tmp_path / "flood.obj", faces=mesh_frame.MAX_MESH_ENTRIES + 10)
    with pytest.raises(mesh_frame.MeshTooLargeError):
        mesh_frame.load_mesh(path, force="mesh")


def test_a_binary_stl_declaring_too_many_triangles_is_refused(tmp_path):
    """A binary STL says its triangle count in its header, so this costs one
    read of 84 bytes however large the claim."""
    path = _binary_stl(tmp_path / "huge.stl", declared=mesh_frame.MAX_MESH_ENTRIES + 1)
    with pytest.raises(mesh_frame.MeshTooLargeError):
        mesh_frame.load_mesh(path, force="mesh")


def test_the_refusal_says_what_it_counted_and_what_the_limit_is(tmp_path):
    path = _obj(tmp_path / "flood.obj", faces=mesh_frame.MAX_MESH_ENTRIES + 10)
    with pytest.raises(mesh_frame.MeshTooLargeError) as caught:
        mesh_frame.load_mesh(path, force="mesh")
    said = str(caught.value)
    assert str(mesh_frame.MAX_MESH_ENTRIES) in said
    assert "simplif" in said.lower(), "a refusal tells the person what to do"


def test_the_refusal_is_a_value_error(tmp_path):
    """Every caller that already turns an unreadable mesh into its own
    decline catches ``ValueError``; a mesh too large to hold is one, so no
    caller has to learn a new exception."""
    assert issubclass(mesh_frame.MeshTooLargeError, ValueError)


# ---------------------------------------------------------------------------
# The ways a file can look small to a census and large to a reader
# ---------------------------------------------------------------------------


def test_one_face_line_of_millions_of_corners_is_refused(tmp_path):
    """A face may name any number of corners and a reader fans an n-corner
    face into n-2 triangles, so ONE line is millions of triangles.  Counting
    face LINES saw one face here."""
    corners = mesh_frame.MAX_MESH_ENTRIES + 10
    path = tmp_path / "polygon.obj"
    with open(path, "wb") as fh:
        fh.write(b"v 0 0 0\nv 1 0 0\nv 0 1 0\nf")
        piece = b" 1" * 1000
        for _ in range(corners // 1000):
            fh.write(piece)
        fh.write(b"\n")
    with pytest.raises(mesh_frame.MeshTooLargeError):
        mesh_frame.load_mesh(str(path), force="mesh")


@pytest.mark.parametrize("gap", [b" ", b"\t", b"\x0c", b"\x0b", b"  "])
def test_a_face_is_a_face_however_it_is_separated(tmp_path, gap):
    """``f`` and then ANY separator is a face to a reader that splits on
    whitespace.  A census looking for ``f`` and a space disagreed with the
    reader, which is this whole class of bug."""
    path = tmp_path / "gap.obj"
    path.write_bytes(b"v 0 0 0\nv 1 0 0\nv 0 1 0\n" + (b"f" + gap + b"1 2 3\n") * 7)
    assert mesh_frame.declared_mesh_entries(str(path)) == 3 + 7


@pytest.mark.parametrize("keyword", [b"vn", b"vt", b"vp"])
def test_a_normal_or_texture_coordinate_is_not_a_vertex(tmp_path, keyword):
    """``vn`` / ``vt`` / ``vp`` are longer keywords, not geometric
    vertices -- counting them would refuse real files."""
    path = tmp_path / "kw.obj"
    path.write_bytes(b"v 0 0 0\n" + (keyword + b" 0 0 1\n") * 50)
    assert mesh_frame.declared_mesh_entries(str(path)) == 1


def test_a_3mf_with_a_long_namespace_prefix_is_still_counted(tmp_path):
    """Bounding the prefix length in the pattern meant a longer prefix than
    the bound walked past the census.  Counted on the character before the
    element's own name instead, so the prefix may be any length."""
    prefix = b"n" * 70  # longer than the 63 the pattern used to allow
    path = tmp_path / "longprefix.3mf"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        z.writestr("[Content_Types].xml", _CT)
        with z.open("3D/3dmodel.model", "w", force_zip64=True) as fh:
            fh.write(
                b'<?xml version="1.0"?><' + prefix + b":model><" + prefix
                + b":triangles>"
            )
            tri = b"<" + prefix + b':triangle v1="0" v2="1" v3="2"/>'
            for _ in range(6_100):
                fh.write(tri * 1000)
            fh.write(b"</" + prefix + b":triangles></" + prefix + b":model>")
    counted = mesh_frame.declared_mesh_entries(str(path))
    assert counted is not None and counted > mesh_frame.MAX_MESH_ENTRIES
    with pytest.raises(mesh_frame.MeshTooLargeError):
        mesh_frame.load_mesh(str(path), force="mesh")


def test_a_3mf_using_a_namespace_prefix_is_refused(tmp_path):
    """A 3MF may bind the core namespace to any prefix and write
    ``<c:vertex``.  Every namespace-aware reader reads it; a flat search for
    ``<vertex`` counted none of it."""
    path = tmp_path / "prefixed.3mf"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        z.writestr("[Content_Types].xml", _CT)
        with z.open("3D/3dmodel.model", "w", force_zip64=True) as fh:
            fh.write(
                b'<?xml version="1.0"?><c:model unit="millimeter" '
                b'xmlns:c="http://schemas.microsoft.com/3dmanufacturing/core/'
                b'2015/02"><c:resources><c:object id="1" type="model">'
                b"<c:mesh><c:triangles>"
            )
            tri = b'<c:triangle v1="0" v2="1" v3="2"/>'
            for _ in range(14_000):
                fh.write(tri * 1000)
            fh.write(
                b"</c:triangles></c:mesh></c:object></c:resources>"
                b'<c:build><c:item objectid="1"/></c:build></c:model>'
            )
    with pytest.raises(mesh_frame.MeshTooLargeError):
        mesh_frame.load_mesh(str(path), force="mesh")


def test_a_3mf_the_archive_guard_refuses_is_refused_here_too(tmp_path):
    """An archive declaring more than Kiln's ZIP guard unpacks used to make
    the census answer "not counted" -- and trimesh reads a 3MF with plain
    ``zipfile``, which has no such guard, so the reader was handed exactly
    the archive the guard had just turned away."""
    path = tmp_path / "enormous.3mf"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        z.writestr("[Content_Types].xml", _CT)
        with z.open("3D/3dmodel.model", "w", force_zip64=True) as fh:
            fh.write(_MODEL_HEAD)
            one = b'<vertex x="1.5" y="2.5" z="3.5"/>'
            for _ in range(40_000):  # declares well over the guard's ceiling
                fh.write(one * 1000)
            fh.write(b"</vertices><triangles>" + _MODEL_TAIL)

    from kiln.untrusted_zip import UntrustedArchiveError

    from kiln.threemf_parser import open_untrusted_zip

    with pytest.raises(UntrustedArchiveError), open_untrusted_zip(str(path)):
        pass  # the guard refuses it on the way in
    with pytest.raises(mesh_frame.MeshTooLargeError):
        mesh_frame.load_mesh(str(path), force="mesh")


def test_a_face_continued_over_many_lines_is_counted_whole(tmp_path):
    """A line ending in a backslash continues, and a reader joins them --
    measured 2026-10-18: trimesh reads a backslash-continued face as the
    whole face.  So one face may be spread over a thousand lines, and
    finishing the line at each newline counted only its first part."""
    path = tmp_path / "continued.obj"
    corners = mesh_frame.MAX_MESH_ENTRIES + 10
    with open(path, "wb") as fh:
        fh.write(b"v 0 0 0\nv 1 0 0\nv 0 1 0\nf")
        for _ in range(corners // 100 + 10):
            fh.write(b" 1" * 100 + b" \\\n")
        fh.write(b"\n")
    counted = mesh_frame.declared_mesh_entries(str(path))
    assert counted is not None and counted > mesh_frame.MAX_MESH_ENTRIES
    with pytest.raises(mesh_frame.MeshTooLargeError):
        mesh_frame.load_mesh(str(path), force="mesh")


def test_a_short_continued_face_still_loads(tmp_path):
    """The real reason continuations exist: a tidy exporter wrapping a long
    face.  It must still load."""
    path = tmp_path / "wrapped.obj"
    path.write_bytes(
        b"v 0 0 0\nv 1 0 0\nv 0 1 0\nv 1 1 0\nf 1 2 \\\n3 4\n"
    )
    assert mesh_frame.declared_mesh_entries(str(path)) == 4 + 2
    assert len(mesh_frame.load_mesh(str(path), force="mesh").faces) == 2


def test_a_3mf_written_in_utf16_is_refused(tmp_path):
    """The census reads bytes, and UTF-16 spells ``<vertex`` as
    ``<\\x00v\\x00...``: a byte pattern matches none of it while the reader
    decodes it and reads every element.  So it refuses rather than counting
    nothing and calling that none."""
    model = (
        '<?xml version="1.0" encoding="UTF-16"?><model><triangles>'
        + '<triangle v1="0" v2="1" v3="2"/>' * 2000
        + "</triangles></model>"
    ).encode("utf-16")
    path = tmp_path / "wide.3mf"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _CT)
        z.writestr("3D/3dmodel.model", model)
    counted = mesh_frame.declared_mesh_entries(str(path))
    assert counted is not None and counted > mesh_frame.MAX_MESH_ENTRIES


def test_a_model_part_that_is_not_recognisable_xml_is_refused(tmp_path):
    """Asking only whether the opening lacks a NUL byte was fail-OPEN:
    anything unfamiliar answered "countable", and the census then counted
    none of it and called that none.  It now says yes only to an opening
    that positively reads as ASCII-compatible XML."""
    path = tmp_path / "odd.3mf"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _CT)
        # No NUL bytes, no declaration, and not XML either.
        z.writestr("3D/3dmodel.model", b"%!PS-Adobe-3.0\nnot xml at all\n")
    counted = mesh_frame.declared_mesh_entries(str(path))
    assert counted is not None and counted > mesh_frame.MAX_MESH_ENTRIES


def test_a_part_declaring_an_encoding_kiln_cannot_count_is_refused(tmp_path):
    """A declaration naming a wide or unfamiliar encoding, with no BOM to
    give it away."""
    path = tmp_path / "declared.3mf"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _CT)
        z.writestr(
            "3D/3dmodel.model",
            b'<?xml version="1.0" encoding="ISO-10646-UCS-2"?><model/>',
        )
    counted = mesh_frame.declared_mesh_entries(str(path))
    assert counted is not None and counted > mesh_frame.MAX_MESH_ENTRIES


@pytest.mark.parametrize("encoding", ["utf-16-le", "utf-16-be"])
def test_a_wide_part_with_no_bom_at_all_is_refused(tmp_path, encoding):
    """The nastiest of the three: UTF-16LE with no BOM OPENS with ``<``, so
    a check for "does it start with a tag" says countable -- and its second
    byte is a NUL, so the byte pattern matches none of its elements.  Both
    halves of the check are load-bearing, and each was tried alone first.
    """
    model = (
        "<model><triangles>"
        + '<triangle v1="0" v2="1" v3="2"/>' * 2000
        + "</triangles></model>"
    ).encode(encoding)
    path = tmp_path / f"{encoding}.3mf"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _CT)
        z.writestr("3D/3dmodel.model", model)
    counted = mesh_frame.declared_mesh_entries(str(path))
    assert counted is not None and counted > mesh_frame.MAX_MESH_ENTRIES


def test_an_ordinary_utf8_3mf_with_a_bom_is_still_counted(tmp_path):
    """A UTF-8 BOM is still UTF-8 -- refusing it would turn away real
    files written by editors that add one."""
    path = _three_mf(tmp_path / "bom.3mf", vertices=3, triangles=3)
    with zipfile.ZipFile(path) as z:
        model = z.read("3D/3dmodel.model")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _CT)
        z.writestr("3D/3dmodel.model", b"\xef\xbb\xbf" + model)
    assert mesh_frame.declared_mesh_entries(str(path)) == 6


def test_a_corrupt_3mf_is_left_to_the_reader(tmp_path):
    """Not every unreadable archive is an oversized one: a corrupt file
    fails cheaply in the reader, with the reader's own wording, and must not
    be reported as too large."""
    path = tmp_path / "corrupt.3mf"
    path.write_bytes(b"PK\x03\x04" + b"\x00" * 200)
    assert mesh_frame.declared_mesh_entries(str(path)) is None


def test_the_declared_type_decides_the_census_not_the_name(tmp_path):
    """A reader told the type reads the file as that type whatever the name
    says, so the census has to follow the type too -- otherwise a name the
    census does not know counted nothing."""
    path = tmp_path / "upload.dat"
    path.write_bytes(
        b"v 0 0 0\nv 1 0 0\nv 0 1 0\n"
        + b"f 1 2 3\n" * (mesh_frame.MAX_MESH_ENTRIES + 10)
    )
    assert mesh_frame.declared_mesh_entries(str(path)) is None
    with pytest.raises(mesh_frame.MeshTooLargeError):
        mesh_frame.load_mesh(str(path), file_type="obj", force="mesh")


# ---------------------------------------------------------------------------
# A real customer's file still loads
# ---------------------------------------------------------------------------


def test_a_dense_real_model_still_loads(tmp_path):
    """The largest real model measured 2026-10-13 holds 2.2 M vertices and
    triangles.  A file of that order must not be refused -- a limit that
    turns away a real customer's model is the same bug the other way."""
    path = _three_mf(tmp_path / "dense.3mf", vertices=1_100_000, triangles=1_100_000)
    mesh = mesh_frame.load_mesh(path, force="mesh")
    assert len(getattr(mesh, "faces", ())) > 0


def test_an_ordinary_stl_still_loads(tmp_path):
    path = _binary_stl(tmp_path / "small.stl", declared=1, real=1)
    assert len(mesh_frame.load_mesh(path, force="mesh").faces) == 1


def test_an_ordinary_obj_still_loads(tmp_path):
    path = _obj(tmp_path / "small.obj", faces=1)
    assert len(mesh_frame.load_mesh(path, force="mesh").faces) == 1


def test_a_file_object_is_read_as_before(tmp_path):
    """A mesh handed over as bytes has no path to census; it is read as it
    always was, rather than refused for being uncountable."""
    tri = struct.pack("<12fH", 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0)
    data = b"\x00" * 80 + struct.pack("<I", 1) + tri
    mesh = mesh_frame.load_mesh(io.BytesIO(data), file_type="stl", force="mesh")
    assert len(mesh.faces) == 1


def _census_peak(path: str) -> int:
    import tracemalloc

    tracemalloc.start()
    try:
        mesh_frame.declared_mesh_entries(path)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return peak


def test_a_file_that_is_one_enormous_line_stays_cheap(tmp_path):
    """The file with NO newline in it is the one a census can get wrong
    twice: once by missing its mesh, and once by accumulating bytes until a
    newline that never comes -- copying its growing buffer per chunk, which
    is quadratic in the file, on exactly the file it exists to refuse.

    So: never holds the line, and stops as soon as the count is past the
    limit rather than reading to the end.
    """
    import time

    path = tmp_path / "one_line.obj"
    with open(path, "wb") as fh:
        fh.write(b"v 0 0 0\nv 1 0 0\nv 0 1 0\nf")
        piece = b" 1" * 4000
        for _ in range(4000):  # ~32 MB on a single line, 16 M corners
            fh.write(piece)
    started = time.time()
    peak = _census_peak(str(path))
    elapsed = time.time() - started
    assert peak < 4 * 1024 * 1024, f"the census held {peak} bytes"
    assert elapsed < 5.0, f"the census took {elapsed:.1f}s on one line"
    with pytest.raises(mesh_frame.MeshTooLargeError):
        mesh_frame.load_mesh(str(path), force="mesh")


def test_the_census_does_not_hold_the_file(tmp_path):
    """The census streams, so what one count costs is the chunk and not the
    file: four times the file is the same peak, not four times the peak.

    The property, not a round number: a census whose memory grew with the
    file would be the very thing it exists to prevent.
    """
    small = _census_peak(_obj(tmp_path / "small.obj", faces=100_000))
    large = _census_peak(_obj(tmp_path / "large.obj", faces=400_000))
    assert large < 4 * 1024 * 1024, f"the census held {large} bytes"
    assert large < small * 2, (
        f"the census grew with the file: {small} -> {large} bytes for 4x "
        "the faces"
    )


# ---------------------------------------------------------------------------
# The census itself
# ---------------------------------------------------------------------------


def test_a_binary_stl_is_counted_from_its_header(tmp_path):
    path = _binary_stl(tmp_path / "s.stl", declared=1234, real=1)
    assert mesh_frame.declared_mesh_entries(path) == 1234


def test_an_obj_counts_its_vertices_and_faces(tmp_path):
    path = _obj(tmp_path / "s.obj", faces=7)
    assert mesh_frame.declared_mesh_entries(path) == 3 + 7


def test_a_format_with_no_cheap_census_is_not_guessed(tmp_path):
    """``None`` means "not counted", never "zero": a format the census
    cannot read cheaply is read as before rather than refused."""
    path = tmp_path / "part.glb"
    path.write_bytes(b"glTF\x02\x00\x00\x00")
    assert mesh_frame.declared_mesh_entries(str(path)) is None
