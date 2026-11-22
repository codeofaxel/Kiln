"""A print's real length teaches a printer's pace only if it ran as sliced.

Pinned through the real adapter template, the real lifecycle and the real
database: a print whose speed, flow, pauses, objects or file Kiln changed,
or the printer reported changed, carries a reason on its row and is left
out of the pace; a running print Kiln knows will not run as sliced says the
slicer's time no longer holds and gives no adjusted number.
"""

from __future__ import annotations

import pytest

from kiln import learned_print_time as lpt
from kiln import ran_as_sliced as ras
from kiln.printers.base import PrinterState, PrinterStatus
from tests import test_learned_print_time as _lifecycle
from tests.test_learned_print_time import _Adapter, _gcode, _row, _seed, _watch_to_the_end

kiln_env = _lifecycle.kiln_env


def _finish_a_print(adapter: _Adapter, tmp_path, *, estimate: int, actual: int, during=None) -> dict:
    assert adapter.start_print(_gcode(tmp_path, seconds=estimate)).success
    if during is not None:
        during(adapter)
    _watch_to_the_end(adapter, elapsed=actual)
    return _row()


def test_a_print_whose_speed_kiln_changed_is_no_pace_sample(kiln_env):
    _seed("voron", [(3600, 3960, "success")] * 3)  # 1.10, three times
    adapter = _Adapter()

    row = _finish_a_print(
        adapter, kiln_env, estimate=3600, actual=2400,  # 0.67: it ran at 150%
        during=lambda a: a.send_gcode(["M220 S150"]),
    )

    assert row["outcome"] == "success" and row["print_seconds"] == 2400
    assert ras.reasons_of(row) == [{"kind": "speed_changed", "seen_by": "kiln"}]
    pace = lpt.pace_for("voron")
    assert pace.prints == 3
    assert pace.factor == pytest.approx(1.10)


def test_a_print_kiln_did_not_change_counts_and_says_nothing_was_seen(kiln_env):
    adapter = _Adapter()

    row = _finish_a_print(adapter, kiln_env, estimate=3600, actual=3960)

    assert ras.reasons_of(row) == []
    assert lpt.pace_for("voron").prints == 1


def test_a_print_from_before_the_reasons_were_kept_still_counts(kiln_env):
    _seed("voron", [(3600, 3960, "success")] * 3)
    from kiln.persistence import get_db

    assert all(r["not_as_sliced"] is None for r in get_db().print_time_rows(["voron"]))
    assert lpt.pace_for("voron").prints == 3


@pytest.mark.parametrize(
    ("command", "kind"),
    [
        ("M220 S150", "speed_changed"),
        ("M221 S90", "flow_changed"),
        ("M109 S240", "temperature_wait"),
        ("M600", "paused"),
        ("EXCLUDE_OBJECT NAME=part_2", "objects_skipped"),
    ],
)
def test_each_command_that_changes_a_print_names_its_reason(kiln_env, command, kind):
    adapter = _Adapter()
    adapter.start_print(_gcode(kiln_env))

    adapter.send_gcode([command])

    assert ras.reasons_of(_row()) == [{"kind": kind, "seen_by": "kiln"}]


def test_a_command_that_changes_nothing_names_no_reason(kiln_env):
    adapter = _Adapter()
    adapter.start_print(_gcode(kiln_env))

    adapter.send_gcode(["M104 S215", "M106 S255", "; M220 S150 in a comment"])

    assert ras.reasons_of(_row()) == []


def test_a_resume_after_a_pause_is_no_pace_sample(kiln_env):
    from kiln.printers.base import PrintResult

    class _Resuming(_Adapter):
        def _resume_print_impl(self) -> PrintResult:
            self.status = PrinterStatus.PRINTING
            return PrintResult(success=True, message="resumed")

    adapter = _Resuming()
    adapter.start_print(_gcode(kiln_env))
    adapter.status = PrinterStatus.PAUSED  # paused unseen: no status read in between

    assert adapter.resume_print(force=True).success

    assert {"kind": "paused", "seen_by": "kiln"} in ras.reasons_of(_row())


class _Reporting(_Adapter):
    """A printer that reports its own speed, as a Bambu does."""

    magnitude = 100

    def get_state(self) -> PrinterState:
        return PrinterState(
            connected=True, state=self.status, last_job_result=self.job_result, speed_magnitude=self.magnitude,
        )


def test_a_speed_changed_on_the_printer_itself_is_read_and_named(kiln_env):
    adapter = _Reporting()
    adapter.start_print(_gcode(kiln_env))
    adapter.get_state()
    assert ras.reasons_of(_row()) == []

    adapter.magnitude = 124
    adapter.get_state()

    assert ras.reasons_of(_row()) == [{"kind": "speed_changed", "seen_by": "printer"}]


def test_a_pause_on_the_printer_itself_is_read_and_named(kiln_env):
    adapter = _Adapter()
    adapter.start_print(_gcode(kiln_env))
    adapter.status = PrinterStatus.PAUSED

    adapter.get_state()

    assert ras.reasons_of(_row()) == [{"kind": "paused", "seen_by": "printer"}]


def test_skipped_objects_are_named(kiln_env):
    from kiln.printers.command_verdict import CommandVerdict

    class _Skipping(_Adapter):
        def skip_objects(self, object_ids):
            return CommandVerdict.accepted_only("skipped")

    adapter = _Skipping()
    adapter.start_print(_gcode(kiln_env))

    adapter.skip_objects([2])

    assert ras.reasons_of(_row()) == [{"kind": "objects_skipped", "seen_by": "kiln"}]


def _resume_file(tmp_path, name: str = "bracket_resume.gcode") -> str:
    path = tmp_path / name
    path.write_text(
        "; RESUME FROM LAYER\n"
        "; target_layer: 9\n"
        "; generated by PrusaSlicer 2.8.1\n"
        "G1 X0 Y0\n"
        "; estimated printing time (normal mode) = 3h 0m 0s\n"
    )
    return str(path)


def test_a_start_from_a_file_that_carries_a_print_on_keeps_no_estimate(kiln_env):
    adapter = _Adapter()
    adapter.start_print(_resume_file(kiln_env))

    row = _row()
    assert row["estimated_seconds"] is None
    assert ras.reasons_of(row) == [{"kind": "start_file_edited", "seen_by": "kiln"}]


def test_a_running_print_kiln_changed_says_the_slicers_time_no_longer_holds(kiln_env):
    _seed("voron", [(3600, 3960, "success")] * 3)
    adapter = _Adapter()
    adapter.start_print(_gcode(kiln_env, seconds=3600))
    assert lpt.learned_time_left("voron", 600)["seconds_left"] == 3960 - 600

    adapter.send_gcode(["M220 S150"])

    left = lpt.learned_time_left("voron", 600, printer_seconds_left=1500)
    assert left["said"] == ras.NOT_AT_SLICED_SPEED
    assert left["ran_as_sliced"] is False
    assert left["seconds_left"] == 1500 and left["from"] == "printer"
    no_countdown = lpt.learned_time_left("voron", 600)
    assert no_countdown["seconds_left"] is None


def test_the_reasons_stay_on_this_computer(kiln_env):
    from kiln.outcome_courier import account_row

    adapter = _Adapter()
    row = _finish_a_print(adapter, kiln_env, estimate=3600, actual=2400, during=lambda a: a.send_gcode(["M220 S150"]))

    assert "not_as_sliced" not in account_row(row, "sync-1")
    assert not any("speed_changed" in str(v) for v in account_row(row, "sync-1").values())


def test_a_speed_schedule_names_its_reason_before_its_first_segment(kiln_env):
    from kiln import speed_schedule_runner as runner

    adapter = _Adapter()
    adapter.start_print(_gcode(kiln_env))

    run = runner.start(
        [{"from_layer": 50, "to_layer": 60, "speed_percent": 80}], lambda: adapter, printer_name="voron", poll_s=60,
    )
    try:
        assert ras.reasons_of(_row()) == [{"kind": "speed_changed", "seen_by": "kiln"}]
    finally:
        runner.stop(run, timeout_s=1)


def test_a_version_keeps_no_estimate_from_a_file_that_carries_a_print_on(kiln_env):
    from kiln.slicer_estimate import read_estimate

    assert read_estimate(_gcode(kiln_env, seconds=3600)) == {"seconds": 3600, "slicer": "PrusaSlicer 2.8.1"}
    assert read_estimate(_resume_file(kiln_env)) is None
