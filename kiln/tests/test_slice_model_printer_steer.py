"""slice_model steers to the file the TARGET printer can actually print.

THE INCIDENT (2026-08-17)
-------------------------
``slice_model(printer_id="bambu_a1")`` produced two artifacts — a raw
``.gcode`` and the wrapped ``.gcode.3mf`` — and its human-readable
message named the ``.gcode``.  That file has no start block, so on a
Bambu it homes nothing and drives straight into an extruding move.  A
caller holding both files picked the one the message named, and the
homing gate refused the upload three steps later.

The gate did its job.  But a tool that was TOLD the printer should not
route a caller into a file that printer cannot use and rely on a net
downstream: validation-at-the-end is a safety net, steering-at-the-start
is the product.

WHAT THIS PINS
--------------
That the response says WHICH file to upload, and that the prose agrees
with the field.  ``output_path`` already pointed at the 3MF before this
change — the message did not, and prose is what an agent reads.
"""

from __future__ import annotations

import os

import pytest


def _fake_slice_response(threemf: str, gcode: str) -> dict:
    """The response shape slice_model builds after a successful Bambu wrap."""
    return {
        "success": True,
        "message": f"Sliced part.stl -> {os.path.basename(gcode)}",
        "output_path": threemf,
        "output_3mf_path": threemf,
        "raw_gcode_path": gcode,
        "printer_id": "bambu_a1",
    }


def test_the_steer_names_the_wrapped_file_not_the_raw_gcode(tmp_path):
    """The recommendation must be the 3MF — the only startable file.

    Calls the REAL production helper, not a mirror of it: an earlier
    version of this test rebuilt the steer by hand on a fake dict, so
    deleting the block from slicer_tools left every test here green.
    """
    from kiln.plugins.slicer_tools import _steer_to_wrapped_upload

    threemf = str(tmp_path / "part.gcode.3mf")
    gcode = str(tmp_path / "part.gcode")

    resp = _fake_slice_response(threemf, gcode)
    _steer_to_wrapped_upload(resp, threemf, "bambu_a1")

    assert resp["recommended_upload_path"] == threemf
    assert resp["recommended_upload_path"] != resp["raw_gcode_path"]
    assert "bambu_a1" in resp["recommended_upload_reason"]
    assert "raw_gcode_note" in resp


def test_the_message_agrees_with_the_recommended_file():
    """Prose and field must not disagree — the prose is what gets read.

    Before the fix the message named the raw gcode while output_path
    named the 3MF.  Both were 'correct'; together they misdirected.
    """
    from kiln.plugins.slicer_tools import _steer_to_wrapped_upload

    threemf = "/tmp/part.gcode.3mf"
    resp = _fake_slice_response(threemf, "/tmp/part.gcode")
    _steer_to_wrapped_upload(resp, threemf, None)

    assert os.path.basename(threemf) in resp["message"], (
        "the message must name the file the caller should actually upload"
    )
    assert resp["message"].endswith(f"Upload {os.path.basename(threemf)}."), (
        "the steer must be the LAST sentence — the prose before it still "
        "names the raw gcode (built in kiln.slicer before the wrap exists)"
    )


def test_upload_file_docstring_no_longer_claims_gcode_only():
    """The docstring is the agent's contract; it must not be narrower
    than the function.

    ``upload_file``'s own bed-fit gate handles ``.3mf`` and
    ``.gcode.3mf``, and the Bambu adapter declares ``.3mf`` first — but
    the docstring said the extension had to be .gcode/.gco/.g, which is
    what sent a caller to the unprintable file.
    """
    from kiln import server

    doc = server.upload_file.__doc__ or ""
    assert ".3mf" in doc, (
        "upload_file's docstring must acknowledge .3mf — it is the ONLY "
        "format a Bambu starts a print from, and the docstring is what an "
        "agent reads before choosing a file"
    )
    assert "recommended_upload_path" in doc, (
        "the docstring should point callers at slice_model's steer instead "
        "of letting them choose between its outputs by hand"
    )


def test_bambu_adapter_still_declares_3mf_first():
    """The steer is derived from this, so it must not silently reorder."""
    from kiln.printers.bambu import BambuAdapter  # noqa: F401

    import inspect

    src = inspect.getsource(
        __import__("kiln.printers.bambu", fromlist=["bambu"])
    )
    assert 'supported_extensions=(".3mf"' in src, (
        "Bambu must declare .3mf first — the recommendation and the "
        "docstring both rest on that ordering being meaningful"
    )
