"""Revenue tracking for 3D model creators.

Tracks earnings from published models across marketplaces. Provides
per-model, per-marketplace, and aggregate revenue analytics. Enables
the "think → print → earn" loop.
"""

from __future__ import annotations

import csv
import io
import logging
import math
import os
import time
from dataclasses import asdict, dataclass
from typing import Any

_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class RevenueEntry:
    """A single revenue event (sale, royalty, tip, or refund)."""

    model_id: str  # file_hash or listing_id
    marketplace: str
    amount_usd: float
    currency: str
    transaction_type: str  # "sale", "royalty", "tip", "refund"
    description: str
    timestamp: float
    platform_fee_usd: float = 0.0  # Kiln platform fee deducted
    creator_net_usd: float = 0.0  # Amount after fee

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ModelRevenueSummary:
    """Revenue summary for a single model."""

    model_id: str
    title: str
    total_revenue_usd: float
    total_sales: int
    total_refunds: int
    net_revenue_usd: float
    total_platform_fees_usd: float
    creator_net_total_usd: float
    marketplaces: list[dict[str, Any]]  # [{marketplace, revenue, sales}]
    first_sale_at: float | None
    last_sale_at: float | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RevenueDashboard:
    """Aggregate revenue dashboard across all models and marketplaces."""

    total_revenue_usd: float
    total_sales: int
    total_models: int
    total_marketplaces: int
    net_revenue_usd: float
    total_platform_fees_usd: float
    platform_fee_pct: float
    top_models: list[ModelRevenueSummary]
    monthly_revenue: list[dict[str, Any]]  # [{month, revenue, sales}]
    marketplace_breakdown: list[dict[str, Any]]  # [{marketplace, revenue, percentage}]

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["top_models"] = [m.to_dict() for m in self.top_models]
        return data


# ---------------------------------------------------------------------------
# Valid transaction types
# ---------------------------------------------------------------------------

_VALID_TRANSACTION_TYPES = {"sale", "royalty", "tip", "refund"}

# ---------------------------------------------------------------------------
# Platform fee configuration
# ---------------------------------------------------------------------------

# Kiln takes a small platform fee on tracked revenue (default 2.5%).
# Configurable via KILN_PLATFORM_FEE_PCT (0.0–15.0).
_DEFAULT_PLATFORM_FEE_PCT = 2.5
_MAX_PLATFORM_FEE_PCT = 15.0


def _get_platform_fee_pct() -> float:
    """Return the platform fee percentage, respecting env var override."""
    raw = os.environ.get("KILN_PLATFORM_FEE_PCT")
    if raw is None:
        return _DEFAULT_PLATFORM_FEE_PCT
    try:
        val = float(raw)
    except ValueError:
        _logger.warning(
            "Invalid KILN_PLATFORM_FEE_PCT %r, using default %.1f%%",
            raw,
            _DEFAULT_PLATFORM_FEE_PCT,
        )
        return _DEFAULT_PLATFORM_FEE_PCT
    return max(0.0, min(val, _MAX_PLATFORM_FEE_PCT))


# ---------------------------------------------------------------------------
# Core functions
# ---------------------------------------------------------------------------


def record_revenue(entry: RevenueEntry) -> None:
    """Record a revenue event in the database."""
    from kiln.persistence import get_db

    if entry.transaction_type not in _VALID_TRANSACTION_TYPES:
        raise ValueError(
            f"Invalid transaction_type {entry.transaction_type!r}. "
            f"Valid options: {', '.join(sorted(_VALID_TRANSACTION_TYPES))}"
        )

    if not entry.model_id:
        raise ValueError("model_id is required")

    if not entry.marketplace:
        raise ValueError("marketplace is required")

    if not isinstance(entry.amount_usd, (int, float)) or math.isnan(entry.amount_usd) or math.isinf(entry.amount_usd):
        raise ValueError("amount_usd must be a valid finite number")

    if entry.amount_usd < 0 and entry.transaction_type != "refund":
        raise ValueError("amount_usd must be non-negative for non-refund transactions")

    db = get_db()
    fee_pct = _get_platform_fee_pct()

    with db._write_lock:
        # Check if model was published through Kiln (inside lock to avoid TOCTOU).
        is_kiln_published = (
            db._conn.execute(
                "SELECT 1 FROM published_models WHERE file_hash = ? OR listing_id = ? LIMIT 1",
                (entry.model_id, entry.model_id),
            ).fetchone()
            is not None
        )

        if is_kiln_published and entry.transaction_type != "refund" and entry.amount_usd > 0:
            entry.platform_fee_usd = round(entry.amount_usd * fee_pct / 100.0, 2)
            entry.creator_net_usd = round(entry.amount_usd - entry.platform_fee_usd, 2)
        else:
            entry.platform_fee_usd = 0.0
            entry.creator_net_usd = entry.amount_usd
        db._conn.execute(
            """
            INSERT INTO revenue
                (model_id, marketplace, amount_usd, currency,
                 transaction_type, description, timestamp,
                 platform_fee_usd, creator_net_usd)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entry.model_id,
                entry.marketplace,
                entry.amount_usd,
                entry.currency,
                entry.transaction_type,
                entry.description,
                entry.timestamp,
                entry.platform_fee_usd,
                entry.creator_net_usd,
            ),
        )
        db._conn.commit()


def get_model_revenue(model_id: str) -> ModelRevenueSummary:
    """Get revenue summary for a specific model."""
    from kiln.persistence import get_db

    db = get_db()

    rows = db._conn.execute(
        "SELECT * FROM revenue WHERE model_id = ? ORDER BY timestamp ASC",
        (model_id,),
    ).fetchall()

    entries = [dict(r) for r in rows]

    # Calculate totals.
    total_revenue = sum(e["amount_usd"] for e in entries if e["transaction_type"] != "refund")
    total_refunds_amount = sum(e["amount_usd"] for e in entries if e["transaction_type"] == "refund")
    total_sales = sum(1 for e in entries if e["transaction_type"] == "sale")
    total_refunds = sum(1 for e in entries if e["transaction_type"] == "refund")

    # Per-marketplace breakdown.
    marketplace_data: dict[str, dict[str, Any]] = {}
    for e in entries:
        mp = e["marketplace"]
        if mp not in marketplace_data:
            marketplace_data[mp] = {"marketplace": mp, "revenue": 0.0, "sales": 0}
        if e["transaction_type"] != "refund":
            marketplace_data[mp]["revenue"] += e["amount_usd"]
        if e["transaction_type"] == "sale":
            marketplace_data[mp]["sales"] += 1

    # Find timestamps.
    sale_timestamps = [e["timestamp"] for e in entries if e["transaction_type"] == "sale"]
    first_sale = min(sale_timestamps) if sale_timestamps else None
    last_sale = max(sale_timestamps) if sale_timestamps else None

    # Resolve title from published_models if available.
    title_row = db._conn.execute(
        "SELECT title FROM published_models WHERE file_hash = ? LIMIT 1",
        (model_id,),
    ).fetchone()
    title = dict(title_row)["title"] if title_row else model_id

    # Sum platform fees from DB rows.
    total_fees = sum(float(e.get("platform_fee_usd") or 0.0) for e in entries)
    creator_net = round(total_revenue - total_refunds_amount - total_fees, 2)

    return ModelRevenueSummary(
        model_id=model_id,
        title=title,
        total_revenue_usd=round(total_revenue, 2),
        total_sales=total_sales,
        total_refunds=total_refunds,
        net_revenue_usd=round(total_revenue - total_refunds_amount, 2),
        total_platform_fees_usd=round(total_fees, 2),
        creator_net_total_usd=creator_net,
        marketplaces=list(marketplace_data.values()),
        first_sale_at=first_sale,
        last_sale_at=last_sale,
    )


def get_revenue_dashboard(*, days: int = 30) -> RevenueDashboard:
    """Get aggregate revenue dashboard for recent activity."""
    from kiln.persistence import get_db

    db = get_db()
    cutoff = time.time() - (days * 86400)

    rows = db._conn.execute(
        "SELECT * FROM revenue WHERE timestamp >= ? ORDER BY timestamp ASC",
        (cutoff,),
    ).fetchall()

    entries = [dict(r) for r in rows]

    # Totals.
    total_revenue = sum(e["amount_usd"] for e in entries if e["transaction_type"] != "refund")
    total_refunds_amount = sum(e["amount_usd"] for e in entries if e["transaction_type"] == "refund")
    total_sales = sum(1 for e in entries if e["transaction_type"] == "sale")
    model_ids = {e["model_id"] for e in entries}
    marketplace_names = {e["marketplace"] for e in entries}

    # Per-marketplace breakdown.
    mp_revenue: dict[str, float] = {}
    for e in entries:
        mp = e["marketplace"]
        if e["transaction_type"] != "refund":
            mp_revenue[mp] = mp_revenue.get(mp, 0.0) + e["amount_usd"]

    marketplace_breakdown = []
    for mp, rev in sorted(mp_revenue.items(), key=lambda x: x[1], reverse=True):
        pct = (rev / total_revenue * 100) if total_revenue > 0 else 0.0
        marketplace_breakdown.append(
            {
                "marketplace": mp,
                "revenue": round(rev, 2),
                "percentage": round(pct, 1),
            }
        )

    # Monthly revenue.
    monthly: dict[str, dict[str, Any]] = {}
    for e in entries:
        import datetime

        dt = datetime.datetime.fromtimestamp(e["timestamp"], tz=datetime.timezone.utc)
        month_key = dt.strftime("%Y-%m")
        if month_key not in monthly:
            monthly[month_key] = {"month": month_key, "revenue": 0.0, "sales": 0}
        if e["transaction_type"] != "refund":
            monthly[month_key]["revenue"] += e["amount_usd"]
        if e["transaction_type"] == "sale":
            monthly[month_key]["sales"] += 1

    monthly_revenue = [
        {
            "month": v["month"],
            "revenue": round(v["revenue"], 2),
            "sales": v["sales"],
        }
        for v in sorted(monthly.values(), key=lambda x: x["month"])
    ]

    # Top models by revenue.
    model_rev: dict[str, float] = {}
    for e in entries:
        mid = e["model_id"]
        if e["transaction_type"] != "refund":
            model_rev[mid] = model_rev.get(mid, 0.0) + e["amount_usd"]

    top_model_ids = sorted(model_rev, key=lambda m: model_rev[m], reverse=True)[:10]
    top_models = [get_model_revenue(mid) for mid in top_model_ids]

    # Sum platform fees.
    total_fees = sum(float(e.get("platform_fee_usd") or 0.0) for e in entries)

    return RevenueDashboard(
        total_revenue_usd=round(total_revenue, 2),
        total_sales=total_sales,
        total_models=len(model_ids),
        total_marketplaces=len(marketplace_names),
        net_revenue_usd=round(total_revenue - total_refunds_amount, 2),
        total_platform_fees_usd=round(total_fees, 2),
        platform_fee_pct=_get_platform_fee_pct(),
        top_models=top_models,
        monthly_revenue=monthly_revenue,
        marketplace_breakdown=marketplace_breakdown,
    )


def get_revenue_by_marketplace(
    marketplace: str,
    *,
    days: int = 30,
) -> list[RevenueEntry]:
    """Get revenue entries for a specific marketplace."""
    from kiln.persistence import get_db

    db = get_db()
    cutoff = time.time() - (days * 86400)

    rows = db._conn.execute(
        "SELECT * FROM revenue WHERE marketplace = ? AND timestamp >= ? ORDER BY timestamp DESC",
        (marketplace, cutoff),
    ).fetchall()

    return [
        RevenueEntry(
            model_id=dict(r)["model_id"],
            marketplace=dict(r)["marketplace"],
            amount_usd=dict(r)["amount_usd"],
            currency=dict(r)["currency"],
            transaction_type=dict(r)["transaction_type"],
            description=dict(r)["description"] or "",
            timestamp=dict(r)["timestamp"],
        )
        for r in rows
    ]


def get_total_revenue(*, days: int | None = None) -> float:
    """Get total net revenue (sales + tips + royalties - refunds)."""
    from kiln.persistence import get_db

    db = get_db()

    if days is not None:
        cutoff = time.time() - (days * 86400)
        row = db._conn.execute(
            """
            SELECT
                COALESCE(SUM(CASE WHEN transaction_type != 'refund' THEN amount_usd ELSE 0 END), 0)
                - COALESCE(SUM(CASE WHEN transaction_type = 'refund' THEN amount_usd ELSE 0 END), 0)
                AS net
            FROM revenue WHERE timestamp >= ?
            """,
            (cutoff,),
        ).fetchone()
    else:
        row = db._conn.execute(
            """
            SELECT
                COALESCE(SUM(CASE WHEN transaction_type != 'refund' THEN amount_usd ELSE 0 END), 0)
                - COALESCE(SUM(CASE WHEN transaction_type = 'refund' THEN amount_usd ELSE 0 END), 0)
                AS net
            FROM revenue
            """,
        ).fetchone()

    return round(dict(row)["net"], 2) if row else 0.0


def export_revenue_csv(*, days: int = 30) -> str:
    """Export revenue entries as a CSV string."""
    from kiln.persistence import get_db

    db = get_db()
    cutoff = time.time() - (days * 86400)

    rows = db._conn.execute(
        "SELECT * FROM revenue WHERE timestamp >= ? ORDER BY timestamp ASC",
        (cutoff,),
    ).fetchall()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "model_id",
            "marketplace",
            "amount_usd",
            "currency",
            "transaction_type",
            "description",
            "timestamp",
        ]
    )

    for r in rows:
        d = dict(r)
        writer.writerow(
            [
                d["model_id"],
                d["marketplace"],
                d["amount_usd"],
                d["currency"],
                d["transaction_type"],
                d.get("description", ""),
                d["timestamp"],
            ]
        )

    return output.getvalue()
