"""The camera is a source on the plate record, not an afterthought.

Kiln could always see the bed -- a printer's own camera, or one a person
registered against any printer at all (``camera_snapshot_url``) -- and the
plate record ignored all of it, so a machine Kiln could have LOOKED at was
told to go and look by hand.  These tests pin the look as a first-class
source:

* ``camera_of`` / ``look`` say whether this machine can answer the question
  and hand over a frame for eyes that can see it, judging nothing;
* a frame that cannot settle anything -- no camera, no answer, a capped
  lens -- is a refusal with the reason, never a guess;
* ``mark_from_camera`` records what was seen WITH who looked, and refuses
  an answer it cannot attribute;
* the asymmetry: "occupied" is written over a ``clear`` record and keeps
  the parts already named; "clear" is written as a camera-sourced record;
* ``look_at_plate`` is the two-step door -- a frame, then the answer -- and
  an agent's answer is recorded as an agent's;
* a refusal about an unknown plate offers the camera when there is one and
  does not when there is not.

Nothing here runs a vision model: Kiln ships none, and the point of the
design is that the looking is done by eyes and the record says whose.
"""

from __future__ import annotations

import base64
import struct
import zlib
from types import SimpleNamespace

import pytest

from kiln.plate_state import (
    CAMERA_SOURCE_PREFIX,
    PlateJob,
    camera_could_settle,
    camera_of,
    look,
    mark_clear,
    mark_from_camera,
    mark_occupied,
    read,
)


def _png(width: int = 640, height: int = 480, grey: int = 128) -> bytes:
    """A real PNG the snapshot screen will accept: big enough, mid-bright,
    and with enough variance not to read as a blank frame."""
    rows = b""
    for y in range(height):
        row = b"\x00"
        for x in range(width):
            v = (grey + ((x * 7 + y * 13) % 90)) % 256
            row += bytes((v, v, v))
        rows += row
    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


def _machine(name="cam", *, camera="printer", frame=b"", raises=False):
    """A printer with a serial (the record's key) and a camera that answers."""
    def get_snapshot():
        if raises:
            raise RuntimeError("camera timed out")
        return frame

    return SimpleNamespace(
        name=name, serial=f"01P00A00000{name}", _printer_model="bambu_a1",
        snapshot_source=lambda: camera, get_snapshot=get_snapshot,
    )


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("KILN_HOME", str(tmp_path / "kiln-home"))


# ---------------------------------------------------------------------------
# 1. Whether this machine can answer at all
# ---------------------------------------------------------------------------


class TestWhetherKilnCanLook:
    def test_a_printer_camera_and_a_registered_one_both_count(self):
        assert camera_of(_machine(camera="printer")) == "printer"
        assert camera_of(_machine(camera="user_supplied")) == "user_supplied"

    def test_a_machine_with_no_camera_says_so_rather_than_raising(self):
        assert camera_of(_machine(camera=None)) is None
        assert camera_of(SimpleNamespace()) is None

    def test_the_refusal_clause_offers_the_camera_only_when_there_is_one(self):
        assert camera_could_settle(_machine(camera=None)) is None
        own = camera_could_settle(_machine(camera="printer"))
        assert own is not None and "this printer's own camera" in own
        mine = camera_could_settle(_machine(camera="user_supplied"))
        assert mine is not None and "the camera you registered" in mine


class TestTheLook:
    def test_a_usable_frame_comes_back_for_eyes_to_judge(self):
        frame = _png()
        found = look(_machine(frame=frame))
        assert found.available is True and found.possible is True
        assert found.camera == "printer" and found.media_type == "image/png"
        assert base64.b64decode(found.image_b64) == frame

    def test_the_frame_is_not_in_the_dict_that_rides_answers_and_logs(self):
        found = look(_machine(frame=_png()))
        assert "image_b64" not in found.to_dict()
        assert set(found.to_dict()) == {"available", "possible", "camera", "media_type", "why"}

    def test_no_camera_is_a_reason_not_a_guess(self):
        found = look(_machine(camera=None))
        assert found.available is False and found.possible is False
        assert "no camera" in found.why

    def test_a_camera_that_does_not_answer_says_which_way_it_failed(self):
        assert "did not answer" in look(_machine(raises=True)).why
        assert "no image" in look(_machine(frame=b"")).why

    def test_a_frame_that_cannot_settle_anything_is_refused_with_the_reason(self):
        """A capped lens is not an empty plate.  The screen that already
        exists for print monitoring is what tells the two apart."""
        found = look(_machine(frame=_png(width=16, height=16)))
        assert found.available is False
        assert found.possible is True, "the camera is there; it is the picture that is unusable"
        assert found.why

    def test_looking_never_writes_the_record(self):
        machine = _machine(frame=_png())
        look(machine)
        assert read(machine).status == "unknown"


# ---------------------------------------------------------------------------
# 2. Recording what was seen
# ---------------------------------------------------------------------------


class TestRecordingWhatWasSeen:
    def test_an_empty_plate_is_recorded_clear_with_who_looked(self):
        machine = _machine()
        assert mark_from_camera(machine, seen="clear", judged_by="agent") == "clear"
        state = read(machine)
        assert state.clear and state.from_camera and state.looked_by == "agent"
        assert state.source == f"{CAMERA_SOURCE_PREFIX}agent"
        assert state.to_dict()["from_camera"] is True and state.to_dict()["looked_by"] == "agent"

    def test_a_person_looking_is_recorded_as_a_person_not_an_agent(self):
        machine = _machine()
        mark_from_camera(machine, seen="clear", judged_by="human")
        assert read(machine).looked_by == "human"

    def test_a_look_that_cannot_be_attributed_is_refused_not_written(self):
        machine = _machine()
        assert mark_from_camera(machine, seen="clear", judged_by="somebody") is None
        assert mark_from_camera(machine, seen="maybe", judged_by="agent") is None
        assert read(machine).status == "unknown", "a look nobody can be held to leaves no record"

    def test_a_record_written_by_hand_is_not_a_camera_record(self):
        machine = _machine()
        mark_clear(machine, "human")
        state = read(machine)
        assert state.clear and state.from_camera is False and state.looked_by is None

    def test_the_description_says_a_camera_settled_it(self):
        machine = _machine()
        mark_from_camera(machine, seen="clear", judged_by="agent")
        assert "through the camera" in read(machine).describe()
        mark_from_camera(machine, seen="clear", judged_by="human")
        assert "by a person" in read(machine).describe()


class TestTheAsymmetry:
    def test_seeing_a_part_overrides_a_clear_record(self):
        """The direction that can only ever stop a motion.  A stale `clear`
        is exactly what a look exists to catch."""
        machine = _machine()
        mark_clear(machine, "human")
        assert read(machine).clear
        assert mark_from_camera(machine, seen="occupied", judged_by="agent") == "occupied"
        assert read(machine).occupied

    def test_seeing_a_part_keeps_the_parts_already_named(self):
        """A look cannot say which part is there or how tall it is, so it
        confirms the record's parts rather than blanking them."""
        machine = _machine()
        mark_occupied(machine, PlateJob(file="jar_v2.gcode", footprint_mm=[10, 10, 50, 50], max_z_mm=42.0))
        mark_from_camera(machine, seen="occupied", judged_by="agent")
        state = read(machine)
        assert state.occupied and state.job is not None
        assert state.job.file == "jar_v2.gcode" and state.tallest_mm == 42.0
        assert state.looked_by == "agent"

    def test_an_unknown_plate_seen_empty_becomes_routable(self):
        """The whole point: a machine nobody had confirmed is now confirmed
        without anyone walking to it."""
        machine = _machine()
        assert read(machine).status == "unknown"
        mark_from_camera(machine, seen="clear", judged_by="agent")
        assert read(machine).clear


# ---------------------------------------------------------------------------
# 3. The door
# ---------------------------------------------------------------------------


class _FakeMCP:
    def __init__(self):
        self.tools = {}

    def tool(self, *a, **k):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


@pytest.fixture
def door(monkeypatch):
    import kiln.server as srv
    from kiln.plugins import homing_tools

    monkeypatch.setattr(srv, "_check_auth", lambda scope: None)

    def make(machine):
        monkeypatch.setattr(srv, "_resolve_control_target", lambda name=None: (machine, machine.name))
        mcp = _FakeMCP()
        homing_tools._HomingToolsPlugin().register(mcp)
        return mcp.tools

    return make


class TestTheDoor:
    def test_the_door_exists_and_is_registered(self, door):
        assert "look_at_plate" in door(_machine(frame=_png()))

    def test_called_without_an_answer_it_hands_over_the_picture_and_records_nothing(self, door):
        machine = _machine(frame=_png())
        result = door(machine)["look_at_plate"]()
        assert result["success"] is True
        assert base64.b64decode(result["image_b64"]) == _png()
        assert result["look"]["camera"] == "printer"
        assert "seen=" in result["next"]
        assert read(machine).status == "unknown", "handing over a picture is not an answer"

    def test_the_answer_lands_on_the_record_as_an_agents_look(self, door):
        machine = _machine(frame=_png())
        result = door(machine)["look_at_plate"](seen="clear")
        assert result["success"] is True
        assert result["plate"]["status"] == "clear"
        assert result["plate"]["looked_by"] == "agent"
        assert read(machine).clear

    def test_seeing_something_is_recorded_too(self, door):
        machine = _machine(frame=_png())
        assert door(machine)["look_at_plate"](seen="occupied")["plate"]["status"] == "occupied"

    def test_a_machine_with_no_camera_is_told_so_and_stays_unknown(self, door):
        machine = _machine(camera=None)
        result = door(machine)["look_at_plate"]()
        assert result["success"] is False
        assert result["error"]["code"] == "PLATE_LOOK_UNAVAILABLE"
        assert read(machine).status == "unknown"

    def test_a_nonsense_answer_is_refused(self, door):
        machine = _machine(frame=_png())
        result = door(machine)["look_at_plate"](seen="probably")
        assert result["success"] is False and result["error"]["code"] == "INVALID_INPUT"
        assert read(machine).status == "unknown"

    def test_plate_status_says_whether_a_camera_could_settle_it(self, door):
        assert door(_machine(frame=_png()))["plate_status"]()["camera"] == "printer"
        assert door(_machine(camera=None))["plate_status"]()["camera"] is None
