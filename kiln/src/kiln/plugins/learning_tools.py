"""Learning and agent memory tools plugin.

Extracts cross-printer learning and persistent agent memory tools from
server.py into a focused plugin module.  These tools enable agents to
record print outcomes, query learning insights, get printer suggestions,
recommend settings, and persist notes across sessions.

Auto-discovered by :func:`~kiln.plugin_loader.register_all_plugins` —
no manual imports needed.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants — validation sets and safety limits
# ---------------------------------------------------------------------------

_VALID_OUTCOMES = frozenset({"success", "failed", "partial"})
_VALID_QUALITY_GRADES = frozenset({"excellent", "good", "acceptable", "poor"})
_VALID_FAILURE_MODES = frozenset({
    "spaghetti", "layer_shift", "warping", "adhesion", "stringing",
    "under_extrusion", "over_extrusion", "clog", "thermal_runaway",
    "power_loss", "mechanical", "other",
})

# Hard safety limits — recorded settings cannot exceed these.
# Prevents malicious agents from poisoning the learning database
# with dangerous temperature data that could damage printers.
_MAX_SAFE_TOOL_TEMP: float = 320.0   # Above this, even high-temp materials are dangerous
_MAX_SAFE_BED_TEMP: float = 140.0
_MAX_SAFE_SPEED: float = 500.0       # mm/s — beyond any consumer printer

_LEARNING_SAFETY_NOTICE = (
    "These insights are advisory only. They do NOT override safety limits. "
    "Always run preflight checks before starting a print. Temperature and "
    "G-code safety enforcement applies regardless of learning data."
)


# ---------------------------------------------------------------------------
# Plugin class
# ---------------------------------------------------------------------------


class _LearningToolsPlugin:
    """Cross-printer learning and persistent agent memory tools.

    Provides tools for recording print outcomes, querying learning
    insights, suggesting printers and settings, and managing agent
    memory that persists across sessions.
    """

    @property
    def name(self) -> str:
        return "learning_tools"

    @property
    def description(self) -> str:
        return "Cross-printer learning and agent memory tools"

    def register(self, mcp: Any) -> None:
        """Register learning and memory tools with the MCP server."""

        # Lazy imports — by the time register() runs, server.py is fully
        # initialized so these resolve without circular import issues.
        from kiln.persistence import get_db
        from kiln.server import _check_auth, _error_dict, _registry

        @mcp.tool()
        def record_print_outcome(
            job_id: str,
            outcome: str,
            quality_grade: str | None = None,
            failure_mode: str | None = None,
            settings: dict | None = None,
            environment: dict | None = None,
            notes: str | None = None,
            printer_name: str | None = None,
            file_name: str | None = None,
            file_hash: str | None = None,
            material_type: str | None = None,
        ) -> dict:
            """Record the outcome of a print for cross-printer learning.

            The learning database helps agents make better decisions about which
            printer to use for a given job and material.  Outcomes are agent-curated
            quality data — separate from the auto-populated print history.

            **Safety**: Settings are validated against hard safety limits.  Outcomes
            with temperatures exceeding safe maximums are rejected to prevent
            poisoning the learning database with dangerous data.

            Args:
                job_id: The job ID from the print queue.
                outcome: One of ``"success"``, ``"failed"``, or ``"partial"``.
                quality_grade: Optional — ``"excellent"``, ``"good"``, ``"acceptable"``, ``"poor"``.
                failure_mode: Optional — e.g. ``"spaghetti"``, ``"layer_shift"``, ``"warping"``.
                settings: Optional dict of print settings used (temp_tool, temp_bed, speed, etc.).
                environment: Optional dict of environment conditions (ambient_temp, humidity).
                notes: Optional free-text notes about the print.
                printer_name: Printer used.  Auto-resolved from job if omitted.
                file_name: File printed.  Auto-resolved from job if omitted.
                file_hash: Optional hash of the file for cross-printer comparison.
                material_type: Material used (e.g. ``"PLA"``, ``"PETG"``).
            """
            if err := _check_auth("learning"):
                return err

            # --- Validate enums ---
            if outcome not in _VALID_OUTCOMES:
                return _error_dict(
                    f"Invalid outcome {outcome!r}. Must be one of: {', '.join(sorted(_VALID_OUTCOMES))}",
                    code="VALIDATION_ERROR",
                )
            if quality_grade and quality_grade not in _VALID_QUALITY_GRADES:
                return _error_dict(
                    f"Invalid quality_grade {quality_grade!r}. "
                    f"Must be one of: {', '.join(sorted(_VALID_QUALITY_GRADES))}",
                    code="VALIDATION_ERROR",
                )
            if failure_mode and failure_mode not in _VALID_FAILURE_MODES:
                return _error_dict(
                    f"Invalid failure_mode {failure_mode!r}. "
                    f"Must be one of: {', '.join(sorted(_VALID_FAILURE_MODES))}",
                    code="VALIDATION_ERROR",
                )

            # --- Safety: validate settings against hard limits ---
            if settings:
                _SETTING_LIMITS = {
                    "temp_tool": (0.0, _MAX_SAFE_TOOL_TEMP, "\u00b0C"),
                    "temp_bed": (0.0, _MAX_SAFE_BED_TEMP, "\u00b0C"),
                    "speed": (0.0, _MAX_SAFE_SPEED, "mm/s"),
                }
                for key, (lo, hi, unit) in _SETTING_LIMITS.items():
                    raw = settings.get(key)
                    if raw is None:
                        continue
                    try:
                        val = float(raw)
                    except (ValueError, TypeError):
                        return _error_dict(
                            f"Setting {key!r} value {raw!r} is not a valid number.",
                            code="VALIDATION_ERROR",
                        )
                    if val < lo or val > hi:
                        return _error_dict(
                            f"Recorded {key} {val}{unit} is outside safe range "
                            f"({lo}\u2013{hi}{unit}). Outcome rejected to protect hardware.",
                            code="SAFETY_VIOLATION",
                        )

            # --- Resolve printer/file from job if not provided ---
            try:
                job_record = get_db().get_print_record(job_id)
                if job_record and not printer_name:
                    printer_name = job_record.get("printer_name", "unknown")
                if job_record and not file_name:
                    file_name = job_record.get("file_name")
            except Exception as exc:
                logger.debug("Failed to resolve printer/file from job %s: %s", job_id, exc)  # Best-effort resolution

            if not printer_name:
                printer_name = "unknown"

            try:
                row_id = get_db().save_print_outcome({
                    "job_id": job_id,
                    "printer_name": printer_name,
                    "file_name": file_name,
                    "file_hash": file_hash,
                    "material_type": material_type,
                    "outcome": outcome,
                    "quality_grade": quality_grade,
                    "failure_mode": failure_mode,
                    "settings": settings,
                    "environment": environment,
                    "notes": notes,
                    "agent_id": "mcp",
                    "created_at": time.time(),
                })
                return {
                    "success": True,
                    "outcome_id": row_id,
                    "job_id": job_id,
                    "printer_name": printer_name,
                    "outcome": outcome,
                    "quality_grade": quality_grade,
                }
            except Exception as exc:
                _logger.exception("Unexpected error in record_print_outcome")
                return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")

        @mcp.tool()
        def get_printer_insights(
            printer_name: str,
            limit: int = 20,
        ) -> dict:
            """Query cross-printer learning insights for a specific printer.

            Returns success rates, failure mode breakdown, and per-material
            statistics based on previously recorded outcomes.

            **Note**: Insights are advisory.  They do NOT override safety limits
            or preflight checks.

            Args:
                printer_name: The printer to get insights for.
                limit: Maximum recent outcomes to include (default 20).
            """
            if err := _check_auth("learning"):
                return err
            try:
                insights = get_db().get_printer_learning_insights(printer_name)
                recent = get_db().list_print_outcomes(printer_name=printer_name, limit=limit)

                # Confidence level based on sample size
                total = insights.get("total_outcomes", 0)
                if total < 5:
                    confidence = "low"
                elif total < 20:
                    confidence = "medium"
                else:
                    confidence = "high"

                return {
                    "success": True,
                    "printer_name": printer_name,
                    "insights": insights,
                    "recent_outcomes": recent,
                    "confidence": confidence,
                    "safety_notice": _LEARNING_SAFETY_NOTICE,
                }
            except Exception as exc:
                _logger.exception("Unexpected error in get_printer_insights")
                return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")

        @mcp.tool()
        def suggest_printer_for_job(
            file_hash: str | None = None,
            material_type: str | None = None,
            file_name: str | None = None,
        ) -> dict:
            """Suggest the best printer for a job based on historical outcomes.

            Rankings are based on success rates from previously recorded outcomes,
            optionally filtered by file hash or material type.  Cross-references
            the printer registry for current availability.

            **Note**: Suggestions are advisory.  They do NOT override safety limits
            or preflight checks.  Always run preflight validation before starting
            a print regardless of learning data.

            Args:
                file_hash: Optional hash of the file to match previous prints.
                material_type: Optional material type to filter by (e.g. ``"PLA"``).
                file_name: Optional file name (informational, not used for matching).
            """
            if err := _check_auth("learning"):
                return err
            try:
                ranked = get_db().suggest_printer_for_outcome(
                    file_hash=file_hash, material_type=material_type,
                )

                # Cross-reference availability from registry
                try:
                    idle = set(_registry.get_idle_printers())
                except Exception as exc:
                    logger.debug("Failed to get idle printers for ranking: %s", exc)
                    idle = set()

                suggestions = []
                for entry in ranked:
                    pname = entry["printer_name"]
                    rate = entry["success_rate"]
                    total = entry["total_prints"]
                    suggestions.append({
                        "printer_name": pname,
                        "success_rate": rate,
                        "total_prints": total,
                        "score": round(rate * (1 - 1 / (1 + total)), 2),
                        "reason": f"{int(rate * 100)}% success rate ({total} prints)",
                        "currently_available": pname in idle,
                    })

                # Sort by score descending
                suggestions.sort(key=lambda s: s["score"], reverse=True)

                total_outcomes = sum(e["total_prints"] for e in ranked)
                confidence = "low" if total_outcomes < 5 else ("medium" if total_outcomes < 20 else "high")

                return {
                    "success": True,
                    "suggestions": suggestions,
                    "query": {
                        "file_hash": file_hash,
                        "material_type": material_type,
                        "file_name": file_name,
                    },
                    "data_quality": {
                        "total_outcomes": total_outcomes,
                        "printers_with_data": len(ranked),
                        "confidence": confidence,
                    },
                    "safety_notice": _LEARNING_SAFETY_NOTICE,
                }
            except Exception as exc:
                _logger.exception("Unexpected error in suggest_printer_for_job")
                return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")

        @mcp.tool()
        def recommend_settings(
            printer_name: str | None = None,
            material_type: str | None = None,
            file_hash: str | None = None,
        ) -> dict:
            """Recommend print settings based on historical successful outcomes.

            Queries the learning database for settings that produced successful
            prints, filtered by printer, material, and/or file hash.  Returns
            aggregated recommendations (most common temps, speeds, slicer profiles)
            plus the raw successful settings for agent review.

            **Note**: Recommendations are advisory.  They do NOT override safety
            limits or preflight checks.  Always validate settings against printer
            safety profiles before use.

            Args:
                printer_name: Filter by printer (e.g. ``"voron-350"``).
                material_type: Filter by material (e.g. ``"PLA"``, ``"PETG"``).
                file_hash: Filter by file hash for exact file matching.
            """
            if err := _check_auth("learning"):
                return err

            if not printer_name and not material_type and not file_hash:
                return _error_dict(
                    "At least one filter required: printer_name, material_type, or file_hash",
                    code="VALIDATION_ERROR",
                )

            try:
                outcomes = get_db().get_successful_settings(
                    printer_name=printer_name,
                    material_type=material_type,
                    file_hash=file_hash,
                    limit=20,
                )

                if not outcomes:
                    return {
                        "success": True,
                        "has_data": False,
                        "message": "No successful outcomes found for the given criteria.",
                        "query": {
                            "printer_name": printer_name,
                            "material_type": material_type,
                            "file_hash": file_hash,
                        },
                        "safety_notice": _LEARNING_SAFETY_NOTICE,
                    }

                # Aggregate settings across successful outcomes
                temp_tools: list[float] = []
                temp_beds: list[float] = []
                speeds: list[float] = []
                slicer_profiles: list[str] = []
                quality_grades: list[str] = []

                for o in outcomes:
                    settings = o.get("settings") or {}
                    if isinstance(settings, dict):
                        if "temp_tool" in settings:
                            try:
                                temp_tools.append(float(settings["temp_tool"]))
                            except (ValueError, TypeError):
                                pass
                        if "temp_bed" in settings:
                            try:
                                temp_beds.append(float(settings["temp_bed"]))
                            except (ValueError, TypeError):
                                pass
                        if "speed" in settings:
                            try:
                                speeds.append(float(settings["speed"]))
                            except (ValueError, TypeError):
                                pass
                        if "slicer_profile" in settings:
                            slicer_profiles.append(str(settings["slicer_profile"]))
                    if o.get("quality_grade"):
                        quality_grades.append(o["quality_grade"])

                def _median(vals: list[float]) -> float | None:
                    if not vals:
                        return None
                    s = sorted(vals)
                    n = len(s)
                    if n % 2 == 1:
                        return round(s[n // 2], 1)
                    return round((s[n // 2 - 1] + s[n // 2]) / 2, 1)

                def _mode_str(vals: list[str]) -> str | None:
                    if not vals:
                        return None
                    from collections import Counter
                    return Counter(vals).most_common(1)[0][0]

                recommended = {}
                if temp_tools:
                    recommended["temp_tool"] = _median(temp_tools)
                if temp_beds:
                    recommended["temp_bed"] = _median(temp_beds)
                if speeds:
                    recommended["speed"] = _median(speeds)
                if slicer_profiles:
                    recommended["slicer_profile"] = _mode_str(slicer_profiles)

                # Confidence based on sample size
                n = len(outcomes)
                confidence = "low" if n < 3 else ("medium" if n < 10 else "high")

                return {
                    "success": True,
                    "has_data": True,
                    "recommended_settings": recommended,
                    "sample_size": n,
                    "confidence": confidence,
                    "quality_distribution": {
                        grade: quality_grades.count(grade)
                        for grade in ["excellent", "good", "acceptable", "poor"]
                        if quality_grades.count(grade) > 0
                    },
                    "query": {
                        "printer_name": printer_name,
                        "material_type": material_type,
                        "file_hash": file_hash,
                    },
                    "recent_successful_settings": [
                        {
                            "settings": o.get("settings"),
                            "quality_grade": o.get("quality_grade"),
                            "printer_name": o.get("printer_name"),
                            "material_type": o.get("material_type"),
                            "notes": o.get("notes"),
                        }
                        for o in outcomes[:5]  # Only show top 5
                    ],
                    "safety_notice": _LEARNING_SAFETY_NOTICE,
                }
            except Exception as exc:
                _logger.exception("Unexpected error in recommend_settings")
                return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")

        @mcp.tool()
        def save_agent_note(
            key: str,
            value: str,
            scope: str = "global",
            printer_name: str | None = None,
            ttl_seconds: int | None = None,
        ) -> dict:
            """Save a persistent note or preference that survives across sessions.

            Use this to remember printer quirks, calibration findings, material
            preferences, or any operational knowledge worth preserving.

            Args:
                key: Name for this memory (e.g., ``"z_offset_adjustment"``, ``"pla_temp_notes"``).
                value: The information to store.
                scope: Namespace — ``"global"``, ``"fleet"``, or use *printer_name* for printer-specific.
                printer_name: If provided, scope is automatically set to ``"printer:<name>"``.
                ttl_seconds: Optional time-to-live in seconds.  The note will be
                    automatically excluded from queries after this duration.  Pass
                    ``None`` (default) for notes that should never expire.
            """
            if err := _check_auth("memory"):
                return err
            try:
                agent_id = os.environ.get("KILN_AGENT_ID", "default")
                effective_scope = f"printer:{printer_name}" if printer_name else scope
                get_db().save_memory(agent_id, effective_scope, key, value, ttl_seconds=ttl_seconds)
                return {
                    "success": True,
                    "agent_id": agent_id,
                    "scope": effective_scope,
                    "key": key,
                    "ttl_seconds": ttl_seconds,
                }
            except Exception as exc:
                _logger.exception("Unexpected error in save_agent_note")
                return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")

        @mcp.tool()
        def get_agent_context(
            printer_name: str | None = None,
            scope: str | None = None,
        ) -> dict:
            """Retrieve all stored agent memory for context.

            Call this at the start of a session to recall what you've learned
            about printers, materials, and past print outcomes.  Expired entries
            are automatically filtered out.  Each entry includes a ``version``
            field showing how many times it has been updated.

            Args:
                printer_name: If provided, retrieves printer-specific memory.
                scope: Filter by scope (e.g., ``"global"``, ``"fleet"``).
            """
            if err := _check_auth("memory"):
                return err
            try:
                agent_id = os.environ.get("KILN_AGENT_ID", "default")
                effective_scope = f"printer:{printer_name}" if printer_name else scope
                entries = get_db().list_memory(agent_id, scope=effective_scope)
                return {"success": True, "agent_id": agent_id, "entries": entries, "count": len(entries)}
            except Exception as exc:
                _logger.exception("Unexpected error in get_agent_context")
                return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")

        @mcp.tool()
        def delete_agent_note(
            key: str,
            scope: str = "global",
            printer_name: str | None = None,
        ) -> dict:
            """Remove a stored note or preference.

            Args:
                key: The key of the note to delete.
                scope: The scope namespace (default ``"global"``).
                printer_name: If provided, targets ``"printer:<name>"`` scope.
            """
            if err := _check_auth("memory"):
                return err
            try:
                agent_id = os.environ.get("KILN_AGENT_ID", "default")
                effective_scope = f"printer:{printer_name}" if printer_name else scope
                deleted = get_db().delete_memory(agent_id, effective_scope, key)
                if not deleted:
                    return _error_dict(
                        f"No memory entry found for key '{key}' in scope '{effective_scope}'.",
                        code="NOT_FOUND",
                    )
                return {"success": True, "key": key, "scope": effective_scope}
            except Exception as exc:
                _logger.exception("Unexpected error in delete_agent_note")
                return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")

        @mcp.tool()
        def clean_agent_memory() -> dict:
            """Remove all expired agent memory entries.

            Entries with a TTL that has elapsed are permanently deleted.
            Returns the count of entries removed.
            """
            if err := _check_auth("memory"):
                return err
            try:
                deleted = get_db().clean_expired_notes()
                return {
                    "success": True,
                    "deleted_count": deleted,
                    "message": f"Cleaned {deleted} expired memory entries.",
                }
            except Exception as exc:
                _logger.exception("Unexpected error in clean_agent_memory")
                return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")

        _logger.debug("Registered learning and agent memory tools")


plugin = _LearningToolsPlugin()
