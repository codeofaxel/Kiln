"""A home or a park learned with the person, through the doors they meet.

The walk itself (:mod:`kiln.printers.motion_walk`) holds the floor under
every step.  Pinned here is what a person and their agent meet:

* ``printer_bench(lesson="walk", move=...)``, the registered tool, runs a
  walk one step per call, and sends a step only on the person's yes;
* a "no" ends the walk and tells Kiln; a "later" is honoured; a refusal is
  said in its own sentence, and a fault ends the walk, never as a traceback;
* nothing on offer is said plainly, as an answer and never as an error;
* a home or park refusal offers a walk only when one is on offer, worded the
  same on every plan, and the doctor's line through the same helper;
* the funnel counts the walk like the other lessons.

Every step here is made up: the shape, never a sequence.
"""

from __future__ import annotations

import asyncio
import json
import sys

import pytest

from kiln import bench, plate_state
from kiln.printers import motion_walk as walk
from kiln.printers.base import PrinterInfo, PrinterState, PrinterStatus
from kiln.printers.command_verdict import CommandVerdict

MODEL = "bambu_p1s"
TOTAL = 3
OFFER = ("Printing is not affected. Kiln can learn this park on your {name} with you, one small step at a time, "
         "while you watch.")


class _Printer:
    """A cold, idle printer that proves its model, and has no camera."""

    def __init__(self) -> None:
        self._printer_model = MODEL
        self.serial = "01P00A000000001"
        self.name = "garage"
        self.host = "10.0.0.51"
        self.sent: list[list[str]] = []
        self.state = PrinterState(
            connected=True, state=PrinterStatus.IDLE,
            tool_temp_actual=24.0, tool_temp_target=0.0, bed_temp_actual=23.0, bed_temp_target=0.0,
        )

    def __getattr__(self, name: str):
        raise AttributeError(name)

    def declared_printer_model(self) -> str:
        return self._printer_model

    def get_printer_info(self) -> PrinterInfo:
        return PrinterInfo(model=MODEL, raw_model=MODEL)

    def get_identity_channels(self) -> dict[str, str]:
        return {"serial_prefix": MODEL, "firmware_product_name": MODEL}

    def get_state(self) -> PrinterState:
        return self.state

    def reported_firmware_version(self) -> str:
        return "01.07.00.00"

    def _snapshot_faults(self) -> set:
        return set()

    def send_gcode(self, lines: list[str]) -> CommandVerdict:
        self.sent.append(list(lines))
        return CommandVerdict.coerce(True, what="walk")


class _Servers:
    """Kiln's servers as a walk meets them: an offer, one step per ask, a report."""

    def __init__(self) -> None:
        self.asks: list[dict] = []
        self.reported: list[dict] = []
        self.offering = True
        self.refuse: dict | None = None
        self.lines = {1: ["G91", "G0 Z1 F100", "G90"], 2: ["G0 X1 F100"], 3: ["G0 Y1 F100"]}

    def __call__(self, tool: str, *, _asked_by_user: bool = True, **request):
        assert tool == "motion_plan"
        asked = request["walk"]
        self.asks.append(asked)
        if asked["op"] == "report":
            self.reported.append(asked)
            return {"walk": {"reported": True}}
        if asked["op"] == "offer":
            if not self.offering:
                return {"status": "error", "code": "NO_WALK", "error": "Kiln has no park to walk at the moment."}
            return {"walk": {"offered": True, "steps_total": TOTAL}}
        if self.refuse is not None:
            return dict(self.refuse)
        number = asked["step"]
        return {"walk": {
            "schema": walk.SCHEMA, "printer_id": request["printer_id"], "verb": request["verb"],
            "steps_total": TOTAL, "review": "reviewed", "sequence": "0" * 32,
            "step": {"number": number, "label": f"made-up step {number}", "you_will_see": "the head moves a little",
                     "stops_when": "It has moved", "gcode": list(self.lines[number]), "confirm": "Did it do that?",
                     "if_not": "Press the printer's own stop."},
            "stop": "The printer's own stop always works.",
            "next": None if number >= TOTAL else {"number": number + 1, "label": f"made-up step {number + 1}"},
        }}


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch, served_roster_records):
    monkeypatch.setenv("KILN_HOME", str(tmp_path / "kiln-home"))
    monkeypatch.setenv("KILN_TELEMETRY", "true")
    for name in list(sys.modules):
        if name == "kiln_pro" or name.startswith("kiln_pro."):
            monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setitem(sys.modules, "kiln_pro", None)
    monkeypatch.setattr(walk, "STEP_WATCH_S", 0.0)
    monkeypatch.setattr("kiln.outbound_guard._own_printer_values", lambda: [
        ("the printer's serial number", "01P00A000000001"), ("the printer's network address", "10.0.0.51"),
    ])
    walk._progress.clear()


@pytest.fixture
def funnel(tmp_path, monkeypatch):
    from kiln import daily_stats

    monkeypatch.setattr(daily_stats, "_STATS_PATH", tmp_path / "stats.json")
    return lambda: daily_stats.get_daily_stats()["bench_funnel"]


@pytest.fixture
def printer(monkeypatch):
    import kiln.server as srv

    machine = _Printer()
    monkeypatch.setattr(srv, "_check_auth", lambda *_a, **_k: None)
    monkeypatch.setattr(srv, "_resolve_control_target", lambda name: (machine, name or "garage"))
    monkeypatch.setattr(srv, "_emergency_latch_error", lambda *_a, **_k: None)
    plate_state.mark_clear(machine, "human")
    return machine


@pytest.fixture
def servers(monkeypatch):
    fake = _Servers()
    import kiln.server as srv

    monkeypatch.setattr(srv, "_pro_api_call", fake)
    return fake


@pytest.fixture
def bench_tool():
    """``printer_bench`` as an agent calls it: registered on a real server."""
    from kiln.mcp_compat import FastMCP, tool_result_blocks
    from kiln.plugins import printer_bench_tools

    mcp = FastMCP("bench")
    printer_bench_tools.plugin.register(mcp)

    def call(**arguments) -> dict:
        blocks = list(tool_result_blocks(asyncio.run(mcp.call_tool("printer_bench", arguments))))
        return json.loads("".join(b.text for b in blocks if b.type == "text"))

    return call


def _name() -> str:
    return bench._display_name(MODEL)


class TestAWalkThroughTheTool:
    def test_each_step_is_sent_only_on_the_persons_yes_and_the_end_says_what_was_learned(
            self, printer, servers, bench_tool, funnel):
        out = bench_tool(lesson="walk", move="park")
        assert out["step"] == "walk_intro" and out["options"] == ["yes", "later", "no"]
        assert out["ask"] == (
            f"Kiln can learn how your {_name()} parks its head with you, one small step at a time: it sends one "
            "small move, tells you what you should see, and sends the next only after you say the printer did "
            "exactly that. The printer stays cold the whole time, nothing heats or extrudes, and its own stop, "
            "pause and power switch work at every moment. It takes 3 small steps. Ready?")
        assert printer.sent == []
        out = bench_tool(answer="yes")
        assert out["step"] == "walk_plate" and printer.sent == []
        assert out["ask"] == ("Is the plate completely empty? Take everything off it first. When you say yes, "
                              "Kiln sends the first step.")
        out = bench_tool(answer="yes")
        assert printer.sent == [servers.lines[1]] and out["step"] == "walk_confirm"
        assert out["ask"] == ("Step 1 of 3, made-up step 1: the head moves a little. It stops when it has moved. "
                              "Did it do that? Say yes and Kiln sends step 2, made-up step 2; say no and the walk "
                              "stops here.")
        assert out["options"] == ["yes", "no"] and out["stop"] == "The printer's own stop always works."
        out = bench_tool(answer="something else")
        assert out["step"] == "walk_confirm" and len(printer.sent) == 1, "only a yes sends the next step"
        bench_tool(answer="yes")
        out = bench_tool(answer="yes")
        assert printer.sent == [servers.lines[n] for n in (1, 2, 3)]
        assert out["ask"].endswith("Did it do that? That was the last step: say yes if it did, or no if it did not.")
        out = bench_tool(answer="yes")
        assert out["step"] == "done" and len(printer.sent) == 3
        assert out["message"] == (f"That was the last step. Kiln learned the park on your {_name()} with you: all 3 "
                                  "steps did what Kiln said they would, and nothing else. Thank you.")
        assert out["learned"] == ["park: all 3 steps did what Kiln said they would"]
        steps = [a for a in servers.asks if a["op"] == "step"]
        assert [(a["step"], a["confirmed_previous"], a["plate_person_clear"]) for a in steps] == [
            (1, False, True), (2, True, True), (3, True, True)]
        assert funnel() == {"started|walk": 1, "finished|walk": 1, "learned|walk|session": 1}
        assert not bench.may_offer(bench.walk_key(bench.unit_of(printer), "park"), "motion_refusal")

    def test_no_ends_the_walk_and_tells_kiln(self, printer, servers, bench_tool, funnel):
        for answer in (None, "yes", "yes"):
            bench_tool(lesson="walk", move="park", answer=answer)
        out = bench_tool(answer="no")
        assert out["step"] == "stopped" and out["reported"] is True and len(printer.sent) == 1
        assert out["message"] == ("Thank you for saying so. The walk stops at step 1 and Kiln sends nothing more. "
                                  "Use the printer's own controls from here. Kiln was told what happened.")
        assert [r["problem"] for r in servers.reported] == [{"step": 1, "kind": "person", "code": "person"}]
        assert bench_tool(lesson="walk", move="park")["step"] == "walk_intro", "the walk ended; a new one starts over"
        assert "finished|walk" not in funnel()

    def test_later_is_honoured_and_never_offered_again_unasked(self, printer, servers, bench_tool, funnel):
        bench_tool(lesson="walk", move="park")
        out = bench_tool(answer="later")
        assert out["step"] == "later" and printer.sent == []
        assert "Kiln won't bring it up again on its own" in out["message"]
        assert funnel() == {"later|walk": 1}
        assert not bench.may_offer(bench.walk_key(bench.unit_of(printer), "park"), "motion_refusal")
        assert bench.may_offer(bench.walk_key(bench.unit_of(printer), "home"), "motion_refusal"), \
            "an answer about the park says nothing about the home"

    def test_nothing_on_offer_is_an_answer_not_an_error(self, printer, servers, bench_tool):
        servers.offering = False
        out = bench_tool(lesson="walk", move="park")
        assert out["success"] is True and "error" not in out and out["step"] == "not_on_offer"
        assert out["message"] == (f"Kiln has no step-by-step park on offer for your {_name()} right now, so there "
                                  "is nothing to start. Printing is not affected.")
        assert printer.sent == [] and [a["op"] for a in servers.asks] == ["offer"]

    def test_a_walk_needs_a_move(self, printer, servers, bench_tool):
        out = bench_tool(lesson="walk")
        assert out["success"] is False and out["error"]["code"] == "INVALID_ARGS" and servers.asks == []
        assert bench_tool(lesson="walk", move="wipe")["error"]["code"] == "INVALID_ARGS"


class TestWhenAStepIsNotSent:
    def test_a_refusal_is_said_in_its_own_sentence_and_try_again_resends(self, printer, servers, bench_tool):
        bench_tool(lesson="walk", move="park")
        bench_tool(answer="yes")
        printer.state.tool_temp_actual = 60.0
        out = bench_tool(answer="yes")
        assert out["refused"] == "NOT_COLD" and out["options"] == ["try again", "later"] and printer.sent == []
        assert out["ask"] == ("Nothing was sent. The nozzle reads 60 °C; a walk is done cold, at or below 45 °C. "
                              "Let it cool, then try again. Say try again when that is sorted, or later.")
        printer.state.tool_temp_actual = 24.0
        out = bench_tool(answer="try again")
        assert out["step"] == "walk_confirm" and printer.sent == [servers.lines[1]]

    def test_the_servers_refusal_is_said_as_they_said_it(self, printer, servers, bench_tool):
        bench_tool(lesson="walk", move="park")
        bench_tool(answer="yes")
        servers.refuse = {"status": "error", "code": "ACCOUNT_REQUIRED", "error": "Sign in to Kiln to walk this move."}
        out = bench_tool(answer="yes")
        assert out["refused"] == "ACCOUNT_REQUIRED" and printer.sent == []
        assert out["ask"].startswith("Nothing was sent. Sign in to Kiln to walk this move.")
        out = bench_tool(answer="later")
        assert out["step"] == "later" and printer.sent == []

    def test_a_fault_ends_the_walk_in_plain_words(self, printer, servers, bench_tool, monkeypatch):
        from kiln.printers import motion_plan

        monkeypatch.setattr(motion_plan, "_watch_for_fault", lambda adapter, before, watch: ("0300_0D00_0001_0001", "hms"))
        bench_tool(lesson="walk", move="park")
        bench_tool(answer="yes")
        out = bench_tool(answer="yes")
        assert out["success"] is True and out["step"] == "stopped" and out["fault"] == "0300_0D00_0001_0001"
        assert out["message"] == ("The printer reported a fault (0300_0D00_0001_0001) during step 1, so the walk "
                                  "stops here and Kiln sends nothing more. Use the printer's own controls from "
                                  "here. Kiln was told what happened.")
        assert "Traceback" not in json.dumps(out) and "walk_went_wrong" not in json.dumps(out)

    def test_a_latched_emergency_stop_sends_nothing(self, printer, servers, bench_tool, monkeypatch):
        import kiln.server as srv

        latched = {"success": False, "error": {"code": "E_STOP_LATCHED", "message": "Emergency latch is active."}}
        monkeypatch.setattr(srv, "_emergency_latch_error", lambda *_a, **_k: latched)
        bench_tool(lesson="walk", move="park")
        bench_tool(answer="yes")
        out = bench_tool(answer="yes")
        assert out["error"]["code"] == "E_STOP_LATCHED" and printer.sent == []
        assert not [a for a in servers.asks if a["op"] == "step"]


def _refusing(machine: _Printer) -> None:
    from kiln.printers.base import HomingUnsupported

    def refuse(**_kw):
        raise HomingUnsupported("Kiln has no verified motion sequence for this model, so it does not park it.")

    machine.park_head = refuse
    machine.home_axes = refuse


class TestTheRefusalOffersAWalk:
    @pytest.mark.parametrize("pro", [False, True])
    def test_a_park_refusal_offers_the_walk_on_offer_once_the_same_on_every_plan(
            self, printer, servers, funnel, monkeypatch, pro):
        import kiln.licensing as lic
        import kiln.server as srv
        from kiln.mcp_compat import FastMCP, tool_result_blocks
        from kiln.plugins import homing_tools

        monkeypatch.setattr(lic, "check_tier", lambda tier: (pro, "pro" if pro else "free"))
        monkeypatch.setattr(srv, "_check_rate_limit", lambda *_a, **_k: None)
        _refusing(printer)
        mcp = FastMCP("homing")
        homing_tools.plugin.register(mcp)
        blocks = tool_result_blocks(asyncio.run(mcp.call_tool("park_head", {"printer_name": "garage"})))
        out = json.loads("".join(b.text for b in blocks if b.type == "text"))
        assert out["error"]["code"] == "UNSUPPORTED"
        assert out["error"]["message"] == "Kiln has no verified motion sequence for this model, so it does not park it."
        assert out["learn"] == {"tool": "printer_bench", "lesson": "walk", "move": "park",
                                "sentence": OFFER.format(name=_name())}
        assert printer.sent == []
        again = homing_tools.run_home(printer_name="garage", action="park")
        assert again["error"]["code"] == "UNSUPPORTED" and "learn" not in again, "once in a week, not every refusal"
        assert funnel() == {"offered|walk|motion_refusal": 1}

    def test_no_walk_on_offer_and_the_refusal_stands_as_it_is(self, printer, servers, funnel, monkeypatch):
        from kiln.plugins import homing_tools

        servers.offering = False
        _refusing(printer)
        for action in ("home", "park"):
            out = homing_tools.run_home(printer_name="garage", action=action)
            assert out["error"]["code"] == "UNSUPPORTED" and "learn" not in out
            assert "printer_bench" not in json.dumps(out)
        assert funnel() == {}

    def test_the_doctor_line_offers_through_the_same_helper(self, printer, servers, funnel):
        from kiln.cli.main import _doctor_learn

        homing = {"name": "homing", "ok": True, "warn": True, "detail": "park_head refuses"}
        taught = _doctor_learn(printer, dict(homing), ("home", "park"))
        assert taught["learn"]["lesson"] == "walk" and taught["learn"]["move"] == "home"
        assert taught["detail"].endswith(taught["learn"]["sentence"])
        assert funnel() == {"offered|walk|setup_check": 1}
