"""The 3MF readers stream their model parts, so an honest file of any real
size keeps every feature.

Parsed into an ElementTree a model part costs ~16-20x its XML, which took a
real 126 MB downloaded model to ~2 GB.  The readers now build only the structure as elements and read the
mesh as it streams past (``kiln.threemf_parser``, "Streaming model parts").
These tests pin the three promises that make that safe to rely on:

* the bounded readers (bed-fit box, colour count) answer EXACTLY what the
  full readers answer, down to the sign of a zero;
* their memory does not grow with the mesh (A/B measured 2026-10-13: on the
  pre-streaming tree the same model peaked ~1 GB in each; here they stay near
  the import baseline);
* a structure-only bomb — millions of tiny elements, which no lean reader can
  absorb — is still refused, and an honest ~60 MB model is answered.
"""

from __future__ import annotations

import math
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from kiln.generation.validation import _placed_3mf_meshes
from kiln.mesh_payload import _load_3mf_stdlib, _scene_to_single_mesh
from kiln.printers.bed_fit import compute_3mf_geometry_bbox
from kiln.threemf_parser import object_display_colors, parse_colored_3mf

CORE = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
MAT = "http://schemas.microsoft.com/3dmanufacturing/material/2015/02"
PROD = "http://schemas.microsoft.com/3dmanufacturing/production/2015/06"

#: Printed by a measuring child: its OWN peak memory in MB.  On Linux the
#: child reads VmHWM, which belongs to the program it runs; ``ru_maxrss``
#: there carries the forking parent's high-water mark across the exec, so in
#: a pytest worker that has already run heavy tests it reports the worker's
#: peak, not the child's.  macOS has no /proc and keeps no such carry-over.
_PRINT_CHILD_PEAK_MB = (
    "def _peak_mb():\n"
    "    try:\n"
    "        with open('/proc/self/status') as fh:\n"
    "            for line in fh:\n"
    "                if line.startswith('VmHWM:'):\n"
    "                    return int(line.split()[1]) / 1024\n"
    "    except OSError:\n"
    "        pass\n"
    "    r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss\n"
    "    return r / (1024 * 1024) if sys.platform == 'darwin' else r / 1024\n"
    "print(_peak_mb())\n"
)


def _model(body: str, *, unit: str = "millimeter") -> str:
    return (
        f'<?xml version="1.0" encoding="UTF-8"?><model unit="{unit}" xmlns="{CORE}" '
        f'xmlns:m="{MAT}" xmlns:p="{PROD}">{body}</model>'
    )


def _tet(x: float = 0.0, y: float = 0.0, z: float = 0.0, s: float = 10.0, attrs=None) -> str:
    pts = [(x, y, z), (x + s, y, z), (x, y + s, z), (x, y, z + s)]
    tris = [(0, 2, 1), (0, 1, 3), (1, 2, 3), (0, 3, 2)]
    attrs = attrs or [""] * 4
    v = "".join(f'<vertex x="{a}" y="{b}" z="{c}"/>' for a, b, c in pts)
    t = "".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"{attrs[i]}/>' for i, (a, b, c) in enumerate(tris))
    return f"<mesh><vertices>{v}</vertices><triangles>{t}</triangles></mesh>"


def _sphere(target_bytes: int, *, painted: bool = False) -> str:
    """A valid sphere mesh whose XML passes *target_bytes*."""
    n = 8
    while True:
        verts, tris = [], []
        for i in range(n + 1):
            th = math.pi * i / n
            for j in range(n):
                ph = 2 * math.pi * j / n
                verts.append(
                    f'<vertex x="{20 * math.sin(th) * math.cos(ph):.6g}" '
                    f'y="{20 * math.sin(th) * math.sin(ph):.6g}" z="{20 * math.cos(th) + 20:.6g}"/>'
                )
        k = 0
        for i in range(n):
            for j in range(n):
                a, b = i * n + j, i * n + (j + 1) % n
                c, d = (i + 1) * n + j, (i + 1) * n + (j + 1) % n
                for tri in ((a, c, b), (b, c, d)):
                    paint = f' paint_color="{4 if k % 2 else 8}"' if painted and k % 3 else ""
                    tris.append(f'<triangle v1="{tri[0]}" v2="{tri[1]}" v3="{tri[2]}"{paint}/>')
                    k += 1
        mesh = f'<mesh><vertices>{"".join(verts)}</vertices><triangles>{"".join(tris)}</triangles></mesh>'
        if len(mesh) > target_bytes:
            return mesh
        n = int(n * 1.4) + 1


def _write(path: Path, members: dict[str, str]) -> str:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return str(path)


def _ppath_project(tmp_path: Path) -> str:
    """A BambuStudio-shaped project: root assemblies whose meshes live in
    other model parts, a rotated component, two instances, inch units."""
    return _write(tmp_path / "project.3mf", {
        "3D/3dmodel.model": _model(
            '<resources>'
            '<object id="2" type="model" name="Bracket"><components>'
            '<component p:path="/3D/Objects/object_1.model" objectid="1" transform="0 1 0 -1 0 0 0 0 1 3 4 5"/>'
            '</components></object>'
            '<object id="4" type="model" name="Base"><components>'
            '<component p:path="/3D/Objects/object_2.model" objectid="1"/></components></object>'
            '</resources><build>'
            '<item objectid="2" transform="1 0 0 0 1 0 0 0 1 10 0 0"/><item objectid="4"/>'
            '<item objectid="2" transform="0.5 0 0 0 0.5 0 0 0 0.5 -7 1 2"/></build>',
            unit="inch",
        ),
        "3D/Objects/object_1.model": _model(f'<resources><object id="1" type="model">{_sphere(4000)}</object></resources>'),
        "3D/Objects/object_2.model": _model(f'<resources><object id="1" type="model">{_tet(1, 2, 3, 4)}</object></resources>'),
    })


def _brute_bbox(path: str) -> dict[str, float] | None:
    """The bed-fit box the way the list reader built it: every placed vertex
    through the per-point transform (identity included), then ``min`` /
    ``max`` over the whole list in walk order."""
    from kiln.threemf_parser import _apply_3mf_transform, _ModelArchive, open_untrusted_zip

    points = []
    with open_untrusted_zip(path) as zf:
        archive = _ModelArchive(zf, [], keep_mesh=True)
        for placed in archive.placements(root_transform=archive.root_transform_mm()):
            info = archive.mesh(placed)
            if info is not None:
                points.extend(
                    _apply_3mf_transform(placed.transform, tuple(v))
                    for v in info.vertices.tolist()
                )
    if not points:
        return None
    return {
        "x_min": min(p[0] for p in points), "x_max": max(p[0] for p in points),
        "y_min": min(p[1] for p in points), "y_max": max(p[1] for p in points),
        "z_min": min(p[2] for p in points), "z_max": max(p[2] for p in points),
    }


# ---------------------------------------------------------------------------
# The bounded readers answer exactly what the full readers answer
# ---------------------------------------------------------------------------


class TestBedFitBoxIsExact:
    def _cases(self, tmp_path: Path) -> list[str]:
        nan_first = _write(tmp_path / "nan.3mf", {"3D/3dmodel.model": _model(
            '<resources><object id="1" type="model"><mesh><vertices><vertex x="nan" y="1" z="2"/>'
            '<vertex x="5" y="nan" z="1"/><vertex x="3" y="4" z="5"/></vertices>'
            '<triangles><triangle v1="0" v2="1" v3="2"/></triangles></mesh></object></resources>'
            '<build><item objectid="1"/></build>')})
        signed_zero = _write(tmp_path / "zero.3mf", {"3D/3dmodel.model": _model(
            f'<resources><object id="1" type="model">{_tet(0, 0, 0)}</object></resources>'
            '<build><item objectid="1" transform="-1 0 0 0 1 0 0 0 1 0 0 0"/>'
            '<item objectid="1"/></build>')})
        instanced = _write(tmp_path / "inst.3mf", {"3D/3dmodel.model": _model(
            f'<resources><object id="1" type="model">{_tet(1, 1, 1)}</object></resources>'
            '<build><item objectid="1"/><item objectid="1" transform="0 0 1 0 1 0 -1 0 0 40 0 0"/></build>')})
        return [_ppath_project(tmp_path), nan_first, signed_zero, instanced]

    def test_streamed_box_equals_the_list_readers_box_bit_for_bit(self, tmp_path):
        for path in self._cases(tmp_path):
            got = compute_3mf_geometry_bbox(path)
            want = _brute_bbox(path)
            # repr: equal down to NaN and the sign of a zero
            assert {k: repr(v) for k, v in got.items()} == {k: repr(v) for k, v in want.items()}, path


class TestColourCountIsExact:
    def test_bounded_count_equals_the_full_parse(self, tmp_path):
        from kiln.threemf_parser import summarize_3mf_colors

        files = [
            _write(tmp_path / "base.3mf", {"3D/3dmodel.model": _model(
                '<resources><basematerials id="5"><base name="r" displaycolor="#FF0000"/>'
                '<base name="b" displaycolor="#0000FF"/></basematerials>'
                f'<object id="1" type="model" pid="5" pindex="0">{_tet()}</object>'
                f'<object id="2" type="model" pid="5" pindex="1">{_tet(20)}</object></resources>'
                '<build><item objectid="1"/><item objectid="2"/></build>')}),
            _write(tmp_path / "group.3mf", {"3D/3dmodel.model": _model(
                '<resources><m:colorgroup id="7"><m:color color="#00FF00"/><m:color color="#123456"/></m:colorgroup>'
                '<object id="1" type="model">'
                + _tet(attrs=[' pid="7" p1="0"', ' pid="7" p1="1"', ' pid="7"', ' pid="9" p1="0"'])
                + '</object></resources><build><item objectid="1"/></build>')}),
            _write(tmp_path / "painted.3mf", {"3D/3dmodel.model": _model(
                f'<resources><object id="1" type="model">{_sphere(3000, painted=True)}</object></resources>'
                '<build><item objectid="1"/></build>')}),
            _write(tmp_path / "sidecar.3mf", {
                "3D/3dmodel.model": _model(
                    f'<resources><object id="1" type="model">{_tet()}</object>'
                    f'<object id="2" type="model">{_tet(20)}</object></resources>'
                    '<build><item objectid="1"/><item objectid="2"/></build>'),
                "Metadata/model_settings.config": (
                    '<config><object id="1"><metadata key="color" value="#FF8800"/></object>'
                    '<object id="2"><metadata key="color" value="00AAFF"/></object></config>'),
            }),
            _ppath_project(tmp_path),
        ]
        for path in files:
            full = parse_colored_3mf(path)
            assert summarize_3mf_colors(path) == (full.colors_found, full.color_count), path


# ---------------------------------------------------------------------------
# Preview: a project that keeps its meshes in other model parts
# ---------------------------------------------------------------------------


def test_preview_follows_ppath_parts_into_the_right_place(tmp_path):
    path = Path(_ppath_project(tmp_path))
    mesh = _scene_to_single_mesh(_load_3mf_stdlib(path), path)
    triangles = [t for _p, _v, ts in _placed_3mf_meshes(path, []) for t in ts]
    assert len(mesh.faces) == len(triangles)
    # The preview keeps the file's own unit (inch); the validation walk is in mm.
    flat = [v for t in triangles for v in t]
    lows = [min(v[i] for v in flat) for i in range(3)]
    highs = [max(v[i] for v in flat) for i in range(3)]
    assert mesh.bounds[0] * 25.4 == pytest.approx(lows, abs=1e-6)
    assert mesh.bounds[1] * 25.4 == pytest.approx(highs, abs=1e-6)


# ---------------------------------------------------------------------------
# Bombs are refused; honest files of any size are not
# ---------------------------------------------------------------------------


def test_a_structure_bomb_is_refused(tmp_path):
    from kiln.threemf_parser import _MAX_RETAINED_ELEMENTS
    from kiln.untrusted_zip import UntrustedArchiveError

    objects = "".join(f'<object id="{i}" type="model"/>' for i in range(_MAX_RETAINED_ELEMENTS + 1))
    path = _write(tmp_path / "structure.3mf", {
        "3D/3dmodel.model": _model(f"<resources>{objects}</resources><build/>"),
    })
    with pytest.raises(UntrustedArchiveError, match="structure"):
        parse_colored_3mf(path)
    assert compute_3mf_geometry_bbox(path) is None
    assert object_display_colors(path) == {}



def test_whole_mesh_readers_refuse_past_their_limit_and_bed_fit_still_answers(
    tmp_path, monkeypatch,
):
    """A reader that hands the whole mesh on holds a bounded amount of it; a
    model past that limit is refused there, while the bed-fit box (which holds
    nothing per vertex) still answers for it."""
    import kiln.threemf_parser as tp
    from kiln.untrusted_zip import UntrustedArchiveError

    body = "".join(
        f'<object id="{i + 1}" type="model">{_tet(x=20.0 * i)}</object>' for i in range(10)
    )
    items = "".join(f'<item objectid="{i + 1}"/>' for i in range(10))
    path = _write(tmp_path / "many.3mf", {
        "3D/3dmodel.model": _model(f"<resources>{body}</resources><build>{items}</build>"),
    })
    # 10 tetrahedra hold 90 entries: 40 vertices, 40 triangles, and one
    # colour signature per object.
    monkeypatch.setattr(tp, "_MAX_KEPT_MESH_ENTRIES", 89)
    with pytest.raises(UntrustedArchiveError, match="too large to hold whole"):
        parse_colored_3mf(path)
    with pytest.raises(ValueError, match="too large to hold whole"):
        _load_3mf_stdlib(Path(path))
    box = compute_3mf_geometry_bbox(path)
    assert box is not None and box["x_max"] == pytest.approx(190.0)

    monkeypatch.setattr(tp, "_MAX_KEPT_MESH_ENTRIES", 90)
    assert len(parse_colored_3mf(path).triangles) == 40


def test_bounded_readers_stay_flat_on_a_60_mb_model(tmp_path):
    """An honest ~60 MB model is answered, and the bed-fit box and the colour
    count stay near their import baseline.

    A/B measured 2026-10-13 on Python 3.12: on the pre-streaming tree the same
    child peaked ~1 GB (each reader built an element per vertex and triangle);
    here it stays well under the bound.  The child calls the colour count the
    way the quote does on either tree (the bounded summary when it exists, the
    full parse before it), so this test runs, and fails, on the old code too.
    The bound is an order of magnitude, never a margin.
    """
    mesh = _sphere(60 * 1024 * 1024)
    path = _write(tmp_path / "big.3mf", {
        "3D/3dmodel.model": _model(
            f'<resources><object id="1" type="model">{mesh}</object></resources>'
            '<build><item objectid="1"/></build>'),
    })
    del mesh
    script = (
        "import sys, resource\n"
        f"sys.path[:0] = {list(sys.path)!r}\n"
        "import numpy, trimesh\n"
        "from kiln.printers.bed_fit import compute_3mf_geometry_bbox\n"
        "import kiln.threemf_parser as tp\n"
        f"p = {path!r}\n"
        "box = compute_3mf_geometry_bbox(p)\n"
        "assert box is not None, 'the box was refused'\n"
        "if hasattr(tp, 'summarize_3mf_colors'):\n"
        "    found, count = tp.summarize_3mf_colors(p)\n"
        "else:\n"
        "    m = tp.parse_colored_3mf(p)\n"
        "    found, count = m.colors_found, m.color_count\n"
        "assert count == 1 and not found, (found, count)\n"
        + _PRINT_CHILD_PEAK_MB
    )
    env = {"PATH": os.environ.get("PATH", "")}
    out = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=300, env=env,
    )
    assert out.returncode == 0, out.stderr[-1500:]
    peak_mb = float(out.stdout.strip().splitlines()[-1])
    assert peak_mb < 400, f"the bounded readers peaked at {peak_mb:.0f} MB"
