"""Community print registry — Waze for 3D printing.

Anonymous, opt-in registry of print outcomes. Aggregates success rates,
optimal settings, and failure patterns across the community. When someone
tries to print a model, Kiln already knows the optimal settings from
thousands of prior prints.

Data is stored locally with optional sync to a community API endpoint.
Privacy: only geometric signatures and settings are shared — never file
contents, user IDs, or file paths.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class CommunityPrintRecord:
    """A single anonymous print outcome for community aggregation."""

    geometric_signature: str
    printer_model: str
    material: str
    settings_hash: str  # hash of settings dict for dedup
    settings: dict[str, Any]
    outcome: str
    quality_grade: str
    failure_mode: str | None
    print_time_seconds: int
    region: str  # "anonymous"
    timestamp: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CommunityInsight:
    """Aggregated community data for a geometric signature."""

    geometric_signature: str
    total_prints: int
    success_rate: float
    top_printer_models: list[dict[str, Any]]  # [{model, count, success_rate}]
    top_materials: list[dict[str, Any]]  # [{material, count, success_rate}]
    recommended_settings: dict[str, Any] | None
    common_failures: list[dict[str, Any]]  # [{mode, count, percentage}]
    average_print_time_seconds: int
    confidence: str  # "low" (<5), "medium" (5-20), "high" (>20)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CommunityStats:
    """Overall community registry statistics."""

    total_records: int
    unique_models: int
    unique_printers: int
    unique_materials: int
    overall_success_rate: float
    last_updated: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_VALID_OUTCOMES = frozenset({"success", "failed", "partial"})
_VALID_GRADES = frozenset({"A", "B", "C", "D", "F"})
_SHARING_SETTING_KEY = "community_sharing_enabled"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def contribute_print(record: CommunityPrintRecord) -> None:
    """Add a print record to the local community registry.

    :param record: The community print record to store.
    :raises ValueError: If outcome or quality_grade is invalid.
    """
    from kiln.persistence import get_db

    if record.outcome not in _VALID_OUTCOMES:
        raise ValueError(f"Invalid outcome {record.outcome!r}. Must be one of: {', '.join(sorted(_VALID_OUTCOMES))}")
    if record.quality_grade not in _VALID_GRADES:
        raise ValueError(
            f"Invalid quality_grade {record.quality_grade!r}. Must be one of: {', '.join(sorted(_VALID_GRADES))}"
        )

    db = get_db()

    with db._write_lock:
        db._conn.execute(
            """
            INSERT INTO community_prints (
                geometric_signature, printer_model, material,
                settings_hash, settings, outcome, quality_grade,
                failure_mode, print_time_seconds, region, timestamp
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.geometric_signature,
                record.printer_model,
                record.material,
                record.settings_hash,
                json.dumps(record.settings),
                record.outcome,
                record.quality_grade,
                record.failure_mode,
                record.print_time_seconds,
                record.region,
                record.timestamp,
            ),
        )
        db._conn.commit()


def get_community_insight(geometric_signature: str) -> CommunityInsight | None:
    """Get aggregated community data for a geometric signature.

    :param geometric_signature: The geometric signature to look up.
    :returns: Aggregated insight or ``None`` if no data exists.
    """
    from kiln.persistence import get_db

    db = get_db()

    rows = db._conn.execute(
        "SELECT * FROM community_prints WHERE geometric_signature = ?",
        (geometric_signature,),
    ).fetchall()

    if not rows:
        return None

    total = len(rows)
    success_count = 0
    printer_stats: dict[str, dict[str, int]] = {}  # {model: {total, success}}
    material_stats: dict[str, dict[str, int]] = {}  # {material: {total, success}}
    failure_counts: dict[str, int] = {}
    total_time = 0
    all_settings: list[dict[str, Any]] = []

    for row in rows:
        row_dict = dict(row)
        outcome = row_dict["outcome"]
        printer = row_dict["printer_model"]
        material = row_dict["material"]
        failure = row_dict.get("failure_mode")
        print_time = row_dict.get("print_time_seconds", 0)

        if outcome == "success":
            success_count += 1
            settings = json.loads(row_dict.get("settings") or "{}")
            if settings:
                all_settings.append(settings)

        # Printer stats
        if printer not in printer_stats:
            printer_stats[printer] = {"total": 0, "success": 0}
        printer_stats[printer]["total"] += 1
        if outcome == "success":
            printer_stats[printer]["success"] += 1

        # Material stats
        if material not in material_stats:
            material_stats[material] = {"total": 0, "success": 0}
        material_stats[material]["total"] += 1
        if outcome == "success":
            material_stats[material]["success"] += 1

        # Failure modes
        if failure:
            failure_counts[failure] = failure_counts.get(failure, 0) + 1

        total_time += print_time

    # Build top printer models
    top_printers = sorted(
        [
            {
                "model": model,
                "count": stats["total"],
                "success_rate": round(stats["success"] / stats["total"], 4) if stats["total"] else 0.0,
            }
            for model, stats in printer_stats.items()
        ],
        key=lambda x: x["count"],
        reverse=True,
    )[:5]

    # Build top materials
    top_materials = sorted(
        [
            {
                "material": mat,
                "count": stats["total"],
                "success_rate": round(stats["success"] / stats["total"], 4) if stats["total"] else 0.0,
            }
            for mat, stats in material_stats.items()
        ],
        key=lambda x: x["count"],
        reverse=True,
    )[:5]

    # Build common failures
    common_failures = sorted(
        [
            {
                "mode": mode,
                "count": count,
                "percentage": round(count / total * 100, 1),
            }
            for mode, count in failure_counts.items()
        ],
        key=lambda x: x["count"],
        reverse=True,
    )

    # Recommended settings from successful prints
    recommended: dict[str, Any] | None = None
    if all_settings:
        recommended = _merge_settings(all_settings)

    # Confidence level
    if total < 5:
        confidence = "low"
    elif total <= 20:
        confidence = "medium"
    else:
        confidence = "high"

    return CommunityInsight(
        geometric_signature=geometric_signature,
        total_prints=total,
        success_rate=round(success_count / total, 4) if total else 0.0,
        top_printer_models=top_printers,
        top_materials=top_materials,
        recommended_settings=recommended,
        common_failures=common_failures,
        average_print_time_seconds=total_time // total if total else 0,
        confidence=confidence,
    )


def _merge_settings(settings_list: list[dict[str, Any]]) -> dict[str, Any]:
    """Merge multiple settings dicts using median for numerics, mode for strings."""
    all_keys: set[str] = set()
    for s in settings_list:
        all_keys.update(s.keys())

    merged: dict[str, Any] = {}
    for key in all_keys:
        values = [s[key] for s in settings_list if key in s]
        if not values:
            continue
        if all(isinstance(v, (int, float)) for v in values):
            sorted_vals = sorted(values)
            n = len(sorted_vals)
            if n % 2 == 1:
                merged[key] = sorted_vals[n // 2]
            else:
                merged[key] = round((sorted_vals[n // 2 - 1] + sorted_vals[n // 2]) / 2, 2)
        else:
            from collections import Counter

            merged[key] = Counter(values).most_common(1)[0][0]

    return merged


def get_community_stats() -> CommunityStats:
    """Return overall community registry statistics."""
    from kiln.persistence import get_db

    db = get_db()

    total = db._conn.execute("SELECT COUNT(*) FROM community_prints").fetchone()[0]
    unique_models = db._conn.execute("SELECT COUNT(DISTINCT geometric_signature) FROM community_prints").fetchone()[0]
    unique_printers = db._conn.execute("SELECT COUNT(DISTINCT printer_model) FROM community_prints").fetchone()[0]
    unique_materials = db._conn.execute("SELECT COUNT(DISTINCT material) FROM community_prints").fetchone()[0]

    success_count = db._conn.execute("SELECT COUNT(*) FROM community_prints WHERE outcome = 'success'").fetchone()[0]

    last_row = db._conn.execute("SELECT MAX(timestamp) FROM community_prints").fetchone()
    last_updated = last_row[0] if last_row and last_row[0] else 0.0

    return CommunityStats(
        total_records=total,
        unique_models=unique_models,
        unique_printers=unique_printers,
        unique_materials=unique_materials,
        overall_success_rate=round(success_count / total, 4) if total else 0.0,
        last_updated=last_updated,
    )


def search_community(
    *,
    printer_model: str | None = None,
    material: str | None = None,
    min_success_rate: float = 0.0,
    limit: int = 20,
) -> list[CommunityInsight]:
    """Search the community registry with optional filters.

    Returns aggregated insights per geometric signature.

    :param printer_model: Filter by printer model.
    :param material: Filter by material.
    :param min_success_rate: Minimum success rate (0.0 - 1.0).
    :param limit: Maximum results.
    """
    from kiln.persistence import get_db

    db = get_db()

    query = "SELECT DISTINCT geometric_signature FROM community_prints WHERE 1=1"
    params: list[Any] = []

    if printer_model:
        query += " AND printer_model = ?"
        params.append(printer_model)
    if material:
        query += " AND material = ?"
        params.append(material)

    query += " LIMIT ?"
    params.append(limit * 2)  # over-fetch to filter by success rate

    rows = db._conn.execute(query, params).fetchall()

    results: list[CommunityInsight] = []
    for row in rows:
        sig = row[0]
        insight = get_community_insight(sig)
        if insight and insight.success_rate >= min_success_rate:
            results.append(insight)
            if len(results) >= limit:
                break

    return results


def opt_in_sharing(enabled: bool = True) -> None:
    """Toggle community data sharing.

    :param enabled: ``True`` to enable sharing, ``False`` to disable.
    """
    from kiln.persistence import get_db

    db = get_db()
    with db._write_lock:
        db._conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (_SHARING_SETTING_KEY, json.dumps(enabled)),
        )
        db._conn.commit()


def is_sharing_enabled() -> bool:
    """Check whether community data sharing is enabled."""
    from kiln.persistence import get_db

    db = get_db()
    row = db._conn.execute(
        "SELECT value FROM settings WHERE key = ?",
        (_SHARING_SETTING_KEY,),
    ).fetchone()

    if row is None:
        return False

    try:
        return json.loads(row[0])
    except (json.JSONDecodeError, TypeError):
        return False
