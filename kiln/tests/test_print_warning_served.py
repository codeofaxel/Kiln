"""The failure warning while Kiln watches a print, on an install without kiln-pro.

A print watcher that returns at once when kiln-pro is not on the computer
leaves a plain ``pip install kiln3d`` never warned before a print fails, at
any plan, with nothing said.  The warning comes from Kiln's servers through
the same served tool an agent calls; these tests pin what the watcher
sends, what it records and what it keeps on a miss.
"""

from __future__ import annotations

import builtins
import errno
import uuid
from unittest.mock import patch

import pytest

import kiln._pro_print_warning_bridge as bridge
import kiln.print_health_monitor as _phm_mod
from kiln.print_health_monitor import (
    HealthMetric,
    HealthSeverity,
    MonitorPolicy,
    MonitorSession,
    PrintHealthMonitor,
    PrinterHealthReport,
    _StallTracker,
)

_REAL_IMPORT = builtins.__import__


def _no_kiln_pro(name, *args, **kwargs):
    if name == "kiln_pro" or name.startswith("kiln_pro."):
        raise ImportError(f"No module named {name!r}", name=name)
    return _REAL_IMPORT(name, *args, **kwargs)


@pytest.fixture(autouse=True)
def _clean():
    _phm_mod._print_health_monitor = None
    bridge._reset_for_tests()
    with patch("builtins.__import__", side_effect=_no_kiln_pro):
        yield
    bridge._reset_for_tests()
    _phm_mod._print_health_monitor = None


def _session(printer: str = "bambu_a1") -> tuple[PrintHealthMonitor, MonitorSession]:
    monitor = PrintHealthMonitor()
    session = MonitorSession(
        session_id=str(uuid.uuid4()), printer_name=printer, job_id="benchy.gcode", policy=MonitorPolicy(),
    )
    monitor._sessions[session.session_id] = session
    monitor._stall_state[session.session_id] = _StallTracker()
    session.health_reports.append(
        PrinterHealthReport(
            printer_name=printer,
            metrics=[
                HealthMetric(
                    metric_name="hotend_temperature", current_value=231.0, expected_value=220.0,
                    deviation=11.0, is_warning=True, timestamp=1000.0, severity=HealthSeverity.WARNING, unit="C",
                )
            ],
            overall_status=HealthSeverity.WARNING,
            checked_at=1000.0,
        )
    )
    return monitor, session


class _Servers:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, tool, **kwargs):
        self.calls.append((tool, kwargs))
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]


HIGH_RISK_FREE = {
    "status": "success", "readings_counted": 7, "risk": "high risk",
    "said": "High risk: the printer's readings are drifting the way they do before a print fails.",
    "signals_gated": {"upgrade_url": "https://kiln3d.com/pricing"},
}


def test_a_plain_install_asks_kilns_servers_for_the_warning():
    monitor, session = _session()
    servers = _Servers(HIGH_RISK_FREE)
    with patch("kiln.server._pro_api_call", servers):
        monitor._maybe_record_predictive_signals(session)

    assert len(servers.calls) == 1
    tool, sent = servers.calls[0]
    assert tool == "predict_print_failure_risk"
    # The newest reading only: the servers keep the print's timeline.
    assert sent["telemetry"] == {"hotend_temp": 231.0, "hotend_target": 220.0}
    assert "telemetry_history" not in sent
    assert sent["job_info"] == {"job_id": "benchy.gcode"}
    assert sent["printer_id"] == "bambu_a1"
    assert sent["_asked_by_user"] is False

    assert session.latest_risk_assessment["severity"] == "red"
    assert session.latest_risk_assessment["risk"] == "high risk"
    # Nothing else the answer carried rides into the watch.
    assert "signals_gated" not in session.latest_risk_assessment
    issues = [i["issue_type"] for i in session.issues]
    assert issues == ["predictive_red_readings"]
    assert session.issues[0]["detail"].startswith("High risk")


def test_kiln_pro_names_each_drifting_reading_as_its_own_issue():
    monitor, session = _session()
    answer = {
        "status": "success", "risk": "high risk", "said": "High risk.",
        "signals": [
            {"kind": "thermal_drift", "severity": "red", "message": "Hotend is 11 C over.", "evidence": {}, "since": "t"},
            {"kind": "flow", "severity": "amber", "message": "Flow is dipping.", "evidence": {}, "since": "t"},
        ],
    }
    with patch("kiln.server._pro_api_call", _Servers(answer)):
        monitor._maybe_record_predictive_signals(session)
    assert [i["issue_type"] for i in session.issues] == ["predictive_red_thermal_drift"]
    assert session.issues[0]["detail"] == "Hotend is 11 C over."
    summary = monitor.get_latest_signals("bambu_a1")
    assert summary["risk"]["severity"] == "red"
    assert summary["risk"]["kinds"] == ["flow", "thermal_drift"]
    assert summary["predictive"]["kind"] == "thermal_drift"


def test_asks_at_the_tools_pace_and_records_a_warning_once():
    monitor, session = _session()
    servers = _Servers(HIGH_RISK_FREE)
    clock = [100.0]
    with patch("kiln.server._pro_api_call", servers), patch.object(bridge.time, "monotonic", lambda: clock[0]):
        monitor._maybe_record_predictive_signals(session)
        clock[0] += 5.0
        monitor._maybe_record_predictive_signals(session)  # too soon: not asked
        clock[0] += bridge.ASK_EVERY_S
        monitor._maybe_record_predictive_signals(session)
    assert len(servers.calls) == 2
    assert [i["issue_type"] for i in session.issues] == ["predictive_red_readings"]


@pytest.mark.parametrize("word", ["not enough readings yet", "not judged today", "can't judge these readings"])
def test_a_word_that_is_not_a_verdict_never_reads_clear(word):
    monitor, session = _session()
    answer = {"status": "success", "risk": word, "said": "Not a verdict yet."}
    with patch("kiln.server._pro_api_call", _Servers(answer)):
        monitor._maybe_record_predictive_signals(session)
    assert session.latest_risk_assessment["severity"] == bridge.NOT_JUDGED
    assert not session.issues


def test_signed_out_says_why_no_warning_is_coming():
    monitor, session = _session()
    signed_out = {
        "status": "error", "code": "KILN_ACCOUNT_NOT_PAIRED", "why": "signed_out",
        "error": "This install is not signed in to Kiln.",
    }
    with patch("kiln.server._pro_api_call", _Servers(signed_out)):
        monitor._maybe_record_predictive_signals(session)
    kept = session.latest_risk_assessment
    assert kept["severity"] == bridge.NOT_JUDGED
    assert kept["why"] == "signed_out"
    assert "not warning before this print fails" in kept["said"]
    assert "sign in" in kept["said"]
    assert not session.issues
    assert monitor.get_latest_signals("bambu_a1")["risk"]["said"] == kept["said"]


def test_offline_backs_off_instead_of_asking_every_reading():
    monitor, session = _session()
    calls = []

    def _offline(tool, **kwargs):
        calls.append(tool)
        raise OSError(errno.ENETUNREACH, "Network is unreachable")

    clock = [100.0]
    with patch("kiln.server._pro_api_call", _offline), patch.object(bridge.time, "monotonic", lambda: clock[0]):
        monitor._maybe_record_predictive_signals(session)
        clock[0] += bridge.ASK_EVERY_S + 1
        monitor._maybe_record_predictive_signals(session)
    assert calls == ["predict_print_failure_risk"]
    assert session.latest_risk_assessment["severity"] == bridge.NOT_JUDGED
    assert not session.issues
