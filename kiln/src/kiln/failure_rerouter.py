"""Instant re-routing on failure — evaluate and execute job reroutes when a
printer fails mid-print.

When a print job fails, the rerouter evaluates whether the job should be
automatically moved to an alternative printer in the fleet.  Safety-critical
failures (thermal runaway, bed adhesion failure) are never auto-rerouted
because they require physical inspection.  Low-progress failures prefer
restarting on the same printer since the overhead of switching is not
worthwhile.

The rerouter enforces cooldown periods and maximum attempt limits to prevent
reroute storms.  All decisions are logged and recorded for fleet-wide
analytics.

Example::

    from kiln.failure_rerouter import get_failure_rerouter

    rerouter = get_failure_rerouter()
    decision = rerouter.evaluate_reroute(
        job_id="job-42",
        printer_id="prusa-mk4",
        failure_type="nozzle_clog",
        progress_pct=55.0,
        available_printers=["ender-3", "bambu-x1c"],
    )
    if decision.should_reroute:
        rerouter.execute_reroute(decision)
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Safety constants
# ---------------------------------------------------------------------------

# Failure types that must NEVER be auto-rerouted.  These indicate physical
# hazards requiring hands-on inspection before any printer resumes work.
_EXCLUDED_FAILURE_TYPES_DEFAULT: frozenset[str] = frozenset(
    {
        "thermal_runaway",
        "bed_adhesion_failure",
    }
)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class RerouteDecision:
    """Result of evaluating whether a failed job should be rerouted.

    :param original_printer_id: Printer that was running the job.
    :param original_job_id: The failed print job.
    :param failure_type: Category of failure (e.g. ``"nozzle_clog"``).
    :param should_reroute: Whether the job should move to another printer.
    :param target_printer_id: Chosen alternative printer, or ``None``.
    :param reason: Human-readable explanation for the decision.
    :param estimated_time_saved_s: Seconds saved by rerouting vs restarting
        on the same printer from scratch.
    :param estimated_waste_pct: Percentage of the print already completed
        that will be wasted by rerouting.
    :param rerouted_at: Unix timestamp when the reroute was decided.
    """

    original_printer_id: str
    original_job_id: str
    failure_type: str
    should_reroute: bool
    target_printer_id: str | None = None
    reason: str = ""
    estimated_time_saved_s: float = 0.0
    estimated_waste_pct: float = 0.0
    rerouted_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dictionary."""
        return {
            "original_printer_id": self.original_printer_id,
            "original_job_id": self.original_job_id,
            "failure_type": self.failure_type,
            "should_reroute": self.should_reroute,
            "target_printer_id": self.target_printer_id,
            "reason": self.reason,
            "estimated_time_saved_s": self.estimated_time_saved_s,
            "estimated_waste_pct": self.estimated_waste_pct,
            "rerouted_at": self.rerouted_at,
        }


@dataclass
class ReroutePolicy:
    """Configuration governing automatic reroute behaviour.

    :param auto_reroute_enabled: Master switch for automatic rerouting.
    :param max_reroute_attempts: Maximum times a single job can be rerouted.
    :param min_progress_for_restart_pct: Below this progress threshold, the
        job restarts on the same printer instead of rerouting — switching
        printers is not worthwhile at low progress.
    :param excluded_failure_types: Failure categories that must never trigger
        an auto-reroute (safety-critical failures).
    :param cooldown_s: Minimum seconds between reroutes for the same job to
        prevent reroute storms.
    """

    auto_reroute_enabled: bool = True
    max_reroute_attempts: int = 2
    min_progress_for_restart_pct: float = 10.0
    excluded_failure_types: list[str] = field(
        default_factory=lambda: list(_EXCLUDED_FAILURE_TYPES_DEFAULT),
    )
    cooldown_s: float = 300.0


# ---------------------------------------------------------------------------
# Core rerouter
# ---------------------------------------------------------------------------


class FailureRerouter:
    """Evaluate and execute job reroutes when a printer fails mid-print.

    Thread-safe.  All reroute decisions are logged at INFO level for
    operator visibility.

    :param policy: Reroute policy overrides.  Uses sensible defaults when
        ``None``.
    """

    def __init__(self, *, policy: ReroutePolicy | None = None) -> None:
        self._policy = policy or ReroutePolicy()
        self._lock = threading.Lock()
        # job_id -> number of reroute attempts
        self._attempt_counts: dict[str, int] = {}
        # job_id -> timestamp of last reroute
        self._last_reroute_times: dict[str, float] = {}
        # job_id -> list of decisions (all, including non-reroutes)
        self._history: dict[str, list[RerouteDecision]] = {}
        # Aggregate counters for stats
        self._total_reroutes: int = 0
        self._successful_reroutes: int = 0
        self._total_time_saved_s: float = 0.0

    # -- evaluation --------------------------------------------------------

    def evaluate_reroute(
        self,
        job_id: str,
        printer_id: str,
        failure_type: str,
        progress_pct: float,
        available_printers: list[str],
    ) -> RerouteDecision:
        """Decide whether a failed job should be rerouted to another printer.

        Applies the reroute policy rules in priority order:

        1. Safety-critical failures are never rerouted.
        2. Auto-reroute disabled in policy blocks all reroutes.
        3. Low progress prefers same-printer restart.
        4. No alternative printers available blocks reroute.
        5. Max reroute attempts exceeded blocks reroute.
        6. Cooldown period not elapsed blocks reroute.
        7. Otherwise, pick the best alternative and reroute.

        :param job_id: The failed print job.
        :param printer_id: Printer that was running the job.
        :param failure_type: Category of failure.
        :param progress_pct: Completion percentage at failure time (0-100).
        :param available_printers: Printer IDs that are idle and capable.
        :returns: A :class:`RerouteDecision` with the evaluation result.
        """
        now = time.time()

        # 1. Safety-critical failures
        if failure_type in self._policy.excluded_failure_types:
            decision = RerouteDecision(
                original_printer_id=printer_id,
                original_job_id=job_id,
                failure_type=failure_type,
                should_reroute=False,
                reason="Safety-critical failure requires physical inspection",
                estimated_waste_pct=progress_pct,
                rerouted_at=now,
            )
            self._record_decision(decision)
            return decision

        # 2. Auto-reroute globally disabled
        if not self._policy.auto_reroute_enabled:
            decision = RerouteDecision(
                original_printer_id=printer_id,
                original_job_id=job_id,
                failure_type=failure_type,
                should_reroute=False,
                reason="Auto-reroute is disabled by policy",
                estimated_waste_pct=progress_pct,
                rerouted_at=now,
            )
            self._record_decision(decision)
            return decision

        # 3. Low progress — restart on same printer is faster
        if progress_pct < self._policy.min_progress_for_restart_pct:
            decision = RerouteDecision(
                original_printer_id=printer_id,
                original_job_id=job_id,
                failure_type=failure_type,
                should_reroute=False,
                reason="Low progress — restart on same printer is faster",
                estimated_waste_pct=progress_pct,
                rerouted_at=now,
            )
            self._record_decision(decision)
            return decision

        # 4. No alternative printers
        # Filter out the failing printer from available list
        alternatives = [p for p in available_printers if p != printer_id]
        if not alternatives:
            decision = RerouteDecision(
                original_printer_id=printer_id,
                original_job_id=job_id,
                failure_type=failure_type,
                should_reroute=False,
                reason="No alternative printers available",
                estimated_waste_pct=progress_pct,
                rerouted_at=now,
            )
            self._record_decision(decision)
            return decision

        # 5. Max reroute attempts exceeded
        with self._lock:
            attempts = self._attempt_counts.get(job_id, 0)
        if attempts >= self._policy.max_reroute_attempts:
            decision = RerouteDecision(
                original_printer_id=printer_id,
                original_job_id=job_id,
                failure_type=failure_type,
                should_reroute=False,
                reason=(f"Max reroute attempts ({self._policy.max_reroute_attempts}) exceeded"),
                estimated_waste_pct=progress_pct,
                rerouted_at=now,
            )
            self._record_decision(decision)
            return decision

        # 6. Cooldown period
        with self._lock:
            last_time = self._last_reroute_times.get(job_id, 0.0)
        elapsed = now - last_time
        if last_time > 0.0 and elapsed < self._policy.cooldown_s:
            remaining = self._policy.cooldown_s - elapsed
            decision = RerouteDecision(
                original_printer_id=printer_id,
                original_job_id=job_id,
                failure_type=failure_type,
                should_reroute=False,
                reason=(f"Cooldown active — {remaining:.0f}s remaining before next reroute allowed"),
                estimated_waste_pct=progress_pct,
                rerouted_at=now,
            )
            self._record_decision(decision)
            return decision

        # 7. Pick best alternative and reroute
        target = self._select_best_printer(alternatives, failure_type)
        estimated_waste = progress_pct
        # Time saved = avoiding restart overhead.  Rough model: the time
        # to reroute to an idle printer is near-zero vs. restarting from
        # scratch on the same (now-failed) printer which requires
        # diagnosing and fixing the original issue.  Estimate as
        # progress_pct * 10 seconds per percentage point (placeholder).
        estimated_time_saved = progress_pct * 10.0

        decision = RerouteDecision(
            original_printer_id=printer_id,
            original_job_id=job_id,
            failure_type=failure_type,
            should_reroute=True,
            target_printer_id=target,
            reason=f"Rerouting to {target} after {failure_type} at {progress_pct:.1f}% progress",
            estimated_time_saved_s=estimated_time_saved,
            estimated_waste_pct=estimated_waste,
            rerouted_at=now,
        )
        self._record_decision(decision)
        return decision

    def _select_best_printer(
        self,
        alternatives: list[str],
        failure_type: str,
    ) -> str:
        """Choose the best alternative printer from the available list.

        Tries to use the cross-printer learning engine for material success
        rates when available; falls back to first available printer.

        :param alternatives: Idle printer IDs to choose from.
        :param failure_type: The failure type (for future learning-based
            weighting).
        :returns: The chosen printer ID.
        """
        try:
            from kiln.cross_printer_learning import get_learning_engine

            engine = get_learning_engine()
            # Score each alternative by success rate — higher is better
            best_id = alternatives[0]
            best_score = -1.0
            for pid in alternatives:
                stats = engine.get_printer_stats(pid)
                score = stats.get("success_rate", 0.0) if stats else 0.0
                if score > best_score:
                    best_score = score
                    best_id = pid
            return best_id
        except Exception:
            # Learning engine not available — fall back to first alternative
            return alternatives[0]

    # -- execution ---------------------------------------------------------

    def execute_reroute(self, decision: RerouteDecision) -> dict[str, Any]:
        """Record a reroute execution and update internal counters.

        :param decision: A reroute decision with ``should_reroute=True``.
        :returns: Dict with execution details.
        :raises ValueError: If the decision does not approve a reroute.
        """
        if not decision.should_reroute:
            raise ValueError("Cannot execute reroute: decision.should_reroute is False")

        with self._lock:
            self._attempt_counts[decision.original_job_id] = self._attempt_counts.get(decision.original_job_id, 0) + 1
            self._last_reroute_times[decision.original_job_id] = time.time()
            self._total_reroutes += 1
            self._successful_reroutes += 1
            self._total_time_saved_s += decision.estimated_time_saved_s

        logger.info(
            "Executed reroute: job=%s from=%s to=%s failure=%s progress=%.1f%%",
            decision.original_job_id,
            decision.original_printer_id,
            decision.target_printer_id,
            decision.failure_type,
            decision.estimated_waste_pct,
        )

        return {
            "status": "rerouted",
            "job_id": decision.original_job_id,
            "from_printer": decision.original_printer_id,
            "to_printer": decision.target_printer_id,
            "failure_type": decision.failure_type,
            "attempt": self._attempt_counts.get(decision.original_job_id, 0),
        }

    # -- history & stats ---------------------------------------------------

    def get_reroute_history(self, job_id: str) -> list[RerouteDecision]:
        """Return all reroute decisions for a job.

        :param job_id: The job to look up.
        :returns: List of decisions, oldest first.
        """
        with self._lock:
            return list(self._history.get(job_id, []))

    def get_reroute_stats(self) -> dict[str, Any]:
        """Return aggregate reroute statistics.

        :returns: Dict with ``total_reroutes``, ``successful_reroutes``,
            ``success_rate``, and ``avg_time_saved_s``.
        """
        with self._lock:
            total = self._total_reroutes
            successful = self._successful_reroutes
            total_saved = self._total_time_saved_s

        success_rate = (successful / total * 100.0) if total > 0 else 0.0
        avg_saved = (total_saved / total) if total > 0 else 0.0

        return {
            "total_reroutes": total,
            "successful_reroutes": successful,
            "success_rate": success_rate,
            "avg_time_saved_s": avg_saved,
        }

    # -- internal ----------------------------------------------------------

    def _record_decision(self, decision: RerouteDecision) -> None:
        """Store a decision in the history and log it."""
        with self._lock:
            self._history.setdefault(decision.original_job_id, []).append(decision)
        logger.info(
            "Reroute decision: job=%s printer=%s failure=%s reroute=%s reason=%s",
            decision.original_job_id,
            decision.original_printer_id,
            decision.failure_type,
            decision.should_reroute,
            decision.reason,
        )


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_rerouter: FailureRerouter | None = None
_rerouter_lock = threading.Lock()


def get_failure_rerouter() -> FailureRerouter:
    """Return the module-level :class:`FailureRerouter` singleton.

    The instance is lazily created on first call.  Thread-safe.
    """
    global _rerouter
    if _rerouter is None:
        with _rerouter_lock:
            if _rerouter is None:
                _rerouter = FailureRerouter()
    return _rerouter
