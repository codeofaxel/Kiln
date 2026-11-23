"""A model larger than Kiln's servers keep is said here, never sent up.

Kiln's servers keep a model of up to 50 MB.  Every door on this computer
that sends a model to them reads one number for that
(:data:`kiln.served_makes.MAX_MODEL_BYTES`): a served tool's model, the
turn-it-over link, and the model kept beside a print for its twin.  A model
just past the limit is refused or skipped here, with nothing uploaded,
because the servers would only take the whole file and then refuse it.

The 50 MB is stated below a second time on purpose: it is the servers'
number, and a test that read it back from the constant could not notice the
constant moving away from it.
"""

from __future__ import annotations

import pytest

from kiln import monitor_twin, served_makes, stage_link
from tests.test_monitor_twin import twin_dir  # noqa: F401 -- fixture
from tests.test_served_makes import this_computer, wire  # noqa: F401 -- fixtures
from tests.test_stage_link import _reset, _wire

#: The largest model Kiln's servers keep, as they state it.
SERVERS_KEEP = 50 * 1024 * 1024


def _just_past(path):
    """A model one megabyte past what the servers keep: a binary STL header,
    and the rest of the file left empty, so it costs no disk to make."""
    with open(path, "wb") as fh:
        fh.write(b"\x00" * 80 + (1).to_bytes(4, "little"))
        fh.truncate(SERVERS_KEEP + 1024 * 1024)
    return path


def test_every_door_reads_the_one_number():
    assert served_makes.MAX_MODEL_BYTES == SERVERS_KEEP
    assert stage_link._MAX_UPLOAD_BYTES == served_makes.MAX_MODEL_BYTES
    assert monitor_twin._MAX_MESH_BYTES == served_makes.MAX_MODEL_BYTES


def test_a_served_tools_model_is_refused_here_with_nothing_uploaded(wire, tmp_path):  # noqa: F811
    calls, _ = wire
    model = _just_past(tmp_path / "statue.stl")

    _kwargs, refusal = served_makes.send_inputs(
        "apply_procedural_texture", {"input_path": str(model)}, {"mesh": "input_path"},
    )

    assert refusal is not None and refusal["code"] == "UPLOAD_TOO_LARGE", refusal
    assert "over 50 MB" in refusal["error"], refusal["error"]
    assert calls == [], "a model past what the servers keep was sent up"


def test_the_turn_it_over_link_is_not_attempted(tmp_path, monkeypatch):
    _reset()
    monkeypatch.delenv(stage_link._OPT_OUT_ENV, raising=False)
    calls = _wire(monkeypatch)
    model = _just_past(tmp_path / "statue.stl")

    assert stage_link.stage_link_for(str(model)) is None
    assert calls == [], "started an upload the servers would refuse"
    _reset()


def test_the_twin_does_not_keep_a_model_it_could_only_send_to_be_refused(twin_dir, tmp_path):  # noqa: F811
    mesh = _just_past(tmp_path / "part.stl")
    gcode = tmp_path / "part.gcode"
    gcode.write_text("; sliced by test\nG28\nG1 X10 Y10 E1\n")

    monitor_twin.note_sliced(str(mesh), str(gcode))
    monitor_twin.note_print_started("p1", "part.gcode")

    kept = monitor_twin.active_twin("p1")
    assert kept is not None and kept["gcode"] is not None
    assert kept["mesh"] is None


@pytest.mark.parametrize("size", [SERVERS_KEEP - 1024 * 1024])
def test_a_model_inside_the_limit_still_goes_up(wire, tmp_path, size):  # noqa: F811
    calls, answers = wire
    from tests.test_served_makes import _Response

    answers[("POST", "/api/view/mesh")] = _Response(
        body={"status": "success", "artifact_token": "UPLOADED-token-0123456789"}
    )
    model = tmp_path / "statue.stl"
    with open(model, "wb") as fh:
        fh.write(b"\x00" * 80 + (1).to_bytes(4, "little"))
        fh.truncate(size)

    kwargs, refusal = served_makes.send_inputs(
        "apply_procedural_texture", {"input_path": str(model)}, {"mesh": "input_path"},
    )

    assert refusal is None, refusal
    assert kwargs["source_artifact_token"] == "UPLOADED-token-0123456789"
    assert [path for _method, path, _kw in calls] == ["/api/view/mesh"]
