"""decorate_surface keeps each carve's files apart.

Coverage areas:
    - two carves of different models that share a file name land at two
      different paths, and the second never replaces the first
    - both stay inside the folder Kiln saves results in for the request
      (``kiln.output_paths.kiln_temp_dir``)
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from kiln import output_paths


def _openscad_available() -> bool:
    try:
        subprocess.run(["openscad", "--version"], capture_output=True, check=True)
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False


needs_openscad = pytest.mark.skipif(
    not _openscad_available(), reason="OpenSCAD required for a real carve"
)


def _disc(folder: Path, d_mm: float) -> str:
    folder.mkdir(parents=True, exist_ok=True)
    scad = folder / "disc.scad"
    scad.write_text(f"cylinder(h=4, d={d_mm}, $fn=96);", encoding="utf-8")
    stl = folder / "disc.stl"
    subprocess.run(["openscad", "-o", str(stl), str(scad)], check=True, capture_output=True)
    return str(stl)


def _carve(model: str, text: str) -> dict:
    import kiln.server as server

    res = server.decorate_surface(
        model_path=model, content=f"text:{text}", content_type="text",
        face="top", mode="deboss", depth_mm=1.0,
    )
    if isinstance(res, list):
        res = next(r for r in res if isinstance(r, dict))
    assert res.get("success"), res
    return res


@needs_openscad
class TestEachCarveKeepsItsOwnFiles:
    def test_same_named_models_never_overwrite_each_other(self, tmp_path):
        root = tmp_path / "request_root"
        root.mkdir()
        token = output_paths.REQUEST_OUTPUT_ROOT.set(str(root))
        try:
            first = _carve(_disc(tmp_path / "a", 80.0), "ONE")
            first_bytes = Path(first["output_stl"]).read_bytes()
            second = _carve(_disc(tmp_path / "b", 50.0), "TWO")
        finally:
            output_paths.REQUEST_OUTPUT_ROOT.reset(token)

        assert first["output_stl"] != second["output_stl"]
        assert first["scad_path"] != second["scad_path"]
        assert Path(first["output_stl"]).read_bytes() == first_bytes
        assert Path(second["output_stl"]).read_bytes() != first_bytes
        for path in (first["output_stl"], second["output_stl"]):
            assert os.path.commonpath([str(root.resolve()), str(Path(path).resolve())]) == str(root.resolve())
