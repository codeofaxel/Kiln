"""The printer's progress counter has not started while its layers have.

2026-09-24, a live A1: the inline monitor said "PRINTING 0%" beside
"layer 2 / 225" twelve minutes in, and the printer's own screen said 0%
too -- Bambu's ``mc_percent`` sits at 0 through the start sequence and the
first layers.  Kiln was relaying the firmware faithfully, and a person read
it as a lie.  The rule: never derive a percent the printer did not say;
carry ONE verdict on the job block (``progress_pending``) and let every
reader -- the text report here, the inline monitor and the web Monitor in
kiln-pro -- hide the percent while it holds and lead with the layer count
and time left, which are real.
"""

from __future__ import annotations

from unittest import mock
from unittest.mock import MagicMock

import pytest

from kiln.printers.base import JobProgress, JobResult


class TestTheVerdictOnTheJobBlock:
    def test_zero_percent_at_layer_two_is_pending_and_the_printers_number_rides_untouched(self):
        job = JobProgress(file_name="a.3mf", completion=0.0, current_layer=2, total_layers=225)
        assert job.progress_pending is True
        data = job.to_dict()
        assert data["progress_pending"] is True
        assert data["completion"] == 0.0

    @pytest.mark.parametrize(
        "completion, layer",
        [(3.0, 2), (0.4, 1), (0.0, 0), (0.0, None), (None, 2), (100.0, 225)],
    )
    def test_anything_else_carries_no_verdict(self, completion, layer):
        job = JobProgress(file_name="a.3mf", completion=completion, current_layer=layer, total_layers=225)
        assert job.progress_pending is False
        assert "progress_pending" not in job.to_dict()

    def test_a_job_that_ended_at_layer_two_is_not_pending_it_is_over(self):
        ended = JobProgress(file_name="a.3mf", completion=0.0, current_layer=2, ended_as=JobResult.CANCELLED)
        assert ended.progress_pending is False
        assert "progress_pending" not in ended.to_dict()
        idle = JobProgress(file_name="a.3mf", completion=0.0, current_layer=2, active=False)
        assert "progress_pending" not in idle.to_dict()

    def test_the_first_layer_counts(self):
        assert JobProgress(file_name="a.3mf", completion=0, current_layer=1).progress_pending is True


class TestTheReportHidesThePercentOnTheSameFlag:
    """The text report reads the job block's own verdict, never its own copy
    of the rule, so it hides the percent exactly when the panels do."""

    def _report(self, job_dict: dict) -> str:
        from kiln import server

        adapter = MagicMock()
        state = MagicMock()
        state.state = "printing"
        state.to_dict.return_value = {"state": "printing"}
        adapter.get_state.return_value = state
        job = MagicMock()
        job.to_dict.return_value = job_dict
        adapter.get_job.return_value = job
        adapter.get_snapshot.return_value = None
        adapter.get_temperatures.return_value = {}
        with mock.patch.object(server, "_get_adapter", return_value=adapter), mock.patch.object(
            server, "_pro_bridge", return_value=None
        ):
            report = server.monitor_print(include_snapshot=False)
        assert isinstance(report, str), report
        return report

    def test_pending_leads_with_the_layer_and_says_the_counter_has_not_started(self):
        report = self._report(
            {"file_name": "part.gcode", "completion": 0.0, "current_layer": 2, "total_layers": 225,
             "print_time_seconds": 720, "print_time_left_seconds": 2820, "progress_pending": True}
        )
        first = report.splitlines()[0]
        assert first == (
            "Print Status — layer 2 / 225, the printer's own progress counter "
            "reads 0 and has not started counting yet"
        )
        assert "0% complete" not in report
        assert "- Layer: 2 / 225" in report

    def test_a_moving_counter_reads_as_before(self):
        report = self._report(
            {"file_name": "part.gcode", "completion": 3.0, "current_layer": 4, "total_layers": 225}
        )
        assert report.splitlines()[0] == "Print Status — 3% complete"

    def test_layer_zero_at_zero_percent_is_the_start_sequence_not_pending(self):
        """Layer 0 is the start block, which has its own line; the percent
        stays, because 0% before the first layer is the truth."""
        report = self._report(
            {"file_name": "part.gcode", "completion": 0.0, "current_layer": 0, "total_layers": 225}
        )
        assert report.splitlines()[0] == "Print Status — 0% complete"
