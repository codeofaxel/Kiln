"""The CAD intake report — exact numbers, and the grade that must not appear.

Everything Kiln says about a CAD part's size was measured off triangles Kiln
generated.  These tests pin the two halves of the fix:

1. ``read_exact_geometry`` answers from the file's own analytic surfaces, and
   its numbers match a part whose true volume is computed by hand — not
   against another Kiln number, which would only prove Kiln agrees with
   itself.
2. ``cad_intake_report`` never returns a letter grade for CAD, keeps its
   three bands separate, and degrades honestly when there is no kernel.

The fixture is authored analytically on purpose: a 72x46x11 plate with 9 mm
and 14 mm through-holes has a closed-form volume, so the assertions compare
Kiln to arithmetic rather than to itself.

The cache traps, both already paid for elsewhere in this repo: the STEP cache
is CONTENT-addressed, so copying a fixture to a new name is still a hit and
the only way to force a fresh read is to change the geometry.  Every fixture
here that needs a distinct cache entry therefore has distinct dimensions.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from kiln.step_import import (
    ExactGeometry,
    MeshConversion,
    SourceTopology,
    TessellationBound,
    _exact_from_payload,
    read_exact_geometry,
)

pytest.importorskip("OCP", reason="the exact read is a CAD-kernel measurement")


# ---------------------------------------------------------------------------
# Fixtures — authored parts whose true volume is arithmetic, not a Kiln number
# ---------------------------------------------------------------------------


def _write_plate(path: Path, w: float, d: float, t: float, holes) -> float:
    """A plate with through-holes.  Returns its exact analytic volume."""
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt
    from OCP.STEPControl import STEPControl_StepModelType, STEPControl_Writer

    shape = BRepPrimAPI_MakeBox(w, d, t).Shape()
    for cx, cy, r in holes:
        axis = gp_Ax2(gp_Pnt(cx, cy, -1.0), gp_Dir(0, 0, 1))
        shape = BRepAlgoAPI_Cut(
            shape, BRepPrimAPI_MakeCylinder(axis, r, t + 2.0).Shape()
        ).Shape()

    writer = STEPControl_Writer()
    writer.Transfer(shape, STEPControl_StepModelType.STEPControl_AsIs)
    writer.Write(str(path))
    return w * d * t - sum(math.pi * r * r * t for _, _, r in holes)


def _report(path: Path) -> dict:
    """Build a report the way the tool does — door first, then compose.

    ``cad_intake_report`` deliberately takes an already-converted mesh so the
    CAD door stays visible in the tool body; this mirrors that, so a test
    never exercises a composition no caller performs.
    """
    from kiln.generation.validation import cad_intake_report
    from kiln.step_import import resolve_mesh_input

    mesh_path, conversion, refusal = resolve_mesh_input(str(path))
    assert refusal is None, refusal
    return cad_intake_report(str(path), mesh_path, conversion)


@pytest.fixture(scope="module")
def plate(tmp_path_factory) -> tuple[Path, float]:
    """72 x 46 x 11 plate, dia-9 and dia-14 through-holes."""
    path = tmp_path_factory.mktemp("intake") / "plate.step"
    volume = _write_plate(path, 72.0, 46.0, 11.0, [(18.0, 23.0, 4.5), (54.0, 23.0, 7.0)])
    return path, volume


# ---------------------------------------------------------------------------
# 1. The exact read agrees with arithmetic, and the mesh does not
# ---------------------------------------------------------------------------


def test_exact_volume_matches_the_authored_part(plate):
    """The whole point: Kiln's number equals the one the user can compute.

    Tolerance is 1e-9 relative — floating-point noise, not a measurement
    band.  The mesh comparison below is the contrast: three orders of
    magnitude looser, and legitimately so.
    """
    path, true_volume = plate
    exact = read_exact_geometry(str(path))

    assert exact.available, exact.reason
    assert exact.volume_mm3 == pytest.approx(true_volume, rel=1e-9)


def test_exact_area_and_envelope_match_the_authored_part(plate):
    path, _ = plate
    w, d, t = 72.0, 46.0, 11.0
    r1, r2 = 4.5, 7.0
    true_area = (
        2 * (w * d - math.pi * r1 * r1 - math.pi * r2 * r2)
        + 2 * (w * t)
        + 2 * (d * t)
        + 2 * math.pi * r1 * t
        + 2 * math.pi * r2 * t
    )

    exact = read_exact_geometry(str(path))

    assert exact.surface_area_mm2 == pytest.approx(true_area, rel=1e-9)
    assert exact.size_mm == pytest.approx((w, d, t), rel=1e-9)
    assert exact.is_valid is True
    assert exact.topology == SourceTopology(solids=1, shells=1, faces=8)


def test_the_mesh_is_measurably_further_off_than_the_kernel(plate):
    """The regression this whole change exists to prevent.

    If a later refactor quietly routes the exact band through the mesh, the
    numbers would still look plausible — this is what catches it.  The mesh
    is out by roughly 3e-5 relative on this part; the kernel by 1e-12.  The
    assertion is that the kernel is at least a thousand times closer, which
    is loose enough to survive a tessellation-constant change and tight
    enough that a mesh-sourced "exact" band fails it.
    """
    from kiln.generation.validation import analyze_mesh
    from kiln.step_import import resolve_mesh_input

    path, true_volume = plate
    exact = read_exact_geometry(str(path))
    mesh_path, _conversion, refusal = resolve_mesh_input(str(path))
    assert refusal is None, refusal
    mesh = analyze_mesh(mesh_path)

    kernel_error = abs(exact.volume_mm3 - true_volume) / true_volume
    mesh_error = abs(mesh.volume_mm3 - true_volume) / true_volume

    assert mesh_error > 0, "a tessellation that lands exactly is a stubbed test"
    assert kernel_error * 1000 < mesh_error


def test_units_are_mm_whatever_the_file_declares(tmp_path):
    """An inch-declared STEP reads back in millimetres.

    Kiln's mesh pipeline INFERS units from overall size elsewhere; for CAD
    that guess is unnecessary, and this pins why.  Note what the header says:
    the base unit is still ``SI_UNIT(.MILLI.,.METRE.)`` with the inch layered
    on as a ``CONVERSION_BASED_UNIT``, so a text scan for ``SI_UNIT`` would
    call this an inch part millimetres.  Nothing reads it that way.
    """
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.Interface import Interface_Static
    from OCP.STEPControl import (
        STEPControl_Controller,
        STEPControl_StepModelType,
        STEPControl_Writer,
    )

    STEPControl_Controller.Init_s()  # without this the statics are unset
    path = tmp_path / "cube_inch.step"
    assert Interface_Static.SetCVal_s("write.step.unit", "INCH")
    try:
        writer = STEPControl_Writer()
        writer.Transfer(
            BRepPrimAPI_MakeBox(25.4, 25.4, 25.4).Shape(),
            STEPControl_StepModelType.STEPControl_AsIs,
        )
        writer.Write(str(path))
    finally:
        Interface_Static.SetCVal_s("write.step.unit", "MM")

    text = path.read_text(errors="replace")
    assert "CONVERSION_BASED_UNIT('INCH'" in text.replace(" ", "")
    assert "SI_UNIT(.MILLI.,.METRE.)" in text  # the trap, pinned

    exact = read_exact_geometry(str(path))

    assert exact.size_mm == pytest.approx((25.4, 25.4, 25.4), rel=1e-9)
    assert exact.volume_mm3 == pytest.approx(25.4**3, rel=1e-9)


# ---------------------------------------------------------------------------
# 2. Degrading honestly — a gap is a reason, never a number
# ---------------------------------------------------------------------------


def test_a_mesh_has_no_exact_geometry_and_says_so(tmp_path):
    mesh = tmp_path / "part.stl"
    mesh.write_bytes(b"solid x\nendsolid x\n")

    exact = read_exact_geometry(str(mesh))

    assert exact.available is False
    assert exact.volume_mm3 is None
    assert "already triangles" in exact.reason


def test_no_kernel_names_the_install_rather_than_the_file(plate, monkeypatch):
    """Two failures with opposite fixes must not share a message."""
    import kiln.step_import as step_import

    path, _ = plate
    monkeypatch.setattr(step_import, "_ocp_available", lambda: False)

    exact = read_exact_geometry(str(path))

    assert exact.available is False
    assert step_import.INSTALL_COMMAND in exact.reason


def test_a_hosted_caller_is_never_handed_an_install_command(plate, monkeypatch):
    """A hosted caller has no shell, so an install instruction is a dead end
    dressed up as help — the mistake ``install_help`` already fixed once,
    and easy to re-introduce because the local wording reads fine locally.
    """
    import kiln.runtime_env as runtime_env
    import kiln.step_import as step_import

    path, _ = plate
    monkeypatch.setattr(step_import, "_ocp_available", lambda: False)
    monkeypatch.setattr(runtime_env, "is_hosted_multitenant", lambda: True)

    reason = read_exact_geometry(str(path)).reason

    assert step_import.INSTALL_COMMAND not in reason
    assert "pip install" not in reason
    assert "gap on our side" in reason


def test_the_withheld_reason_never_points_at_numbers_that_are_absent(
    plate, monkeypatch
):
    """The first draft closed on "the exact numbers above come from your
    file" unconditionally, which reads as a reassurance sitting next to a
    band that says ``available: false``."""
    import kiln.step_import as step_import

    path, _ = plate
    with_exact = _report(path)
    assert "come from your file" in with_exact["grade_withheld"]

    monkeypatch.setattr(
        step_import,
        "read_exact_geometry",
        lambda p: ExactGeometry(available=False, reason="no kernel here"),
    )
    without = _report(path)

    assert "come from your file" not in without["grade_withheld"]
    assert "describes Kiln's mesh copy" in without["grade_withheld"]
    # The reason for withholding is unchanged — only the pointer to numbers.
    assert "tessellation" in without["grade_withheld"]


def test_a_file_the_kernel_refuses_is_a_reason_not_an_exception(tmp_path):
    """A STEP header on garbage: sniffs as CAD, dies in the kernel."""
    broken = tmp_path / "broken.step"
    broken.write_text("ISO-10303-21;\nHEADER;\nnot actually a step file\n")

    exact = read_exact_geometry(str(broken))

    assert exact.available is False
    assert exact.reason
    assert exact.volume_mm3 is None


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"volume_mm3": 1.0},  # partial
        {"volume_mm3": "not a number", "surface_area_mm2": 1.0,
         "bbox_min_mm": [0, 0, 0], "bbox_max_mm": [1, 1, 1], "is_valid": True},
        {"volume_mm3": 1.0, "surface_area_mm2": 1.0,
         "bbox_min_mm": [0, 0], "bbox_max_mm": [1, 1, 1], "is_valid": True},
    ],
)
def test_a_partial_measurement_is_never_dressed_up_as_an_exact_one(payload):
    """The claim these numbers make is that they are exact.

    Half a payload — from a truncated cache file, or a child killed mid
    write — must read as no measurement rather than as a partial one.
    """
    assert _exact_from_payload(payload) is None


def test_the_cache_returns_the_same_measurement(plate):
    path, _ = plate
    first = read_exact_geometry(str(path))
    second = read_exact_geometry(str(path))
    assert first == second
    assert second.available


# ---------------------------------------------------------------------------
# 3. The report — three bands, and no letter
# ---------------------------------------------------------------------------


def _freecad_console() -> list[str] | None:
    """A FreeCAD that can run a script headless, or None.

    Deliberately looks past ``shutil.which``: on macOS FreeCAD installs as an
    app bundle, and FreeCAD 1.x ships no ``FreeCADCmd`` binary at all —
    console mode is ``FreeCAD -c script.py``.  ``step_import._find_freecad_cmd``
    searches PATH for ``FreeCADCmd``/``freecadcmd``/``freecad-cmd`` and so
    finds neither the location nor the name, which is why a machine with
    FreeCAD installed still reports no backend.  This test wants the real
    second converter, so it goes and gets it.
    """
    import shutil

    if cmd := shutil.which("FreeCADCmd") or shutil.which("freecadcmd"):
        return [cmd]
    bundle = Path("/Applications/FreeCAD.app/Contents/MacOS/FreeCAD")
    return [str(bundle), "-c"] if bundle.is_file() else None


def test_two_backends_grade_one_file_differently(tmp_path):
    """The measurement the design rests on, taken with two REAL converters.

    A 150 mm sphere, written once, converted by the OCCT kernel and by
    FreeCAD.  Measured 2026-08-11: occt 150,970 triangles grading B/83,
    freecad 8,000 grading A/92.  One object, one file, a full letter apart.

    The direction is the part worth keeping: the converter that is 19x finer
    and 19x closer on volume gets the WORSE grade, because its denser mesh
    trips the manifold check and its pole slivers trip degenerate_pct > 0.
    The exact read is unmoved by any of it, which is the whole point.

    Skips without FreeCAD rather than failing — one backend cannot measure a
    two-backend claim, and a test that quietly passes on one would be
    asserting nothing.
    """
    import subprocess

    console = _freecad_console()
    if console is None:
        pytest.skip("needs a second backend (FreeCAD) to compare against")

    from kiln.generation.validation import design_scorecard
    from kiln.step_import import (
        _FREECAD_SCRIPT_TEMPLATE,
        TESSELLATION_TOLERANCE,
        resolve_mesh_input,
    )
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeSphere
    from OCP.gp import gp_Pnt
    from OCP.STEPControl import STEPControl_StepModelType, STEPControl_Writer

    path = tmp_path / "sphere150.step"
    writer = STEPControl_Writer()
    writer.Transfer(
        BRepPrimAPI_MakeSphere(gp_Pnt(0, 0, 0), 75.0).Shape(),
        STEPControl_StepModelType.STEPControl_AsIs,
    )
    writer.Write(str(path))

    out_dir = tmp_path / "fc"
    out_dir.mkdir()
    script = tmp_path / "convert.py"
    script.write_text(
        _FREECAD_SCRIPT_TEMPLATE.format(
            step_path=str(path),
            output_dir=str(out_dir),
            merge=True,
            tolerance=TESSELLATION_TOLERANCE,
        )
    )
    # stdin closed: console mode drops to an interactive prompt otherwise.
    subprocess.run(
        [*console, str(script)],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=300,
        check=False,
    )
    freecad_mesh = out_dir / "merged.stl"
    if not freecad_mesh.is_file():
        pytest.skip("FreeCAD is present but did not produce a mesh here")

    occt_mesh, _conversion, refusal = resolve_mesh_input(str(path))
    assert refusal is None, refusal

    occt = design_scorecard(occt_mesh)
    freecad = design_scorecard(str(freecad_mesh))
    exact = read_exact_geometry(str(path))

    assert occt["triangle_count"] > freecad["triangle_count"] * 5, (
        "the two backends should disagree on density; if they no longer do, "
        "re-measure before trusting the rest of this"
    )
    assert occt["grade"] != freecad["grade"], (
        f"one file graded {occt['grade']} by occt and {freecad['grade']} by "
        "freecad is the reason a CAD file gets no letter"
    )
    # And the number that does not move, whichever converter ran.
    assert exact.volume_mm3 == pytest.approx(4 / 3 * math.pi * 75.0**3, rel=1e-9)


def test_the_score_moves_when_only_the_tessellation_does(tmp_path, monkeypatch):
    """One file, two tessellation densities, two different scores.

    This asserted a LETTER change until 2026-08-11, when `overhangs: the face
    a model stands on is not an overhang` raised printability across the board
    and lifted this disc (99 -> 97 across the sweep) clear of the boundary it
    used to straddle.  Re-measured rather than retuned: on a single backend no
    shape in the sweep crosses a letter any more, so the honest claim here is
    the score, and the letter evidence lives in the cross-backend test above,
    where it is unchanged (occt B/83, freecad A/92).

    That is not a reprieve for the grade.  Whether a letter crosses depends on
    how near a boundary a part happens to sit, so the same dependence is in
    every score and merely invisible in most.

    If this ever stops being true — if tessellation density stops moving the
    score at all — the reason for withholding the grade deserves re-examining,
    so a failure here is a prompt to re-decide rather than a number to nudge.
    """
    import tempfile

    import kiln.step_import as step_import
    from kiln.generation.validation import design_scorecard
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt
    from OCP.STEPControl import STEPControl_StepModelType, STEPControl_Writer

    disc = BRepPrimAPI_MakeCylinder(
        gp_Ax2(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1)), 40.0, 8.0
    ).Shape()
    path = tmp_path / "disc.step"
    writer = STEPControl_Writer()
    writer.Transfer(disc, STEPControl_StepModelType.STEPControl_AsIs)
    writer.Write(str(path))

    scored = {}
    for linear, angular, label in ((5e-3, 0.1, "fine"), (0.2, 0.6, "coarse")):
        monkeypatch.setattr(step_import, "_OCP_LINEAR_DEFLECTION", linear)
        monkeypatch.setattr(step_import, "_OCP_ANGULAR_DEFLECTION", angular)
        outputs, _bodies, _topology = step_import._convert_via_ocp(
            path, Path(tempfile.mkdtemp()), merge_bodies=True
        )
        scored[label] = design_scorecard(outputs[0])

    assert scored["fine"]["overall_score"] > scored["coarse"]["overall_score"], (
        "the score must still move when only the tessellation does"
    )
    # And the exact numbers, which is the contrast: unmoved by any of it.
    exact = read_exact_geometry(str(path))
    assert exact.volume_mm3 == pytest.approx(math.pi * 40.0**2 * 8.0, rel=1e-9)


def test_a_cad_file_gets_no_grade_and_says_why(plate):
    """The core rule.  A grade whose Quality factor scores Kiln's own
    tessellation would move when the converter changes and the part does
    not, so there is no grade — and the reason ships next to the absence."""

    path, _ = plate
    report = _report(path)

    assert report["subject"] == "cad_file"
    assert report["grade"] is None
    assert "overall_score" not in report, "a composite is the thing being avoided"
    assert "tessellation" in report["grade_withheld"]


def test_the_exact_band_carries_the_files_own_numbers(plate):

    path, true_volume = plate
    band = _report(path)["exact"]

    assert band["available"] is True
    assert band["units"] == "mm"
    assert band["volume_mm3"] == pytest.approx(true_volume, rel=1e-9)
    assert band["size_mm"] == {"width_mm": 72.0, "depth_mm": 46.0, "height_mm": 11.0}
    assert band["solids"] == 1
    assert band["is_valid"] is True


def test_our_copys_triangle_counts_are_counted_never_scored(plate):
    """Band 3 is conversion QA.  The moment it grows a score it becomes a
    verdict on somebody's design, which is the original defect."""

    path, _ = plate
    band = _report(path)["about_our_copy"]

    assert band["triangle_count"] > 0
    assert band["degenerate_triangles"] == 0
    assert band["backend"]
    assert not any("score" in key for key in band), band.keys()


def test_an_assembly_says_its_volume_is_a_total(tmp_path):
    """The kernel sums an assembly.  That is the right answer to "how much
    material" and the wrong one to read as a part, so the count alone is not
    enough — a single figure invites the assumption that it is one part."""
    from kiln.generation.validation import _exact_band
    from OCP.BRep import BRep_Builder
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.gp import gp_Pnt
    from OCP.STEPControl import STEPControl_StepModelType, STEPControl_Writer
    from OCP.TopoDS import TopoDS_Compound

    builder = BRep_Builder()
    compound = TopoDS_Compound()
    builder.MakeCompound(compound)
    builder.Add(compound, BRepPrimAPI_MakeBox(20.0, 10.0, 5.0).Shape())
    builder.Add(
        compound, BRepPrimAPI_MakeBox(gp_Pnt(40.0, 0.0, 0.0), 10.0, 10.0, 5.0).Shape()
    )
    path = tmp_path / "assembly.step"
    writer = STEPControl_Writer()
    writer.Transfer(compound, STEPControl_StepModelType.STEPControl_AsIs)
    writer.Write(str(path))

    exact = read_exact_geometry(str(path))

    assert exact.topology.solids == 2
    assert exact.volume_mm3 == pytest.approx(20 * 10 * 5 + 10 * 10 * 5, rel=1e-9)
    band = _exact_band(exact)
    assert band["solids"] == 2
    assert "total across all of them, not per part" in " ".join(band["notes"])


def test_our_copy_band_carries_no_server_path(plate):
    """On hosted this would be a temp path the caller cannot reach; once
    every geometry door converts for itself, it earned nothing anywhere."""
    band = _report(plate[0])["about_our_copy"]

    assert not any("path" in key for key in band), band.keys()


def test_the_measured_band_names_whose_mesh_it_measured(plate):

    path, _ = plate
    band = _report(path)["measured"]

    assert "not the file itself" in band["measured_on"]
    assert "Converted from CAD" in band["conversion"]
    assert band["printability"]["score"] >= 0


def test_the_conversion_difference_is_this_part_not_a_benchmark(plate):
    """The per-backend figures on file are taken against a 150 mm sphere —
    they compare converters.  This is the caller's own part, measured both
    ways, which is the question they actually asked."""

    path, true_volume = plate
    diff = _report(path)["measured"]["conversion_difference"]

    assert diff["available"] is True
    assert diff["volume_delta_mm3"] != 0
    assert abs(diff["volume_delta_pct"]) < 0.01
    # It must never read as a tolerance: a volume integral cancels inward
    # and outward error, and the wording has to keep saying so.
    assert "Not a surface tolerance" in diff["what_this_is"]


def test_the_report_survives_a_machine_with_no_kernel(plate, monkeypatch):
    """The exact band is the only one that needs a kernel.  Losing it must
    not take the other two down — a report that dies entirely is worse than
    the report Kiln gave before any of this existed."""
    import kiln.step_import as step_import

    path, _ = plate
    # Convert first, so the mesh is cached and the conversion still succeeds
    # with the exact read stubbed out.
    read_exact_geometry(str(path))
    step_import.resolve_mesh_input(str(path))
    monkeypatch.setattr(
        step_import,
        "read_exact_geometry",
        lambda p: ExactGeometry(available=False, reason="no kernel here"),
    )

    report = _report(path)

    assert report["exact"] == {"available": False, "reason": "no kernel here"}
    assert report["measured"]["printability"]["score"] >= 0
    assert report["about_our_copy"]["triangle_count"] > 0
    assert report["measured"]["conversion_difference"] == {"available": False}


def test_a_surface_model_says_no_solid_without_calling_it_unprintable(tmp_path):
    """Measured twice in this repo: a STEP with zero solids tessellates
    watertight to the solid's own volume, because STL carries no topology
    and vertex welding closes it.  So the note reports what the FILE said
    and leaves printability to the mesh layer."""
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.BRep import BRep_Builder
    from OCP.STEPControl import STEPControl_StepModelType, STEPControl_Writer
    from OCP.TopAbs import TopAbs_ShapeEnum
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS_Compound

    box = BRepPrimAPI_MakeBox(63.0, 41.0, 9.0).Shape()
    builder = BRep_Builder()
    compound = TopoDS_Compound()
    builder.MakeCompound(compound)
    explorer = TopExp_Explorer(box, TopAbs_ShapeEnum.TopAbs_FACE)
    while explorer.More():
        builder.Add(compound, explorer.Current())
        explorer.Next()

    path = tmp_path / "faces.step"
    writer = STEPControl_Writer()
    writer.Transfer(compound, STEPControl_StepModelType.STEPControl_AsIs)
    writer.Write(str(path))

    exact = read_exact_geometry(str(path))

    assert exact.available, exact.reason
    assert exact.topology.solids == 0
    assert exact.topology.is_surface_model

    from kiln.generation.validation import _exact_band

    notes = " ".join(_exact_band(exact)["notes"]).lower()
    assert "declares no solid" in notes
    assert "may still mesh and print correctly" in notes
    # The claim that must never appear: it is false on this very fixture, and
    # a false refusal costs an engineer a re-export they did not need.
    for refusal in ("not printable", "unprintable", "cannot be printed"):
        assert refusal not in notes


# ---------------------------------------------------------------------------
# 4. The conversion sentence — one wording, wherever it comes from
# ---------------------------------------------------------------------------


def test_the_conversion_sentence_is_the_shared_one_when_available():
    """kiln-pro owns the measured per-backend figures and their careful
    wording.  Handed the dataclass instead of a dict that reader normalizes
    to None and answers "no record of how this mesh was made" for a mesh
    whose record is right there — well-formed and wrong."""
    pytest.importorskip("kiln_pro", reason="the shared wording lives in kiln-pro")
    from kiln.generation.validation import _conversion_sentence

    record = MeshConversion(
        backend="occt",
        bound=TessellationBound(kind="linear_angular", linear=5e-3, angular=0.1),
        source=SourceTopology(solids=1, shells=1, faces=6),
    )

    sentence = _conversion_sentence(record)

    assert "OCCT" in sentence
    assert "no record" not in sentence.lower()
    assert "150 mm sphere" in sentence, "a deviation must name its reference"


def test_the_conversion_sentence_stands_alone_without_kiln_pro(monkeypatch):
    """Public Kiln states the backend and the density it chose — what this
    repo knows for certain — and claims no deviation figure it cannot
    substantiate."""
    import builtins

    from kiln.generation.validation import _conversion_sentence

    real_import = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name.startswith("kiln_pro"):
            raise ImportError("kiln_pro is not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)

    sentence = _conversion_sentence(
        MeshConversion(
            backend="occt",
            bound=TessellationBound(kind="linear_angular", linear=5e-3, angular=0.1),
        )
    )

    assert "occt" in sentence
    assert "0.005 mm chord" in sentence
    assert "sphere" not in sentence, "no figure this repo cannot substantiate"


def test_an_ordinary_mesh_reports_no_conversion():
    from kiln.generation.validation import _conversion_sentence

    assert "already a mesh" in _conversion_sentence(None)


# ---------------------------------------------------------------------------
# 5. The tool surface — what a caller actually invokes
# ---------------------------------------------------------------------------


def _register_mesh_tools():
    """Register the mesh plugin against a fake MCP and return its tools.

    Testing the engine is not testing the product: the wrapper carries the
    CAD routing, and it is the wrapper an agent and the REST API call.
    """
    from kiln.plugins.mesh_tools import plugin

    tools: dict[str, object] = {}

    class FakeMCP:
        def tool(self_mcp):
            def decorate(fn):
                tools[fn.__name__] = fn
                return fn

            return decorate

    plugin.register(FakeMCP())
    return tools


def test_the_tool_routes_a_step_file_to_the_intake_report(plate):
    path, true_volume = plate
    scorecard = _register_mesh_tools()["mesh_quality_scorecard"]

    result = scorecard(str(path))

    assert result["success"] is True
    assert result["subject"] == "cad_file"
    assert result["grade"] is None
    assert result["exact"]["volume_mm3"] == pytest.approx(true_volume, rel=1e-9)


def test_the_tool_still_grades_a_mesh(plate):
    """Widening the door must not close it behind meshes."""
    from kiln.step_import import resolve_mesh_input

    path, _ = plate
    mesh_path, _conversion, refusal = resolve_mesh_input(str(path))
    assert refusal is None

    result = _register_mesh_tools()["mesh_quality_scorecard"](mesh_path)

    assert result["success"] is True
    assert result["subject"] == "mesh"
    assert result["grade"] in {"A", "B", "C", "D", "F"}
    assert result["overall_score"] >= 0


def test_a_disguised_step_is_told_the_real_problem(plate, tmp_path):
    """A STEP saved as .txt is still CAD, and the tool routes by CONTENT —
    believing the extension is how a binary-STL reader once told an engineer
    their export was corrupt when it was simply not an STL.

    Kiln's converter, though, goes by extension, so this file cannot be
    meshed.  The honest answer names that: the format is supported, the NAME
    is not.  A bare "unsupported format" would be the wrong half of it.
    """
    path, _ = plate
    disguised = tmp_path / "part.txt"
    disguised.write_bytes(path.read_bytes())

    # The kernel does not care what the file is called.
    assert read_exact_geometry(str(disguised)).available

    result = _register_mesh_tools()["mesh_quality_scorecard"](str(disguised))

    assert result.get("success") is False
    message = result["error"]["message"]
    assert "not named like a CAD file" in message
    assert "part.step" in message


def test_the_size_door_carries_the_exact_numbers_too(plate):
    """The every-door rule.  ``analyze_mesh_geometry`` is the tool an agent
    reaches for first when asked how big a part is; answering there off
    triangles while the scorecard answers off the file would be two right
    answers with no way to tell which you are holding."""
    path, true_volume = plate

    result = _register_mesh_tools()["analyze_mesh_geometry"](str(path))

    assert result["success"] is True
    assert result["exact"]["volume_mm3"] == pytest.approx(true_volume, rel=1e-9)
    # The mesh metrics are still there, still measured on the mesh, and
    # still legitimately different.
    assert result["volume_mm3"] != result["exact"]["volume_mm3"]


def test_the_size_door_adds_nothing_for_an_ordinary_mesh(plate):
    """The helper is None for anything that is not CAD, so a mesh caller
    pays a content sniff and carries no empty band."""
    from kiln.generation.validation import exact_geometry_block
    from kiln.step_import import resolve_mesh_input

    path, _ = plate
    mesh_path, _conversion, refusal = resolve_mesh_input(str(path))
    assert refusal is None

    assert exact_geometry_block(mesh_path) is None
    assert "exact" not in _register_mesh_tools()["analyze_mesh_geometry"](mesh_path)


def test_both_doors_read_the_users_file_not_kilns_copy(plate):
    """The shadowing trap: ``analyze_mesh_geometry`` rebinds ``file_path``
    to the converted mesh at its door, so an exact read taken after that
    would answer "this is already triangles" for a CAD file."""
    path, _ = plate

    scorecard = _register_mesh_tools()["mesh_quality_scorecard"](str(path))
    geometry = _register_mesh_tools()["analyze_mesh_geometry"](str(path))

    assert scorecard["exact"]["available"] is True
    assert geometry["exact"]["available"] is True
    assert (
        scorecard["exact"]["volume_mm3"] == geometry["exact"]["volume_mm3"]
    ), "one file, one exact answer, whichever door asked"


def test_the_tool_used_to_dead_end_on_cad(plate):
    """Before this change ``mesh_quality_scorecard`` handed a STEP straight
    to the mesh analyzer, which refused it: ``Unsupported format: .step``.
    A CAD user's most natural next question after importing had no answer.
    Pinned as a regression so the routing cannot be dropped and leave the
    dead end behind."""
    from kiln.generation.validation import design_scorecard

    path, _ = plate
    with pytest.raises(ValueError, match="Unsupported format"):
        design_scorecard(str(path))

    assert _register_mesh_tools()["mesh_quality_scorecard"](str(path))["success"]
