"""A pre-flight that runs into the one-printer rule hands the person its words.

Below the fleet tier Kiln works with one printer at a time, and a status read
on a second machine is refused with a refusal written for a person: what Kiln
is doing, what they can do now, the tier once.  Pre-flight reads that status,
and used to wrap the refusal as "Failed to run preflight check: ... Check that
the printer is online", which start_print then dropped for "Pre-flight checks
failed ... set KILN_SKIP_PREFLIGHT=1".  Pinned here: the refusal reaches the
person whole through pre-flight and start_print, every other pre-flight
failure keeps today's words exactly, and nothing that was refused is allowed.
"""

from __future__ import annotations

import pytest

import kiln.server as srv
from kiln import hardware_stops as hs
from kiln.printers import engagement
from kiln.printers.base import PrinterError, PrinterStatus
from kiln.registry import PrinterRegistry
from tests.test_a_machine_at_a_stop_is_not_printing import _Machine
from tests.test_hardware_stops import _job_file


def _tool(fn):
    return getattr(fn, "fn", fn)


@pytest.fixture
def bench(monkeypatch, tmp_path):
    """voron engaged and paused at its planned stop; ender idle beside it."""

    def _build(*, plan=True, engaged=True):
        voron = _Machine("AAA111", PrinterStatus.PAUSED, layer=29)
        ender = _Machine("BBB222", file=None)
        if plan:
            hs.note_print_started(voron, _job_file(tmp_path))
        registry = PrinterRegistry()
        registry.register("voron", voron)
        registry.register("ender", ender)
        monkeypatch.setattr("kiln.registry.get_registry", lambda: registry)
        monkeypatch.setattr(srv, "_get_registry", lambda: registry)
        monkeypatch.setattr(engagement, "_multi_machine_tier", lambda: False)
        monkeypatch.setenv("KILN_SKIP_PREVIEW_GATE", "1")
        monkeypatch.setattr(srv, "_tool_limiter", srv._ToolRateLimiter())  # each test starts fresh
        if engaged:
            engagement.engage(voron, voron.get_job(), reason="started", label="voron")
        return voron, ender

    return _build


def _start(name="ender"):
    return _tool(srv.start_print)("gasket.gcode", printer_name=name)


class TestPreflight:
    def test_it_answers_with_the_refusal_not_a_connection_fault(self, bench):
        bench()
        pf = _tool(srv.preflight_check)(printer_name="ender")
        assert pf["success"] is False and pf["ready"] is False
        assert pf["error"]["code"] == "TIER_SINGLE_PRINTER_LIMIT"
        message = pf["error"]["message"]
        assert message.startswith("At a planned hardware stop, voron has paused, still hot")
        assert "online" not in message and "KILN_PRINTER_HOST" not in message
        assert message.lower().count("business") == 1
        assert pf["summary"] == message
        assert pf["refusal"]["code"] == "TIER_SINGLE_PRINTER_LIMIT"

    def test_any_other_printer_error_keeps_todays_words(self, bench, monkeypatch):
        _, ender = bench(engaged=False)

        def _boom():
            raise PrinterError("socket closed")

        monkeypatch.setattr(ender, "get_state", _boom)
        pf = _tool(srv.preflight_check)(printer_name="ender")
        assert pf["error"]["message"] == (
            "Failed to run preflight check: socket closed. "
            "Check that the printer is online and KILN_PRINTER_HOST is correct."
        )
        assert pf["error"]["code"] == "ERROR"
        assert "refusal" not in pf


class TestStartPrint:
    def test_the_person_reads_the_refusal_through_the_tool(self, bench):
        bench()
        result = _start()
        assert result["success"] is False
        assert result["error"]["code"] == "PREFLIGHT_FAILED"
        message = result["error"]["message"]
        assert message.startswith("At a planned hardware stop, voron has paused, still hot")
        assert 'resume_print(printer_name="voron", hardware_confirmed=true)' in message
        assert "KILN_SKIP_PREFLIGHT" not in message
        assert "Pre-flight checks failed" not in message
        assert message.lower().count("business") == 1
        assert result["preflight"]["refusal"]["code"] == "TIER_SINGLE_PRINTER_LIMIT"

    def test_without_a_plan_it_is_the_ordinary_engagement_refusal(self, bench):
        bench(plan=False)
        message = _start()["error"]["message"]
        assert message.startswith("Kiln is working with voron right now, and with one printer at a time")
        assert "hand_back_printer" in message
        assert "KILN_SKIP_PREFLIGHT" not in message

    def test_a_failed_check_keeps_todays_words(self, bench, monkeypatch):
        _, ender = bench(engaged=False)

        def _boom():
            raise PrinterError("socket closed")

        monkeypatch.setattr(ender, "get_state", _boom)
        result = _start()
        assert result["error"]["code"] == "PREFLIGHT_FAILED"
        assert result["error"]["message"] == (
            "Pre-flight checks failed\n\nTo bypass pre-flight checks (advanced users only), set KILN_SKIP_PREFLIGHT=1."
        )

    def test_skipping_preflight_still_refuses_the_second_machine(self, bench, monkeypatch):
        bench()
        monkeypatch.setenv("KILN_SKIP_PREFLIGHT", "1")
        result = _start()
        assert result["success"] is False
        assert "preflight" not in result
        assert "waiting for you to put in" in result["message"]  # the start gate's own words


class TestTheOtherPreflightDoorsCarryTheSummary:
    """Every other door prints ``pf["summary"]``; the refusal now fills it."""

    def test_the_summary_is_the_refusal(self, bench):
        bench()
        pf = _tool(srv.preflight_check)(printer_name="ender")
        door = srv._error_dict(pf.get("summary", "Pre-flight checks failed"), code="PREFLIGHT_FAILED")
        assert door["error"]["message"].startswith("At a planned hardware stop")
