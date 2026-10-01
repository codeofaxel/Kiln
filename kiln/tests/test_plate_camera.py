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
* a refusal about a recorded part LOOKS instead of assuming: it carries how
  old the record is and, where there is a camera, a frame of the plate and
  the one call that records what the frame shows -- and with no camera it
  says so and names the person's own doors.

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
    LIKELY_GONE_AFTER_HOURS,
    START_NOT_YET_CODE,
    PlateJob,
    camera_could_settle,
    camera_of,
    look,
    mark_clear,
    mark_from_camera,
    mark_occupied,
    offer_look,
    read,
    start_refusal,
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

    # ``snapshot_source`` is a value, as it is on a real adapter (a property
    # there) -- a stand-in that hands over a function is how a look that
    # worked on no real printer stayed green.
    return SimpleNamespace(
        name=name, serial=f"01P00A00000{name}", _printer_model="bambu_a1",
        snapshot_source=camera, get_snapshot=get_snapshot,
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
        assert camera_of(SimpleNamespace(snapshot_source=lambda: "printer")) is None, "not how an adapter states it"

    @pytest.mark.parametrize("backend", ["bambu", "octoprint", "moonraker"])
    def test_a_real_adapter_with_a_camera_reads_as_having_one(self, backend, monkeypatch):
        """The adapter class itself, not a stand-in: every one of these has
        ``can_snapshot`` and read as camera-less from 2026-09-22 to
        2026-10-01."""
        from kiln.printers.base import ExternalCamera

        from .test_filament_handling import _build

        adapter = _build(backend)
        assert adapter.capabilities.can_snapshot
        assert camera_of(adapter) == "printer"
        monkeypatch.setattr(adapter, "get_snapshot", lambda: _png())
        found = look(adapter)
        assert found.available and found.camera == "printer"
        adapter._external_camera = ExternalCamera(snapshot_url="http://cam.local/snap.jpg", stream_url=None)
        assert camera_of(adapter) == "user_supplied"

    def test_the_refusal_clause_offers_the_camera_only_when_there_is_one(self):
        assert camera_could_settle(_machine(camera=None)) is None
        own = camera_could_settle(_machine(camera="printer"))
        assert own is not None and "this printer's own camera" in own
        mine = camera_could_settle(_machine(camera="user_supplied"))
        assert mine is not None and "the camera you registered" in mine

    def test_a_camera_that_answers_with_something_that_is_not_a_picture_is_no_look(self):
        found = look(_machine(frame="not bytes"))
        assert found.available is False and "no image" in found.why


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
# 2b. A refusal looks instead of assuming
# ---------------------------------------------------------------------------


def _recorded(machine, monkeypatch, *, hours_ago: float, file: str = "cube.gcode.3mf") -> None:
    """A part recorded *hours_ago*, the way a print seen ending records it."""
    from datetime import datetime, timedelta

    from kiln import plate_state

    then = (datetime.now().astimezone() - timedelta(hours=hours_ago)).isoformat(timespec="seconds")
    with monkeypatch.context() as patched:
        patched.setattr(plate_state, "_now_iso", lambda: then)
        mark_occupied(machine, PlateJob(file=file, max_z_mm=20.0), source="print_ended")


class TestARefusalLooksInsteadOfAssuming:
    """2026-10-01, a live demo: the record said a cube was on the plate, from
    a print days earlier.  The plate was empty and the printer has a camera.
    The refusal said "clear the plate and say so", named no camera and no
    tool, and the agent took the record's word for it.
    """

    def test_the_demo_a_stale_record_a_camera_and_an_empty_plate(self, monkeypatch):
        machine = _machine(frame=_png())
        _recorded(machine, monkeypatch, hours_ago=3 * 24 + 2)
        block = start_refusal(machine, file_name="base.gcode.3mf")
        assert block is not None and block["error"]["code"] == START_NOT_YET_CODE
        message = block["error"]["message"]
        assert message.startswith("The last print, cube, is still on the plate (since ")
        assert "3 days ago" in message and "most likely been taken off" in message
        # The frame is on disk, named in the sentence, with the call that records it.
        path = block["snapshot_path"]
        assert path and path in message
        with open(path, "rb") as fh:
            assert fh.read() == _png()
        assert 'look_at_plate with seen="clear"' in message
        assert "(or call look_at_plate to see it)" in message, "a host that cannot open the file is told how to see it"
        assert block["look"]["settle_with"] == "look_at_plate" and block["look"]["likely_gone"] is True
        assert block["look"]["recorded_ago"] == "3 days ago" and block["look"]["camera"] == "printer"
        assert block["plate"]["recorded_ago"] == "3 days ago"
        assert "image_b64" not in str(block), "the frame rides as a path, never as base64 in an answer"
        # Offering a look writes nothing: the record changes when eyes answer.
        assert read(machine).occupied
        mark_from_camera(machine, seen="clear", judged_by="agent")
        assert start_refusal(machine, file_name="base.gcode.3mf") is None

    def test_age_alone_never_clears_the_record(self, monkeypatch):
        machine = _machine(camera=None)
        _recorded(machine, monkeypatch, hours_ago=90 * 24)
        block = start_refusal(machine)
        assert block is not None and read(machine).occupied
        assert "90 days ago" in block["error"]["message"]

    def test_a_fresh_record_is_not_called_stale(self, monkeypatch):
        machine = _machine(frame=_png())
        _recorded(machine, monkeypatch, hours_ago=0.5)
        block = start_refusal(machine)
        assert "30 minutes ago" in block["error"]["message"]
        assert "most likely" not in block["error"]["message"] and block["look"]["likely_gone"] is False

    def test_the_line_between_fresh_and_most_likely_gone(self, monkeypatch):
        machine = _machine(camera=None)
        _recorded(machine, monkeypatch, hours_ago=LIKELY_GONE_AFTER_HOURS - 1)
        assert offer_look(machine).likely_gone is False
        _recorded(machine, monkeypatch, hours_ago=LIKELY_GONE_AFTER_HOURS + 1)
        assert offer_look(machine).likely_gone is True

    def test_no_camera_says_so_and_names_the_persons_own_doors(self, monkeypatch):
        machine = _machine(camera=None)
        _recorded(machine, monkeypatch, hours_ago=5)
        block = start_refusal(machine)
        message = block["error"]["message"]
        assert "5 hours ago" in message and "no camera Kiln can read" in message
        assert "`kiln plate clear`" in message and "look_at_plate" not in message
        assert block["snapshot_path"] is None and block["look"]["settle_with"] is None
        assert block["look"]["possible"] is False

    def test_a_camera_that_cannot_settle_it_says_why_and_falls_back_to_the_person(self, monkeypatch):
        for machine, why in ((_machine(raises=True), "did not answer"), (_machine(frame=b""), "no image")):
            _recorded(machine, monkeypatch, hours_ago=5)
            block = start_refusal(machine)
            message = block["error"]["message"]
            assert "This printer's own camera could settle it, but" in message and why in message
            assert "`kiln plate clear`" in message and "look_at_plate" not in message
            assert block["snapshot_path"] is None and block["look"]["possible"] is True

    def test_a_registered_camera_is_named_as_the_persons(self, monkeypatch):
        machine = _machine(camera="user_supplied", frame=_png())
        _recorded(machine, monkeypatch, hours_ago=5)
        assert "the camera you registered for it" in start_refusal(machine)["error"]["message"]

    def test_the_offer_never_raises_and_never_blocks_the_refusal(self, monkeypatch):
        from unittest import mock

        machine = _machine(frame=_png())
        _recorded(machine, monkeypatch, hours_ago=5)
        with mock.patch("kiln.plate_state.look", side_effect=RuntimeError("camera stack fell over")):
            block = start_refusal(machine)
        assert block is not None and block["error"]["code"] == START_NOT_YET_CODE
        assert "`kiln plate clear`" in block["error"]["message"] and block["snapshot_path"] is None


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


def _image_block(item) -> dict:
    """An image content block (or an ``Image`` that becomes one) by its wire names."""
    block = item.to_image_content() if hasattr(item, "to_image_content") else item
    return block.model_dump(by_alias=True, mode="json")


class TestTheDoor:
    def test_the_door_exists_and_is_registered(self, door):
        assert "look_at_plate" in door(_machine(frame=_png()))

    def test_called_without_an_answer_it_hands_over_the_picture_and_records_nothing(self, door):
        machine = _machine(frame=_png())
        answer, picture = door(machine)["look_at_plate"]()
        assert answer["success"] is True
        assert "image_b64" not in answer, "the frame rides as a picture, never as text"
        assert _image_block(picture)["data"] == base64.b64encode(_png()).decode()
        with open(answer["snapshot_path"], "rb") as fh:
            assert fh.read() == _png(), "the same picture, where eyes with a file reader can open it"
        assert answer["look"]["camera"] == "printer"
        assert "seen=" in answer["next"]
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


class TestThePictureReachesTheModel:
    """Through a real MCP server, as a host's ``tools/call`` reaches it.

    2026-10-01: ``look_at_plate`` handed the frame back as base64 inside its
    JSON.  A model whose host cannot open files saw a path it could not use
    and a wall of characters it could not read as a picture -- and a real
    frame is ~220,000 of them (a 167 KB A1 frame), past the size at which a
    host that reads results as text refused the monitor's whole result on
    2026-09-16, the record with it.  The picture now travels as an image
    block, and the text that rides beside it stays small.
    """

    @pytest.fixture
    def server(self, monkeypatch):
        import kiln.server as srv
        from kiln.mcp_compat import FastMCP
        from kiln.plugins import homing_tools

        monkeypatch.setattr(srv, "_check_auth", lambda scope: None)

        def make(machine):
            monkeypatch.setattr(srv, "_resolve_control_target", lambda name=None: (machine, machine.name))
            mcp = FastMCP("plate-look")
            homing_tools._HomingToolsPlugin().register(mcp)
            return mcp

        return make

    @staticmethod
    def _call(mcp, **arguments):
        import asyncio

        from kiln.mcp_compat import result_structured_content, tool_result_blocks

        result = asyncio.run(mcp.call_tool("look_at_plate", arguments))
        structured = result[1] if isinstance(result, tuple) else result_structured_content(result)
        blocks = list(tool_result_blocks(result))
        images = [_image_block(b) for b in blocks if getattr(b, "type", None) == "image"]
        text = "".join(b.text for b in blocks if getattr(b, "type", None) == "text")
        return images, text, structured

    def test_the_picture_arrives_as_an_image_and_the_text_stays_small(self, server):
        machine = _machine(frame=_png())
        images, text, structured = self._call(server(machine))
        assert len(images) == 1, "the model is handed the picture itself"
        assert images[0]["mimeType"] == "image/png"
        assert base64.b64decode(images[0]["data"]) == _png()
        assert "snapshot_path" in text and '"success": true' in text
        assert images[0]["data"] not in text and "image_b64" not in text
        assert len(text) < 5_000, "the record travels as text; the picture never does"
        assert structured is None, "no structured copy for a host to read back as text"
        assert read(machine).status == "unknown", "handing over a picture is still not an answer"

    def test_the_tool_publishes_no_schema_a_picture_would_break(self, server):
        import asyncio

        from kiln.mcp_compat import tool_input_schema

        tools = {t.name: t for t in asyncio.run(server(_machine(frame=_png())).list_tools())}
        tool = tools["look_at_plate"]
        assert getattr(tool, "outputSchema", getattr(tool, "output_schema", None)) is None
        assert tool_input_schema(tool)["properties"].keys() >= {"printer_name", "seen"}

    def test_an_answer_and_a_machine_with_no_camera_carry_no_picture(self, server):
        machine = _machine(frame=_png())
        images, text, _ = self._call(server(machine), seen="clear")
        assert images == [] and '"status": "clear"' in text
        images, text, _ = self._call(server(_machine(name="dark", camera=None)))
        assert images == [] and "PLATE_LOOK_UNAVAILABLE" in text
