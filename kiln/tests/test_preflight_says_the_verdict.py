"""The pre-flight check says whether the part itself prints, and agrees
with the print door about it.

``preflight_check`` once judged the printer and the file's format and never
looked at the part: given a cone resting on its tip it said "All pre-flight
checks passed. Ready to print." while the print door one call later refused
the same file.  On 2026-10-23 it gained a ``model_printable`` row carrying
the printability verdict and its sentence, as an advisory that left
``ready`` alone, so it could still call a part ready that the next call
refuses.  Since 2026-10-24 the row FAILS exactly where the print door
refuses the file and passes everywhere else: both read the same pre-print
check (``gate_for_print``), so the two cannot answer differently.  A part
that prints with supports passes, as it does at the door, and a check that
could not run is listed as not checked and fails nothing.  The tests that
asserted the advisory rule were changed, not deleted, so the reversal is
on the record.
"""

from __future__ import annotations

import struct
import sys
from unittest.mock import MagicMock, patch

import pytest

trimesh = pytest.importorskip("trimesh")

from tests.test_the_verdict_is_the_answer import (  # noqa: E402,F401 -- fixtures
    _EXPECTED,
    _SCORE_SAID,
    block,
    cone_on_its_tip,
    mushroom,
)


def _state():
    from kiln.printers.base import PrinterStatus

    state = MagicMock()
    state.connected = True
    state.state = PrinterStatus.IDLE
    state.tool_temp_actual = state.bed_temp_actual = 25.0
    state.tool_temp_target = state.bed_temp_target = 0.0
    return state


@pytest.fixture()
def preflight(monkeypatch):
    """``preflight_check`` against an idle stand-in printer, no kiln-pro."""
    monkeypatch.setitem(sys.modules, "kiln_pro.plugins.git_render_tools", None)
    with (
        patch("kiln.server._get_adapter") as adapter,
        patch("kiln.server._get_temp_limits", return_value=(280.0, 120.0)),
        patch("kiln.server.get_db"),
        patch("kiln.server._registry") as registry,
    ):
        adapter.return_value.get_state.return_value = _state()
        registry.count = 1
        registry.list_names.return_value = ["bench"]
        from kiln.server import preflight_check

        yield preflight_check


@pytest.fixture()
def too_big_for_the_bed(tmp_path) -> str:
    """A plain slab wider than a 256 mm bed: refused once a printer is known."""
    slab = trimesh.creation.box(extents=(300, 300, 10))
    slab.apply_translation((150, 150, 5))
    path = tmp_path / "slab.3mf"
    slab.export(path)
    return str(path)


@pytest.fixture()
def no_part(tmp_path) -> str:
    """A well-formed STL holding no triangles: the door refuses it outright."""
    path = tmp_path / "empty.stl"
    path.write_bytes(b"\0" * 80 + struct.pack("<I", 0))
    return str(path)


def _as_3mf(stl: str) -> str:
    out = stl[: -len(".stl")] + ".3mf"
    trimesh.load(stl, force="mesh").export(out)
    return out


def _row(result: dict) -> dict | None:
    rows = [c for c in result["checks"] if c["name"] == "model_printable"]
    assert len(rows) <= 1, rows
    return rows[0] if rows else None


def _door(path: str, material: str = ""):
    """The print door's pre-print check on *path*, for the machine the door
    would aim at when no printer is named (``slice_and_print``'s own
    resolution), so the comparison is with the door and not a guess at it."""
    from kiln import server
    from kiln.plugins.validation_pipeline_tools import gate_for_print

    return gate_for_print(
        path, printer_id=server._resolve_printer_profile_id(None, None) or "", material=material,
    )


@pytest.mark.parametrize("part", sorted(_EXPECTED))
def test_a_model_file_gets_the_verdict_row(part, request, preflight):
    from kiln.printability import analyze_printability, verdict

    stl = request.getfixturevalue(part)
    key, text = verdict(analyze_printability(stl))
    for path in (stl, _as_3mf(stl)):
        result = preflight(file_path=path)
        row = _row(result)
        assert row is not None, (path, result["checks"])
        assert row["printability_verdict"] == key == _EXPECTED[part], row
        assert row["printability_verdict_text"] == text, row
        assert not _SCORE_SAID.search(row["message"]), row
        door = _door(path)
        # The row says what the door says: the verdict when it would print,
        # the door's own reason when it would refuse.
        assert row["passed"] is (door.reason is None), (row, door.reason)
        assert row["message"] == (text if door.reason is None else door.reason), row
        # The row is a real check now: ``ready`` counts it like any other.
        others = [c for c in result["checks"] if c["name"] != "model_printable"]
        assert result["ready"] is (all(c["passed"] for c in others) and row["passed"]), result


def test_a_part_that_wont_print_is_not_ready(cone_on_its_tip, preflight):
    path = _as_3mf(cone_on_its_tip)
    result = preflight(file_path=path)
    row = _row(result)
    others = [c for c in result["checks"] if c["name"] != "model_printable"]
    # Every other check passes, so what keeps it from ready is the part.
    assert all(c["passed"] for c in others), others
    assert row["passed"] is False and row["printability_verdict"] == "wont_print", row
    assert result["ready"] is False, result
    assert "Ready to print." not in result["summary"], result["summary"]
    assert "Won't print as it is" in result["summary"], result["summary"]
    assert row["message"] in result["errors"], result["errors"]
    # ...and the print door refuses the same file for the same reason.
    door = _door(path)
    assert door.reason and door.reason == row["message"], (door.reason, row)


@pytest.mark.parametrize(("part", "opening"), [
    ("mushroom", "Prints with supports"),
    ("block", "Ready to print."),
])
def test_a_part_that_prints_passes(part, opening, request, preflight):
    """No stricter than the door: a part it prints, supports and all, passes
    and leaves ``ready`` to the other checks."""
    path = _as_3mf(request.getfixturevalue(part))
    result = preflight(file_path=path)
    row = _row(result)
    assert row["passed"] is True, row
    assert row["printability_verdict_text"].startswith(opening), row
    assert row["message"] == row["printability_verdict_text"], row
    others = [c for c in result["checks"] if c["name"] != "model_printable"]
    assert result["ready"] is all(c["passed"] for c in others), result
    assert _door(path).reason is None


@pytest.mark.parametrize(("part", "printer"), [
    ("block", ""),
    ("mushroom", ""),
    ("cone_on_its_tip", ""),
    ("no_part", ""),
    ("too_big_for_the_bed", ""),
    ("too_big_for_the_bed", "bambu_a1"),
])
def test_the_row_and_the_print_door_agree(part, printer, request, preflight, monkeypatch):
    """For each shape, the row passes exactly when the door would print it."""
    import kiln.server as server

    monkeypatch.setattr(server, "_PRINTER_MODEL", printer)
    path = request.getfixturevalue(part)
    row = _row(preflight(file_path=path))
    door = _door(path)
    assert row is not None and row.get("checked", True) is True, row
    assert row["passed"] is (door.reason is None), (part, printer, row, door.reason)


def test_a_check_that_could_not_run_fails_nothing(block, preflight):
    """Kiln's own check breaking is not a verdict on the part: listed as not
    checked, it neither fails the pre-flight nor claims the part prints."""
    with patch(
        "kiln.plugins.validation_pipeline_tools.run_full_validation_pipeline",
        side_effect=RuntimeError("boom"),
    ):
        row = _row(preflight(file_path=_as_3mf(block)))
    assert row["passed"] is True and row["checked"] is False, row
    assert "printability_verdict" not in row, row


def test_only_a_model_is_judged(cone_on_its_tip, tmp_path, preflight):
    """A G-code file is already sliced: there is no part left to judge."""
    gcode = tmp_path / "part.gcode"
    gcode.write_text(";FLAVOR:Marlin\nM104 S200\nM140 S60\nG28\nG1 X10 Y10 E1\n")
    assert _row(preflight(file_path=str(gcode))) is None
    assert _row(preflight(file_path=cone_on_its_tip)) is not None
