"""The print monitor says the risk verdict and its warning signs, never a score.

When Kiln Pro's failure-risk check runs beside the monitor, its reply is a
verdict (clear, amber or red), the warning signs behind it in words, and the
readings each was seen in.  It carries no score (2026-10-23).  The monitor's
summary used to read one with a default of 0.0, so the report would have said
"RISK: 0.00 amber" -- a number nobody measured -- beside a real warning.
These pin the summary and the two places it is printed: ``monitor_print``'s
report here, and ``kiln monitor`` (``test_cli_main.py``).
"""

from __future__ import annotations

import re
from unittest import mock

#: What the monitor caches from the check: its reply, as it is served.
_SERVED = {
    "severity": "amber",
    "signals": [
        {
            "kind": "thermal_drift",
            "severity": "amber",
            "message": "Hotend drifting +0.60 °C/min from target (215°C).  Current: 217.7°C.",
            "evidence": {"slope_c_per_min": 0.6, "current_temp_c": 217.7, "target_c": 215.0},
        },
        {"kind": "insufficient_history", "severity": "info", "message": "", "evidence": {}},
    ],
    "signal_count": 2,
}


def _active(risk: dict | None) -> dict:
    return {
        "monitoring_active": True,
        "session_id": "abc12345-fake",
        "session_started_at": 0.0,
        "issue_count": 0,
        "report_count": 7,
        "risk": risk,
        "predictive": None,
        "detective": None,
        "auto_pause": None,
        "as_of": 0.0,
    }


def test_the_summary_carries_the_verdict_and_the_signs_and_no_score():
    from kiln.print_health_monitor import (
        MonitorPolicy,
        MonitorSession,
        PrintHealthMonitor,
    )

    monitor = PrintHealthMonitor()
    session = MonitorSession(
        session_id="s1", printer_name="voron", job_id="j1", policy=MonitorPolicy(),
    )
    session.latest_risk_assessment = _SERVED
    monitor._sessions[session.session_id] = session  # noqa: SLF001 — the monitor's own store

    risk = monitor.get_latest_signals("voron")["risk"]
    assert risk == {"severity": "amber", "kinds": ["thermal_drift"]}, risk


def test_the_report_line_says_the_verdict_not_a_number():
    from kiln import server

    adapter = mock.MagicMock()
    state = mock.MagicMock()
    state.state = "printing"
    state.to_dict.return_value = {"state": "printing"}
    adapter.get_state.return_value = state
    job = mock.MagicMock()
    job.completion = 42.0
    job.file_name = "part.gcode"
    job.print_time_elapsed = 600
    job.print_time_left = 900
    job.to_dict.return_value = {"completion": 42.0, "file_name": "part.gcode"}
    adapter.get_job.return_value = job
    adapter.get_snapshot.return_value = None
    adapter.get_temperatures.return_value = {}

    monitor = mock.MagicMock()
    monitor.get_latest_signals.return_value = _active({"severity": "amber", "kinds": ["thermal_drift"]})
    with mock.patch.object(server, "_get_adapter", return_value=adapter), mock.patch.object(
        server, "_pro_bridge", return_value=None
    ), mock.patch("kiln.print_health_monitor.get_print_health_monitor", return_value=monitor):
        report = server.monitor_print(include_snapshot=False)

    assert isinstance(report, str), report
    (line,) = [ln for ln in report.splitlines() if ln.startswith("- RISK:")]
    assert line == "- RISK: amber (thermal_drift)", line
    assert not re.search(r"\d", line), line
