"""The cost pass of the printability analysis reads the mesh once.

It estimates twice (as asked, and at a cheaper infill for the saving it
suggests), and each estimate used to parse and analyse the whole file again:
8.6 s of a 34 s analysis on a 327k-triangle model (2026-10-26, cProfile).
"""

from __future__ import annotations

import struct
from pathlib import Path

from kiln import printability
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
    real = validation.analyze_mesh

    def counted(file_path):
        reads.append(file_path)
        return real(file_path)

    monkeypatch.setattr(validation, "analyze_mesh", counted)
    cost = printability._analyze_cost(None, path, material="PLA", infill_percent=20.0)
    assert cost.material_cost_usd > 0
    assert len(reads) == 1, f"the file was analysed {len(reads)} times"
