"""Fleet analytics engine — unified dashboard data for the entire printer fleet.

Aggregates real-time printer states, job outcomes, material usage, and revenue
into structured snapshots and time-series data suitable for dashboards, reports,
and agent decision-making.

The analytics engine works with in-memory data fed by the event bus.  It does
not query the persistence layer directly — it aggregates from what's available
via the registry and recorded events.

Usage::

    from kiln.fleet_analytics import get_fleet_analytics

    analytics = get_fleet_analytics()
    snapshot = analytics.get_fleet_snapshot()
    report = analytics.generate_report(period="24h")
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PERIOD_SECONDS: dict[str, int] = {
    "24h": 86400,
    "7d": 604800,
    "30d": 2592000,
}

_VALID_PERIODS = frozenset(_PERIOD_SECONDS.keys())


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class FleetSnapshot:
    """Point-in-time snapshot of the entire fleet's state and daily metrics."""

    timestamp: float
    total_printers: int
    online_printers: int
    printing_printers: int
    idle_printers: int
    error_printers: int
    total_jobs_today: int
    successful_jobs_today: int
    failed_jobs_today: int
    fleet_utilization_pct: float
    avg_print_time_today_s: float
    total_filament_used_today_mm: float
    revenue_today: float
    top_material: str
    top_failure_mode: str | None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dictionary."""
        return {
            "timestamp": self.timestamp,
            "total_printers": self.total_printers,
            "online_printers": self.online_printers,
            "printing_printers": self.printing_printers,
            "idle_printers": self.idle_printers,
            "error_printers": self.error_printers,
            "total_jobs_today": self.total_jobs_today,
            "successful_jobs_today": self.successful_jobs_today,
            "failed_jobs_today": self.failed_jobs_today,
            "fleet_utilization_pct": self.fleet_utilization_pct,
            "avg_print_time_today_s": self.avg_print_time_today_s,
            "total_filament_used_today_mm": self.total_filament_used_today_mm,
            "revenue_today": self.revenue_today,
            "top_material": self.top_material,
            "top_failure_mode": self.top_failure_mode,
        }


@dataclass
class PrinterAnalytics:
    """Analytics summary for a single printer over a time window."""

    printer_id: str
    printer_model: str
    uptime_pct: float
    job_count_24h: int
    success_rate_24h: float
    avg_job_duration_s: float
    current_state: str
    current_job: str | None
    filament_used_24h_mm: float
    error_count_24h: int

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dictionary."""
        return {
            "printer_id": self.printer_id,
            "printer_model": self.printer_model,
            "uptime_pct": self.uptime_pct,
            "job_count_24h": self.job_count_24h,
            "success_rate_24h": self.success_rate_24h,
            "avg_job_duration_s": self.avg_job_duration_s,
            "current_state": self.current_state,
            "current_job": self.current_job,
            "filament_used_24h_mm": self.filament_used_24h_mm,
            "error_count_24h": self.error_count_24h,
        }


@dataclass
class TimeSeriesPoint:
    """Single point in a time-series dataset."""

    timestamp: float
    value: float
    label: str

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dictionary."""
        return {
            "timestamp": self.timestamp,
            "value": self.value,
            "label": self.label,
        }


@dataclass
class AnalyticsReport:
    """Comprehensive analytics report over a given period."""

    period: str
    fleet_snapshot: FleetSnapshot
    printer_analytics: list[PrinterAnalytics]
    utilization_history: list[TimeSeriesPoint]
    success_rate_history: list[TimeSeriesPoint]
    revenue_history: list[TimeSeriesPoint]
    material_breakdown: dict[str, int]
    failure_breakdown: dict[str, int]

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dictionary."""
        return {
            "period": self.period,
            "fleet_snapshot": self.fleet_snapshot.to_dict(),
            "printer_analytics": [p.to_dict() for p in self.printer_analytics],
            "utilization_history": [p.to_dict() for p in self.utilization_history],
            "success_rate_history": [p.to_dict() for p in self.success_rate_history],
            "revenue_history": [p.to_dict() for p in self.revenue_history],
            "material_breakdown": self.material_breakdown,
            "failure_breakdown": self.failure_breakdown,
        }


# ---------------------------------------------------------------------------
# Internal event records
# ---------------------------------------------------------------------------


@dataclass
class _JobRecord:
    """Internal record of a completed or failed job."""

    job_id: str
    printer_id: str
    success: bool
    duration_s: float
    material: str
    filament_used_mm: float
    failure_mode: str | None
    revenue: float
    recorded_at: float = field(default_factory=time.time)


@dataclass
class _PrinterStateRecord:
    """Internal record of a printer state observation."""

    printer_id: str
    state: str
    timestamp: float = field(default_factory=time.time)


@dataclass
class _ErrorRecord:
    """Internal record of a printer error event."""

    printer_id: str
    error_message: str
    timestamp: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# Core engine
# ---------------------------------------------------------------------------


class FleetAnalytics:
    """Aggregates fleet-wide analytics from in-memory event data.

    Thread-safe.  All public methods acquire the internal lock.

    :param period: Default reporting period (``"24h"``, ``"7d"``, ``"30d"``).
    """

    def __init__(self, *, period: str = "24h") -> None:
        if period not in _VALID_PERIODS:
            raise ValueError(f"Invalid period {period!r}; must be one of {sorted(_VALID_PERIODS)}")
        self._default_period = period
        self._lock = threading.Lock()

        # In-memory stores
        self._job_records: list[_JobRecord] = []
        self._printer_states: dict[str, _PrinterStateRecord] = {}
        self._printer_models: dict[str, str] = {}
        self._printer_jobs: dict[str, str | None] = {}
        self._error_records: list[_ErrorRecord] = []
        self._state_history: list[_PrinterStateRecord] = []
        self._revenue_records: list[dict[str, Any]] = []

        # Max records to keep in memory
        self._max_job_records = 50000
        self._max_error_records = 10000
        self._max_state_history = 100000

    # ------------------------------------------------------------------
    # Event ingestion
    # ------------------------------------------------------------------

    def record_job(
        self,
        job_id: str,
        printer_id: str,
        *,
        success: bool,
        duration_s: float = 0.0,
        material: str = "unknown",
        filament_used_mm: float = 0.0,
        failure_mode: str | None = None,
        revenue: float = 0.0,
        recorded_at: float | None = None,
    ) -> None:
        """Record a completed or failed job.

        :param job_id: Unique job identifier.
        :param printer_id: Printer that executed the job.
        :param success: Whether the job completed successfully.
        :param duration_s: Job duration in seconds.
        :param material: Filament material type.
        :param filament_used_mm: Filament consumed in millimeters.
        :param failure_mode: Description of failure, if any.
        :param revenue: Revenue generated by this job.
        :param recorded_at: Override timestamp (defaults to now).
        """
        record = _JobRecord(
            job_id=job_id,
            printer_id=printer_id,
            success=success,
            duration_s=duration_s,
            material=material,
            filament_used_mm=filament_used_mm,
            failure_mode=failure_mode,
            revenue=revenue,
            recorded_at=recorded_at if recorded_at is not None else time.time(),
        )
        with self._lock:
            self._job_records.append(record)
            if len(self._job_records) > self._max_job_records:
                self._job_records = self._job_records[-self._max_job_records :]

    def record_printer_state(
        self,
        printer_id: str,
        state: str,
        *,
        model: str | None = None,
        current_job: str | None = None,
        timestamp: float | None = None,
    ) -> None:
        """Record a printer state observation.

        :param printer_id: Printer identifier.
        :param state: Current state string (e.g. ``"printing"``, ``"idle"``).
        :param model: Printer model name (cached for analytics).
        :param current_job: Current job name, if any.
        :param timestamp: Override timestamp (defaults to now).
        """
        ts = timestamp if timestamp is not None else time.time()
        record = _PrinterStateRecord(
            printer_id=printer_id,
            state=state,
            timestamp=ts,
        )
        with self._lock:
            self._printer_states[printer_id] = record
            if model is not None:
                self._printer_models[printer_id] = model
            self._printer_jobs[printer_id] = current_job
            self._state_history.append(record)
            if len(self._state_history) > self._max_state_history:
                self._state_history = self._state_history[-self._max_state_history :]

    def record_error(
        self,
        printer_id: str,
        error_message: str,
        *,
        timestamp: float | None = None,
    ) -> None:
        """Record a printer error event.

        :param printer_id: Printer identifier.
        :param error_message: Human-readable error description.
        :param timestamp: Override timestamp (defaults to now).
        """
        record = _ErrorRecord(
            printer_id=printer_id,
            error_message=error_message,
            timestamp=timestamp if timestamp is not None else time.time(),
        )
        with self._lock:
            self._error_records.append(record)
            if len(self._error_records) > self._max_error_records:
                self._error_records = self._error_records[-self._max_error_records :]

    def record_revenue(
        self,
        amount: float,
        *,
        timestamp: float | None = None,
    ) -> None:
        """Record a revenue event.

        :param amount: Revenue amount.
        :param timestamp: Override timestamp (defaults to now).
        """
        with self._lock:
            self._revenue_records.append(
                {
                    "amount": amount,
                    "timestamp": timestamp if timestamp is not None else time.time(),
                }
            )

    # ------------------------------------------------------------------
    # Snapshots
    # ------------------------------------------------------------------

    def get_fleet_snapshot(self) -> FleetSnapshot:
        """Return a point-in-time snapshot of the entire fleet."""
        now = time.time()
        cutoff = now - _PERIOD_SECONDS["24h"]

        with self._lock:
            return self._build_fleet_snapshot(now, cutoff)

    def _build_fleet_snapshot(self, now: float, cutoff: float) -> FleetSnapshot:
        """Build a fleet snapshot from current in-memory data.

        Must be called with ``self._lock`` held.
        """
        # Printer state counts
        total = len(self._printer_states)
        online = 0
        printing = 0
        idle = 0
        error = 0
        for record in self._printer_states.values():
            if record.state == "offline":
                continue
            online += 1
            if record.state == "printing":
                printing += 1
            elif record.state == "idle":
                idle += 1
            elif record.state == "error":
                error += 1

        # Job stats for the period
        period_jobs = [j for j in self._job_records if j.recorded_at >= cutoff]
        total_jobs = len(period_jobs)
        successful = sum(1 for j in period_jobs if j.success)
        failed = sum(1 for j in period_jobs if not j.success)

        # Average print time
        durations = [j.duration_s for j in period_jobs if j.duration_s > 0]
        avg_print_time = sum(durations) / len(durations) if durations else 0.0

        # Filament usage
        total_filament = sum(j.filament_used_mm for j in period_jobs)

        # Revenue
        period_revenue = [r for r in self._revenue_records if r["timestamp"] >= cutoff]
        revenue = sum(r["amount"] for r in period_revenue)

        # Top material
        material_counts: dict[str, int] = {}
        for j in period_jobs:
            material_counts[j.material] = material_counts.get(j.material, 0) + 1
        top_material = max(material_counts, key=material_counts.get) if material_counts else "none"

        # Top failure mode
        failure_counts: dict[str, int] = {}
        for j in period_jobs:
            if j.failure_mode:
                failure_counts[j.failure_mode] = failure_counts.get(j.failure_mode, 0) + 1
        top_failure = max(failure_counts, key=failure_counts.get) if failure_counts else None

        # Utilization = printing / total (avoid division by zero)
        utilization = (printing / total * 100.0) if total > 0 else 0.0

        return FleetSnapshot(
            timestamp=time.time(),
            total_printers=total,
            online_printers=online,
            printing_printers=printing,
            idle_printers=idle,
            error_printers=error,
            total_jobs_today=total_jobs,
            successful_jobs_today=successful,
            failed_jobs_today=failed,
            fleet_utilization_pct=utilization,
            avg_print_time_today_s=avg_print_time,
            total_filament_used_today_mm=total_filament,
            revenue_today=revenue,
            top_material=top_material,
            top_failure_mode=top_failure,
        )

    # ------------------------------------------------------------------
    # Printer analytics
    # ------------------------------------------------------------------

    def get_printer_analytics(self, printer_id: str) -> PrinterAnalytics:
        """Return analytics for a single printer over the last 24 hours.

        :param printer_id: Printer identifier.
        :raises ValueError: If printer_id is not tracked.
        """
        now = time.time()
        cutoff = now - _PERIOD_SECONDS["24h"]

        with self._lock:
            return self._build_printer_analytics(printer_id, cutoff)

    def get_all_printer_analytics(self) -> list[PrinterAnalytics]:
        """Return analytics for all tracked printers."""
        now = time.time()
        cutoff = now - _PERIOD_SECONDS["24h"]

        with self._lock:
            printer_ids = sorted(self._printer_states.keys())
            return [self._build_printer_analytics(pid, cutoff) for pid in printer_ids]

    def _build_printer_analytics(self, printer_id: str, cutoff: float) -> PrinterAnalytics:
        """Build analytics for one printer.

        Must be called with ``self._lock`` held.

        :raises ValueError: If printer_id is not tracked.
        """
        if printer_id not in self._printer_states:
            raise ValueError(f"Printer not tracked: {printer_id!r}")

        current = self._printer_states[printer_id]
        model = self._printer_models.get(printer_id, "unknown")
        current_job = self._printer_jobs.get(printer_id)

        # Jobs in window
        jobs = [j for j in self._job_records if j.printer_id == printer_id and j.recorded_at >= cutoff]
        job_count = len(jobs)
        successful = sum(1 for j in jobs if j.success)
        success_rate = (successful / job_count * 100.0) if job_count > 0 else 0.0

        # Average duration
        durations = [j.duration_s for j in jobs if j.duration_s > 0]
        avg_duration = sum(durations) / len(durations) if durations else 0.0

        # Filament
        filament = sum(j.filament_used_mm for j in jobs)

        # Errors in window
        errors = [e for e in self._error_records if e.printer_id == printer_id and e.timestamp >= cutoff]
        error_count = len(errors)

        # Uptime: fraction of state observations that are not offline/error
        state_obs = [s for s in self._state_history if s.printer_id == printer_id and s.timestamp >= cutoff]
        if state_obs:
            up_obs = sum(1 for s in state_obs if s.state not in ("offline", "error"))
            uptime_pct = up_obs / len(state_obs) * 100.0
        else:
            # No observations — use current state as a single sample
            uptime_pct = 0.0 if current.state in ("offline", "error") else 100.0

        return PrinterAnalytics(
            printer_id=printer_id,
            printer_model=model,
            uptime_pct=uptime_pct,
            job_count_24h=job_count,
            success_rate_24h=success_rate,
            avg_job_duration_s=avg_duration,
            current_state=current.state,
            current_job=current_job,
            filament_used_24h_mm=filament,
            error_count_24h=error_count,
        )

    # ------------------------------------------------------------------
    # Reports
    # ------------------------------------------------------------------

    def generate_report(self, period: str = "24h") -> AnalyticsReport:
        """Generate a comprehensive analytics report.

        :param period: Time window (``"24h"``, ``"7d"``, ``"30d"``).
        :raises ValueError: If *period* is invalid.
        """
        if period not in _VALID_PERIODS:
            raise ValueError(f"Invalid period {period!r}; must be one of {sorted(_VALID_PERIODS)}")

        now = time.time()
        cutoff = now - _PERIOD_SECONDS[period]

        with self._lock:
            snapshot = self._build_fleet_snapshot(now, cutoff)
            printer_ids = sorted(self._printer_states.keys())
            printer_analytics = [self._build_printer_analytics(pid, cutoff) for pid in printer_ids]

            # Time series (hourly buckets for 24h, daily for 7d/30d)
            if period == "24h":
                interval_minutes = 60
            elif period == "7d":
                interval_minutes = 360
            else:
                interval_minutes = 1440
            utilization_ts = self._build_utilization_trend(cutoff, now, interval_minutes)
            success_ts = self._build_success_rate_trend(cutoff, now, interval_minutes)
            revenue_ts = self._build_revenue_trend(cutoff, now, interval_minutes)

            # Breakdowns
            material_breakdown = self._build_material_breakdown(cutoff)
            failure_breakdown = self._build_failure_breakdown(cutoff)

        return AnalyticsReport(
            period=period,
            fleet_snapshot=snapshot,
            printer_analytics=printer_analytics,
            utilization_history=utilization_ts,
            success_rate_history=success_ts,
            revenue_history=revenue_ts,
            material_breakdown=material_breakdown,
            failure_breakdown=failure_breakdown,
        )

    # ------------------------------------------------------------------
    # Time series
    # ------------------------------------------------------------------

    def get_utilization_trend(
        self,
        period: str = "24h",
        interval_minutes: int = 60,
    ) -> list[TimeSeriesPoint]:
        """Return fleet utilization as a time series.

        :param period: Time window.
        :param interval_minutes: Bucket size in minutes.
        """
        if period not in _VALID_PERIODS:
            raise ValueError(f"Invalid period {period!r}; must be one of {sorted(_VALID_PERIODS)}")
        now = time.time()
        cutoff = now - _PERIOD_SECONDS[period]
        with self._lock:
            return self._build_utilization_trend(cutoff, now, interval_minutes)

    def _build_utilization_trend(
        self,
        cutoff: float,
        now: float,
        interval_minutes: int,
    ) -> list[TimeSeriesPoint]:
        """Build utilization time series from state history.

        Must be called with ``self._lock`` held.
        """
        interval_s = interval_minutes * 60
        points: list[TimeSeriesPoint] = []

        bucket_start = cutoff
        while bucket_start < now:
            bucket_end = min(bucket_start + interval_s, now)

            # State observations in this bucket
            obs = [s for s in self._state_history if bucket_start <= s.timestamp < bucket_end]
            if obs:
                # Unique printers observed
                printer_ids = {s.printer_id for s in obs}
                printing_ids = {s.printer_id for s in obs if s.state == "printing"}
                utilization = len(printing_ids) / len(printer_ids) * 100.0 if printer_ids else 0.0
            else:
                utilization = 0.0

            points.append(
                TimeSeriesPoint(
                    timestamp=bucket_start,
                    value=round(utilization, 2),
                    label="utilization_pct",
                )
            )
            bucket_start = bucket_end

        return points

    def _build_success_rate_trend(
        self,
        cutoff: float,
        now: float,
        interval_minutes: int,
    ) -> list[TimeSeriesPoint]:
        """Build success rate time series from job records.

        Must be called with ``self._lock`` held.
        """
        interval_s = interval_minutes * 60
        points: list[TimeSeriesPoint] = []

        bucket_start = cutoff
        while bucket_start < now:
            bucket_end = min(bucket_start + interval_s, now)

            jobs = [j for j in self._job_records if bucket_start <= j.recorded_at < bucket_end]
            if jobs:
                success_count = sum(1 for j in jobs if j.success)
                rate = success_count / len(jobs) * 100.0
            else:
                rate = 0.0

            points.append(
                TimeSeriesPoint(
                    timestamp=bucket_start,
                    value=round(rate, 2),
                    label="success_rate_pct",
                )
            )
            bucket_start = bucket_end

        return points

    def _build_revenue_trend(
        self,
        cutoff: float,
        now: float,
        interval_minutes: int,
    ) -> list[TimeSeriesPoint]:
        """Build revenue time series from revenue records.

        Must be called with ``self._lock`` held.
        """
        interval_s = interval_minutes * 60
        points: list[TimeSeriesPoint] = []

        bucket_start = cutoff
        while bucket_start < now:
            bucket_end = min(bucket_start + interval_s, now)

            records = [r for r in self._revenue_records if bucket_start <= r["timestamp"] < bucket_end]
            total = sum(r["amount"] for r in records)

            points.append(
                TimeSeriesPoint(
                    timestamp=bucket_start,
                    value=round(total, 2),
                    label="revenue",
                )
            )
            bucket_start = bucket_end

        return points

    # ------------------------------------------------------------------
    # Breakdowns
    # ------------------------------------------------------------------

    def _build_material_breakdown(self, cutoff: float) -> dict[str, int]:
        """Material → job count for the period.

        Must be called with ``self._lock`` held.
        """
        counts: dict[str, int] = {}
        for j in self._job_records:
            if j.recorded_at >= cutoff:
                counts[j.material] = counts.get(j.material, 0) + 1
        return counts

    def _build_failure_breakdown(self, cutoff: float) -> dict[str, int]:
        """Failure mode → count for the period.

        Must be called with ``self._lock`` held.
        """
        counts: dict[str, int] = {}
        for j in self._job_records:
            if j.recorded_at >= cutoff and j.failure_mode:
                counts[j.failure_mode] = counts.get(j.failure_mode, 0) + 1
        return counts

    # ------------------------------------------------------------------
    # Ranking queries
    # ------------------------------------------------------------------

    def get_top_performers(self, *, limit: int = 5) -> list[PrinterAnalytics]:
        """Return printers sorted by success rate (descending).

        :param limit: Maximum number of results.
        """
        now = time.time()
        cutoff = now - _PERIOD_SECONDS["24h"]

        with self._lock:
            printer_ids = list(self._printer_states.keys())
            analytics = [self._build_printer_analytics(pid, cutoff) for pid in printer_ids]

        # Sort by success rate descending, then by job count descending
        analytics.sort(key=lambda a: (a.success_rate_24h, a.job_count_24h), reverse=True)
        return analytics[:limit]

    def get_problem_printers(self, *, error_threshold: int = 3) -> list[PrinterAnalytics]:
        """Return printers with error count exceeding the threshold.

        :param error_threshold: Minimum error count to include.
        """
        now = time.time()
        cutoff = now - _PERIOD_SECONDS["24h"]

        with self._lock:
            printer_ids = list(self._printer_states.keys())
            analytics = [self._build_printer_analytics(pid, cutoff) for pid in printer_ids]

        return [a for a in analytics if a.error_count_24h > error_threshold]


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_instance: FleetAnalytics | None = None
_instance_lock = threading.Lock()


def get_fleet_analytics() -> FleetAnalytics:
    """Return the module-level :class:`FleetAnalytics` singleton.

    Thread-safe; the instance is created on first call.
    """
    global _instance
    if _instance is not None:
        return _instance
    with _instance_lock:
        if _instance is None:
            _instance = FleetAnalytics()
        return _instance
