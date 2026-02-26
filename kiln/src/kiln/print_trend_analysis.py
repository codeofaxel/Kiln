"""Local print history trend analysis.

Analyzes print history and outcome data stored in the local SQLite
database to surface actionable insights about printer health,
reliability trends, and failure patterns.  All analysis is performed
on-device — no data leaves the machine.

Provides:

* **Failure rate trending** — is a printer getting more or less reliable?
* **Temperature stability** — are temperature deviations increasing?
* **Duration anomaly detection** — are prints taking longer than expected?
* **Failure pattern detection** — recurring failure modes worth investigating.
* **Material reliability** — which materials work best on which printer.

Configure via environment variables:

    KILN_TREND_MIN_PRINTS      -- minimum prints before trends are meaningful (default 5)
    KILN_TREND_LOOKBACK_DAYS   -- how far back to analyze (default 30)

Usage::

    from kiln.print_trend_analysis import analyze_printer_trends

    report = analyze_printer_trends("ender3", db=get_db())
    print(report.health_score)   # 0.0 – 1.0
    print(report.alerts)         # ["Failure rate increasing: 10% → 25% over last 14 days"]
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any

_MIN_PRINTS = int(os.environ.get("KILN_TREND_MIN_PRINTS", "5"))
_LOOKBACK_DAYS = int(os.environ.get("KILN_TREND_LOOKBACK_DAYS", "30"))


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class TrendAlert:
    """A single actionable alert surfaced by trend analysis.

    :param severity: ``"info"``, ``"warning"``, or ``"critical"``.
    :param category: Alert category (e.g. ``"failure_rate"``, ``"duration"``).
    :param message: Human-readable description of the finding.
    """

    severity: str
    category: str
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "category": self.category,
            "message": self.message,
        }


@dataclass
class TrendReport:
    """Complete trend analysis report for a single printer.

    :param printer_name: Which printer was analyzed.
    :param health_score: 0.0 (poor) to 1.0 (excellent) overall health.
    :param total_prints: Number of prints in the analysis window.
    :param success_rate: Overall success rate in the window.
    :param failure_rate_trend: ``"improving"``, ``"stable"``, or ``"worsening"``.
    :param avg_duration_seconds: Mean print duration.
    :param duration_trend: ``"stable"``, ``"increasing"``, or ``"decreasing"``.
    :param top_failure_modes: Most common failure modes, ordered by frequency.
    :param material_reliability: Per-material success rates.
    :param alerts: Actionable alerts for the user/agent.
    :param analysis_window_days: How many days of data were analyzed.
    """

    printer_name: str
    health_score: float
    total_prints: int
    success_rate: float
    failure_rate_trend: str
    avg_duration_seconds: float | None
    duration_trend: str
    top_failure_modes: list[dict[str, Any]]
    material_reliability: dict[str, dict[str, Any]]
    alerts: list[TrendAlert] = field(default_factory=list)
    analysis_window_days: int = _LOOKBACK_DAYS

    def to_dict(self) -> dict[str, Any]:
        return {
            "printer_name": self.printer_name,
            "health_score": round(self.health_score, 2),
            "total_prints": self.total_prints,
            "success_rate": round(self.success_rate, 4),
            "failure_rate_trend": self.failure_rate_trend,
            "avg_duration_seconds": (
                round(self.avg_duration_seconds, 1)
                if self.avg_duration_seconds is not None
                else None
            ),
            "duration_trend": self.duration_trend,
            "top_failure_modes": self.top_failure_modes,
            "material_reliability": self.material_reliability,
            "alerts": [a.to_dict() for a in self.alerts],
            "analysis_window_days": self.analysis_window_days,
        }


# ---------------------------------------------------------------------------
# Analysis helpers (pure functions operating on local data)
# ---------------------------------------------------------------------------


def _split_halves(records: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split records into older half and newer half by timestamp."""
    if len(records) < 2:
        return records, records
    mid = len(records) // 2
    return records[:mid], records[mid:]


def _success_rate(records: list[dict[str, Any]]) -> float:
    """Compute success rate from a list of outcome/status records."""
    if not records:
        return 0.0
    successes = sum(
        1
        for r in records
        if r.get("outcome") == "success" or r.get("status") == "completed"
    )
    return successes / len(records)


def _classify_rate_trend(older_rate: float, newer_rate: float) -> str:
    """Classify failure rate trend from two success rates.

    A worsening trend means newer success rate is notably lower.
    """
    delta = newer_rate - older_rate
    if delta < -0.10:
        return "worsening"
    if delta > 0.10:
        return "improving"
    return "stable"


def _avg_duration(records: list[dict[str, Any]]) -> float | None:
    """Compute average duration from records with duration_seconds."""
    durations = [
        r["duration_seconds"]
        for r in records
        if r.get("duration_seconds") is not None and r["duration_seconds"] > 0
    ]
    if not durations:
        return None
    return sum(durations) / len(durations)


def _classify_duration_trend(older_avg: float | None, newer_avg: float | None) -> str:
    """Classify duration trend from two averages."""
    if older_avg is None or newer_avg is None:
        return "stable"
    if older_avg == 0:
        return "stable"
    ratio = newer_avg / older_avg
    if ratio > 1.20:
        return "increasing"
    if ratio < 0.80:
        return "decreasing"
    return "stable"


def _failure_mode_counts(outcomes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Count failure modes from outcome records."""
    counts: dict[str, int] = {}
    for r in outcomes:
        mode = r.get("failure_mode")
        if mode and r.get("outcome") == "failed":
            counts[mode] = counts.get(mode, 0) + 1
    return sorted(
        [{"mode": m, "count": c} for m, c in counts.items()],
        key=lambda x: x["count"],
        reverse=True,
    )


def _material_stats(outcomes: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Compute per-material success rates from outcome records."""
    by_material: dict[str, list[dict[str, Any]]] = {}
    for r in outcomes:
        mat = r.get("material_type")
        if mat:
            by_material.setdefault(mat, []).append(r)

    result: dict[str, dict[str, Any]] = {}
    for mat, recs in by_material.items():
        total = len(recs)
        successes = sum(1 for r in recs if r.get("outcome") == "success")
        result[mat] = {
            "total": total,
            "success_rate": round(successes / total, 2) if total > 0 else 0.0,
        }
    return result


def _compute_health_score(
    success_rate: float,
    failure_trend: str,
    duration_trend: str,
    total_prints: int,
) -> float:
    """Compute a composite health score from 0.0 to 1.0.

    Weighted blend: success rate dominates, trend adjustments are secondary.
    """
    # Base: success rate (0-1)
    score = success_rate

    # Trend penalties/bonuses
    if failure_trend == "worsening":
        score -= 0.15
    elif failure_trend == "improving":
        score += 0.05

    if duration_trend == "increasing":
        score -= 0.05

    # Low sample size discount — less confident
    if total_prints < _MIN_PRINTS:
        score *= 0.8

    return max(0.0, min(1.0, score))


# ---------------------------------------------------------------------------
# Main analysis function
# ---------------------------------------------------------------------------


def analyze_printer_trends(
    printer_name: str,
    *,
    db: Any,
    lookback_days: int | None = None,
) -> TrendReport:
    """Analyze local print history trends for a printer.

    All data comes from the local SQLite database.  Nothing leaves
    the machine.

    :param printer_name: Printer to analyze.
    :param db: A :class:`~kiln.persistence.KilnDB` instance.
    :param lookback_days: Override the default lookback window.
    :returns: :class:`TrendReport` with health score, trends, and alerts.
    """
    window = lookback_days if lookback_days is not None else _LOOKBACK_DAYS
    cutoff = time.time() - (window * 86400)

    # Fetch print history records (returned newest-first; reverse to chronological)
    history = db.list_print_history(printer_name=printer_name, limit=500)
    history = [r for r in history if (r.get("completed_at") or r.get("created_at", 0)) >= cutoff]
    history.reverse()

    # Fetch outcome records (returned newest-first; reverse to chronological)
    outcomes = db.list_print_outcomes(printer_name=printer_name, limit=500)
    outcomes = [r for r in outcomes if r.get("created_at", 0) >= cutoff]
    outcomes.reverse()

    # Use whichever dataset has more records for rate/trend analysis
    records = outcomes if len(outcomes) >= len(history) else history
    total = len(records)

    # Overall success rate
    rate = _success_rate(records)

    # Trend analysis: compare first half (older) vs second half (newer)
    older, newer = _split_halves(records)
    older_rate = _success_rate(older)
    newer_rate = _success_rate(newer)
    failure_trend = _classify_rate_trend(older_rate, newer_rate)

    # Duration trend (from history — has duration_seconds)
    h_older, h_newer = _split_halves(history)
    old_dur = _avg_duration(h_older)
    new_dur = _avg_duration(h_newer)
    dur_trend = _classify_duration_trend(old_dur, new_dur)
    avg_dur = _avg_duration(history)

    # Failure modes (from outcomes — has failure_mode)
    top_failures = _failure_mode_counts(outcomes)

    # Material reliability
    mat_stats = _material_stats(outcomes)

    # Health score
    health = _compute_health_score(rate, failure_trend, dur_trend, total)

    # Generate alerts
    alerts: list[TrendAlert] = []

    if total < _MIN_PRINTS:
        alerts.append(TrendAlert(
            severity="info",
            category="sample_size",
            message=f"Only {total} prints in the last {window} days — trends may not be meaningful yet.",
        ))

    if failure_trend == "worsening" and total >= _MIN_PRINTS:
        alerts.append(TrendAlert(
            severity="warning",
            category="failure_rate",
            message=(
                f"Failure rate increasing: success rate dropped from "
                f"{older_rate:.0%} to {newer_rate:.0%} over the last {window} days."
            ),
        ))

    if dur_trend == "increasing" and total >= _MIN_PRINTS:
        old_str = f"{old_dur:.0f}s" if old_dur else "unknown"
        new_str = f"{new_dur:.0f}s" if new_dur else "unknown"
        alerts.append(TrendAlert(
            severity="info",
            category="duration",
            message=f"Average print duration increasing: {old_str} → {new_str}. May indicate mechanical wear or clogging.",
        ))

    if top_failures:
        top = top_failures[0]
        if top["count"] >= 3:
            alerts.append(TrendAlert(
                severity="warning",
                category="recurring_failure",
                message=f"Recurring failure mode: '{top['mode']}' occurred {top['count']} times. Consider investigating.",
            ))

    # Material-specific alerts
    for mat, stats in mat_stats.items():
        if stats["total"] >= 3 and stats["success_rate"] < 0.5:
            alerts.append(TrendAlert(
                severity="warning",
                category="material_reliability",
                message=f"Low success rate with {mat}: {stats['success_rate']:.0%} over {stats['total']} prints.",
            ))

    if health < 0.5 and total >= _MIN_PRINTS:
        alerts.append(TrendAlert(
            severity="critical",
            category="health",
            message=f"Printer health score is {health:.0%}. Recommend maintenance inspection.",
        ))

    return TrendReport(
        printer_name=printer_name,
        health_score=health,
        total_prints=total,
        success_rate=rate,
        failure_rate_trend=failure_trend,
        avg_duration_seconds=avg_dur,
        duration_trend=dur_trend,
        top_failure_modes=top_failures,
        material_reliability=mat_stats,
        alerts=alerts,
        analysis_window_days=window,
    )
