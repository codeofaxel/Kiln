"""Handing a printer back is a way to move Kiln, never a way to work
several printers by turns.

Below the fleet plan Kiln works with one printer at a time and may go back
once to a print it stepped away from.  These go through real adapter
methods, with the slot emptied by a hand-back before every move, because
that is the path the return count has to hold on too.
"""

from __future__ import annotations

import pytest

from kiln.printers import engagement
from kiln.printers.base import PrinterEngagementError, PrinterStatus
from kiln.registry import PrinterRegistry
from tests.test_single_printer_engagement import _real_adapter_class


@pytest.fixture(autouse=True)
def _one_printer_plan(monkeypatch):
    monkeypatch.setattr(engagement, "_multi_machine_tier", lambda: False)


def _bench(monkeypatch, **printers):
    cls = _real_adapter_class()
    registry = PrinterRegistry()
    made = {}
    for index, (name, spec) in enumerate(printers.items()):
        state, job = spec
        made[name] = cls(f"SER{index}00{index}", state=state, job=job)
        registry.register(name, made[name])
    monkeypatch.setattr("kiln.registry.get_registry", lambda: registry)
    return made


PRINTING = PrinterStatus.PRINTING


def _refused(call) -> dict | None:
    try:
        call()
    except PrinterEngagementError as exc:
        return exc.verdict
    return None


def _move(frm, to, action="get_state") -> dict | None:
    """Hand *frm* back, then touch *to*: the verdict, or None when allowed."""
    engagement.hand_back(frm)
    return _refused(getattr(to, action))


class TestAlternatingByHandingBackRunsOut:
    def test_looking_at_two_running_prints_by_turns_stops(self, monkeypatch):
        bench = _bench(monkeypatch, a1=(PRINTING, "bracket.gcode"), garage=(PRINTING, "gasket.gcode"))
        a, b = bench["a1"], bench["garage"]

        a.get_state()                       # Kiln works with a1
        assert _move(a, b) is None          # over to garage
        assert _move(b, a) is None          # a1's one return
        assert _move(a, b) is None          # garage's one return
        verdict = _move(b, a)               # a1 again: spent

        assert verdict is not None and verdict["code"] == "TIER_SINGLE_PRINTER_LIMIT"
        assert verdict["returns_left"] == 0

    def test_three_running_prints_cannot_be_cycled_either(self, monkeypatch):
        bench = _bench(
            monkeypatch,
            a1=(PRINTING, "bracket.gcode"), garage=(PRINTING, "gasket.gcode"), office=(PRINTING, "knob.gcode"),
        )
        order = [bench["a1"], bench["garage"], bench["office"]]
        order[0].get_state()
        refusals = 0
        for step in range(1, 13):
            if _move(order[(step - 1) % 3], order[step % 3]) is not None:
                refusals += 1
        # One fresh look and one return each; after that only the printer
        # last worked with is taken up again.
        assert refusals >= 5

    def test_commands_do_not_roam_the_bench_once_the_slot_is_empty(self, monkeypatch):
        bench = _bench(monkeypatch, a1=(PRINTING, "bracket.gcode"), garage=(PRINTING, "gasket.gcode"))
        a, b = bench["a1"], bench["garage"]
        a.get_state()
        engagement.hand_back(a)

        # Eight commands, turn and turn about.  The first takes garage up;
        # each print has one return; after that the slot stops moving.
        turns = [_refused(m.pause_print) for m in (b, a, b, a, b, a, b, a)]
        assert turns[0] is None
        assert sum(verdict is not None for verdict in turns) >= 3


class TestWhatStaysFree:
    def test_one_printer_can_be_handed_back_and_taken_up_any_number_of_times(self, monkeypatch):
        bench = _bench(monkeypatch, a1=(PRINTING, "bracket.gcode"))
        a = bench["a1"]
        a.get_state()
        for _ in range(8):
            assert _move(a, a) is None
            assert _refused(a.pause_print) is None

    def test_the_printer_just_handed_back_is_never_shut_out(self, monkeypatch):
        """However the returns were spent, Kiln can always go on with the
        printer it was last working with: nobody is left with no printer."""
        bench = _bench(monkeypatch, a1=(PRINTING, "bracket.gcode"), garage=(PRINTING, "gasket.gcode"))
        a, b = bench["a1"], bench["garage"]
        a.get_state()
        _move(a, b)
        _move(b, a)
        _move(a, b)
        assert _move(b, a) is not None      # a1 is spent
        assert _refused(b.get_state) is None
        assert _refused(b.emergency_stop) is None
        assert engagement.current().machine == engagement.machine_id(b)

    def test_a_new_print_on_the_same_printer_starts_fresh(self, monkeypatch):
        bench = _bench(monkeypatch, a1=(PRINTING, "bracket.gcode"), garage=(PRINTING, "gasket.gcode"))
        a, b = bench["a1"], bench["garage"]
        a.get_state()
        _move(a, b)
        _move(b, a)
        _move(a, b)
        assert _move(b, a) is not None
        a._job = "second-part.gcode"
        assert _refused(a.get_state) is None

    def test_an_idle_spare_never_holds_kiln(self, monkeypatch):
        bench = _bench(monkeypatch, a1=(PRINTING, "bracket.gcode"), spare=(PrinterStatus.IDLE, ""))
        a, spare = bench["a1"], bench["spare"]
        for _ in range(4):
            assert _refused(lambda: spare.set_bed_temp(60)) is None
            assert _refused(spare.get_state) is None
            assert _refused(a.get_state) is None
            engagement.hand_back(a)

    def test_the_fleet_plan_moves_freely(self, monkeypatch):
        monkeypatch.setattr(engagement, "_multi_machine_tier", lambda: True)
        bench = _bench(monkeypatch, a1=(PRINTING, "bracket.gcode"), garage=(PRINTING, "gasket.gcode"))
        a, b = bench["a1"], bench["garage"]
        for _ in range(6):
            assert _refused(a.get_state) is None and _refused(b.pause_print) is None

    def test_a_print_that_cannot_be_read_is_allowed(self, monkeypatch):
        bench = _bench(monkeypatch, a1=(PRINTING, "bracket.gcode"), garage=(PRINTING, "gasket.gcode"))
        a, b = bench["a1"], bench["garage"]
        a.get_state()
        _move(a, b)
        _move(b, a)
        _move(a, b)
        engagement.hand_back(b)
        monkeypatch.setattr(type(a), "get_job", lambda self: (_ for _ in ()).throw(OSError("no answer")))
        assert _refused(a.pause_print) is None


class TestTheRefusalIsTrueWhenNothingIsEngaged:
    def test_it_names_both_printers_and_claims_no_printer_kiln_is_not_on(self, monkeypatch):
        bench = _bench(monkeypatch, a1=(PRINTING, "bracket.gcode"), garage=(PRINTING, "gasket.gcode"))
        a, b = bench["a1"], bench["garage"]
        a.get_state()
        _move(a, b)
        _move(b, a)
        _move(a, b)
        verdict = _move(b, a)

        words = engagement.refusal_words(verdict)
        assert "already come back to a1 once" in words
        assert "garage" in words
        assert "is working with garage now" not in words
        assert words.count("Kiln Business") == 1

    def test_emergency_stop_still_leads_with_the_printers_own_controls(self, monkeypatch):
        bench = _bench(monkeypatch, a1=(PRINTING, "bracket.gcode"), garage=(PRINTING, "gasket.gcode"))
        a, b = bench["a1"], bench["garage"]
        a.get_state()
        _move(a, b)
        _move(b, a)
        _move(a, b)
        verdict = _move(b, a, "emergency_stop")

        assert verdict is not None
        assert verdict["suggestions"][0].startswith("To stop a1 right now, use the printer's own controls")


def _spend_a1(a, b):
    """Leave a1's one return spent, garage handed back last, nothing engaged."""
    a.get_state()
    _move(a, b)
    _move(b, a)
    _move(a, b)
    assert _move(b, a) is not None


class TestNobodyIsLockedOut:
    def test_a_printer_whose_print_finished_is_free_even_if_it_still_names_that_job(self, monkeypatch):
        """Some printers go on reporting the finished job; idle is idle."""
        bench = _bench(monkeypatch, a1=(PRINTING, "bracket.gcode"), garage=(PRINTING, "gasket.gcode"))
        a, b = bench["a1"], bench["garage"]
        _spend_a1(a, b)
        a._state = PrinterStatus.IDLE            # finished; the job name is unchanged

        assert _refused(a.get_state) is None
        assert _refused(lambda: a.set_bed_temp(60)) is None
        assert _refused(lambda: a.start_print("next.gcode")) is None

    def test_the_only_running_print_is_never_refused(self, monkeypatch):
        """The other printer finished: there is no second print to take turns
        with, so the one still running is the person's to work with."""
        bench = _bench(monkeypatch, a1=(PRINTING, "bracket.gcode"), garage=(PRINTING, "gasket.gcode"))
        a, b = bench["a1"], bench["garage"]
        _spend_a1(a, b)
        b._state = PrinterStatus.IDLE

        assert _refused(a.emergency_stop) is None
        assert _refused(a.pause_print) is None
        assert _refused(a.get_state) is None
        assert engagement.current().machine == engagement.machine_id(a)

    def test_it_is_still_refused_while_the_other_print_runs(self, monkeypatch):
        bench = _bench(monkeypatch, a1=(PRINTING, "bracket.gcode"), garage=(PRINTING, "gasket.gcode"))
        a, b = bench["a1"], bench["garage"]
        _spend_a1(a, b)

        assert _refused(a.pause_print) is not None


class TestLooksThatAreNotAStatusRead:
    def test_camera_and_job_reads_cannot_take_turns_on_two_running_prints(self, monkeypatch):
        bench = _bench(monkeypatch, a1=(PRINTING, "bracket.gcode"), garage=(PRINTING, "gasket.gcode"))
        a, b = bench["a1"], bench["garage"]

        turns = [_refused(m.get_job) for m in (a, b) * 10]

        assert turns[0] is None
        assert sum(verdict is not None for verdict in turns) >= 9

    def test_a_job_read_on_an_idle_spare_holds_nothing(self, monkeypatch):
        bench = _bench(monkeypatch, a1=(PRINTING, "bracket.gcode"), spare=(PrinterStatus.IDLE, ""))
        a, spare = bench["a1"], bench["spare"]

        for _ in range(4):
            assert _refused(spare.get_job) is None
            assert _refused(a.get_job) is None
            assert engagement.current().machine == engagement.machine_id(a)
            engagement.hand_back(a)
