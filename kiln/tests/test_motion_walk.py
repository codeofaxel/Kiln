"""A walked home or park: the floor under every step.

A walk hands a printer one step at a time, on its owner's word that the
last step did what it described.  Kiln's servers decide what a step is;
this pins what the install itself refuses, before anything is asked or
sent, whatever the servers say:

* a printer whose own report does not name the model it is set up as;
* a printer that is not idle, not cold, or has a heater set;
* a plate the record does not call empty, or one the person has not;
* a printer with no firmware version to count the walk under;
* a step out of order, or one the person has not confirmed the last for;
* a handed-over step that would heat or extrude.

And what it does when a step goes wrong: the walk ends, the fault is
counted, and Kiln's servers are told.  Nothing in a walk holds the printer.

Every step here is made up: the shape, never a sequence.
"""

from __future__ import annotations

import pytest

from kiln import plate_state
from kiln.printers import motion_walk as walk
from kiln.printers.base import PrinterInfo, PrinterState, PrinterStatus
from kiln.printers.command_verdict import CommandVerdict

MODEL = "bambu_p1s"
FIRMWARE = "01.07.00.00"
TOTAL = 3


class _Printer:
    """A stand-in printer that answers only what a walk may ask of it.

    Anything else a walk reaches for is not there, and is written down in
    ``reached``: a lock, a hold or a control touched from inside a walk is
    named by the test that reads the list.
    """

    _MODEL_REPORT_HOLDS_MOVES = True

    def __init__(self) -> None:
        self.reached: list[str] = []
        self._printer_model = MODEL
        self._serial = self.serial = "01P00A000000001"
        self.name = "garage"
        self.host = "10.0.0.51"
        self.sent: list[list[str]] = []
        self.state = PrinterState(
            connected=True, state=PrinterStatus.IDLE,
            tool_temp_actual=24.0, tool_temp_target=0.0, bed_temp_actual=23.0, bed_temp_target=0.0,
        )
        self.firmware: str | None = FIRMWARE
        self.channels = {"serial_prefix": MODEL, "firmware_product_name": MODEL}
        self.info_model = MODEL
        self.verdict = True
        self.faults: set[tuple[str, str]] = set()

    def __getattr__(self, name: str):
        if not name.startswith("__"):
            self.__dict__.setdefault("reached", []).append(name)
        raise AttributeError(name)

    def declared_printer_model(self) -> str:
        return self._printer_model

    def get_printer_info(self) -> PrinterInfo:
        return PrinterInfo(model=self.info_model, raw_model=self.info_model)

    def get_identity_channels(self) -> dict[str, str]:
        return dict(self.channels)

    def get_state(self) -> PrinterState:
        return self.state

    def reported_firmware_version(self) -> str | None:
        return self.firmware

    def _snapshot_faults(self) -> set[tuple[str, str]]:
        return set(self.faults)

    def send_gcode(self, lines: list[str]) -> CommandVerdict:
        self.sent.append(list(lines))
        return CommandVerdict.coerce(self.verdict, what="walk")


class _Servers:
    """Kiln's servers, as a walk meets them: one step per ask, or a refusal."""

    def __init__(self) -> None:
        self.asks: list[dict] = []
        self.refuse: dict | None = None
        self.lines = {1: ["G91", "G0 Z1 F100", "G90"], 2: ["G0 X1 F100"], 3: ["G0 Y1 F100"]}
        self.reported: list[dict] = []

    def __call__(self, tool: str, *, _asked_by_user: bool = True, **request):
        assert tool == "motion_plan"
        self.asks.append({"asked_by_person": _asked_by_user, **request})
        asked = request["walk"]
        if asked["op"] == "report":
            self.reported.append(asked)
            return {"walk": {"reported": True}}
        if self.refuse is not None:
            return dict(self.refuse)
        if asked["op"] == "offer":
            return {"walk": {"offered": True, "steps_total": TOTAL}}
        number = asked["step"]
        return {"walk": {
            "schema": walk.SCHEMA, "printer_id": request["printer_id"], "verb": request["verb"],
            "steps_total": TOTAL, "firmware": asked.get("firmware"),
            "step": {"number": number, "label": f"made-up step {number}", "you_will_see": "something small",
                     "stops_when": "it ends", "gcode": list(self.lines[number]), "confirm": "Did it?",
                     "if_not": "Stop."},
            "stop": "The printer's own stop always works.",
            "next": None if number >= TOTAL else {"number": number + 1, "label": f"made-up step {number + 1}"},
        }}


@pytest.fixture
def printer(tmp_path, monkeypatch):
    monkeypatch.setenv("KILN_HOME", str(tmp_path / "kiln-home"))
    monkeypatch.setattr(walk, "STEP_WATCH_S", 0.0)
    monkeypatch.setattr("kiln.outbound_guard._own_printer_values", lambda: [
        ("the printer's serial number", "01P00A000000001"), ("the printer's network address", "10.0.0.51"),
    ])
    walk._progress.clear()
    machine = _Printer()
    plate_state.mark_clear(machine, "human")
    assert plate_state.plate_occupancy(machine).clear
    return machine


@pytest.fixture
def servers(monkeypatch):
    fake = _Servers()
    import kiln.server as srv

    monkeypatch.setattr(srv, "_pro_api_call", fake)
    return fake


@pytest.fixture
def outcomes(monkeypatch):
    seen: list[tuple[str, str, object]] = []
    from kiln.printers import motion_plan

    monkeypatch.setattr(motion_plan, "note_motion_outcome", lambda adapter, verb, kind, detail=None: seen.append((verb, kind, detail)))
    return seen


def _refused(code: str, printer, servers, *args, **kwargs) -> walk.WalkRefused:
    with pytest.raises(walk.WalkRefused) as caught:
        walk.walk_step(printer, *args, **kwargs)
    assert caught.value.code == code, str(caught.value)
    assert printer.sent == [], "a refused step sent something"
    assert "Nothing was sent" in str(caught.value)
    return caught.value


def _step(printer, number: int, verb: str = "park", **kw):
    return walk.walk_step(printer, verb, number, plate_clear=True, saw_previous=number > 1, **kw)


class TestTheWalk:
    def test_each_step_is_asked_for_sent_and_described_one_at_a_time(self, printer, servers, outcomes):
        for number in (1, 2, 3):
            out = _step(printer, number)
            assert out["success"] and out["step_sent"] == number and out["steps_total"] == TOTAL
            assert out["confirm"] == "Did it?" and out["you_should_see"] and "stop" in out["stop"]
            assert printer.sent[-1] == servers.lines[number]
        assert len(printer.sent) == 3 and out["next_step"] is None and "complete" in out["message"]
        assert [(v, k, d) for v, k, d in outcomes] == [("park", "step_sent", (n, TOTAL)) for n in (1, 2, 3)]

    @pytest.mark.parametrize("asked,watched", [(None, 10.0), (20, 20.0), (3, 10.0), (9999, 60.0), ("long", 10.0), (True, 10.0)])
    def test_a_step_is_watched_as_long_as_it_asks_within_bounds(self, printer, servers, monkeypatch, asked, watched):
        from kiln.printers import motion_plan

        monkeypatch.setattr(walk, "STEP_WATCH_S", 10.0)
        seen: list[float] = []
        monkeypatch.setattr(motion_plan, "_watch_for_fault", lambda adapter, before, watch: seen.append(watch))
        real = servers.__call__

        def with_watch(tool, **kw):
            answer = real(tool, **kw)
            if asked is not None:
                answer["walk"]["step"]["watch_seconds"] = asked
            return answer

        import kiln.server as srv

        monkeypatch.setattr(srv, "_pro_api_call", with_watch)
        assert _step(printer, 1)["success"] and seen == [watched]

    def test_what_leaves_is_the_reading_and_no_serial_or_address(self, printer, servers):
        _step(printer, 1)
        (ask,) = servers.asks
        assert ask["printer_id"] == MODEL and ask["verb"] == "park" and "serial" not in ask
        sent = ask["walk"]
        assert sent["firmware"] == FIRMWARE and sent["machine_reports"] == MODEL and sent["step"] == 1
        assert (sent["nozzle_c"], sent["nozzle_target_c"], sent["bed_c"], sent["bed_target_c"]) == (24.0, 0.0, 23.0, 0.0)
        assert sent["plate_record_clear"] is True and sent["plate_person_clear"] is True
        assert "01P00A" not in repr(ask) and "10.0.0.51" not in repr(ask)

    def test_an_offer_asks_and_sends_nothing_to_the_printer(self, printer, servers):
        assert walk.walk_offer(printer, "park") == {"offered": True, "verb": "park", "steps_total": TOTAL}
        assert printer.sent == [] and servers.asks[0]["asked_by_person"] is False
        servers.refuse = {"status": "error", "code": "NO_WALK", "error": "none at the moment"}
        assert walk.walk_offer(printer, "home") == {"offered": False}
        assert walk.walk_offer(printer, "wipe") == {"offered": False}

    def test_an_offer_is_not_made_to_a_printer_that_has_not_proved_its_model(self, printer, servers):
        printer.channels = {}
        assert walk.walk_offer(printer, "park") == {"offered": False} and servers.asks == []


class TestIdentityFirst:
    def test_a_printer_that_reports_another_model_is_refused_before_anything_is_asked(self, printer, servers):
        printer.info_model = "bambu_a1"
        printer.channels = {"serial_prefix": "bambu_a1"}
        _refused("PRINTER_MODEL_DISAGREES", printer, servers, "park", 1, plate_clear=True)
        assert servers.asks == []

    def test_a_printer_that_reports_nothing_is_refused(self, printer, servers):
        printer.channels = {}
        _refused("PRINTER_MODEL_UNPROVEN", printer, servers, "park", 1, plate_clear=True)
        assert servers.asks == []

    def test_one_way_of_reporting_that_disagrees_is_enough_to_refuse(self, printer, servers):
        printer.channels = {"serial_prefix": MODEL, "firmware_product_name": "bambu_x1c"}
        _refused("PRINTER_MODEL_UNPROVEN", printer, servers, "park", 1, plate_clear=True)

    def test_identity_is_judged_before_the_plate_and_the_heat(self, printer, servers):
        printer.channels = {}
        printer.state = PrinterState(connected=True, state=PrinterStatus.PRINTING)
        _refused("PRINTER_MODEL_UNPROVEN", printer, servers, "park", 1, plate_clear=False)


class TestPlateEmpty:
    def test_the_record_must_say_the_plate_is_empty(self, printer, servers):
        plate_state.mark_occupied(printer, None, source="test")
        _refused("PLATE_NOT_CLEAR", printer, servers, "park", 1, plate_clear=True)
        assert servers.asks == []

    def test_a_plate_nobody_has_recorded_is_not_an_empty_one(self, printer, servers, tmp_path, monkeypatch):
        monkeypatch.setenv("KILN_HOME", str(tmp_path / "another-home"))
        _refused("PLATE_NOT_CLEAR", printer, servers, "park", 1, plate_clear=True)

    @pytest.mark.parametrize("word", [None, False, "yes", 1])
    def test_the_person_must_say_so_as_well(self, printer, servers, word):
        _refused("PLATE_CLEAR_REQUIRED", printer, servers, "park", 1, plate_clear=word)
        assert servers.asks == []

    def test_it_is_asked_again_on_a_later_step(self, printer, servers):
        _step(printer, 1)
        with pytest.raises(walk.WalkRefused) as caught:
            walk.walk_step(printer, "park", 2, plate_clear=None, saw_previous=True)
        assert caught.value.code == "PLATE_CLEAR_REQUIRED" and len(printer.sent) == 1


class TestCold:
    @pytest.mark.parametrize("field,value", [
        ("tool_temp_target", 220.0), ("bed_temp_target", 55.0), ("tool_temp_target", 1.0),
        ("tool_temp_actual", walk.COLD_MAX_C + 0.5), ("bed_temp_actual", 60.0),
        ("tool_temp_actual", None), ("bed_temp_target", None),
    ])
    def test_a_heater_set_a_warm_reading_or_no_reading_refuses(self, printer, servers, field, value):
        setattr(printer.state, field, value)
        _refused("NOT_COLD", printer, servers, "park", 1, plate_clear=True)
        assert servers.asks == []

    @pytest.mark.parametrize("status", [PrinterStatus.PRINTING, PrinterStatus.PAUSED, PrinterStatus.BUSY,
                                        PrinterStatus.ERROR, PrinterStatus.OFFLINE, PrinterStatus.UNKNOWN])
    def test_a_printer_that_is_not_idle_is_refused(self, printer, servers, status):
        printer.state.state = status
        _refused("PRINTER_NOT_IDLE", printer, servers, "park", 1, plate_clear=True)

    @pytest.mark.parametrize("line", ["M104 S200", "M109 S200", "M140 S60", "M190 S60", "M104 S0", "G1 X5 E2 F100",
                                      "G1 E-0.5 F300", "m109 s140"])
    def test_a_handed_over_step_that_heats_or_extrudes_is_not_sent(self, printer, servers, line):
        servers.lines[1] = ["G91", line]
        _refused("WARM_LINE_REFUSED", printer, servers, "park", 1, plate_clear=True)

    def test_the_ceiling_itself_is_cold(self, printer, servers):
        printer.state.tool_temp_actual = printer.state.bed_temp_actual = walk.COLD_MAX_C
        assert _step(printer, 1)["success"]


class TestOneConfirmedStep:
    def test_a_later_step_needs_the_persons_word_on_the_one_before(self, printer, servers):
        _step(printer, 1)
        for word in (None, False, "yes"):
            with pytest.raises(walk.WalkRefused) as caught:
                walk.walk_step(printer, "park", 2, plate_clear=True, saw_previous=word)
            assert caught.value.code == "STEP_NOT_CONFIRMED"
        assert len(printer.sent) == 1 and len(servers.asks) == 1
        assert walk.walk_step(printer, "park", 2, plate_clear=True, saw_previous=True)["success"]

    def test_a_step_cannot_be_skipped_to(self, printer, servers):
        _refused("STEP_OUT_OF_ORDER", printer, servers, "park", 2, plate_clear=True, saw_previous=True)
        _step(printer, 1)
        with pytest.raises(walk.WalkRefused) as caught:
            walk.walk_step(printer, "park", 3, plate_clear=True, saw_previous=True)
        assert caught.value.code == "STEP_OUT_OF_ORDER" and len(printer.sent) == 1

    def test_a_walk_left_too_long_starts_again(self, printer, servers, monkeypatch):
        _step(printer, 1)
        monkeypatch.setattr(walk, "WALK_IDLE_S", -1.0)
        with pytest.raises(walk.WalkRefused) as caught:
            _step(printer, 2)
        assert caught.value.code == "STEP_OUT_OF_ORDER"

    def test_one_moves_walk_does_not_unlock_the_others_step(self, printer, servers):
        _step(printer, 1, verb="home")
        _refused_later = pytest.raises(walk.WalkRefused)
        with _refused_later as caught:
            _step(printer, 2, verb="park")
        assert caught.value.code == "STEP_OUT_OF_ORDER"

    @pytest.mark.parametrize("number", [0, -1, None, True, "1", 1.0])
    def test_a_step_is_asked_for_by_a_whole_number(self, printer, servers, number):
        _refused("INVALID_ARGUMENT", printer, servers, "park", number, plate_clear=True)

    def test_a_step_that_is_not_the_one_asked_for_is_not_sent(self, printer, servers, monkeypatch):
        real = servers.__call__

        def wrong(tool, **kw):
            answer = real(tool, **kw)
            answer["walk"]["step"]["number"] = 2
            return answer

        import kiln.server as srv

        monkeypatch.setattr(srv, "_pro_api_call", wrong)
        _refused("MALFORMED_STEP", printer, servers, "park", 1, plate_clear=True)

    def test_a_refusal_from_the_servers_sends_nothing(self, printer, servers, outcomes):
        servers.refuse = {"status": "error", "code": "NO_WALK", "error": "Kiln has no park to walk at the moment."}
        refusal = _refused("NO_WALK", printer, servers, "park", 1, plate_clear=True)
        assert "no park to walk" in str(refusal) and outcomes == [("park", "refused", "walk_not_served")]

    def test_only_a_home_or_a_park_is_walked(self, printer, servers):
        for verb in ("wipe", "purge", "print"):
            _refused("INVALID_ARGUMENT", printer, servers, verb, 1, plate_clear=True)


class TestFirmware:
    @pytest.mark.parametrize("version", [None, "", "   "])
    def test_a_printer_with_no_version_is_not_walked(self, printer, servers, version):
        printer.firmware = version
        _refused("FIRMWARE_UNKNOWN", printer, servers, "park", 1, plate_clear=True)
        assert servers.asks == []

    def test_a_version_that_is_not_one_never_leaves(self, printer, servers):
        printer.firmware = "10.0.0.51"
        _step(printer, 1)
        assert "firmware" not in servers.asks[0]["walk"]


class TestWhenAStepGoesWrong:
    def test_a_fault_ends_the_walk_is_counted_and_is_reported(self, printer, servers, outcomes, monkeypatch):
        from kiln.printers import motion_plan

        _step(printer, 1)
        monkeypatch.setattr(motion_plan, "_watch_for_fault", lambda adapter, before, watch: ("0300_0D00_0001_0001", "hms"))
        out = _step(printer, 2)
        assert out["success"] is False and out["error_code"] == "0300_0D00_0001_0001" and out["reported"] is True
        assert "do not send another step" in out["message"]
        assert ("park", "fault", "0300_0D00_0001_0001") in outcomes
        (report,) = servers.reported
        assert report["problem"] == {"step": 2, "kind": "fault", "code": "0300_0D00_0001_0001"}
        assert report["firmware"] == FIRMWARE and report["machine_reports"] == MODEL
        # ... and the walk does not carry on from where it faulted.
        monkeypatch.setattr(motion_plan, "_watch_for_fault", lambda adapter, before, watch: None)
        with pytest.raises(walk.WalkRefused) as caught:
            _step(printer, 3)
        assert caught.value.code == "STEP_OUT_OF_ORDER"

    def test_a_step_the_printer_refuses_is_a_fault_too(self, printer, servers, outcomes):
        printer.verdict = False
        out = _step(printer, 1)
        assert out["success"] is False and out["error_code"] == "rejected" and servers.reported[0]["problem"]["code"] == "rejected"
        assert ("park", "fault", "rejected") in outcomes

    def test_the_persons_word_that_it_went_wrong_ends_it_and_is_reported(self, printer, servers, outcomes):
        _step(printer, 1)
        out = walk.walk_went_wrong(printer, "park", 1)
        assert out["reported"] is True and "withdrawn for everyone" in out["message"]
        assert servers.reported[0]["problem"] == {"step": 1, "kind": "person", "code": "person"}
        assert ("park", "fault", "person") in outcomes and len(printer.sent) == 1
        with pytest.raises(walk.WalkRefused) as caught:
            _step(printer, 2)
        assert caught.value.code == "STEP_OUT_OF_ORDER"

    def test_a_report_that_cannot_be_sent_says_so_and_never_raises(self, printer, monkeypatch, outcomes):
        import kiln.server as srv

        def down(tool, **kw):
            raise OSError("offline")

        monkeypatch.setattr(srv, "_pro_api_call", down)
        out = walk.walk_went_wrong(printer, "park", 2)
        assert out["reported"] is False and "could not be told" in out["message"]


class TestNothingHoldsThePrinter:
    def test_a_walk_reaches_for_nothing_that_controls_the_printer(self, printer, servers):
        for number in (1, 2, 3):
            _step(printer, number)
        walk.walk_went_wrong(printer, "park", 3)
        walk.walk_offer(printer, "home")
        controls = ("lock", "hold", "pause", "cancel", "resume", "stop", "engage", "temp", "heat", "start", "print")
        touched = sorted({name for name in printer.reached if any(word in name.lower() for word in controls)})
        assert touched == [], f"a walk reached for {touched} on the printer"

    def test_a_walk_in_progress_leaves_the_engagement_record_alone(self, printer, servers):
        from kiln.printers import engagement

        before = engagement.check_command(printer, "cancel_print")
        _step(printer, 1)
        assert engagement.check_command(printer, "cancel_print") == before
        assert engagement.check_command(printer, "emergency_stop") == engagement.check_command(printer, "pause_print")
