"""A scene exported with an exporter's default settings, judged at every door.

2026-10-23, measured on a real scene (a torus with a coat laid on its top and
sixty small pieces) exported in meters, the default unit of the exporter that
made it, and again in millimeters:

* the meter file is a thousandth of its size, and the checks said the part
  rests on a point, with a printability grade of A beside "won't print";
* the piece count was 62,780 in meters and 550 in millimeters, the truth 62:
  the coat shares its corners with the body it lies on, and the count left
  out every edge more than two triangles share;
* those shared edges were reported as thousands of pinched edges and
  self-intersecting faces of one body;
* the millimeter file, written with its origin at the middle as exporters do,
  failed at 10/100 "below the build plate", which every slicer Kiln drives,
  and Kiln's own slice step, settles by setting the part on the plate;
* a printer's NAME passed as ``printer_id`` was read as a model id and
  refused as an internal error worth retrying.

Each case below is one of those, small, through the tool doors a person
reaches.
"""

from __future__ import annotations

import struct
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import trimesh


def _facet(a, b, c) -> bytes:
    return struct.pack("<3f", 0, 0, 0) + b"".join(struct.pack("<3f", *map(float, v)) for v in (a, b, c)) + b"\0\0"


def _write_stl(path: Path, triangles: Any) -> str:
    tris = np.asarray(triangles, dtype=np.float64).reshape(-1, 3, 3)
    path.write_bytes(b"\0" * 80 + struct.pack("<I", len(tris)) + b"".join(_facet(*t) for t in tris))
    return str(path)


def _box(lo, hi, splits: int = 0) -> trimesh.Trimesh:
    lo_a, hi_a = np.asarray(lo, float), np.asarray(hi, float)
    box = trimesh.creation.box(extents=hi_a - lo_a)
    for _ in range(splits):
        box = box.subdivide()
    box.apply_translation((lo_a + hi_a) / 2)
    return box


def _stack_triangles(scale: float = 1.0, nudge: float = 0.0) -> np.ndarray:
    """A 20 x 20 x 10 block with a 4 mm slab laid on its top, sharing the
    block's top corners (lifted by *nudge* of the width, a few float steps)."""
    block = _box((0, 0, 0), (20, 20, 10), splits=2)
    slab = _box((0, 0, 10), (20, 20, 14), splits=2)
    v = slab.vertices.copy()
    v[np.isclose(v[:, 2], 10.0), 2] += nudge * 20.0
    slab = trimesh.Trimesh(v, slab.faces, process=False)
    return np.concatenate([block.triangles, slab.triangles]) * scale


@pytest.fixture(scope="module")
def tools() -> dict[str, Any]:
    from kiln.plugins.generation_tools import _GenerationToolsPlugin
    from kiln.plugins.mesh_diagnostic_tools import _MeshDiagnosticToolsPlugin
    from kiln.plugins.mesh_tools import _MeshToolsPlugin
    from kiln.plugins.printability_tools import _PrintabilityToolsPlugin

    registered: dict[str, Any] = {}

    class _FakeMcp:
        def tool(self, name: str | None = None, **_kwargs):
            def decorator(fn):
                registered[name or fn.__name__] = fn
                return fn

            return decorator

    for plugin in (_PrintabilityToolsPlugin, _GenerationToolsPlugin, _MeshToolsPlugin, _MeshDiagnosticToolsPlugin):
        plugin().register(_FakeMcp())
    return registered


@pytest.fixture
def no_auth(monkeypatch):
    import kiln.server as srv

    monkeypatch.setattr(srv, "_check_auth", lambda *_a, **_k: None)


@pytest.fixture
def meter_cube(tmp_path) -> str:
    """A 20 mm cube written in meters: 0.02 across."""
    return _write_stl(tmp_path / "cube_m.stl", _box((0, 0, 0), (20, 20, 20)).triangles * 0.001)


class TestAPartWrittenInMetersIsAskedAboutItsUnits:
    def test_the_printability_verdict_leads_with_the_units(self, tools, meter_cube):
        out = tools["analyze_printability"](file_path=meter_cube)

        assert out["verdict"] == "wont_print"
        assert out["message"].startswith("Won't print as it is: This model measures 0.02mm")
        assert "rescale_model x1000" in out["message"]
        # The other stops measured a part nobody meant, so they stay out
        # of the headline: no "lay it on a flat face" for a units error.
        assert "flat face" not in out["message"]

    def test_mesh_diagnosis_says_it_before_anything_else(self, tools, meter_cube):
        report = tools["diagnose_mesh"](file_path=meter_cube)["report"]

        assert report["severity"] != "clean"
        assert "meters export" in report["defects"][0]

    def test_validate_and_prepare_mesh_fails_it_and_says_why(self, tools, meter_cube, no_auth):
        out = tools["validate_and_prepare_mesh"](file_path=meter_cube)

        assert out["passed"] is False
        assert "meters export" in out["message"]

    def test_the_same_cube_in_millimeters_is_not_asked(self, tools, tmp_path):
        cube = _write_stl(tmp_path / "cube_mm.stl", _box((0, 0, 0), (20, 20, 20)).triangles)
        out = tools["analyze_printability"](file_path=cube)

        assert out["verdict"] == "ready"
        assert out["report"]["units_question"] == ""


class TestAScoreNeverReadsAsAnABesideWontPrint:
    def test_a_part_on_its_point_grades_f(self, tools, tmp_path):
        cone = trimesh.creation.cone(radius=10.0, height=20.0, sections=48)
        cone.apply_transform(trimesh.transformations.rotation_matrix(np.pi, (1, 0, 0)))
        cone.apply_translation((0, 0, 20.0))
        out = tools["analyze_printability"](file_path=_write_stl(tmp_path / "tip.stl", cone.triangles))

        assert out["verdict"] == "wont_print"
        report = out["report"]
        assert report["grade"] == "F"
        assert report["placement"]["printable_if_placed"] is False
        # The grade is in the report; the message says the verdict alone.
        assert "grade" not in out["message"] and "/100" not in out["message"]

    def test_the_meter_cube_grades_f(self, tools, meter_cube):
        out = tools["analyze_printability"](file_path=meter_cube)

        assert out["report"]["grade"] == "F"
        assert "(grade A)" not in out["message"]


class TestPiecesLaidOnEachOther:
    @pytest.mark.parametrize("nudge", [0.0, 2e-7])
    @pytest.mark.parametrize("scale", [1.0, 0.001])
    def test_diagnosis_counts_two_pieces_in_either_unit(self, tools, tmp_path, scale, nudge):
        """*nudge* 0 is the shared-corner contact the scene had; 2e-7 lifts
        the slab a few float steps, which a fixed weld length joined in the
        meter file and not in the millimeter one."""
        path = _write_stl(tmp_path / f"stack_{scale}_{nudge}.stl", _stack_triangles(scale, nudge=nudge))
        report = tools["diagnose_mesh"](file_path=path)["report"]

        assert report["component_count"] == 2
        assert report["self_intersection_count"] == 0
        assert report["is_watertight"] is True
        assert any("sit on each other" in note for note in report["notes"])
        assert not any("self-intersecting" in d for d in report["defects"])

    def test_every_counting_door_agrees(self, tools, tmp_path):
        path = _write_stl(tmp_path / "stack.stl", _stack_triangles())

        geometry = tools["analyze_mesh_geometry"](file_path=path)
        printability = tools["analyze_printability"](file_path=path)["report"]

        assert geometry["connected_components"] == 2
        assert printability["connected_components"] == 2

    def test_touching_edges_are_not_called_pinched(self, tools, tmp_path, no_auth):
        path = _write_stl(tmp_path / "stack.stl", _stack_triangles())
        out = tools["validate_and_prepare_mesh"](file_path=path)
        noted = " ".join(out["result"]["notes"])
        warned = " ".join(out["result"]["warnings"])

        # Pieces laid on each other are each closed: said as a note, never a
        # defect, and the file is not failed for it.
        assert "sit on each other" in noted
        assert "pinched" not in noted + warned and "not manifold" not in warned
        assert out["result"]["is_manifold"] is True


class TestAPartBelowThePlateIsJudgedOnIt:
    def test_a_part_centred_on_the_origin_reads_like_one_on_the_plate(self, tools, tmp_path):
        seated = _write_stl(tmp_path / "seated.stl", _box((0, 0, 0), (20, 20, 20)).triangles)
        centred = _write_stl(tmp_path / "centred.stl", _box((-10, -10, -10), (10, 10, 10)).triangles)

        on_plate = tools["analyze_printability"](file_path=seated)
        out = tools["analyze_printability"](file_path=centred)

        assert out["verdict"] == on_plate["verdict"] == "ready"
        assert out["report"]["score"] == on_plate["report"]["score"]
        assert out["report"]["placement"]["faults"] == []
        assert out["report"]["placement"]["below_plate_mm"] == 10.0
        assert "judged resting on the plate" in out["message"]

    def test_validate_and_prepare_mesh_passes_it(self, tools, tmp_path, no_auth):
        centred = _write_stl(tmp_path / "centred.stl", _box((-10, -10, -10), (10, 10, 10)).triangles)
        out = tools["validate_and_prepare_mesh"](file_path=centred)

        assert out["passed"] is True


class TestAPrinterNamedByItsOwner:
    def test_a_registered_name_resolves_to_its_model_bed(self, tools, tmp_path, no_auth, monkeypatch):
        monkeypatch.setattr(
            "kiln.printer_model_resolver.resolve_printer_model_for",
            lambda name: "bambu_a1" if name == "default" else None,
        )
        cube = _write_stl(tmp_path / "cube.stl", _box((0, 0, 0), (20, 20, 20)).triangles)
        out = tools["validate_and_prepare_mesh"](file_path=cube, printer_id="default")

        assert out["success"] is True
        assert out["bed_size_model_id"] == "bambu_a1"

    def test_a_name_nobody_registered_is_a_clear_refusal(self, tools, tmp_path, no_auth, monkeypatch):
        monkeypatch.setattr("kiln.printer_model_resolver.resolve_printer_model_for", lambda name: None)
        cube = _write_stl(tmp_path / "cube.stl", _box((0, 0, 0), (20, 20, 20)).triangles)
        out = tools["validate_and_prepare_mesh"](file_path=cube, printer_id="default")

        assert out["success"] is False
        assert out["error"]["code"] == "UNKNOWN_PRINTER"
        assert out["error"]["retryable"] is False
        assert "not a printer model Kiln supports" in out["error"]["message"]
        assert "printer you registered" in out["error"]["message"]


class TestAGlbSplitAtItsSeamsIsSealed:
    def test_diagnosis_joins_seam_corners_before_judging_closure(self, tools, tmp_path):
        box = _box((0, 0, 0), (20, 20, 20))
        tris = box.triangles.reshape(-1, 3)
        # One texture island per triangle: every corner written per triangle.
        uv = np.tile([[0.0, 0.0], [1.0, 0.0], [0.0, 1.0]], (len(box.faces), 1))
        seamed = trimesh.Trimesh(
            tris, np.arange(len(tris)).reshape(-1, 3),
            visual=trimesh.visual.TextureVisuals(uv=uv), process=False,
        )
        path = tmp_path / "seamed.glb"
        seamed.export(path)

        report = tools["diagnose_mesh"](file_path=str(path))["report"]

        assert report["is_watertight"] is True
        assert report["component_count"] == 1


class TestASliceOfAMeterFileSaysWhy:
    """The slicer's own words for a part a thousandth of its size are "No
    layers were detected", which names no unit.  Every slicing door passes
    through ``slice_file``, so the units question is said there first."""

    @staticmethod
    def _failing_prusa(monkeypatch):
        from types import SimpleNamespace

        from kiln.slicer import SlicerInfo

        monkeypatch.setattr(
            "kiln.slicer.find_slicer",
            lambda *_a, **_k: SlicerInfo(path="/fake/PrusaSlicer", name="prusa-slicer",
                                         version="PrusaSlicer-2.9.4 based on Slic3r"),
        )
        monkeypatch.setattr(
            "kiln.slicer._run_slicer_with_startup_retry",
            lambda *_a, **_k: SimpleNamespace(
                returncode=1, stdout="",
                stderr="No layers were detected. You might want to repair your STL file(s) "
                "or check their size or thickness and retry.",
            ),
        )

    def test_the_failure_leads_with_the_units(self, meter_cube, tmp_path, monkeypatch):
        from kiln.slicer import SlicerError, slice_file

        self._failing_prusa(monkeypatch)
        with pytest.raises(SlicerError) as refused:
            slice_file(meter_cube, output_dir=str(tmp_path))

        said = str(refused.value)
        assert said.startswith("This model measures 0.02mm")
        assert "rescale_model x1000" in said
        # The slicer's "No layers were detected ... repair your STL" is advice
        # for a broken file; this one's only fault is its unit, so it is said
        # as what it means at that size.
        assert "At that size the slicer found no layer to print." in said
        assert "repair your" not in said

    def test_a_printable_size_keeps_the_slicers_own_words(self, tmp_path, monkeypatch):
        from kiln.slicer import SlicerError, slice_file

        self._failing_prusa(monkeypatch)
        cube = _write_stl(tmp_path / "cube_mm.stl", _box((0, 0, 0), (20, 20, 20)).triangles)
        with pytest.raises(SlicerError) as refused:
            slice_file(cube, output_dir=str(tmp_path))

        assert str(refused.value).startswith("Slicer exited with code 1")
