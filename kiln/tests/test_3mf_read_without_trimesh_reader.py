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
