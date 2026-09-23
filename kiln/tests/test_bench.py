"""Help Kiln get to know your printer: the guided session and what it produces.

The record Kiln judges plate clearance from has blanks for many models --
a pause or cancel nobody has described, a head nobody has measured -- and
a blank is judged at the worst case.  ``printer_bench`` fills them for one
unit: it prints a coin-sized square, pauses, resumes, cancels and ends it,
and watches where the head goes; a Klipper or Marlin printer says where
its head is, a closed-firmware printer's owner answers with a zone number
and a lift word.  What it produces is an observation per block, numbers
only, kept on this machine, carried by every placement request for the
unit, and sent to Kiln under the one telemetry switch.

Pinned here: the zone drawing's numbers; the observation shape and its
fingerprint; where the documents live and how they ride a request (with
nothing that names the unit when telemetry is off); what the record
lacks, read from the verdict; the two doors that offer the session and
the ledger that keeps anybody from being nagged twice; the position log's
readers; the send; and the conversation itself, walked end to end on a
closed-firmware printer and on one that logs its own position, dropped
and resumed in the middle.
"""

from __future__ import annotations

import base64
import json
import sys
from types import SimpleNamespace

import pytest

from kiln import bench

BED = (256.0, 256.0)


class _Adapter:
    """A closed-firmware printer (a Bambu): no position, a camera, a serial."""

    name = "bambu"
    serial = "01P00A000000001"
    _printer_model = "bambu_a1"

    def __init__(self, *, camera: bool = True, position: bool = False, firmware: str | None = "01.04.00.00"):
        self._camera = camera
        self._position = position
        self._firmware = firmware
        self.state = "idle"
        self.layer: int | None = None
        self.completion: float | None = None
        self.head = [30.0, 30.0, 1.0]
        self.commands: list[str] = []

    def declared_printer_model(self):
        return self._printer_model

    def reported_firmware_version(self):
        return self._firmware

    def snapshot_source(self):
        return "printer" if self._camera else None

    def get_snapshot(self):
        return _png() if self._camera else None

    def get_status(self):
        return SimpleNamespace(state=SimpleNamespace(value=self.state)), SimpleNamespace(
            current_layer=self.layer, completion=self.completion, file_name="kiln_bench_square.gcode")

    def get_tool_position(self):
        return {"x": self.head[0], "y": self.head[1], "z": self.head[2]} if self._position else None


def _png(width: int = 640, height: int = 480) -> bytes:
    import struct
    import zlib

    raw = b"".join(b"\x00" + bytes([128, 128, 128] * width) for _ in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def _verdict(*, ok: bool, blanks: list[str] | None = None, sentences: list[str] = (), code: str = "PLACEMENT_HEAD_STRIKE") -> dict:
    return {
        "schema": "placement_verdict/1", "ok": ok, "placed_by": "agent",
        "refusals": [] if ok else [{"code": code, "sentence": s} for s in (sentences or ["refused"])],
        "record": None if code == "PLACEMENT_UNKNOWN_PRINTER" else {
            "printer_id": "bambu_a1", "measured": True, "source": "measured_at_machine", "quiet_start": True,
            "machine_blocks": {}, "bench_blocks": {}, "blanks": list(blanks or [])},
        "spots": [], "occupancy": None, "tier": {"verdict": "free", "plan": "pro"},
    }


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("KILN_HOME", str(tmp_path / "kiln-home"))
    monkeypatch.setenv("KILN_TELEMETRY", "true")
    for name in list(sys.modules):
        if name == "kiln_pro" or name.startswith("kiln_pro."):
            monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setitem(sys.modules, "kiln_pro", None)


# ---------------------------------------------------------------------------
# The zone drawing, as numbers
# ---------------------------------------------------------------------------


class TestTheZones:
    def test_nine_on_the_plate_like_a_keypad_and_sixteen_around_clockwise(self):
        c = BED[0] / 3
        assert bench.zone_rect(1, BED) == pytest.approx((0.0, 2 * c, c, 3 * c))          # back-left
        assert bench.zone_rect(5, BED) == pytest.approx((c, c, 2 * c, 2 * c))             # centre
        assert bench.zone_rect(9, BED) == pytest.approx((2 * c, 0.0, 3 * c, c))          # front-right
        assert bench.zone_rect(10, BED) == pytest.approx((-c, 3 * c, 0.0, 4 * c))         # back-left corner, off both edges
        assert bench.zone_rect(16, BED) == pytest.approx((3 * c, c, 4 * c, 2 * c))        # right of the plate, middle
        assert bench.zone_rect(20, BED) == pytest.approx((c, -c, 2 * c, 0.0))             # in front, middle
        assert bench.zone_rect(24, BED) == pytest.approx((-c, c, 0.0, 2 * c))             # LEFT of the plate: an A1's chute
        assert len({bench.zone_rect(z, BED) for z in range(1, 26)}) == 25
        for bad in (0, 26):
            with pytest.raises(ValueError):
                bench.zone_rect(bad, BED)

    def test_a_point_resolves_to_its_zone_and_the_a1_chute_is_zone_24(self):
        assert bench.zone_of(128.0, 128.0, BED) == 5
        assert bench.zone_of(-48.2, 128.0, BED) == 24
        assert bench.zone_of(-48.0, 180.0, BED) == 25
        assert bench.zone_of(1.0, 1.0, BED) == 7
        assert bench.zone_of(-200.0, 0.0, BED) is None

    def test_an_answer_is_read_for_its_zone_and_its_lift(self):
        assert bench.parse_zone("zone 24") == 24 and bench.parse_zone("24") == 24 and bench.parse_zone("it's in 7 I think") == 7
        assert bench.parse_zone("nowhere") is None and bench.parse_zone("99") is None
        assert bench.parse_lift("barely") == 0.5 and bench.parse_lift("a finger") == 10.0
        assert bench.parse_lift("a hand span") == 100.0 and bench.parse_lift("about 35 mm") == 35.0
        assert bench.parse_lift("2 cm") == 20.0 and bench.parse_lift("") is None
        assert bench.LIFT_WORDS_MM == {"barely": 0.5, "finger": 10.0, "hand": 100.0}

    def test_the_pictures_are_png(self):
        pytest.importorskip("PIL")
        zones = bench.draw_zones(BED)
        head = bench.draw_head_sketch()
        assert zones[:8] == b"\x89PNG\r\n\x1a\n" and head[:8] == b"\x89PNG\r\n\x1a\n"
        assert len(zones) > 2000 and len(head) > 1000


# ---------------------------------------------------------------------------
# The observation, where it lives, how it travels
# ---------------------------------------------------------------------------


class TestTheObservation:
    def test_the_document_is_numbers_only_in_the_served_shape(self):
        doc = bench.observation("bambu_a1", "pause", "owner_zone", bed_mm=BED, layer_z_mm=1.0, zones=[24], lift_mm=10.0,
                                returns=True, return_how="level", unit="ab" * 16, firmware="01.04.00.00")
        assert doc["format"] == bench.OBSERVATION_FORMAT and doc["block"] == "pause" and doc["how"] == "owner_zone"
        assert doc["zones"] == [24] and doc["lift_mm"] == 10.0 and doc["points"] is None and doc["returns"] is True
        assert doc["return_how"] == "level" and doc["bed_mm"] == [256.0, 256.0] and doc["when"]
        logged = bench.observation("k1", "cancel", "position_log", bed_mm=(220.0, 220.0), layer_z_mm=1.0,
                                   points=[(30.0, 30.0, 1.0), (30.0, 30.0, 11.0), (0.0, 220.0, 11.0)])
        assert logged["points"] == [[30.0, 30.0, 1.0], [30.0, 30.0, 11.0], [0.0, 220.0, 11.0]] and logged["zones"] is None
        head = bench.head_observation("bambu_a1", 60.0, 25.0, unit="ab" * 16)
        assert head["block"] == "head" and head["head_mm"] == {"width": 60.0, "rod_height": 25.0}
        assert not any(k in json.dumps(doc) for k in ("image", "photo", "frame"))
        with pytest.raises(ValueError):
            bench.observation("bambu_a1", "runout", "owner_zone", bed_mm=BED, layer_z_mm=1.0, zones=[1])
        with pytest.raises(ValueError):
            bench.observation("bambu_a1", "pause", "position_log", bed_mm=BED, layer_z_mm=1.0)

    def test_the_fingerprint_names_the_path_not_the_unit_or_the_moment(self):
        a = bench.observation("bambu_a1", "pause", "owner_zone", bed_mm=BED, layer_z_mm=1.0, zones=[24], lift_mm=10.0,
                              unit="ab" * 16, firmware="1", when="2026-09-23T10:00:00+00:00")
        b = bench.observation("bambu_a1", "pause", "owner_zone", bed_mm=BED, layer_z_mm=1.0, zones=[24], lift_mm=10.0,
                              unit="cd" * 16, firmware="2", when="2026-10-01T10:00:00+00:00")
        c = bench.observation("bambu_a1", "pause", "owner_zone", bed_mm=BED, layer_z_mm=1.0, zones=[12], lift_mm=10.0)
        assert bench.fingerprint_of(a) == bench.fingerprint_of(b) != bench.fingerprint_of(c)
        assert len(bench.fingerprint_of(a)) == 64

    def test_a_newer_look_at_the_same_block_replaces_the_older_one(self):
        machine = _Adapter()
        first = bench.observation("bambu_a1", "pause", "owner_zone", bed_mm=BED, layer_z_mm=1.0, zones=[12])
        second = bench.observation("bambu_a1", "pause", "owner_zone", bed_mm=BED, layer_z_mm=1.0, zones=[24])
        head = bench.head_observation("bambu_a1", 60.0, None)
        path = bench.keep_observation(machine, first)
        assert path is not None and path.parent == bench.bench_dir() and path.name == f"{bench.unit_of(machine)}.json"
        bench.keep_observation(machine, head)
        bench.keep_observation(machine, second)
        docs = bench.observations_of(machine)
        assert [(d["block"], d.get("zones")) for d in docs] == [("head", None), ("pause", [24])]
        from kiln.plate_state import machine_id

        assert bench.unit_of(machine) == bench.unit_from_machine(machine_id(machine)) and len(bench.unit_of(machine)) == 32
        assert bench.keep_observation(SimpleNamespace(), head) is None      # no identity, nothing kept

    def test_the_request_carries_the_documents_and_names_nobody_when_telemetry_is_off(self, monkeypatch):
        machine = _Adapter()
        assert bench.observations_for_request(machine) is None
        bench.keep_observation(machine, bench.observation("bambu_a1", "pause", "owner_zone", bed_mm=BED, layer_z_mm=1.0,
                                                          zones=[24], unit=bench.unit_of(machine), firmware="01.04.00.00"))
        carried = bench.observations_for_request(machine)
        assert carried[0]["unit"] == bench.unit_of(machine) and carried[0]["firmware"] == "01.04.00.00" and "share" not in carried[0]
        monkeypatch.setenv("KILN_TELEMETRY", "false")
        quiet = bench.observations_for_request(machine)
        assert quiet[0]["unit"] is None and quiet[0]["firmware"] is None and quiet[0]["share"] is False
        assert quiet[0]["zones"] == [24]        # the verdict still gets the numbers
        # And the placement request itself carries them.
        from kiln import _pro_placement_bridge as bridge

        request = bridge.request_for(machine, "bambu_a1", placement="auto", part={"size_mm": [20, 20, 2]})
        assert request["printer_observations"][0]["share"] is False and request["printer_observations"][0]["block"] == "pause"
        monkeypatch.setenv("KILN_TELEMETRY", "true")
        assert bridge.request_for(machine, "bambu_a1", placement="auto", part=None)["printer_observations"][0]["unit"] == bench.unit_of(machine)


# ---------------------------------------------------------------------------
# What the record lacks, and the two doors
# ---------------------------------------------------------------------------


class TestWhatTheRecordLacks:
    def test_blanks_are_read_from_the_verdict_on_a_clear_plate_probe(self, monkeypatch):
        from kiln import _pro_placement_bridge as bridge

        asked: list[dict] = []

        def fake_ask(request):
            asked.append(request)
            return _verdict(ok=True, blanks=["pause", "cancel"]), None

        monkeypatch.setattr(bridge, "ask", fake_ask)
        blanks, verdict = bench.blanks_for(_Adapter(), "bambu_a1")
        assert blanks == ["pause", "cancel"] and verdict["ok"] is True
        assert asked[0]["plate"]["status"] == "clear" and asked[0]["placement"] == "auto" and asked[0]["printer_id"] == "bambu_a1"
        monkeypatch.setattr(bridge, "ask", lambda request: (_verdict(ok=False, code="PLACEMENT_UNKNOWN_PRINTER"), None))
        assert bench.blanks_for(_Adapter(), "nonesuch")[0] == ["pause", "cancel", "end", "filament_change", "head"]
        monkeypatch.setattr(bridge, "ask", lambda request: (None, SimpleNamespace(kind="offline")))
        assert bench.blanks_for(_Adapter(), "bambu_a1") == ([], None)

    def test_a_refusal_names_the_blank_it_rests_on(self):
        worst = "the print head would hit jar (40 mm tall) during a pause at 0.2 mm (this printer's own pause motion is not on record, so the worst case is assumed)"
        assert bench.refusal_blanks(_verdict(ok=False, sentences=[worst])) == ["pause"]
        known = "the print head would hit jar (40 mm tall) during a pause at 0.2 mm (the printer's own pause, as its firmware moves)"
        assert bench.refusal_blanks(_verdict(ok=False, sentences=[known])) == []
        # An older server writes no record line: the sentence alone settles it.
        old = _verdict(ok=False, sentences=[worst]); old["record"] = {"printer_id": "bambu_a1"}
        assert bench.refusal_blanks(old) == ["pause"]
        assert bench.refusal_blanks(_verdict(ok=False, code="PLACEMENT_UNKNOWN_PRINTER")) == ["pause", "cancel", "end", "head"]
        assert bench.refusal_blanks(None) == [] and bench.refusal_blanks(_verdict(ok=True)) == []
        # A refusal that rests on a block no session can teach offers nothing.
        colour = "the print head would hit jar during a filament change (this printer's own filament change motion is not on record, so the worst case is assumed)"
        assert bench.refusal_blanks(_verdict(ok=False, sentences=[colour], blanks=["filament_change"])) == []

    def test_the_offer_names_only_what_a_session_can_teach_in_plain_words(self):
        assert bench.offer_sentence("Bambu Lab A1", ["pause", "cancel"]) == (
            "Kiln doesn't know where the Bambu Lab A1 sends its head when it pauses or cancels a print, so it "
            "assumes the worst. A five-minute session with a coin-sized test print teaches it; the printer_bench "
            "tool runs it.")
        every = bench.offer_sentence("Creality K1", ["pause", "cancel", "end", "head", "quiet_start"])
        assert every.startswith("Kiln doesn't know where the Creality K1 sends its head when it pauses, cancels a "
                                "print or finishes a print, or how big the head is, so it assumes the worst.")
        assert bench.offer_sentence("Creality K1", ["head"]).startswith("Kiln doesn't know how big the Creality K1's head is,")
        # A colour change is never triggered by a session, so it is never offered.
        colour = bench.offer_sentence("Bambu Lab A1", ["filament_change", "pause"])
        assert "colour" not in colour and "when it pauses, so" in colour


class TestNeverNaggedTwice:
    def test_the_registration_door_offers_once_and_later_is_honoured(self):
        unit = "ab" * 16
        assert bench.may_offer(unit, "registration") is True and bench.may_offer(unit, "refusal") is True
        bench.note_offer(unit)
        assert bench.may_offer(unit, "registration") is False and bench.may_offer(unit, "refusal") is True
        bench.note_offer(unit, answer="later")
        assert bench.may_offer(unit, "registration") is False and bench.may_offer(unit, "refusal") is True
        bench.note_offer(unit, answer="declined")
        assert bench.may_offer(unit, "registration") is False and bench.may_offer(unit, "refusal") is False
        assert bench.may_offer(None, "refusal") is False
        other = "cd" * 16
        bench.note_offer(other, answer="done")
        assert bench.may_offer(other, "refusal") is False

    def test_registration_offers_the_session_once_for_a_model_with_blanks(self, monkeypatch):
        machine = _Adapter()
        monkeypatch.setattr(bench, "blanks_for", lambda adapter, pid: (["pause", "cancel"], _verdict(ok=True, blanks=["pause", "cancel"])))
        offer = bench.offer_after_registration(machine, "a1")
        assert offer["tool"] == "printer_bench" and offer["blanks"] == ["pause", "cancel"] and offer["printer_name"] == "a1"
        assert offer["first"] is False and "printer_bench" in offer["sentence"]
        assert bench.offer_after_registration(machine, "a1") is None       # once
        fresh = _Adapter(); fresh.serial = "01P00A000000002"
        monkeypatch.setattr(bench, "blanks_for", lambda adapter, pid: (["pause", "cancel", "end", "filament_change", "head"],
                                                                        _verdict(ok=False, code="PLACEMENT_UNKNOWN_PRINTER")))
        first = bench.offer_after_registration(fresh, "new")
        assert first["first"] is True and first["blanks"] == ["pause", "cancel", "end", "head"]
        assert first["sentence"].startswith("Kiln has no record for the") and "can't check a second part beside the first" in first["sentence"]
        # Blanks no session can fill are no reason to offer one, and the one
        # offer is not spent on them.
        other = _Adapter(); other.serial = "01P00A000000003"
        monkeypatch.setattr(bench, "blanks_for", lambda adapter, pid: (["filament_change", "quiet_start"], _verdict(ok=True)))
        assert bench.offer_after_registration(other, "x") is None
        assert bench.may_offer(bench.unit_of(other), "registration") is True

    def test_registration_stays_quiet_when_there_is_nothing_to_teach_or_nothing_answers(self, monkeypatch):
        machine = _Adapter()
        monkeypatch.setattr(bench, "blanks_for", lambda adapter, pid: ([], None))
        assert bench.offer_after_registration(machine, "a1") is None
        assert bench.may_offer(bench.unit_of(machine), "registration") is True    # offline: not spent
        monkeypatch.setattr(bench, "blanks_for", lambda adapter, pid: ([], _verdict(ok=True)))
        assert bench.offer_after_registration(machine, "a1") is None
        assert bench.may_offer(bench.unit_of(machine), "refusal") is False        # nothing to teach: never asked

    def test_the_slice_refusal_offers_the_session_at_the_point_of_need(self):
        from kiln.plate_state import PlateJob, PlateState
        from kiln.plugins.slicer_tools import _placement_refusal

        state = PlateState(machine="01P00A000000001", status="occupied", source="start",
                           job=PlateJob(file="jar.gcode", footprint_mm=[90, 90, 160, 160], max_z_mm=40.0))
        worst = "the print head would hit jar (40 mm tall) during a pause at 0.2 mm (this printer's own pause motion is not on record, so the worst case is assumed)"
        resp = _placement_refusal("no", "PLACEMENT_REFUSED", state=state, bed=[256.0, 256.0],
                                  verdict=_verdict(ok=False, sentences=[worst], blanks=["pause", "cancel"]))
        assert resp["bench_offer"]["tool"] == "printer_bench" and resp["bench_offer"]["blanks"] == ["pause"]
        assert "printer_bench" in resp["bench_offer"]["sentence"]
        known = _placement_refusal("no", "PLACEMENT_REFUSED", state=state, bed=[256.0, 256.0],
                                   verdict=_verdict(ok=False, sentences=["the head would hit jar during a pause (as its firmware moves)"]))
        assert "bench_offer" not in known
        bench.note_offer(bench.unit_from_machine(state.machine), answer="declined")
        declined = _placement_refusal("no", "PLACEMENT_REFUSED", state=state, bed=[256.0, 256.0],
                                      verdict=_verdict(ok=False, sentences=[worst], blanks=["pause"]))
        assert "bench_offer" not in declined


# ---------------------------------------------------------------------------
# The position log and the send
# ---------------------------------------------------------------------------


class TestThePrintersOwnWord:
    def test_klipper_and_marlin_say_where_the_head_is_and_a_bambu_does_not(self):
        class _Moonraker:
            def _get_json(self, path, params=None):
                # Where the head IS -- never toolhead.position, where the last
                # queued move ends, which skips a macro's waypoints.
                assert path == "/printer/objects/query" and params == {"motion_report": "live_position"}
                return {"result": {"status": {"motion_report": {"live_position": [10.0, 20.0, 5.0, 0.0]}}}}

        assert bench.position_of(_Moonraker()) == (10.0, 20.0, 5.0) and bench.can_log_positions(_Moonraker())
        assert bench.position_of(_Adapter(position=True)) == (30.0, 30.0, 1.0)
        assert bench.position_of(_Adapter()) is None and bench.can_log_positions(_Adapter()) is False

    def test_the_log_stops_when_the_head_has_settled_after_moving(self):
        machine = _Adapter(position=True)
        log = bench.PositionLog(machine, settle_s=0.15, max_s=5.0, hz=50.0)
        log.start()
        import time

        time.sleep(0.05)
        machine.head = [30.0, 30.0, 11.0]
        time.sleep(0.05)
        machine.head = [-48.0, 180.0, 11.0]
        log.join(timeout=5.0)
        assert not log.is_alive() and log.moved is True and log.complete is True
        assert log.points[0] == (30.0, 30.0, 1.0) and log.points[-1] == (-48.0, 180.0, 11.0) and len(log.points) == 3

    def test_a_log_whose_reads_fail_after_the_head_moved_never_completes(self):
        machine = _Adapter(position=True)
        log = bench.PositionLog(machine, settle_s=0.1, max_s=0.6, hz=50.0)
        log.start()
        import time

        time.sleep(0.05)
        machine.head = [30.0, 30.0, 11.0]
        time.sleep(0.05)
        machine._position = False               # the printer stops answering mid-move
        log.join(timeout=5.0)
        assert log.moved is True and log.complete is False

    def test_pending_documents_go_through_the_one_rpc_once_and_only_while_telemetry_is_on(self, monkeypatch):
        machine = _Adapter()
        doc = bench.observation("bambu_a1", "pause", "owner_zone", bed_mm=BED, layer_z_mm=1.0, zones=[24], lift_mm=10.0,
                                unit=bench.unit_of(machine), firmware="01.04.00.00")
        bench.keep_observation(machine, doc)
        posted: list[tuple[str, dict]] = []

        class _Resp:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=None):
            posted.append((req.full_url, json.loads(req.data.decode("utf-8"))))
            return _Resp()

        import urllib.request

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        assert bench.send_pending("https://x.supabase.co", "anon") == 1
        url, body = posted[0]
        assert url.endswith("/rest/v1/rpc/record_printer_motion_observation")
        assert body["p_printer_id"] == "bambu_a1" and body["p_block"] == "pause" and body["p_how"] == "owner_zone"
        assert body["p_unit"] == bench.unit_of(machine) and body["p_firmware"] == "01.04.00.00"
        assert body["p_fingerprint"] == bench.fingerprint_of(doc) and body["p_document"]["zones"] == [24]
        assert "share" not in body["p_document"]
        assert bench.send_pending("https://x.supabase.co", "anon") == 0        # already landed
        assert len(posted) == 1
        bench.keep_observation(machine, bench.head_observation("bambu_a1", 60.0, 25.0, unit=bench.unit_of(machine)))
        monkeypatch.setenv("KILN_TELEMETRY", "false")
        assert bench.send_pending("https://x.supabase.co", "anon") == 0 and len(posted) == 1
        monkeypatch.setenv("KILN_TELEMETRY", "true")
        assert bench.send_after_heartbeat("https://x.supabase.co", "anon") == 1 and posted[1][1]["p_block"] == "head"


# ---------------------------------------------------------------------------
# The conversation
# ---------------------------------------------------------------------------


@pytest.fixture
def server(monkeypatch):
    """The server's doors as the session meets them: auth, the printer,
    pause / resume / cancel that move the fake machine."""
    import kiln.server as srv

    machine = _Adapter()
    log: list[str] = []

    def pause_print(printer_name=None, **kw):
        log.append("pause"); machine.state = "paused"; machine.head = [-48.0, 180.0, 11.0]
        return {"success": True}

    def resume_print(printer_name=None, **kw):
        log.append("resume"); machine.state = "printing"; machine.head = [30.0, 30.0, 1.0]
        return {"success": True}

    def cancel_print(printer_name=None, **kw):
        log.append("cancel"); machine.state = "idle"; machine.head = [-48.0, 180.0, 11.0]
        return {"success": True}

    monkeypatch.setattr(srv, "_check_auth", lambda *_a, **_k: None)
    monkeypatch.setattr(srv, "_resolve_control_target", lambda name: (machine, name or "a1"))
    monkeypatch.setattr(srv, "pause_print", pause_print)
    monkeypatch.setattr(srv, "resume_print", resume_print)
    monkeypatch.setattr(srv, "cancel_print", cancel_print)
    monkeypatch.setattr(bench, "blanks_for", lambda adapter, pid: (["pause", "cancel", "end", "head"],
                                                                    _verdict(ok=True, blanks=["pause", "cancel", "end", "head"])))
    monkeypatch.setattr(bench, "send_pending", lambda url, key: 0)
    from kiln.plugins import printer_bench_tools as tool

    monkeypatch.setattr(tool, "PARK_SETTLE_S", 0.0)
    return SimpleNamespace(machine=machine, log=log, tool=tool)


def _step(server, answer=None, **kw):
    out = server.tool.printer_bench(printer_name="a1", answer=answer, **kw)
    assert out.get("success") is True, out
    return out


class TestTheConversation:
    def test_a_closed_firmware_printer_is_walked_end_to_end_with_pictures_and_one_ask_at_a_time(self, server):
        m = server.machine
        out = _step(server)
        assert out["step"] == "intro" and out["options"] == ["yes", "later", "no"]
        assert out["ask"] == (
            "Help Kiln get to know your Bambu Lab A1 in about five minutes: it prints a coin-sized square, pauses and "
            "resumes it, and lets it finish, then prints a second one and cancels it partway, watching where the head "
            "goes each time, and you measure the head with calipers. Kiln will ask you where the head stopped, with a "
            "picture. Ready?")
        out = _step(server, "yes")
        assert out["step"] == "plate" and out["ask"] == "Is the plate empty?" and out["images"][0]["kind"] == "camera"
        assert out["image_b64"] == out["images"][0]["image_b64"]
        out = _step(server, "no")
        assert "Take everything off" in out["ask"]
        out = _step(server, "yes")
        assert out["step"] == "print" and out["print_file"].endswith("kiln_bench_square.stl") and "run_quick_print" in out["how_to_start"]
        assert "issue_preview_token" in out["how_to_start"]
        assert out["ask"] == "Kiln will print a coin-sized square, pause it partway, then let it finish. Ready to start it?"
        out = _step(server, "started")                       # not printing yet
        assert out["waiting"] is True and "isn't printing yet" in out["ask"]
        m.state, m.layer = "printing", 1
        out = _step(server, "started")
        assert out["step"] == "pause" and out["waiting"] is True and "few layers up" in out["ask"]
        m.layer = 5
        out = _step(server)
        assert server.log == ["pause"] and out["step"] == "pause_zone"
        assert out["ask"].startswith("Where did the head stop when it paused?")
        assert [p["kind"] for p in out["images"]] == ["zones", "camera"]
        out = _step(server, "hmm")
        assert out["step"] == "pause_zone"                    # asked again, nothing recorded
        out = _step(server, "zone 24")
        assert out["step"] == "pause_lift" and out["options"] == ["barely", "a finger", "a hand span", "a number in mm"]
        # The height the verdict relies on is the one BEFORE the head moved sideways.
        assert out["ask"] == ("About how high did it lift when it paused, before it moved sideways: barely, a finger, "
                              "or a hand span?")
        out = _step(server, "a hand span")
        assert out["step"] == "pause_measure" and out["options"] == ["skip"]
        out = _step(server, "skip")
        assert server.log == ["pause", "resume"] and out["step"] == "resume_back"
        assert out["options"] == ["over the square first", "down while travelling"]
        out = _step(server, "over the square first")
        assert "pause" in out["learned"] and out["step"] == "end" and out["waiting"] is True
        m.completion = 90.0
        out = _step(server)
        assert out["waiting"] is True
        m.state, m.completion = "idle", 100.0
        out = _step(server)
        assert out["step"] == "end_zone"
        out = _step(server, "25")
        out = _step(server, "barely")
        assert "end" in out["learned"] and out["step"] == "clear" and out["options"] == ["it's empty"]
        out = _step(server, "it's empty")
        assert out["step"] == "print" and out["ask"] == "Kiln will print a second coin-sized square and cancel it partway. Ready to start it?"
        m.state, m.layer, m.completion = "printing", 2, 10.0
        out = _step(server, "yes")
        assert out["step"] == "cancel" and out["waiting"] is True
        m.layer = 6
        out = _step(server)
        assert server.log == ["pause", "resume", "cancel"] and out["step"] == "cancel_zone"
        out = _step(server, "24")
        out = _step(server, "40 mm")
        assert "cancel" in out["learned"] and out["step"] == "clear"
        out = _step(server, "yes")
        assert out["step"] == "head_width" and out["images"][0]["kind"] == "head" and "calipers" in out["ask"]
        out = _step(server, "60")
        assert out["step"] == "head_rod" and "lowest bar" in out["ask"]
        out = _step(server, "25 mm")
        assert out["step"] == "done"
        # Free: WHETHER a second part fits, never where -- where is the plan's tier.
        assert out["payoff"] == (
            "Kiln now knows how your Bambu Lab A1 moves, so it keeps the head clear of what's on the plate and can "
            "tell you whether a second part fits. The numbers go to Kiln; once enough Bambu Lab A1 owners agree, "
            "every Bambu Lab A1 owner gets them.")
        assert len(out["learned"]) == 4 and out["still_unknown"] == []
        # What was written: one document per block, numbers only, this unit's.
        docs = {d["block"]: d for d in bench.observations_of(m)}
        assert set(docs) == {"pause", "end", "cancel", "head"}
        assert docs["pause"]["how"] == "camera" and docs["pause"]["zones"] == [24] and docs["pause"]["lift_mm"] == 100.0
        assert docs["pause"]["returns"] is True and docs["pause"]["return_how"] == "level" and docs["pause"]["layer_z_mm"] == 1.0
        assert docs["end"]["zones"] == [25] and docs["end"]["lift_mm"] == 0.5 and docs["end"]["returns"] is False
        assert docs["cancel"]["lift_mm"] == 40.0 and docs["cancel"]["layer_z_mm"] == pytest.approx(1.2)
        assert docs["head"]["head_mm"] == {"width": 60.0, "rod_height": 25.0}
        assert all(d["unit"] == bench.unit_of(m) and d["firmware"] == "01.04.00.00" for d in docs.values())
        assert not any(k in json.dumps(docs) for k in ("image", "photo"))
        # The offer ledger says done: no door will offer this unit again.
        assert bench.may_offer(bench.unit_of(m), "refusal") is False
        # And the session is gone; a new call starts over.
        assert server.tool._load_session(bench.unit_of(m)) is None

    def test_later_and_no_end_the_session_and_are_honoured(self, server):
        _step(server)
        out = _step(server, "later")
        assert out["step"] == "later" and "won't bring it up again" in out["message"]
        assert bench.may_offer(bench.unit_of(server.machine), "registration") is False
        assert bench.may_offer(bench.unit_of(server.machine), "refusal") is True
        _step(server, restart=True)
        out = _step(server, "no")
        assert out["step"] == "declined" and bench.may_offer(bench.unit_of(server.machine), "refusal") is False

    def test_a_dropped_chat_resumes_where_it_was(self, server):
        m = server.machine
        _step(server); _step(server, "yes"); _step(server, "yes")
        m.state, m.layer = "printing", 5
        out = _step(server, "started")
        assert out["step"] == "pause_zone"
        # A new process, a new call, no answer: the same ask comes back.
        server.tool._LOGS.clear()
        out = server.tool.printer_bench(printer_name="a1")
        assert out["step"] == "pause_zone" and out["ask"].startswith("Where did the head stop")
        out = _step(server, "24")
        assert out["step"] == "pause_lift"

    def test_a_printer_that_says_where_its_head_is_is_asked_nothing_about_the_moves(self, server, monkeypatch):
        m = server.machine
        m._position = True
        monkeypatch.setattr(bench, "blanks_for", lambda adapter, pid: (["pause"], _verdict(ok=True, blanks=["pause"])))

        class _InstantLog:
            """The log, with the path already walked: the thread's timing is
            its own test above."""

            def __init__(self, adapter, **kw):
                self.points = [(30.0, 30.0, 1.0), (30.0, 30.0, 11.0), (-48.0, 180.0, 11.0)]
                self.moved = True
                self.complete = True

            def start(self):
                pass

            def is_alive(self):
                return False

        monkeypatch.setattr(bench, "PositionLog", _InstantLog)
        out = _step(server)
        assert out["ask"] == (
            "Help Kiln get to know your Bambu Lab A1 in about five minutes: it prints a coin-sized square, pauses and "
            "resumes it, and stops it, watching where the head goes each time. Kiln reads where the head goes from "
            "the printer itself. Ready?")
        _step(server, "yes"); _step(server, "yes")
        m.state, m.layer = "printing", 5
        out = _step(server, "started")
        # Pause sent, park watched, resume sent, way back watched -- no ask;
        # then the square is cancelled, since the cancel is on record.
        assert out["step"] == "cancel_quietly" and out["waiting"] is True and server.log == ["pause", "resume", "cancel"]
        out = _step(server)
        assert out["step"] == "clear"
        out = _step(server, "yes")
        assert out["step"] == "done" and out["learned"][0].startswith("pause: the whole path")
        doc = bench.observations_of(m)[0]
        assert doc["how"] == "position_log" and doc["returns"] is True and doc["layer_z_mm"] == 1.0
        assert doc["points"][0] == [30.0, 30.0, 1.0] and doc["points"][-1] == [-48.0, 180.0, 11.0]

    def test_a_model_with_nothing_to_teach_is_told_so_and_never_offered(self, server, monkeypatch):
        monkeypatch.setattr(bench, "blanks_for", lambda adapter, pid: ([], _verdict(ok=True, blanks=[])))
        out = _step(server)
        assert out["step"] == "done" and "already knows" in out["message"]
        assert bench.may_offer(bench.unit_of(server.machine), "refusal") is False

    def test_no_verdict_is_an_honest_refusal_not_a_session(self, server, monkeypatch):
        monkeypatch.setattr(bench, "blanks_for", lambda adapter, pid: ([], None))
        out = server.tool.printer_bench(printer_name="a1")
        assert out["success"] is False and out["error"]["code"] == "BENCH_NO_VERDICT"

    def test_an_end_only_session_lets_the_square_finish_and_says_so(self, server, monkeypatch):
        monkeypatch.setattr(bench, "blanks_for", lambda adapter, pid: (["end"], _verdict(ok=True, blanks=["end"])))
        out = _step(server)
        assert out["ask"].startswith("Help Kiln get to know your Bambu Lab A1 in about five minutes: it prints a "
                                     "coin-sized square and lets it finish, watching where the head goes each time.")
        _step(server, "yes")
        out = _step(server, "yes")
        assert out["ask"] == "Kiln will print a coin-sized square and let it finish. Ready to start it?"

    def test_a_head_only_session_prints_nothing(self, server, monkeypatch):
        monkeypatch.setattr(bench, "blanks_for", lambda adapter, pid: (["head"], _verdict(ok=True, blanks=["head"])))
        out = _step(server)
        assert out["ask"] == "Help Kiln get to know your Bambu Lab A1: two caliper measurements of its print head. Ready?"
        out = _step(server, "yes")
        assert out["step"] == "head_width" and server.log == []

    def test_blanks_no_test_print_can_show_are_said_and_nothing_runs(self, server, monkeypatch):
        monkeypatch.setattr(bench, "blanks_for", lambda adapter, pid: (["filament_change", "quiet_start"], _verdict(ok=True)))
        out = _step(server)
        assert out["step"] == "done" and "a test print can't show those" in out["message"] and server.log == []

    def test_the_payoff_is_true_on_every_tier_and_printer(self, server, monkeypatch):
        tool = server.tool
        session = {"model_name": "Creality K1", "printer_id": "k1", "blanks": ["pause", "head", "quiet_start"]}
        monkeypatch.setattr(tool, "_has_pro", lambda: False)
        free = tool._payoff(session)
        assert "whether a second part fits" in free and "where a second part" not in free and "start it" not in free
        monkeypatch.setattr(tool, "_has_pro", lambda: True)
        pro = tool._payoff(session)
        # Pro sees where; starting it needs a quiet start this printer has run.
        assert "can tell you where a second part fits." in pro and "start it for you" not in pro
        assert tool._payoff({**session, "blanks": ["pause"]}).startswith(
            "Kiln now knows how your Creality K1 moves, so it keeps the head clear of what's on the plate and can "
            "tell you where a second part fits, and can start it for you.")
        monkeypatch.setenv("KILN_TELEMETRY", "false")
        assert tool._payoff(session).endswith("The numbers stay on this machine.")

    def test_pro_is_asked_through_the_standard_gate(self, server, monkeypatch):
        # The payoff asks kiln-pro's public, caller-aware check_pro -- a route
        # the tier-seam audit can see -- and anything it cannot ask reads as free.
        tool = server.tool
        gate = SimpleNamespace(check_pro=lambda feature="": None)
        monkeypatch.setitem(sys.modules, "kiln_pro", SimpleNamespace(pro_gate=gate))
        monkeypatch.setitem(sys.modules, "kiln_pro.pro_gate", gate)
        assert tool._has_pro() is True
        gate.check_pro = lambda feature="": {"success": False, "error": "needs Pro"}
        assert tool._has_pro() is False
        monkeypatch.setitem(sys.modules, "kiln_pro.pro_gate", None)
        assert tool._has_pro() is False

    def test_nothing_observed_claims_nothing(self, server, monkeypatch):
        m = server.machine
        m._position = True
        monkeypatch.setattr(bench, "blanks_for", lambda adapter, pid: (["pause"], _verdict(ok=True, blanks=["pause"])))

        class _StillLog:
            def __init__(self, adapter, **kw):
                self.points, self.moved, self.complete = [(30.0, 30.0, 1.0)], False, True

            def start(self):
                pass

            def is_alive(self):
                return False

        monkeypatch.setattr(bench, "PositionLog", _StillLog)
        _step(server); _step(server, "yes"); _step(server, "yes")
        m.state, m.layer = "printing", 5
        _step(server, "started")
        _step(server)
        out = _step(server, "yes")
        assert out["step"] == "done" and out["payoff"] == "" and out["learned"] == []
        assert "Kiln still assumes the worst for this printer" in out["message"]
        assert any("caught no movement" in n for n in out["notes"])

    def test_a_log_cut_short_writes_nothing(self, server, monkeypatch):
        m = server.machine
        m._position = True
        monkeypatch.setattr(bench, "blanks_for", lambda adapter, pid: (["pause"], _verdict(ok=True, blanks=["pause"])))

        class _CutLog:
            def __init__(self, adapter, **kw):
                self.points = [(30.0, 30.0, 1.0), (30.0, 30.0, 11.0)]
                self.moved, self.complete = True, False

            def start(self):
                pass

            def is_alive(self):
                return False

        monkeypatch.setattr(bench, "PositionLog", _CutLog)
        _step(server); _step(server, "yes"); _step(server, "yes")
        m.state, m.layer = "printing", 5
        _step(server, "started")
        _step(server)
        out = _step(server, "yes")
        assert out["step"] == "done" and out["learned"] == [] and bench.observations_of(m) == []
        assert any("cut short" in n for n in out["notes"])

    def test_the_plan_covers_every_blank_with_at_most_two_prints(self):
        from kiln.plugins.printer_bench_tools import _plan

        full = _plan(["pause", "cancel", "end", "head"], loggable=False)
        assert full.count("print") == 2 and full[-1] == "done" and full.index("head_width") > full.index("clear")
        assert "pause_zone" in full and "resume_back" in full and "end_zone" in full and "cancel_zone" in full
        logged = _plan(["pause", "cancel", "end"], loggable=True)
        assert logged.count("print") == 2 and not any(s.endswith(("_zone", "_lift", "_back", "_measure")) for s in logged)
        only_pause = _plan(["pause"], loggable=True)
        assert only_pause.count("print") == 1 and "cancel_quietly" in only_pause and "end" not in only_pause
        only_cancel = _plan(["cancel"], loggable=False)
        assert only_cancel.count("print") == 1 and "pause" not in only_cancel and "cancel_zone" in only_cancel
        only_head = _plan(["head"], loggable=False)
        assert only_head == ["intro", "head_width", "head_rod", "done"]      # no print, so no plate ask
        assert "runout" not in " ".join(full)

    def test_the_tool_is_registered_and_classified(self):
        from kiln.plugins.printer_bench_tools import plugin

        captured: list = []

        class _Mcp:
            def tool(self):
                return lambda fn: captured.append(fn.__name__) or fn

        import kiln.server as srv

        plugin.register(_Mcp())
        assert captured == ["printer_bench"] and "printer_bench" in srv._TOOL_RATE_LIMITS
        import importlib.resources as res

        data = json.loads((res.files("kiln") / "data" / "tool_safety.json").read_text())
        assert data["classifications"]["printer_bench"]["level"] == "confirm"
