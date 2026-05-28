"""Tests for the printer_id pass-through to kiln-pro's predict_risk.

The wire adds the consumer half of the bounded-weight lifetime-wear
feature in kiln-pro's predictive risk model.  This file pins:

* :meth:`PrintHealthMonitor._maybe_record_predictive_signals` passes
  ``printer_id=session.printer_name`` into :func:`predict_risk` on
  every tick, so the predictor can consult the nozzle-lifetime state
  store.
* The wire stays a silent no-op when kiln-pro isn't installed (free
  tier).
* The wire stays a silent no-op when the predict_risk call raises —
  the monitor loop must not break on a busted Pro+ heuristic.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch
import uuid

import pytest

import kiln.print_health_monitor as _phm_mod
from kiln.print_health_monitor import (
    HealthMetric,
    HealthSeverity,
    MonitorPolicy,
    MonitorSession,
    PrintHealthMonitor,
    PrinterHealthReport,
)


@pytest.fixture(autouse=True)
def _reset_singleton():
    _phm_mod._print_health_monitor = None
    yield
    _phm_mod._print_health_monitor = None


def _make_health_report(
    printer_name: str = "voron",
    hotend_actual: float = 205.0,
    hotend_target: float = 200.0,
    timestamp: float = 1000.0,
) -> PrinterHealthReport:
    metric = HealthMetric(
        metric_name="hotend_temperature",
        current_value=hotend_actual,
        expected_value=hotend_target,
        deviation=abs(hotend_actual - hotend_target),
        is_warning=False,
        timestamp=timestamp,
        severity=HealthSeverity.OK,
        unit="°C",
    )
    return PrinterHealthReport(
        printer_name=printer_name,
        metrics=[metric],
        overall_status=HealthSeverity.OK,
        checked_at=timestamp,
    )


def _build_active_session(
    printer_name: str = "voron",
) -> tuple[PrintHealthMonitor, MonitorSession]:
    """Build a MonitorSession registered in the monitor's session map."""
    from kiln.print_health_monitor import _StallTracker

    monitor = PrintHealthMonitor()
    session_id = str(uuid.uuid4())
    session = MonitorSession(
        session_id=session_id,
        printer_name=printer_name,
        job_id="j-test",
        policy=MonitorPolicy(),
    )
    monitor._sessions[session_id] = session
    monitor._stall_state[session_id] = _StallTracker()
    return monitor, session


# ---------------------------------------------------------------------------
# 1. The wire — printer_id is forwarded on every tick
# ---------------------------------------------------------------------------


def test_predict_risk_called_with_printer_id_from_session():
    """The wire's foundational promise: ``session.printer_name`` flows
    through to the predictor as the ``printer_id`` kwarg.
    """
    monitor, session = _build_active_session(printer_name="bambu_a1_office")
    session.health_reports.append(
        _make_health_report("bambu_a1_office", 205.0, 200.0, 1000.0)
    )

    captured = {"kwargs": None}

    def _capture_predict_risk(**kw):
        captured["kwargs"] = kw
        return {"risk_score": 0.0, "severity": "clear", "signals": []}

    with patch.dict(
        "sys.modules",
        {
            "kiln_pro": MagicMock(),
            "kiln_pro.recovery": MagicMock(),
            "kiln_pro.recovery.predictive": MagicMock(
                predict_risk=_capture_predict_risk,
            ),
        },
    ):
        monitor._maybe_record_predictive_signals(session)

    assert captured["kwargs"] is not None, "predict_risk should have been called"
    assert "printer_id" in captured["kwargs"], (
        "printer_id kwarg must be forwarded to the predictor"
    )
    assert captured["kwargs"]["printer_id"] == "bambu_a1_office"
    # Backward-compat: existing kwargs still flow.
    assert "telemetry" in captured["kwargs"]
    assert "telemetry_history" in captured["kwargs"]


def test_printer_id_matches_session_printer_name_under_rename():
    """``printer_id`` always tracks ``session.printer_name`` — not a
    hard-coded value or the monitor's identity.  Different sessions
    should send different printer_ids."""
    monitor_a, session_a = _build_active_session(printer_name="printer_a")
    session_a.health_reports.append(
        _make_health_report("printer_a", 205.0, 200.0, 1000.0)
    )
    _monitor_b, session_b = _build_active_session(printer_name="printer_b")
    session_b.health_reports.append(
        _make_health_report("printer_b", 205.0, 200.0, 1000.0)
    )

    captured_ids: list[str] = []

    def _capture(**kw):
        captured_ids.append(kw.get("printer_id"))
        return {"risk_score": 0.0, "severity": "clear", "signals": []}

    with patch.dict(
        "sys.modules",
        {
            "kiln_pro": MagicMock(),
            "kiln_pro.recovery": MagicMock(),
            "kiln_pro.recovery.predictive": MagicMock(predict_risk=_capture),
        },
    ):
        monitor_a._maybe_record_predictive_signals(session_a)
        # Re-attach session_b onto monitor_a (each session carries its
        # own printer_name, so a single monitor handles both):
        monitor_a._sessions[session_b.session_id] = session_b
        monitor_a._stall_state[session_b.session_id] = monitor_a._stall_state[
            session_a.session_id
        ].__class__()
        monitor_a._maybe_record_predictive_signals(session_b)

    assert captured_ids == ["printer_a", "printer_b"]


# ---------------------------------------------------------------------------
# 2. Free-tier safety — no kiln-pro, no crash
# ---------------------------------------------------------------------------


def test_no_kiln_pro_is_silent_noop():
    """ImportError on kiln_pro -> the wire is a no-op, no issues created."""
    monitor, session = _build_active_session()
    session.health_reports.append(_make_health_report())

    real_import = __builtins__["__import__"] if isinstance(
        __builtins__, dict
    ) else __builtins__.__import__

    def _blocking_import(name, *args, **kwargs):
        if name == "kiln_pro.recovery.predictive" or name.startswith("kiln_pro."):
            raise ImportError(f"blocked for test: {name}")
        return real_import(name, *args, **kwargs)

    with patch("builtins.__import__", side_effect=_blocking_import):
        # Must not raise even though kiln_pro is unavailable.
        monitor._maybe_record_predictive_signals(session)

    assert not session.issues
    assert session.latest_risk_assessment is None


# ---------------------------------------------------------------------------
# 3. Defensive — a busted predict_risk doesn't break the monitor loop
# ---------------------------------------------------------------------------


def test_predict_risk_raising_is_caught():
    """If the kiln-pro predictor raises mid-call (busted heuristic,
    nozzle backend error, etc.), the monitor loop catches it and the
    session is unaffected."""
    monitor, session = _build_active_session()
    session.health_reports.append(_make_health_report())

    def _raising_predict_risk(**kw):
        raise RuntimeError("simulated kiln-pro failure")

    with patch.dict(
        "sys.modules",
        {
            "kiln_pro": MagicMock(),
            "kiln_pro.recovery": MagicMock(),
            "kiln_pro.recovery.predictive": MagicMock(
                predict_risk=_raising_predict_risk,
            ),
        },
    ):
        # Must not raise.
        monitor._maybe_record_predictive_signals(session)

    assert not session.issues
    assert session.latest_risk_assessment is None


# ---------------------------------------------------------------------------
# 4. End-to-end — a nozzle_wear red signal from the predictor surfaces
# ---------------------------------------------------------------------------


def test_nozzle_wear_red_signal_becomes_issue():
    """When kiln-pro's predictor reports a red nozzle_wear signal, the
    consumer records it as a predictive_red_nozzle_wear issue — the
    same shape as the existing thermal_drift / flow_drift red signals.
    """
    monitor, session = _build_active_session()
    session.health_reports.append(_make_health_report())

    fake_assessment = {
        "risk_score": 0.65,
        "severity": "red",
        "signals": [
            {
                "kind": "nozzle_wear",
                "severity": "red",
                "weight": 0.20,
                "message": (
                    "Plan a nozzle swap before this print finishes — "
                    "replacement is overdue.  Nozzle wear at 200% of the "
                    "published lifetime estimate (abrasive filament). "
                    "Lifetime-wear contribution: 0.200."
                ),
                "evidence": {
                    "wear_fraction": 2.0,
                    "wear_status": "replace",
                    "filament_class": "abrasive",
                    "threshold_grams": 360.0,
                    "max_contribution": 0.20,
                },
            },
        ],
    }

    with patch.dict(
        "sys.modules",
        {
            "kiln_pro": MagicMock(),
            "kiln_pro.recovery": MagicMock(),
            "kiln_pro.recovery.predictive": MagicMock(
                predict_risk=lambda **kw: fake_assessment,
            ),
        },
    ):
        monitor._maybe_record_predictive_signals(session)

    wear_issues = [
        i for i in session.issues
        if i["issue_type"] == "predictive_red_nozzle_wear"
    ]
    assert len(wear_issues) == 1, (
        "the nozzle_wear red signal should surface as a single "
        "predictive_red_nozzle_wear issue"
    )
    assert session.latest_risk_assessment is fake_assessment
