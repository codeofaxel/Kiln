"""The shared places a real export's reply is decided, each pinned on its own.

``test_real_export_corpus.py`` runs every geometry door on files Blender and
Fusion exported and fails a door that gets one wrong.  These pin the shared
code those doors pass through, so a regression names the chokepoint rather
than forty doors at once:

- the error envelope reads what was raised: a caller's missing or unreadable
  file is a refusal, a network fault keeps its retry;
- the mesh readers say the units question, and the dispatcher puts it in the
  reply once, at a door that never asks;
- an edit of a non-STL file is written beside it as STL, and the reply says so;
- a slope at the overhang limit counts the same wherever the part sits;
- a STEP assembly's bodies are its solids.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("trimesh")

from tests import _real_exports as rx  # noqa: E402


def _envelope(exc: BaseException, code: str) -> dict:
    from kiln.server import _error_dict

    try:
        raise exc
    except BaseException:  # noqa: BLE001 - the envelope reads what is in flight
        return _error_dict(f"Unexpected error in a_door: {exc}", code=code)["error"]


class TestTheEnvelopeReadsTheCause:
    def test_a_missing_file_is_a_refusal_not_an_internal_error(self):
        err = _envelope(FileNotFoundError("no such file"), "INTERNAL_ERROR")
        assert err["code"] == "FILE_NOT_FOUND"
        assert err["retryable"] is False
        assert not err["message"].startswith("Unexpected error")

    def test_a_file_that_is_not_a_model_is_not_retried(self):
        from kiln.mesh_errors import UnreadableMeshError

        assert _envelope(ValueError("Failed to parse STL"), "ERROR")["retryable"] is False
        err = _envelope(UnreadableMeshError("not a model"), "INTERNAL_ERROR")
        assert (err["code"], err["retryable"]) == ("UNREADABLE_INPUT", False)

    def test_a_network_fault_keeps_its_retry(self):
        from kiln.printers.base import PrinterError

        assert _envelope(ConnectionError("refused"), "ERROR")["retryable"] is True
        wrapped = PrinterError("printer did not answer", cause=TimeoutError("timed out"))
        assert _envelope(wrapped, "ERROR")["retryable"] is True

    def test_a_cause_nobody_named_keeps_the_codes_answer(self):
        from kiln.server import _error_dict

        assert _error_dict("timeout")["error"]["retryable"] is True
        assert _envelope(RuntimeError("boom"), "INTERNAL_ERROR")["code"] == "INTERNAL_ERROR"


@pytest.fixture(scope="module")
def registry():
    from kiln import server

    return server.mcp


def _call(registry, tool: str, **args) -> dict:
    import kiln.terms

    accepted = kiln.terms.is_current
    kiln.terms.is_current = lambda *a, **k: True
    try:
        with rx.without_kiln_pro():
            return rx.call(registry, tool, args)
    finally:
        kiln.terms.is_current = accepted


class TestTheReadersSayWhatTheyRead:
    def test_a_metres_file_carries_the_units_question_once(self, registry, tmp_path):
        staged = rx.stage("donut_m.stl", tmp_path / "in")
        reply = _call(registry, "mirror_mesh_model", file_path=str(staged),
                      output_path=str(tmp_path / "out.stl"))
        assert reply["success"] is True
        said = reply["units_question"]
        assert "donut_m.stl" in said and "meters" in said
        assert rx.all_text(reply).count("0.088mm") == 1

    def test_a_millimetre_file_carries_none(self, registry, tmp_path):
        staged = rx.stage("donut_mm.stl", tmp_path / "in")
        reply = _call(registry, "mirror_mesh_model", file_path=str(staged),
                      output_path=str(tmp_path / "out.stl"))
        assert reply["success"] is True
        assert "units_question" not in reply

    def test_a_door_that_says_it_already_is_not_told_twice(self, registry, tmp_path):
        staged = rx.stage("donut_m.stl", tmp_path / "in")
        reply = _call(registry, "analyze_printability", file_path=str(staged))
        assert "0.088mm at its largest" in reply["message"]
        assert "As written, donut_m.stl" not in rx.all_text(reply)

    def test_an_edit_of_an_obj_is_written_beside_it_as_stl(self, registry, tmp_path):
        staged = rx.stage("donut.obj", tmp_path / "in")
        before = staged.read_bytes()
        reply = _call(registry, "mirror_mesh_model", file_path=str(staged))
        assert reply["success"] is True
        written = Path(reply["path"])
        assert written.suffix == ".stl" and written.read_bytes().startswith(b"Created by Kiln")
        assert staged.read_bytes() == before, "the person's own file is left as it was"
        assert "Written as STL" in reply["written_as"] and "OBJ" in reply["written_as"]


class TestWhereThePartSitsDoesNotMoveTheCount:
    def test_a_slope_at_the_limit_counts_the_same_below_and_on_the_plate(self, tmp_path):
        """The knob's chamfer is drawn at 45 degrees and stored in 32-bit
        floats, so its faces read a few hundred-thousandths of a degree
        either side of 45, differently at each height."""
        from kiln.printability import analyze_printability

        counts = {
            rel: analyze_printability(
                str(rx.stage(rel, tmp_path / rel)), max_overhang_angle=45.0,
            ).overhangs.overhang_triangle_count
            for rel in ("knob_below_bed_mm.stl", "knob_on_bed_mm.stl")
        }
        assert len(set(counts.values())) == 1, counts
        assert min(counts.values()) > 0, "the chamfer is at the limit, so it counts"


class TestAStepAssemblyIsItsSolids:
    def test_three_solids_under_an_assembly_product_are_three_bodies(self, tmp_path):
        from kiln.step_import import get_step_metadata

        meta = get_step_metadata(str(rx.stage("assembly_3_bodies.step", tmp_path)))
        assert len(meta["products"]) == 4  # the assembly and its three parts
        assert meta["estimated_body_count"] == 3
