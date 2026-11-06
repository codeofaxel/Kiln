"""A project's support enforcers, blockers, modifiers and negative parts are
not the model.

A project saved by Bambu Studio or OrcaSlicer keeps those volumes as meshes
beside the part they act on, and says which is which in its settings.  They
shape the slice and are never laid down as plastic, and a person is free to
let one hang below the plate.  Kiln measured every mesh as the model, so a
real project (measured 2026-11-05: a support enforcer reaching 8 mm below the
plate under a part standing on it) was refused as "falls outside the
printer's bed", and the enforcer's box was judged for overhangs as though it
would print.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from kiln.generation.validation import _placed_3mf_meshes
from kiln.printers.bed_fit import compute_3mf_geometry_bbox

_CORE = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
_PRODUCTION = "http://schemas.microsoft.com/3dmanufacturing/production/2015/06"
_FACES = [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
          (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]


def _box(low: tuple[float, float, float], high: tuple[float, float, float]) -> str:
    (x0, y0, z0), (x1, y1, z1) = low, high
    corners = [(x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0),
               (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)]
    vertices = "".join(f'<vertex x="{x:g}" y="{y:g}" z="{z:g}"/>' for x, y, z in corners)
    triangles = "".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in _FACES)
    return f"<mesh><vertices>{vertices}</vertices><triangles>{triangles}</triangles></mesh>"


def _project(path: Path, other_kind: str | None) -> str:
    """One object at (100, 100): a 20 mm cube standing on the plate (part 1)
    and a box from 8 mm below the plate to 5 mm above it (part 2), which the
    project's settings call *other_kind* -- or do not mention at all."""
    root = (f'<?xml version="1.0" encoding="UTF-8"?>\n<model unit="millimeter" xmlns="{_CORE}" '
            f'xmlns:p="{_PRODUCTION}" requiredextensions="p">\n <resources>\n  <object id="3" type="model">\n'
            f'   <components>\n    <component p:path="/3D/Objects/object_1.model" objectid="1"/>\n'
            f'    <component p:path="/3D/Objects/object_1.model" objectid="2"/>\n   </components>\n  </object>\n'
            f' </resources>\n <build>\n  <item objectid="3" transform="1 0 0 0 1 0 0 0 1 100 100 0"/>\n </build>\n'
            f"</model>\n")
    parts = (f'<?xml version="1.0" encoding="UTF-8"?>\n<model unit="millimeter" xmlns="{_CORE}">\n <resources>\n'
             f'  <object id="1" type="model">{_box((0, 0, 0), (20, 20, 20))}</object>\n'
             f'  <object id="2" type="model">{_box((0, 0, -8), (30, 30, 5))}</object>\n </resources>\n <build/>\n'
             f"</model>\n")
    second = "" if other_kind is None else f'    <part id="2" subtype="{other_kind}"/>\n'
    settings = ('<?xml version="1.0" encoding="UTF-8"?>\n<config>\n  <object id="3">\n'
                '    <metadata key="name" value="bracket"/>\n    <part id="1" subtype="normal_part"/>\n'
                f"{second}  </object>\n</config>\n")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("3D/3dmodel.model", root)
        zf.writestr("3D/Objects/object_1.model", parts)
        zf.writestr("Metadata/model_settings.config", settings)
    return str(path)


@pytest.mark.parametrize("kind", ["support_enforcer", "support_blocker", "modifier_part", "negative_part"])
def test_a_volume_that_never_prints_is_not_measured_as_the_model(tmp_path: Path, kind: str) -> None:
    project = _project(tmp_path / "project.3mf", kind)

    assert compute_3mf_geometry_bbox(project) == {
        "x_min": 100.0, "x_max": 120.0, "y_min": 100.0, "y_max": 120.0, "z_min": 0.0, "z_max": 20.0,
    }
    errors: list[str] = []
    meshes = _placed_3mf_meshes(Path(project), errors)
    assert [placed.object_id for placed, *_ in meshes] == [1] and not errors


@pytest.mark.parametrize("kind", ["normal_part", None])
def test_a_second_part_that_prints_is_still_the_model(tmp_path: Path, kind: str | None) -> None:
    """Counted unless the project says otherwise: a part it calls plastic, and
    one it says nothing about."""
    project = _project(tmp_path / "project.3mf", kind)

    box = compute_3mf_geometry_bbox(project)
    assert box is not None and box["z_min"] == -8.0 and box["x_max"] == 130.0
    assert [placed.object_id for placed, *_ in _placed_3mf_meshes(Path(project), [])] == [1, 2]


def test_settings_that_cannot_be_read_count_every_part(tmp_path: Path) -> None:
    """The safe way round for a bed check: measure more, never less."""
    project = _project(tmp_path / "project.3mf", "support_enforcer")
    rewritten = tmp_path / "broken.3mf"
    with zipfile.ZipFile(project) as zin, zipfile.ZipFile(rewritten, "w") as zout:
        for info in zin.infolist():
            body = zin.read(info.filename)
            zout.writestr(info, b"<config><object id=" if info.filename.endswith(".config") else body)

    box = compute_3mf_geometry_bbox(str(rewritten))
    assert box is not None and box["z_min"] == -8.0
