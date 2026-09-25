"""A fault the printer has stopped for reaches the agent, whatever it polls.

Measured on a Bambu A1, 2026-09-24 ~15:15 PDT.  At the first colour change
the printer raised 1200-8015 ("Failed to pull out the filament from the
toolhead") beside HMS 1200-1000-0002-0002 (AMS lite slot 1 motor
overloaded) and PAUSED.  Kiln's watchdog was attached and running, and
reported ``red_flags: 0`` -- correctly, by its own rule: a paused reading
is the firmware having acted, and an emergency stop on top of a pause is
the thing that rule exists to prevent.  But "stops nothing" had become
"says nothing".  The agent was polling ``ams_status`` for the colour switch
(``tray_now``), which carries no fault line, so it learned of the pause
from the person at the machine.

MCP gives a server no way to push a message into the model's context; the
results of the calls the agent makes are the only channel.  So the fault
now has one builder (:func:`kiln.printers.base.fault_banner`) and three
readers of it: the watchdog's yellow ``fault_needs_person`` flag, the
``kiln_watch`` watchdog block, and a result hook that puts the banner on
EVERY tool result while a watched printer is faulted.  No per-tool branch,
because the door the agent knocks on is never the one you fixed.
"""

from __future__ import annotations

import types
from types import SimpleNamespace

import pytest

from kiln import watch_state
from kiln.printers.base import PrinterState, PrinterStatus, fault_banner

#: 1200-8015 as the wire carries it: ``0x12008015``.
PULL_OUT_FAILED = 0x12008015
PULL_OUT_FAILED_CODE = "1200-8015"
PULL_OUT_FAILED_TEXT = "Failed to pull out the filament from the toolhead."
#: The HMS entry beside it, in the screen's four-group spelling.
MOTOR_OVERLOADED_CODE = "1200-1000-0002-0002"


def _todays_faults() -> list[dict]:
    return [
        {
            "code": PULL_OUT_FAILED_CODE,
            "kind": "print_error",
            "raw": {"print_error": PULL_OUT_FAILED},
            "screen_text": PULL_OUT_FAILED_TEXT,
            "source": "bambu_hms_service",
            "reading": "A filament operation failed.",
        },
        {
            "code": MOTOR_OVERLOADED_CODE,
            "kind": "hms",
            "raw": {"attr": 0x12001000, "code": 0x00020002},
            "reading": "The AMS lite slot 1 motor is overloaded.",
        },
    ]


def _paused_with_todays_fault() -> PrinterState:
    """The reading the A1 produced: paused underneath, the fault as headline."""
    return PrinterState(
        connected=True,
        state=PrinterStatus.PAUSED,
        tool_temp_actual=220.0,
        tool_temp_target=220.0,
        print_error=PULL_OUT_FAILED,
        faults=_todays_faults(),
    )


# ---------------------------------------------------------------------------
# The one builder
# ---------------------------------------------------------------------------


class TestFaultBanner:
    def test_no_fault_is_no_banner(self) -> None:
        clean = PrinterState(connected=True, state=PrinterStatus.PRINTING)
        assert fault_banner(clean) is None
        assert fault_banner(None) is None
        assert fault_banner({"state": "printing"}) is None

    def test_todays_pause_needs_a_person_and_says_so(self) -> None:
        state = _paused_with_todays_fault()
        assert state.state is PrinterStatus.ERROR  # the headline, live

        banner = fault_banner(state, printer_name="default")

        assert banner is not None
        assert banner["code"] == PULL_OUT_FAILED_CODE
        assert banner["codes"] == [PULL_OUT_FAILED_CODE, MOTOR_OVERLOADED_CODE]
        assert banner["state"] == "paused"
        assert banner["needs_person"] is True
        assert banner["screen_text"] == PULL_OUT_FAILED_TEXT
        assert banner["printer_name"] == "default"
        note = banner["note"]
        assert note.startswith("PRINTER FAULT on default: 1200-8015: Failed to pull out")
        assert "paused" in note and "person at the machine" in note
        assert "polling will not clear it" in note
        # What clears it is the shared remedy sentence, not a retyped one.
        assert "clear_printer_error" in banner["what_to_do"]
        assert banner["lines"][0] == f"{PULL_OUT_FAILED_CODE}: {PULL_OUT_FAILED_TEXT}"

    def test_a_fault_the_printer_prints_through_does_not_call_for_a_person(self) -> None:
        state = PrinterState(
            connected=True,
            state=PrinterStatus.PRINTING,
            print_error=PULL_OUT_FAILED,
            faults=_todays_faults(),
        )
        banner = fault_banner(state)
        assert banner is not None
        assert banner["state"] == "printing"
        assert banner["needs_person"] is False
        assert "still printing" in banner["note"]

    def test_a_serialised_reading_is_read_through_its_headline(self) -> None:
        """The dict twin -- what ``printer_status`` hands a remote reader."""
        row = _paused_with_todays_fault().to_dict()
        assert row["state"] == "error" and row["last_known_state"] == "paused"

        banner = fault_banner(row, printer_name="garage")

        assert banner["state"] == "paused"
        assert banner["needs_person"] is True
        assert banner["code"] == PULL_OUT_FAILED_CODE

    def test_a_bare_code_with_no_composed_faults_still_gets_a_banner(self) -> None:
        """An adapter that composes no ``faults`` list, or a test double."""
        duck = SimpleNamespace(state="paused", print_error=PULL_OUT_FAILED)
        banner = fault_banner(duck)
        assert banner["code"] == PULL_OUT_FAILED_CODE
        assert banner["needs_person"] is True
        assert banner["screen_text"] is None
        assert banner["note"].startswith("PRINTER FAULT: 1200-8015.")


# ---------------------------------------------------------------------------
# The kiln_watch block and the result hook
# ---------------------------------------------------------------------------


class _Dog:
    """A watchdog double: running or not, holding a reading or not."""

    _poll_interval = 2.5

    def __init__(self, *, running: bool, reading=None):
        self._running = running
        self._reading = reading

    def status(self):
        return {"running": self._running, "red_flags": [], "yellow_flags": []}

    def current_fault(self, printer_name=None):
        return fault_banner(self._reading, printer_name=printer_name)


class _Result:
    """Stands in for a CallToolResult: the fields the hook reads and writes."""

    def __init__(self, structured=None, is_error=False, text=None):
        self.structuredContent = structured
        self.isError = is_error
        self.content = [types.SimpleNamespace(type="text", text=text)] if text else []


@pytest.fixture()
def registry(monkeypatch):
    from kiln import server

    monkeypatch.setattr(server, "_print_watchdogs", {})
    monkeypatch.setattr(server, "_watchers", {})
    monkeypatch.setattr(server, "_resolve_effective_printer_name", lambda n=None: n or "default")
    monkeypatch.setattr(server, "_pro_bridge", lambda: None)
    from kiln import print_health_monitor as phm

    monkeypatch.setattr(
        phm,
        "get_print_health_monitor",
        lambda: SimpleNamespace(list_sessions=lambda **kw: [], _background_monitors={}),
    )
    return server._print_watchdogs


class TestTheWatchBlock:
    def test_the_watchdog_block_carries_the_fault(self, registry) -> None:
        registry["default"] = _Dog(running=True, reading=_paused_with_todays_fault())

        block = watch_state.kiln_watch_state("default")["watchdog"]

        assert block["attached"] and block["running"]
        assert block["fault"]["code"] == PULL_OUT_FAILED_CODE
        assert block["fault"]["needs_person"] is True
        assert block["fault"]["printer_name"] == "default"

    def test_no_fault_no_key(self, registry) -> None:
        registry["default"] = _Dog(running=True, reading=None)
        assert "fault" not in watch_state.kiln_watch_state("default")["watchdog"]

    def test_a_double_without_the_reader_is_still_a_watchdog(self, registry) -> None:
        registry["default"] = SimpleNamespace(
            status=lambda: {"running": True, "red_flags": [], "yellow_flags": []}
        )
        block = watch_state.kiln_watch_state("default")["watchdog"]
        assert block["attached"] and "fault" not in block

    def test_the_yellow_rule_has_words(self) -> None:
        words = watch_state._watcher_words()["watchdog"]["yellow"]
        assert words["fault_needs_person"] == "a fault the printer has stopped for"


class TestTheBannerRidesEveryResult:
    def test_ams_status_polled_during_the_pause_sees_the_fault(self, registry) -> None:
        """The door the agent was actually knocking on."""
        registry["default"] = _Dog(running=True, reading=_paused_with_todays_fault())
        result = _Result(
            structured={"success": True, "tray_now": "255"},
            text='{"success": true, "tray_now": "255"}',
        )

        watch_state._attach_fault_banner(result, None, "ams_status")

        banner = result.structuredContent[watch_state.RESULT_FAULT_KEY]
        assert banner["code"] == PULL_OUT_FAILED_CODE
        assert banner["printer_name"] == "default"
        assert banner["needs_person"] is True
        # The tool's own answer survives beside it.
        assert result.structuredContent["tray_now"] == "255"
        # And a host that shows text sees it too, as its own block.
        texts = [getattr(b, "text", "") for b in result.content]
        assert any(t.startswith("PRINTER FAULT on default: 1200-8015") for t in texts)

    def test_a_result_with_only_text_is_seeded_then_carried(self, registry) -> None:
        registry["default"] = _Dog(running=True, reading=_paused_with_todays_fault())
        result = _Result(text='{"success": true, "designs": []}')

        watch_state._attach_fault_banner(result, None, "list_designs")

        assert result.structuredContent["designs"] == []
        assert result.structuredContent[watch_state.RESULT_FAULT_KEY]["code"] == PULL_OUT_FAILED_CODE

    def test_no_fault_leaves_the_result_untouched(self, registry) -> None:
        registry["default"] = _Dog(running=True, reading=None)
        result = _Result(structured={"success": True}, text='{"success": true}')

        watch_state._attach_fault_banner(result, None, "ams_status")

        assert result.structuredContent == {"success": True}
        assert len(result.content) == 1

    def test_the_hosted_server_never_carries_a_banner(self, registry, monkeypatch) -> None:
        """One process serves every tenant there, and its watchdog registry
        is nobody's printer: the guard is explicit, like the update nudge's,
        not a property of the registry happening to be empty."""
        registry["default"] = _Dog(running=True, reading=_paused_with_todays_fault())
        monkeypatch.setenv("KILN_HOSTED_MULTITENANT", "1")
        result = _Result(structured={"success": True}, text='{"success": true}')

        watch_state._attach_fault_banner(result, None, "ams_status")

        assert result.structuredContent == {"success": True}
        assert len(result.content) == 1

    def test_an_error_result_is_left_alone(self, registry) -> None:
        registry["default"] = _Dog(running=True, reading=_paused_with_todays_fault())
        result = _Result(structured={"success": False}, is_error=True, text="boom")

        watch_state._attach_fault_banner(result, None, "ams_status")

        assert watch_state.RESULT_FAULT_KEY not in result.structuredContent
        assert len(result.content) == 1

    def test_a_watchdog_that_has_stopped_holds_no_fact_about_the_machine(self, registry) -> None:
        registry["default"] = _Dog(running=False, reading=_paused_with_todays_fault())
        result = _Result(structured={"success": True}, text='{"success": true}')

        watch_state._attach_fault_banner(result, None, "ams_status")

        assert watch_state.RESULT_FAULT_KEY not in result.structuredContent
        assert watch_state.watched_printer_faults() == []

    def test_a_result_already_carrying_the_key_is_not_doubled(self, registry) -> None:
        registry["default"] = _Dog(running=True, reading=_paused_with_todays_fault())
        result = _Result(
            structured={"success": True, watch_state.RESULT_FAULT_KEY: {"code": "mine"}},
            text='{"success": true}',
        )

        watch_state._attach_fault_banner(result, None, "printer_status")

        assert result.structuredContent[watch_state.RESULT_FAULT_KEY] == {"code": "mine"}
        assert len(result.content) == 1

    def test_two_faulted_printers_are_both_named(self, registry) -> None:
        registry["a1"] = _Dog(running=True, reading=_paused_with_todays_fault())
        registry["x1c"] = _Dog(running=True, reading=_paused_with_todays_fault())
        result = _Result(structured={"success": True}, text='{"success": true}')

        watch_state._attach_fault_banner(result, None, "list_designs")

        names = {f["printer_name"] for f in result.structuredContent[watch_state.RESULT_FAULT_KEY]}
        assert names == {"a1", "x1c"}

    def test_the_hook_installs_on_a_real_server(self) -> None:
        from mcp.server.fastmcp import FastMCP

        mcp = FastMCP("fault-banner-test")
        assert watch_state.install_fault_banner(mcp) is True
        assert watch_state.install_fault_banner(mcp) is False  # once per process


# ---------------------------------------------------------------------------
# Every door that starts or follows a print says how a fault will arrive
# ---------------------------------------------------------------------------


class TestTheDoorsSayHowAFaultArrives:
    def test_ams_status_says_it_carries_no_fault_line(self) -> None:
        from kiln import server

        doc = server.ams_status.__doc__ or ""
        assert "no fault line" in doc
        assert "printer_fault" in doc and 'printer_status(detail="lite")' in doc

    def test_the_skill_manifest_names_the_poll_and_the_banner(self) -> None:
        from kiln.skill_manifest import SkillManifest

        manifest = SkillManifest()
        rules = " ".join(manifest.agent_rules)
        assert "printer_fault" in rules and 'printer_status(detail="lite")' in rules
        steps = " ".join(manifest.workflows["monitor_active_print"])
        assert "printer_fault" in steps
