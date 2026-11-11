"""A 3MF reads the same on every install, through Kiln's own reader.

trimesh's 3MF reader needs lxml and networkx, which ``pip install kiln3d``
does not bring in.  :func:`kiln.mesh_frame.load_mesh` reads a 3MF with
Kiln's standard-library reader instead, so every door that opens a model
answers a 3MF as it answers the same part saved as STL.

Pinned here:

* ``load_mesh`` returns the shape trimesh's loader returns for a 3MF (a
  Scene: each object under its name, each build item and component placed;
  one Trimesh with ``force="mesh"``) without ever calling trimesh's reader;
* a 3MF written in inches is read in millimetres;
* ``process`` reaches the geometry, and a file object reads like a path;
* in a child interpreter where lxml and networkx cannot be imported, the
  registered tools that read a model answer a two-object 3MF with the same
  numbers as the same part saved as STL -- including the four that read
  their file as STL bytes and the three whose read went through trimesh.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import numpy as np
import pytest

trimesh = pytest.importorskip("trimesh")

from kiln.mesh_frame import load_mesh  # noqa: E402

_NS = 'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02"'
_TYPES = (
    '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/'
    'package/2006/content-types"><Default Extension="rels" ContentType="application/'
    'vnd.openxmlformats-package.relationships+xml"/><Default Extension="model" '
    'ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/></Types>'
)
_RELS = (
    '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/'
    'package/2006/relationships"><Relationship Target="/3D/3dmodel.model" Id="rel0" '
    'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/></Relationships>'
)


def _box(w: float, d: float, h: float, *, unmerged: bool = False) -> tuple[str, trimesh.Trimesh]:
    """A box standing on z = 0, as 3MF mesh XML and as a Trimesh; *unmerged*
    writes each triangle its own three corners."""
    mesh = trimesh.creation.box(extents=(w, d, h))
    mesh.apply_translation((0, 0, h / 2))
    if unmerged:
        mesh.unmerge_vertices()
    verts = "".join(f'<vertex x="{x:g}" y="{y:g}" z="{z:g}"/>' for x, y, z in mesh.vertices)
    tris = "".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in mesh.faces)
    return f"<mesh><vertices>{verts}</vertices><triangles>{tris}</triangles></mesh>", mesh


def _write_3mf(path: Path, model: str) -> str:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _TYPES)
        archive.writestr("_rels/.rels", _RELS)
        archive.writestr("3D/3dmodel.model", model)
    return str(path)


def _two_part_model(folder: Path) -> tuple[str, str]:
    """A base and a pegs assembly (one peg object placed twice by
    components), both moved by build items; and the same part as one STL."""
    base_xml, base = _box(40, 30, 10)
    peg_xml, peg = _box(8, 8, 20)
    model = (
        f'<?xml version="1.0" encoding="UTF-8"?><model unit="millimeter" {_NS}><resources>'
        f'<object id="1" name="base" type="model">{base_xml}</object>'
        f'<object id="2" name="peg" type="model">{peg_xml}</object>'
        '<object id="3" name="pegs" type="model"><components>'
        '<component objectid="2" transform="1 0 0 0 1 0 0 0 1 -10 0 10"/>'
        '<component objectid="2" transform="1 0 0 0 1 0 0 0 1 10 0 10"/>'
        "</components></object></resources><build>"
        '<item objectid="1" transform="1 0 0 0 1 0 0 0 1 100 100 0"/>'
        '<item objectid="3" transform="1 0 0 0 1 0 0 0 1 100 100 0"/>'
        "</build></model>"
    )
    placed = [base.copy(), peg.copy(), peg.copy()]
    for part, offset in zip(placed, ((100, 100, 0), (90, 100, 10), (110, 100, 10)), strict=True):
        part.apply_translation(offset)
    stl = folder / "assembly.stl"
    trimesh.util.concatenate(placed).export(str(stl))
    return _write_3mf(folder / "assembly.3mf", model), str(stl)


def _cube(folder: Path) -> tuple[str, str]:
    xml, cube = _box(20, 20, 20)
    stl = folder / "cube.stl"
    cube.export(str(stl))
    model = (
        f'<?xml version="1.0" encoding="UTF-8"?><model unit="millimeter" {_NS}><resources>'
        f'<object id="1" name="cube" type="model">{xml}</object></resources>'
        '<build><item objectid="1"/></build></model>'
    )
    return _write_3mf(folder / "cube.3mf", model), str(stl)


@pytest.fixture
def no_trimesh_3mf_reader(monkeypatch):
    """trimesh's 3MF reader raises if anything reaches it."""
    from trimesh.exchange import load as trimesh_load

    def refused(*_a, **_k):
        raise AssertionError("trimesh's 3MF reader was used")

    monkeypatch.setitem(trimesh_load.mesh_loaders, "3mf", refused)


def test_a_3mf_comes_back_in_the_shape_trimesh_returns(tmp_path, no_trimesh_3mf_reader):
    three_mf, stl = _two_part_model(tmp_path)

    scene = load_mesh(three_mf)
    assert isinstance(scene, trimesh.Scene)
    assert sorted(scene.geometry) == ["base", "peg"]
    assert sorted(scene.graph.nodes_geometry) == ["base", "peg", "peg_1"]
    assert scene.metadata["units"] == "millimeter"

    mesh = load_mesh(three_mf, force="mesh")
    reference = load_mesh(stl, force="mesh")
    assert isinstance(mesh, trimesh.Trimesh)
    assert len(mesh.faces) == len(reference.faces) == 36
    assert np.allclose(mesh.bounds, reference.bounds)
    assert np.allclose(mesh.bounds, [[80, 85, 0], [120, 115, 30]])
    assert mesh.volume == pytest.approx(reference.volume) == pytest.approx(14560.0)


def test_a_3mf_written_in_inches_is_read_in_millimetres(tmp_path, no_trimesh_3mf_reader):
    xml, _ = _box(1, 2, 0.5)
    path = _write_3mf(
        tmp_path / "inches.3mf",
        f'<?xml version="1.0" encoding="UTF-8"?><model unit="inch" {_NS}><resources>'
        f'<object id="1" name="plate" type="model">{xml}</object></resources>'
        '<build><item objectid="1" transform="1 0 0 0 1 0 0 0 1 2 0 0"/></build></model>',
    )
    mesh = load_mesh(path, force="mesh")
    assert np.allclose(mesh.bounds, [[38.1, -25.4, 0.0], [63.5, 25.4, 12.7]])
    assert mesh.metadata["units"] == "millimeter"
    scene = load_mesh(path)
    assert scene.metadata["units"] == "millimeter"
    assert np.allclose(scene.bounds, mesh.bounds)


def test_process_reaches_the_geometry_and_a_file_object_reads_like_a_path(
    tmp_path, no_trimesh_3mf_reader
):
    # Every triangle with its own corners: merged by default, kept apart when asked.
    xml, _ = _box(10, 10, 10, unmerged=True)
    path = _write_3mf(
        tmp_path / "dup.3mf",
        f'<?xml version="1.0" encoding="UTF-8"?><model unit="millimeter" {_NS}><resources>'
        f'<object id="1" name="box" type="model">{xml}</object></resources>'
        '<build><item objectid="1"/></build></model>',
    )
    assert len(load_mesh(path, force="mesh", process=False).vertices) == 36
    assert len(load_mesh(path, force="mesh").vertices) == 8

    with open(path, "rb") as fh:
        from_bytes = load_mesh(io.BytesIO(fh.read()), file_type="3mf", force="mesh")
    assert np.allclose(from_bytes.bounds, load_mesh(path, force="mesh").bounds)


# ---------------------------------------------------------------------------
# The registered tools, where lxml and networkx cannot be imported
# ---------------------------------------------------------------------------

_CHILD = r"""
import sys
for _name in ("lxml", "networkx"):
    if sys.modules.get(_name) is not None:
        sys.exit(f"cannot hide {_name}: imported before the child could hide it")
    sys.modules[_name] = None
sys.path[:0] = PATHS
import asyncio, json
try:
    import lxml  # noqa: F401
    sys.exit("lxml is still importable")
except ImportError:
    pass
from kiln import terms
terms.record_acceptance(method="test")
from kiln import server
getattr(server, "_ensure_internal_tool_plugins_registered", lambda: None)()
from kiln.mcp_compat import tool_result_blocks

def call(name, args):
    out = asyncio.run(server.mcp.call_tool(name, args))
    return json.loads(next(b.text for b in tool_result_blocks(out) if hasattr(b, "text")))

out = {}
for kind, (model, cube) in CASES.items():
    out[kind] = {
        "diagnose_mesh": call("diagnose_mesh", {"file_path": model}),
        "thicken_mesh_walls": call("thicken_mesh_walls", {"file_path": cube, "amount_mm": 0.5,
                                                          "output_path": cube + ".thick.stl"}),
        "arrange_parts_on_plate": call("arrange_parts_on_plate", {"file_paths": json.dumps([model, cube])}),
        "analyze_model_geometry": call("analyze_model_geometry", {"model_path": model}),
        "fingerprint_model": call("fingerprint_model", {"file_path": model}),
        "auto_color_by_height": call("auto_color_by_height", {"input_path": model, "num_colors": 2}),
        "auto_color_by_region": call("auto_color_by_region", {"input_path": model, "num_colors": 2}),
    }
print("RESULT " + json.dumps(out, default=str))
"""


def _same_numbers(tool: str, answer: dict) -> object:
    """What must agree between a 3MF and the same part as STL."""
    if tool == "diagnose_mesh":
        r = answer["report"]
        return r["face_count"], r["volume_mm3"], r["surface_area_mm2"], r["bounding_box"], r["is_watertight"]
    if tool == "thicken_mesh_walls":
        return answer["measured"]["before"], answer["measured"]["after"]
    if tool == "arrange_parts_on_plate":
        return [(p["x"], p["y"], p["width"], p["depth"], p["height"]) for p in answer["arranged_parts"]]
    if tool == "analyze_model_geometry":
        return answer["regions"]
    if tool == "fingerprint_model":
        f = dict(answer["fingerprint"])
        f.pop("file_hash")  # a hash of the file's own bytes
        return f
    return answer["total_faces"], [(z["face_count"], z["estimated_weight_g"]) for z in answer["zones"]]


def test_every_model_reading_tool_answers_a_3mf_like_its_stl_without_lxml(tmp_path):
    cases = {}
    for kind in ("3mf", "stl"):
        folder = tmp_path / kind
        folder.mkdir()
        model_3mf, model_stl = _two_part_model(folder)
        cube_3mf, cube_stl = _cube(folder)
        cases[kind] = (model_3mf, cube_3mf) if kind == "3mf" else (model_stl, cube_stl)
    home = tmp_path / "home"
    home.mkdir()
    script = _CHILD.replace("PATHS", repr(list(sys.path))).replace("CASES", repr(cases))
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(home), "KILN_SANDBOX": "1"}
    run = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=600, env=env,
        cwd=str(tmp_path),
    )
    lines = [line for line in run.stdout.splitlines() if line.startswith("RESULT ")]
    assert run.returncode == 0 and lines, run.stderr[-3000:]
    answers = json.loads(lines[-1][len("RESULT "):])

    failed = {
        tool: answer for tool, answer in answers["3mf"].items() if answer.get("success") is not True
    }
    assert failed == {}, f"these tools could not read a 3MF without lxml: {failed}"
    for tool, answer in answers["3mf"].items():
        assert answers["stl"][tool].get("success") is True, (tool, answers["stl"][tool])
        assert _same_numbers(tool, answer) == _same_numbers(tool, answers["stl"][tool]), tool
    # And the numbers are the part's, not merely equal.
    report = answers["3mf"]["diagnose_mesh"]["report"]
    assert report["face_count"] == 36 and report["volume_mm3"] == pytest.approx(14560.0)
    assert answers["3mf"]["fingerprint_model"]["fingerprint"]["triangle_count"] == 36


# ---------------------------------------------------------------------------
# A small file that asks for far more than it holds
# ---------------------------------------------------------------------------


def _mesh_xml(mesh: trimesh.Trimesh) -> str:
    verts = "".join(f'<vertex x="{x:.4f}" y="{y:.4f}" z="{z:.4f}"/>' for x, y, z in mesh.vertices)
    tris = "".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in mesh.faces)
    return f"<mesh><vertices>{verts}</vertices><triangles>{tris}</triangles></mesh>"


def _fan(child: int, copies: int) -> str:
    """Components placing object *child* *copies* times in a row."""
    return "<components>" + "".join(
        f'<component objectid="{child}" transform="1 0 0 0 1 0 0 0 1 {i * 3} 0 0"/>'
        for i in range(copies)
    ) + "</components>"


def _expanding(folder: Path, shape: str) -> str:
    """A 3MF of a few kilobytes whose build asks for far more than it holds."""
    cube = _mesh_xml(trimesh.creation.box(extents=(2, 2, 2)))
    if shape == "many placements":  # 26,000 copies of a cube
        resources = (
            f'<object id="1" name="cube" type="model">{cube}</object>'
            f'<object id="2" type="model">{_fan(1, 200)}</object>'
            f'<object id="3" type="model">{_fan(2, 130)}</object>'
        )
        item = "3"
    elif shape == "much mesh":  # 3,200 copies of a 1,922-entry ball
        ball = _mesh_xml(trimesh.creation.icosphere(subdivisions=3, radius=1.0))
        resources = (
            f'<object id="1" name="ball" type="model">{ball}</object>'
            f'<object id="2" type="model">{_fan(1, 80)}</object>'
            f'<object id="3" type="model">{_fan(2, 40)}</object>'
        )
        item = "3"
    elif shape == "deep nest":  # 40 levels, one component each
        resources = f'<object id="1" name="cube" type="model">{cube}</object>' + "".join(
            f'<object id="{i}" type="model">{_fan(i - 1, 1)}</object>' for i in range(2, 42)
        )
        item = "41"
    elif shape == "components naming nothing":  # 200 copies x 10,000 dangling
        holder = (
            '<components><component objectid="1"/>'
            + '<component objectid="999" transform="1 0 0 0 1 0 0 0 1 1 1 1"/>' * 10_000
            + "</components>"
        )
        resources = (
            f'<object id="1" name="cube" type="model">{cube}</object>'
            f'<object id="2" type="model">{holder}</object>'
            f'<object id="3" type="model">{_fan(2, 200)}</object>'
        )
        item = "3"
    else:  # a component that contains itself
        resources = (
            f'<object id="1" name="cube" type="model">{cube}'
            '<components><component objectid="1"/></components></object>'
        )
        item = "1"
    return _write_3mf(
        folder / f"{shape.replace(' ', '_')}.3mf",
        f'<?xml version="1.0" encoding="UTF-8"?><model unit="millimeter" {_NS}>'
        f'<resources>{resources}</resources><build><item objectid="{item}"/></build></model>',
    )


_EXPANDING = {
    "many placements": "places more than 25,600 meshes once every copy is counted",
    "much mesh": "more than 6,000,000 vertices and triangles once every copy is counted",
    "deep nest": "nests components more than 32 deep",
    "a component that contains itself": "contains itself",
    "components naming nothing": "names more than 100,000 parts once every copy",
}


@pytest.mark.parametrize("shape", sorted(_EXPANDING))
def test_a_small_3mf_that_expands_past_the_limits_is_refused_in_words(tmp_path, shape):
    """The size census reads what a file holds; a build can place one small
    mesh any number of times, or name nothing many times over.  Every read of
    the build counts each item and component it visits (named object or not),
    what it places, how deep it nests and the mesh each copy adds, and stops
    before the visit that would pass a limit -- through the registered tool,
    the shared loader and the analysis reader alike."""
    import asyncio
    import time

    from kiln import server
    from kiln.generation.validation import _parse_3mf
    from kiln.mcp_compat import tool_result_blocks
    from kiln.mesh_frame import MeshTooLargeError

    path = _expanding(tmp_path, shape)
    assert os.path.getsize(path) < 20_000
    words = _EXPANDING[shape]
    started = time.monotonic()

    with pytest.raises(MeshTooLargeError, match=words):
        load_mesh(path, force="mesh")

    out = asyncio.run(server.mcp.call_tool("diagnose_mesh", {"file_path": path}))
    answer = json.loads(next(b.text for b in tool_result_blocks(out) if hasattr(b, "text")))
    assert answer["success"] is False and words in answer["error"]["message"], answer

    errors: list[str] = []
    triangles, _ = _parse_3mf(Path(path), errors)
    assert triangles == [] and any(words in e for e in errors), errors
    # Every visit is counted before it does any work, so the refusals come
    # fast: a walk that did the uncounted work first takes many seconds.
    assert time.monotonic() - started < 10.0


def test_a_member_that_inflates_past_its_declared_size_stops_there(tmp_path):
    """A member's header may claim less than it holds.  The read stops at
    the claimed size, so the file is refused rather than read whole."""
    import struct

    mesh = trimesh.creation.icosphere(subdivisions=5)
    path = _write_3mf(
        tmp_path / "inflates.3mf",
        f'<?xml version="1.0" encoding="UTF-8"?><model unit="millimeter" {_NS}><resources>'
        f'<object id="1" name="ball" type="model">{_mesh_xml(mesh)}</object></resources>'
        '<build><item objectid="1"/></build></model>',
    )
    member, claimed = "3D/3dmodel.model", 4096
    with zipfile.ZipFile(path) as archive:
        info = archive.getinfo(member)
    assert info.file_size > 100 * claimed
    data = bytearray(Path(path).read_bytes())
    struct.pack_into("<I", data, info.header_offset + 22, claimed)  # local header
    at = data.find(b"PK\x01\x02")
    while at >= 0:  # central directory
        name_len = struct.unpack_from("<H", data, at + 28)[0]
        if data[at + 46:at + 46 + name_len].decode() == member:
            struct.pack_into("<I", data, at + 24, claimed)
        at = data.find(b"PK\x01\x02", at + 4)
    Path(path).write_bytes(bytes(data))

    with pytest.raises(ValueError, match="not a readable 3MF archive"):
        load_mesh(path, force="mesh")


# ---------------------------------------------------------------------------
# One large mesh, placed thousands of times
# ---------------------------------------------------------------------------


def _instanced(folder: Path, *, turned: bool) -> str:
    """A file of tens of kilobytes: one mesh of a million vertices placed
    3,600 times by two rows of 60 components -- every copy sharing one turn,
    or each turned its own way."""
    import math

    def row(child: int, step: float) -> str:
        parts = []
        for i in range(60):
            if turned:
                c, s = math.cos(step * (i + 1)), math.sin(step * (i + 1))
                linear = f"{c!r} {s!r} 0 {-s!r} {c!r} 0 0 0 1"
            else:
                linear = "1 0 0 0 1 0 0 0 1"
            parts.append(f'<component objectid="{child}" transform="{linear} {i} 0 0"/>')
        return "<components>" + "".join(parts) + "</components>"

    mesh = (
        "<mesh><vertices>" + '<vertex x="1" y="2" z="3"/>' * 1_000_000
        + '<vertex x="4" y="5" z="6"/><vertex x="7" y="1" z="0"/></vertices>'
        '<triangles><triangle v1="0" v2="1" v3="2"/></triangles></mesh>'
    )
    path = folder / f"instanced_{'turned' if turned else 'shared'}.3mf"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        archive.writestr(
            "3D/3dmodel.model",
            f'<?xml version="1.0" encoding="UTF-8"?><model unit="millimeter" {_NS}><resources>'
            f'<object id="1" name="flat" type="model">{mesh}</object>'
            f'<object id="2" type="model">{row(1, 0.001)}</object>'
            f'<object id="3" type="model">{row(2, 0.0007)}</object>'
            '</resources><build><item objectid="3"/></build></model>',
        )
    return str(path)


def test_a_large_mesh_placed_thousands_of_times_is_measured_or_refused_fast(tmp_path, caplog):
    """The bed-fit measure folds each mesh once per distinct turn it is placed
    at, so copies that differ only by an offset cost one fold; past the fold
    limit a build is refused in words, and the bed-fit door falls back to the
    bounded mesh read.  Measuring vertex by vertex for every copy, the turned
    file costs billions of steps (about half a minute here)."""
    import asyncio
    import logging
    import time

    from kiln import server
    from kiln.mcp_compat import tool_result_blocks
    from kiln.printers.bed_fit import compute_3mf_geometry_bbox, validate_mesh_for_printer

    shared, turned = _instanced(tmp_path, turned=False), _instanced(tmp_path, turned=True)
    assert os.path.getsize(shared) < 100_000 and os.path.getsize(turned) < 100_000

    started = time.monotonic()
    box = compute_3mf_geometry_bbox(shared)
    assert box == {"x_min": 1.0, "x_max": 125.0, "y_min": 1.0, "y_max": 5.0, "z_min": 0.0, "z_max": 6.0}
    assert time.monotonic() - started < 10.0

    started = time.monotonic()
    with caplog.at_level(logging.WARNING):
        assert compute_3mf_geometry_bbox(turned) is None
    assert "more than 300,000,000 vertices once every differently turned copy is counted" in caplog.text
    fit = validate_mesh_for_printer(turned, "bambu_a1")
    assert "ok" in fit
    assert time.monotonic() - started < 15.0

    started = time.monotonic()
    out = asyncio.run(server.mcp.call_tool("analyze_mesh_geometry", {"file_path": turned}))
    answer = json.loads(next(b.text for b in tool_result_blocks(out) if hasattr(b, "text")))
    assert answer["success"] is False
    assert "places more than 6,000,000 vertices and triangles" in answer["error"]["message"]
    assert time.monotonic() - started < 10.0
