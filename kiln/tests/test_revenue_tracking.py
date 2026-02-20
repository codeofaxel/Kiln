"""Tests for kiln.revenue_tracking -- creator revenue analytics.

Covers:
- RevenueEntry, ModelRevenueSummary, RevenueDashboard dataclasses
- record_revenue validation and DB writes
- get_model_revenue aggregation
- get_revenue_dashboard with time filtering
- get_revenue_by_marketplace filtering
- get_total_revenue calculation
- export_revenue_csv formatting
- Edge cases: empty DB, refunds, invalid transaction types
"""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from kiln.persistence import KilnDB
from kiln.revenue_tracking import (
    ModelRevenueSummary,
    RevenueDashboard,
    RevenueEntry,
    export_revenue_csv,
    get_model_revenue,
    get_revenue_by_marketplace,
    get_revenue_dashboard,
    get_total_revenue,
    record_revenue,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db(tmp_path):
    """Return a KilnDB backed by a temporary file."""
    db_path = str(tmp_path / "test_kiln.db")
    instance = KilnDB(db_path=db_path)
    yield instance
    instance.close()


def _make_entry(**overrides) -> RevenueEntry:
    """Return a valid RevenueEntry with optional overrides."""
    defaults = {
        "model_id": "model_abc",
        "marketplace": "thingiverse",
        "amount_usd": 9.99,
        "currency": "USD",
        "transaction_type": "sale",
        "description": "Test sale",
        "timestamp": time.time(),
    }
    defaults.update(overrides)
    return RevenueEntry(**defaults)


# ---------------------------------------------------------------------------
# Dataclass tests
# ---------------------------------------------------------------------------


class TestRevenueEntry:
    """RevenueEntry dataclass."""

    def test_to_dict(self):
        entry = _make_entry()
        d = entry.to_dict()
        assert d["model_id"] == "model_abc"
        assert d["amount_usd"] == 9.99
        assert d["transaction_type"] == "sale"

    def test_refund_entry(self):
        entry = _make_entry(transaction_type="refund", amount_usd=5.0)
        d = entry.to_dict()
        assert d["transaction_type"] == "refund"
        assert d["amount_usd"] == 5.0


class TestModelRevenueSummary:
    """ModelRevenueSummary dataclass."""

    def test_to_dict(self):
        s = ModelRevenueSummary(
            model_id="abc",
            title="Test Model",
            total_revenue_usd=100.0,
            total_sales=10,
            total_refunds=1,
            net_revenue_usd=90.0,
            total_platform_fees_usd=2.5,
            creator_net_total_usd=87.5,
            marketplaces=[{"marketplace": "thingiverse", "revenue": 100.0, "sales": 10}],
            first_sale_at=1000.0,
            last_sale_at=2000.0,
        )
        d = s.to_dict()
        assert d["total_sales"] == 10
        assert d["net_revenue_usd"] == 90.0
        assert d["total_platform_fees_usd"] == 2.5
        assert d["creator_net_total_usd"] == 87.5


class TestRevenueDashboard:
    """RevenueDashboard dataclass."""

    def test_to_dict(self):
        summary = ModelRevenueSummary(
            model_id="abc",
            title="Test",
            total_revenue_usd=50.0,
            total_sales=5,
            total_refunds=0,
            net_revenue_usd=50.0,
            total_platform_fees_usd=1.25,
            creator_net_total_usd=48.75,
            marketplaces=[],
            first_sale_at=None,
            last_sale_at=None,
        )
        dash = RevenueDashboard(
            total_revenue_usd=50.0,
            total_sales=5,
            total_models=1,
            total_marketplaces=1,
            net_revenue_usd=50.0,
            total_platform_fees_usd=1.25,
            platform_fee_pct=2.5,
            top_models=[summary],
            monthly_revenue=[],
            marketplace_breakdown=[],
        )
        d = dash.to_dict()
        assert d["total_sales"] == 5
        assert len(d["top_models"]) == 1
        assert d["top_models"][0]["model_id"] == "abc"
        assert d["total_platform_fees_usd"] == 1.25
        assert d["platform_fee_pct"] == 2.5


# ---------------------------------------------------------------------------
# record_revenue
# ---------------------------------------------------------------------------


class TestRecordRevenue:
    """record_revenue validation and DB writes."""

    def test_valid_entry(self, db):
        entry = _make_entry()
        with patch("kiln.persistence.get_db", return_value=db):
            record_revenue(entry)

        row = db._conn.execute("SELECT * FROM revenue").fetchone()
        assert row is not None
        d = dict(row)
        assert d["model_id"] == "model_abc"
        # No fee — model not published through Kiln.
        assert d["platform_fee_usd"] == 0.0

    def test_platform_fee_on_kiln_published_model(self, db):
        # Register model as published through Kiln.
        with db._write_lock:
            db._conn.execute(
                "INSERT INTO published_models (file_hash, marketplace, title, published_at) VALUES (?, ?, ?, ?)",
                ("model_abc", "thingiverse", "Test", 1000.0),
            )
            db._conn.commit()

        entry = _make_entry(amount_usd=100.0)
        with patch("kiln.persistence.get_db", return_value=db):
            record_revenue(entry)
        # Default 2.5% fee on $100 = $2.50
        assert entry.platform_fee_usd == 2.5
        assert entry.creator_net_usd == 97.5

    def test_no_fee_on_non_kiln_model(self, db):
        entry = _make_entry(amount_usd=100.0)
        with patch("kiln.persistence.get_db", return_value=db):
            record_revenue(entry)
        assert entry.platform_fee_usd == 0.0
        assert entry.creator_net_usd == 100.0

    def test_platform_fee_not_on_refunds(self, db):
        # Register model as published.
        with db._write_lock:
            db._conn.execute(
                "INSERT INTO published_models (file_hash, marketplace, title, published_at) VALUES (?, ?, ?, ?)",
                ("model_abc", "thingiverse", "Test", 1000.0),
            )
            db._conn.commit()

        entry = _make_entry(transaction_type="refund", amount_usd=10.0)
        with patch("kiln.persistence.get_db", return_value=db):
            record_revenue(entry)
        assert entry.platform_fee_usd == 0.0
        assert entry.creator_net_usd == 10.0

    def test_custom_platform_fee_via_env(self, db):
        # Register model as published.
        with db._write_lock:
            db._conn.execute(
                "INSERT INTO published_models (file_hash, marketplace, title, published_at) VALUES (?, ?, ?, ?)",
                ("model_abc", "thingiverse", "Test", 1000.0),
            )
            db._conn.commit()

        entry = _make_entry(amount_usd=100.0)
        with (
            patch("kiln.persistence.get_db", return_value=db),
            patch.dict("os.environ", {"KILN_PLATFORM_FEE_PCT": "5.0"}),
        ):
            record_revenue(entry)
        assert entry.platform_fee_usd == 5.0
        assert entry.creator_net_usd == 95.0

    def test_invalid_transaction_type(self, db):
        entry = _make_entry(transaction_type="bribe")
        with patch("kiln.persistence.get_db", return_value=db), pytest.raises(ValueError, match="Invalid transaction_type"):
            record_revenue(entry)

    def test_empty_model_id(self, db):
        entry = _make_entry(model_id="")
        with patch("kiln.persistence.get_db", return_value=db), pytest.raises(ValueError, match="model_id"):
            record_revenue(entry)

    def test_empty_marketplace(self, db):
        entry = _make_entry(marketplace="")
        with patch("kiln.persistence.get_db", return_value=db), pytest.raises(ValueError, match="marketplace"):
            record_revenue(entry)

    def test_all_valid_transaction_types(self, db):
        for tt in ("sale", "royalty", "tip", "refund"):
            entry = _make_entry(transaction_type=tt, model_id=f"model_{tt}")
            with patch("kiln.persistence.get_db", return_value=db):
                record_revenue(entry)

        count = db._conn.execute("SELECT COUNT(*) FROM revenue").fetchone()
        assert dict(count)["COUNT(*)"] == 4


# ---------------------------------------------------------------------------
# get_model_revenue
# ---------------------------------------------------------------------------


class TestGetModelRevenue:
    """get_model_revenue aggregation."""

    def test_no_data(self, db):
        with patch("kiln.persistence.get_db", return_value=db):
            summary = get_model_revenue("nonexistent")
        assert summary.total_sales == 0
        assert summary.net_revenue_usd == 0.0

    def test_with_sales_and_refund(self, db):
        now = time.time()
        entries = [
            _make_entry(amount_usd=10.0, timestamp=now - 100),
            _make_entry(amount_usd=15.0, timestamp=now - 50),
            _make_entry(amount_usd=5.0, transaction_type="refund", timestamp=now),
        ]
        with patch("kiln.persistence.get_db", return_value=db):
            for e in entries:
                record_revenue(e)
            summary = get_model_revenue("model_abc")

        assert summary.total_sales == 2
        assert summary.total_refunds == 1
        assert summary.total_revenue_usd == 25.0
        assert summary.net_revenue_usd == 20.0

    def test_per_marketplace_breakdown(self, db):
        now = time.time()
        with patch("kiln.persistence.get_db", return_value=db):
            record_revenue(_make_entry(marketplace="thingiverse", amount_usd=10.0, timestamp=now))
            record_revenue(_make_entry(marketplace="myminifactory", amount_usd=20.0, timestamp=now))
            summary = get_model_revenue("model_abc")

        assert len(summary.marketplaces) == 2
        mp_names = {m["marketplace"] for m in summary.marketplaces}
        assert "thingiverse" in mp_names
        assert "myminifactory" in mp_names

    def test_first_and_last_sale(self, db):
        now = time.time()
        with patch("kiln.persistence.get_db", return_value=db):
            record_revenue(_make_entry(timestamp=now - 1000))
            record_revenue(_make_entry(timestamp=now))
            summary = get_model_revenue("model_abc")

        assert summary.first_sale_at is not None
        assert summary.last_sale_at is not None
        assert summary.first_sale_at < summary.last_sale_at


# ---------------------------------------------------------------------------
# get_revenue_dashboard
# ---------------------------------------------------------------------------


class TestGetRevenueDashboard:
    """get_revenue_dashboard aggregation."""

    def test_empty_dashboard(self, db):
        with patch("kiln.persistence.get_db", return_value=db):
            dash = get_revenue_dashboard(days=30)
        assert dash.total_sales == 0
        assert dash.total_revenue_usd == 0.0
        assert dash.top_models == []

    def test_dashboard_with_data(self, db):
        now = time.time()
        with patch("kiln.persistence.get_db", return_value=db):
            record_revenue(_make_entry(amount_usd=10.0, timestamp=now - 100))
            record_revenue(_make_entry(amount_usd=20.0, model_id="model_xyz", timestamp=now - 50))
            dash = get_revenue_dashboard(days=30)

        assert dash.total_sales == 2
        assert dash.total_revenue_usd == 30.0
        assert dash.total_models == 2
        assert len(dash.top_models) == 2

    def test_dashboard_respects_time_filter(self, db):
        now = time.time()
        with patch("kiln.persistence.get_db", return_value=db):
            # Old entry (60 days ago).
            record_revenue(_make_entry(amount_usd=100.0, timestamp=now - 60 * 86400))
            # Recent entry.
            record_revenue(_make_entry(amount_usd=10.0, timestamp=now - 100))
            dash = get_revenue_dashboard(days=30)

        assert dash.total_sales == 1
        assert dash.total_revenue_usd == 10.0

    def test_marketplace_breakdown(self, db):
        now = time.time()
        with patch("kiln.persistence.get_db", return_value=db):
            record_revenue(_make_entry(marketplace="thingiverse", amount_usd=30.0, timestamp=now))
            record_revenue(_make_entry(marketplace="myminifactory", amount_usd=70.0, timestamp=now))
            dash = get_revenue_dashboard(days=30)

        assert len(dash.marketplace_breakdown) == 2
        total_pct = sum(m["percentage"] for m in dash.marketplace_breakdown)
        assert abs(total_pct - 100.0) < 0.1


# ---------------------------------------------------------------------------
# get_revenue_by_marketplace
# ---------------------------------------------------------------------------


class TestGetRevenueByMarketplace:
    """get_revenue_by_marketplace filtering."""

    def test_empty_result(self, db):
        with patch("kiln.persistence.get_db", return_value=db):
            entries = get_revenue_by_marketplace("thingiverse", days=30)
        assert entries == []

    def test_filtered_by_marketplace(self, db):
        now = time.time()
        with patch("kiln.persistence.get_db", return_value=db):
            record_revenue(_make_entry(marketplace="thingiverse", timestamp=now))
            record_revenue(_make_entry(marketplace="myminifactory", timestamp=now))
            entries = get_revenue_by_marketplace("thingiverse", days=30)

        assert len(entries) == 1
        assert entries[0].marketplace == "thingiverse"


# ---------------------------------------------------------------------------
# get_total_revenue
# ---------------------------------------------------------------------------


class TestGetTotalRevenue:
    """get_total_revenue calculation."""

    def test_empty_db(self, db):
        with patch("kiln.persistence.get_db", return_value=db):
            total = get_total_revenue()
        assert total == 0.0

    def test_net_revenue(self, db):
        now = time.time()
        with patch("kiln.persistence.get_db", return_value=db):
            record_revenue(_make_entry(amount_usd=100.0, timestamp=now))
            record_revenue(_make_entry(amount_usd=20.0, transaction_type="refund", timestamp=now))
            total = get_total_revenue()
        assert total == 80.0

    def test_with_days_filter(self, db):
        now = time.time()
        with patch("kiln.persistence.get_db", return_value=db):
            record_revenue(_make_entry(amount_usd=50.0, timestamp=now - 60 * 86400))
            record_revenue(_make_entry(amount_usd=10.0, timestamp=now - 100))
            total = get_total_revenue(days=30)
        assert total == 10.0

    def test_all_time(self, db):
        now = time.time()
        with patch("kiln.persistence.get_db", return_value=db):
            record_revenue(_make_entry(amount_usd=50.0, timestamp=now - 365 * 86400))
            record_revenue(_make_entry(amount_usd=10.0, timestamp=now))
            total = get_total_revenue()
        assert total == 60.0


# ---------------------------------------------------------------------------
# export_revenue_csv
# ---------------------------------------------------------------------------


class TestExportRevenueCsv:
    """export_revenue_csv formatting."""

    def test_empty_csv(self, db):
        with patch("kiln.persistence.get_db", return_value=db):
            csv_str = export_revenue_csv(days=30)
        lines = csv_str.strip().split("\n")
        assert len(lines) == 1  # header only
        assert "model_id" in lines[0]

    def test_csv_with_data(self, db):
        now = time.time()
        with patch("kiln.persistence.get_db", return_value=db):
            record_revenue(_make_entry(amount_usd=9.99, timestamp=now))
            record_revenue(_make_entry(amount_usd=19.99, timestamp=now, model_id="model_xyz"))
            csv_str = export_revenue_csv(days=30)

        lines = csv_str.strip().split("\n")
        assert len(lines) == 3  # header + 2 rows
        assert "9.99" in lines[1]
