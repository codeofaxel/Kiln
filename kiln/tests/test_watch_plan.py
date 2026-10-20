"""Every start the printer took carries a watch plan, and the plan spends wisely.

A print start said "monitor_printer shows it from here" and nothing else, so
whether anyone watched depended on the person asking.  Now every start the
printer took or may have taken carries ``watch``: when to look, how to wait,
when to speak, what it costs, and how to turn it off.  A refusal carries
nothing -- there is nothing to watch.

A/B: ``test_a_started_print_carries_the_plan`` and its accepted twin fail on
the code before the plan (``KeyError: 'watch'``); the rest pin the shape.
"""

from __future__ import annotations

import math
import sys
import time
from types import SimpleNamespace

from kiln import watch_plan as wp
from kiln.print_start_verdict import resolve_print_start
from kiln.printers.base import PrinterState, PrinterStatus, PrintResult


class _Adapter:
    def __init__(self, *, result: PrintResult, status: PrinterStatus) -> None:
        self.name = "bench"
        self._printer_model = ""
        self._result = result
        self._status = status

    def get_state(self) -> PrinterState:
        return PrinterState(connected=True, state=self._status)


def _verdict(result: PrintResult, status: PrinterStatus) -> dict:
    a = _Adapter(result=result, status=status)
    return resolve_print_start(a, result, sent_at=time.monotonic(), file_name="part.3mf").to_dict()


# ---------------------------------------------------------------------------
# The plan rides every start the printer took
# ---------------------------------------------------------------------------


def test_a_started_print_carries_the_plan():
    out = _verdict(PrintResult(success=True, message="started"), PrinterStatus.PRINTING)
    assert out["print_start"] == "started"
    assert out["watch"]["default"] == "on"
    assert out["watch"]["wait_with"] == wp.LOCAL_WAIT_TOOL


def test_an_accepted_but_unconfirmed_start_carries_it_too():
    out = _verdict(PrintResult(success=True, message="sent"), PrinterStatus.IDLE)
    assert out["print_start"] == "accepted"
    assert "watch" in out


def test_a_refused_start_carries_no_plan():
    out = _verdict(
        PrintResult(success=False, message="refused", refused_before_send=True),
        PrinterStatus.IDLE,
    )
    assert out["success"] is False
    assert "watch" not in out


def test_a_start_the_printer_refuted_carries_no_plan():
    out = _verdict(PrintResult(success=False, message="failed"), PrinterStatus.ERROR)
    assert out["print_start"] == "failed"
    assert "watch" not in out


# ---------------------------------------------------------------------------
# No private package is asked in-process
# ---------------------------------------------------------------------------


def test_a_private_bridge_in_the_process_is_never_asked(monkeypatch):
    """A plan read from the part would be served from Kiln's servers; a
    plain install has no private package, so the start never reaches for one."""
    asked = []

    def door(**kw):
        asked.append(kw)
        return {"mode": "risk_mapped"}

    bridge = SimpleNamespace(pro_features=SimpleNamespace(watch_plan=door))
    monkeypatch.setitem(sys.modules, "kiln_pro", SimpleNamespace(bridge=bridge))
    monkeypatch.setitem(sys.modules, "kiln_pro.bridge", bridge)
    out = _verdict(PrintResult(success=True, message="started"), PrinterStatus.PRINTING)
    assert asked == []
    assert out["watch"]["mode"] == wp.MODE_EARLY_THEN_HANDOFF


# ---------------------------------------------------------------------------
# The floor spends where failures concentrate, and says the price of the rest
# ---------------------------------------------------------------------------


def test_an_unknown_length_is_treated_as_long():
    plan = wp.build_watch_plan()
    assert plan["mode"] == wp.MODE_EARLY_THEN_HANDOFF
    assert plan["watch_until_s"] == wp.EARLY_LAYERS_AT_S
    assert [c["at_s"] for c in plan["checkpoints"]] == [wp.FIRST_LAYER_AT_S, wp.EARLY_LAYERS_AT_S]
    assert all(c["camera"] for c in plan["checkpoints"])


def test_a_short_print_is_watched_to_the_end():
    plan = wp.build_watch_plan(estimated_s=25 * 60)
    assert plan["mode"] == wp.MODE_TO_THE_END
    assert plan["watch_until_s"] == 25 * 60
    ats = [c["at_s"] for c in plan["checkpoints"]]
    assert ats == sorted(ats) and ats[-1] == 25 * 60
    assert "until it finishes" in plan["say_once"]


def test_a_long_print_is_handed_off_after_the_early_window():
    plan = wp.build_watch_plan(estimated_s=6 * 3600)
    assert plan["mode"] == wp.MODE_EARLY_THEN_HANDOFF
    assert plan["watch_until_s"] == wp.EARLY_LAYERS_AT_S
    assert "6-hour" in plan["say_once"]
    assert "keep watching" in plan["say_once"]


def test_the_cost_is_the_wakeups_not_the_cadence():
    plan = wp.build_watch_plan(wait_s=150, estimated_s=20 * 60)
    assert plan["cost"]["wakeups"] == math.ceil(20 * 60 / 150)
    assert plan["cost"]["wakeups_per_hour_if_continuous"] == math.ceil(3600 / 150)
    assert "every 2 minutes" in wp.build_watch_plan(wait_s=150)["say_once"]


def test_a_hardware_stop_gets_a_look_before_at_and_after_it():
    plan = wp.build_watch_plan(estimated_s=6 * 3600, hardware_stops_s=[47 * 60])
    stop = 47 * 60
    looks = {c["at_s"]: c for c in plan["checkpoints"]}
    assert looks[stop - wp.STOP_APPROACH_S]["camera"] is False
    assert looks[stop]["camera"] is True
    after = looks[stop + wp.AFTER_RESUME_S]
    assert after["relative_to"] == "resume"
    assert plan["watch_until_s"] == stop + wp.AFTER_RESUME_S
    assert "47 minutes" in plan["say_once"]


def test_the_door_names_its_own_wait_tool():
    plan = wp.build_watch_plan(wait_with="watch_printer", wait_s=150)
    assert plan["wait_with"] == "watch_printer" and plan["wait_s"] == 150
    assert "watch_printer" in plan["how"]


def test_the_person_is_told_how_to_stop():
    for plan in (wp.build_watch_plan(), wp.build_watch_plan(estimated_s=600)):
        assert wp.STOP_PHRASE in plan["say_once"]
        assert plan["stop_phrase"] == wp.STOP_PHRASE


def test_the_agent_rule_says_watching_is_the_default():
    from kiln.skill_manifest import SkillManifest

    rule = [r for r in SkillManifest().agent_rules if "WATCH IT by default" in r]
    assert len(rule) == 1
    assert wp.LOCAL_WAIT_TOOL in rule[0] and wp.STOP_PHRASE in rule[0]
