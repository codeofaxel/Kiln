"""The cost pass of the printability analysis reads the mesh once.

It estimates twice (as asked, and at a cheaper infill for the saving it
suggests); an estimate that parsed and analysed the file again would repeat
work the analysis has already done.  And it reads only what an estimate
uses (size, volume, surface, overhang share), never the mesh's pieces,
which the analysis has already read for itself.
"""

from __future__ import annotations

import struct
from pathlib import Path

from kiln import mesh_bodies, printability
from kiln.generation import validation


def _cube(path: Path) -> str:
    c = [(x, y, z) for x in (0.0, 10.0) for y in (0.0, 10.0) for z in (0.0, 10.0)]
    faces = [(0, 2, 1), (1, 2, 3), (4, 5, 6), (5, 7, 6), (0, 1, 4), (1, 5, 4),
             (2, 6, 3), (3, 6, 7), (0, 4, 2), (2, 4, 6), (1, 3, 5), (3, 7, 5)]
    with open(path, "wb") as fh:
        fh.write(b"\x00" * 80 + struct.pack("<I", len(faces)))
        for f in faces:
            fh.write(struct.pack("<3f", 0, 0, 0))
            fh.writelines(struct.pack("<3f", *c[i]) for i in f)
            fh.write(struct.pack("<H", 0))
    return str(path)


def test_the_cost_pass_analyses_the_file_once(tmp_path, monkeypatch):
    path = _cube(tmp_path / "cube.stl")
    reads = []
    real = validation.measure_mesh

    def counted(file_path):
        reads.append(file_path)
        return real(file_path)

    monkeypatch.setattr(validation, "measure_mesh", counted)
    cost = printability._analyze_cost(None, path, material="PLA", infill_percent=20.0)
    assert cost.material_cost_usd > 0
    assert len(reads) == 1, f"the file was analysed {len(reads)} times"


def test_the_cost_pass_does_not_read_the_pieces(tmp_path, monkeypatch):
    path = _cube(tmp_path / "cube.stl")
    pieces = []
    real = mesh_bodies.read_triangles

    def counted(*args, **kwargs):
        pieces.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(mesh_bodies, "read_triangles", counted)
    cost = printability._analyze_cost(None, path, material="PLA", infill_percent=20.0)
    assert cost.material_cost_usd > 0
    assert not pieces, f"the cost pass read the mesh's pieces {len(pieces)} times"


def test_the_estimate_is_the_one_the_full_analysis_gives(tmp_path):
    from kiln.cost_estimator import CostEstimator

    path = _cube(tmp_path / "cube.stl")
    estimator = CostEstimator()
    measured = estimator.estimate_from_mesh(path, material="PETG", include_supports=True, adhesion_type="brim")
    analysed = estimator.estimate_from_mesh(
        path, material="PETG", include_supports=True, adhesion_type="brim",
        analysis=validation.analyze_mesh(path),
    )
    assert measured == analysed
