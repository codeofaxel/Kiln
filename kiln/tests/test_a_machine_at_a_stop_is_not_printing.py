"""A printer paused at a planned hardware stop is waiting for hands, not printing.

Below the fleet tier Kiln runs one printer at a time, so a second print is
refused while the first machine is busy -- and a machine paused for a nut or a
magnet counts as busy.  The refusal used to call that machine "printing",
which tells someone standing at a hot, stopped printer that it is running.
Pinned here: both refusals that name the busy machine (the start gate and the
engagement gate) say it is waiting, hot, for what, and how to tend to it; the
tier comes once, last; every uncertainty keeps the old words exactly; and the
words never change what is allowed or refused.
"""

from __future__ import annotations

import time

import pytest

from kiln import hardware_stops as hs
from kiln.printers import engagement
from kiln.printers.base import (
    JobProgress,
    PrinterAdapter,
    PrinterCapabilities,
    PrinterState,
    PrinterStatus,
    PrintResult,
)
from kiln.printers.print_gate import _concurrent_fleet_verdict
from tests.test_hardware_stops import JOB, _job_file
from tests.test_printer_identity_and_fleet_gate import fleet  # noqa: F401 -- fixture

OLD_REASON = "Kiln runs one printer at a time on this plan, and voron is already printing."
OLD_HINT = (
    "Wait for it to finish, or start it after this one. "
    "Kiln Business runs your printers in parallel — https://kiln3d.com/pricing"
)


class _Machine(PrinterAdapter):
    def __init__(self, serial: str, status=PrinterStatus.IDLE, *, layer=None, file=JOB) -> None:
        self.serial = serial
        self.host = ""
        self.status = status
        self.layer = layer
        self.file = file
        self.job_error: Exception | None = None
        self.job_delay = 0.0
        self._capabilities = PrinterCapabilities()

    @property
    def name(self) -> str:
        return "moonraker"

    @property
    def capabilities(self) -> PrinterCapabilities:
        return self._capabilities

    def get_state(self) -> PrinterState:
        return PrinterState(connected=True, state=self.status)

    def get_job(self) -> JobProgress:
        if self.job_delay:
            time.sleep(self.job_delay)
        if self.job_error is not None:
            raise self.job_error
        return JobProgress(file_name=self.file, current_layer=self.layer)

    def _start_print_impl(self, file_name, **kwargs) -> PrintResult:
        return PrintResult(success=True, message="Started.")


_Machine.__abstractmethods__ = frozenset()


@pytest.fixture
def bench(fleet, tmp_path):  # noqa: F811 -- the imported fixture
    """voron paused at its first planned stop (layer 29), ender idle beside it."""

    def _build(*, plan=True, status=PrinterStatus.PAUSED, layer=29, extra=None):
        voron = _Machine("AAA111", status, layer=layer)
        ender = _Machine("BBB222", file=None)
        if plan:
            hs.note_print_started(voron, _job_file(tmp_path))
        machines = {"voron": voron, "ender": ender, **(extra or {})}
        fleet(machines, cap=1)
        return voron, ender

    return _build


def _shape(verdict):
    """What a verdict DECIDES, with its words left out."""
    if verdict is None:
        return None
    return {k: v for k, v in verdict.items() if k in ("blocked", "code", "ok")}


class TestTheStartGate:
    def test_a_machine_at_a_stop_is_not_called_printing(self, bench):
        _, ender = bench()
        verdict = _concurrent_fleet_verdict(ender)
        assert "already printing" not in verdict["reason"]
        assert "waiting for you to put in 2x M3 nut in S1, S2" in verdict["reason"]
        assert "still hot" in verdict["reason"]
        assert "finished or cancelled" in verdict["reason"]

    def test_it_says_how_to_tend_to_it_then_the_tier_once_last(self, bench):
        _, ender = bench()
        verdict = _concurrent_fleet_verdict(ender)
        hint = verdict["override_hint"]
        assert 'resume_print(printer_name="voron", hardware_confirmed=true)' in hint
        assert "cancel that print" in hint
        message = f"{verdict['reason']} {hint}"
        assert message.lower().count("business") == 1, message
        assert message.index("hardware_confirmed") < message.index("Kiln Business")
        assert hint.endswith("https://kiln3d.com/pricing")

    def test_the_rendered_nudge_carries_the_same_story(self, bench):
        _, ender = bench()
        block = _concurrent_fleet_verdict(ender)["upgrade_nudge"]
        assert "has not started" in block["free_included"]
        assert "hardware_confirmed=true" in block["free_included"]
        assert "already running" not in block["headline"]
        assert block["display_text"].lower().count("business") == 1

    def test_without_a_layer_counter_it_says_most_likely(self, bench):
        _, ender = bench(layer=None)
        verdict = _concurrent_fleet_verdict(ender)
        assert "most likely waiting for you to put in 2x M3 nut" in verdict["reason"]

    def test_the_person_reads_it_at_the_adapter_door(self, bench):
        _, ender = bench()
        result = ender.start_print("gasket.gcode")
        assert result.success is False
        assert "waiting for you to put in" in result.message
        assert "already printing" not in result.message


class TestEveryUncertaintyKeepsTodaysWords:
    @pytest.mark.parametrize(
        ("plan", "status", "layer"),
        [
            (False, PrinterStatus.PAUSED, 29),  # no plan on record
            (True, PrinterStatus.PAUSED, 10),  # paused, but not at a stop
            (True, PrinterStatus.PRINTING, 20),  # really printing
        ],
        ids=["no_plan", "paused_elsewhere", "printing"],
    )
    def test_the_old_words_exactly(self, bench, plan, status, layer):
        _, ender = bench(plan=plan, status=status, layer=layer)
        verdict = _concurrent_fleet_verdict(ender)
        assert verdict["reason"] == OLD_REASON
        assert verdict["override_hint"] == OLD_HINT

    def test_an_unreadable_printer_keeps_the_old_words(self, bench):
        voron, ender = bench()
        voron.job_error = OSError("printer went quiet")
        assert _concurrent_fleet_verdict(ender)["reason"] == OLD_REASON

    def test_two_busy_machines_keep_the_old_words(self, bench, fleet):  # noqa: F811
        other = _Machine("CCC333", PrinterStatus.PRINTING)
        _, ender = bench(extra={"prusa": other})
        verdict = _concurrent_fleet_verdict(ender)
        assert "already printing" in verdict["reason"]
        assert "hardware_confirmed" not in verdict["override_hint"]

    def test_a_silent_printer_costs_the_bound_not_the_refusal(self, bench, monkeypatch):
        voron, ender = bench()
        voron.job_delay = 3.0
        monkeypatch.setattr(hs, "PEER_LOOK_TIMEOUT_S", 0.2)
        started = time.monotonic()
        verdict = _concurrent_fleet_verdict(ender)
        assert time.monotonic() - started < 2.0
        assert verdict["reason"] == OLD_REASON


class TestTheWordsNeverChangeTheVerdict:
    @pytest.mark.parametrize("status", [PrinterStatus.PAUSED, PrinterStatus.PRINTING, PrinterStatus.IDLE])
    @pytest.mark.parametrize("cap", [1, 50, None])
    def test_same_verdict_with_and_without_a_plan(self, fleet, tmp_path, status, cap):  # noqa: F811
        verdicts = []
        for plan in (True, False):
            voron = _Machine("AAA111" if plan else "AAA112", status, layer=29)
            ender = _Machine("BBB222", file=None)
            if plan:
                hs.note_print_started(voron, _job_file(tmp_path))
            fleet({"voron": voron, "ender": ender}, cap=cap)
            verdicts.append(_shape(_concurrent_fleet_verdict(ender)))
        assert verdicts[0] == verdicts[1]

    def test_the_engagement_gate_decides_the_same_with_and_without_a_plan(self, bench):
        voron, ender = bench()
        engagement.engage(voron, voron.get_job(), reason="started", label="voron")
        with_plan = {a: _shape(engagement.check_command(ender, a)) for a in sorted(engagement.GATED_ACTIONS)}
        hs.note_print_started(voron, "plain.gcode")  # a start with no plan clears the record
        assert not hs.has_plan(voron)
        engagement._verify_cache.clear()
        without = {a: _shape(engagement.check_command(ender, a)) for a in sorted(engagement.GATED_ACTIONS)}
        assert with_plan == without
        assert all(v == {"blocked": True, "code": "TIER_SINGLE_PRINTER_LIMIT"} for v in with_plan.values())


class TestTheEngagementGate:
    @pytest.fixture(autouse=True)
    def _capped(self, monkeypatch):
        monkeypatch.setattr(engagement, "_multi_machine_tier", lambda: False)

    def _refusal(self, bench, action="get_state", **kw):
        voron, ender = bench(**kw)
        engagement.engage(voron, voron.get_job(), reason="started", label="voron")
        return engagement.check_command(ender, action)

    def test_it_leads_with_the_machine_waiting_for_hands(self, bench):
        verdict = self._refusal(bench)
        assert verdict["reason"].startswith("At a planned hardware stop, voron has paused, still hot")
        assert "Kiln is not watching ender" in verdict["reason"]
        assert any("hardware_confirmed=true" in s for s in verdict["suggestions"])
        assert verdict["upgrade_nudge"]["display_text"].lower().count("business") == 1

    def test_emergency_stop_still_leads_with_stopping_it_yourself(self, bench):
        verdict = self._refusal(bench, action="emergency_stop")
        assert verdict["suggestions"][0].startswith("To stop")

    def test_without_a_plan_the_words_are_todays(self, bench):
        verdict = self._refusal(bench, plan=False)
        assert verdict["reason"] == (
            "Kiln is working with voron right now, and with one printer at a time on the free plan. "
            "Kiln is not watching ender. Nothing here is keeping an eye on it."
        )
        assert len(verdict["suggestions"]) == 1
