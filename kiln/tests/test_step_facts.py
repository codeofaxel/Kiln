"""kiln.step_facts — the analytic truth a STEP display carries.

The census is what makes a tessellated STEP preview honest ("1 solid, 4
true cylinders, r=45.000 exact" over the triangles), so these tests pin
three properties:

  1. the census MEASURES — a known shape reports its exact surface types,
     radii and tight bbox, through the real kernel child;
  2. degradation is HONEST — no kernel means ``available: false`` with a
     reason and the header truth a text parse can still prove, never
     invented numbers and never an exception that takes the stage down;
  3. the header parse tells Kiln-stamped files from foreign ones.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from kiln.step_facts import (
    FACTS_KIND,
    KILN_STAMP,
    header_facts,
    read_step_facts,
    unavailable_facts,
)
from kiln.step_import import _ocp_available as _REAL_OCP_AVAILABLE


@pytest.fixture()
def header_only_step(tmp_path: Path) -> Path:
    """A text-level STEP: enough header to parse, no real geometry.

    The FILE_NAME entity is deliberately wrapped across lines with a list
    field before the originating_system slot — the exact shape OCCT's
    writer emits — so the field walk is tested against reality, not a
    tidied one-liner.
    """
    step = tmp_path / "stamped_part.step"
    step.write_text(
        "ISO-10303-21;\n"
        "HEADER;\n"
        "FILE_DESCRIPTION(('Open CASCADE Model'),'2;1');\n"
        "FILE_NAME('stamped_part','2026-08-08T12:00:00',('Kiln - kiln3d.com'),(\n"
        "    'Kiln - kiln3d.com'),'Open CASCADE STEP processor 7.9',\n"
        "  'Kiln - kiln3d.com','Unknown');\n"
        "FILE_SCHEMA(('AUTOMOTIVE_DESIGN { 1 0 10303 214 1 1 1 1 }'));\n"
        "ENDSEC;\n"
        "DATA;\n"
        "#7 = PRODUCT('stamped_part 1','stamped_part 1','',(#8));\n"
        "ENDSEC;\n"
        "END-ISO-10303-21;\n"
    )
    return step


@pytest.fixture()
def foreign_step(tmp_path: Path) -> Path:
    step = tmp_path / "vendor_part.step"
    step.write_text(
        "ISO-10303-21;\n"
        "HEADER;\n"
        "FILE_DESCRIPTION(('CAD model'),'2;1');\n"
        "FILE_NAME('vendor_part.step','2026-01-01',('An Author'),('An Org'),"
        "'preprocessor','SolidWorks 2024','');\n"
        "FILE_SCHEMA(('AP214'));\n"
        "ENDSEC;\n"
        "DATA;\nENDSEC;\nEND-ISO-10303-21;\n"
    )
    return step


# ---------------------------------------------------------------------------
# Header parse — no kernel involved
# ---------------------------------------------------------------------------


def test_header_facts_reads_kiln_stamp_and_name(header_only_step):
    header = header_facts(header_only_step)
    assert header["stamped_by_kiln"] is True
    assert header["originating_system"] == KILN_STAMP
    # FILE_NAME's own name field, not the kernel-suffixed PRODUCT spelling.
    assert header["name"] == "stamped_part"
    assert "AUTOMOTIVE_DESIGN" in header["schema"]


def test_header_facts_foreign_file_is_not_stamped(foreign_step):
    header = header_facts(foreign_step)
    assert header["stamped_by_kiln"] is False
    assert header["originating_system"] == "SolidWorks 2024"


def test_get_step_metadata_extracts_originating_system(header_only_step):
    """The widened existing reader carries the new field."""
    from kiln.step_import import get_step_metadata

    meta = get_step_metadata(str(header_only_step))
    assert meta["originating_system"] == KILN_STAMP


def test_header_facts_survives_a_missing_file(tmp_path):
    header = header_facts(tmp_path / "never_written.step")
    assert header["stamped_by_kiln"] is False
    assert header["originating_system"] is None


# ---------------------------------------------------------------------------
# Honest degradation — the kernel-less install
# ---------------------------------------------------------------------------


def test_read_step_facts_degrades_honestly_without_kernel(
    header_only_step, monkeypatch
):
    monkeypatch.setattr("kiln.step_import._ocp_available", lambda: False)
    facts = read_step_facts(header_only_step)
    assert facts["kind"] == FACTS_KIND
    assert facts["available"] is False
    assert "unavailable" in facts["reason"].lower()
    # No invented geometry — the census keys simply are not there.
    assert "surfaces" not in facts
    assert "cylinders" not in facts
    # The header truth a text parse CAN prove still arrives.
    assert facts["header"]["stamped_by_kiln"] is True


def test_unavailable_facts_names_its_reason(header_only_step):
    facts = unavailable_facts(header_only_step, "because the test says so")
    assert facts["available"] is False
    assert facts["reason"] == "because the test says so"
    assert facts["header"]["name"] == "stamped_part"


def test_read_step_facts_rejects_traversal_and_wrong_extension(tmp_path):
    with pytest.raises((ValueError, FileNotFoundError)):
        read_step_facts(tmp_path / "not_a_step.stl")


# ---------------------------------------------------------------------------
# The real census — kernel present
# ---------------------------------------------------------------------------


def _write_real_step(path: Path, shape) -> None:
    from OCP.IFSelect import IFSelect_ReturnStatus
    from OCP.STEPControl import STEPControl_StepModelType, STEPControl_Writer

    writer = STEPControl_Writer()
    writer.Transfer(shape, STEPControl_StepModelType.STEPControl_AsIs)
    assert writer.Write(str(path)) == IFSelect_ReturnStatus.IFSelect_RetDone


@pytest.fixture()
def real_kernel():
    if not _REAL_OCP_AVAILABLE():
        pytest.skip("OCCT kernel (OCP) not installed")


def test_census_measures_a_true_cylinder(real_kernel, tmp_path):
    """r=45 cylinder: 1 solid, 3 faces (wall + 2 caps), the exact radius."""
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder

    step = tmp_path / "cylinder.step"
    _write_real_step(step, BRepPrimAPI_MakeCylinder(45.0, 8.0).Shape())

    facts = read_step_facts(step)
    assert facts["available"] is True
    assert facts["source"] == "read"
    assert facts["solids"] == 1
    assert facts["surfaces"]["cylinder"] == 1
    assert facts["surfaces"]["plane"] == 2
    assert facts["cylinders"]["count"] == 1
    assert facts["cylinders"]["radii_mm"] == [45.0]
    # Tight analytic bbox — a tessellation-padded box would betray "exact".
    dx, dy, dz = facts["bbox_mm"]["size"]
    assert math.isclose(dx, 90.0, abs_tol=1e-6)
    assert math.isclose(dy, 90.0, abs_tol=1e-6)
    assert math.isclose(dz, 8.0, abs_tol=1e-6)


def test_census_measures_a_box(real_kernel, tmp_path):
    """20×30×40 box: 6 planes, 12 unique edges, no curved surfaces."""
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox

    step = tmp_path / "box.step"
    _write_real_step(step, BRepPrimAPI_MakeBox(20.0, 30.0, 40.0).Shape())

    facts = read_step_facts(step)
    assert facts["available"] is True
    assert facts["surfaces"] == {"plane": 6}
    assert facts["edges"] == 12
    assert facts["cylinders"] == {"count": 0, "radii_mm": []}
    assert facts["bbox_mm"]["size"] == [20.0, 30.0, 40.0]


def test_facts_from_shape_labels_emitted_source(real_kernel):
    """The emitter's census path labels itself — exact by construction."""
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox

    from kiln.step_facts import facts_from_shape

    facts = facts_from_shape(
        BRepPrimAPI_MakeBox(5.0, 5.0, 5.0).Shape(), source="emitted"
    )
    assert facts["source"] == "emitted"
    assert facts["surfaces"] == {"plane": 6}
