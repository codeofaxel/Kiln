"""Tests for community_registry module.

Covers contributing records, retrieving insights, stats, search,
and sharing opt-in/out.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from kiln.community_registry import (
    CommunityInsight,
    CommunityPrintRecord,
    CommunityStats,
    contribute_print,
    get_community_insight,
    get_community_stats,
    is_sharing_enabled,
    opt_in_sharing,
    search_community,
)
from kiln.persistence import KilnDB

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db(tmp_path: Path) -> KilnDB:
    """Fresh database for each test."""
    db_path = str(tmp_path / "test.db")
    instance = KilnDB(db_path=db_path)
    yield instance
    instance.close()


@pytest.fixture(autouse=True)
def _patch_db(db: KilnDB) -> None:
    """Patch get_db to return the test database."""
    with patch("kiln.persistence.get_db", return_value=db):
        yield


def _make_record(**overrides: Any) -> CommunityPrintRecord:
    """Create a CommunityPrintRecord with sensible defaults."""
    settings = overrides.pop("settings", {"layer_height": 0.2, "speed": 50})
    defaults = {
        "geometric_signature": "sig_abc123",
        "printer_model": "ender3",
        "material": "PLA",
        "settings_hash": hashlib.sha256(json.dumps(settings, sort_keys=True).encode()).hexdigest()[:16],
        "settings": settings,
        "outcome": "success",
        "quality_grade": "B",
        "failure_mode": None,
        "print_time_seconds": 3600,
        "region": "anonymous",
        "timestamp": time.time(),
    }
    defaults.update(overrides)
    return CommunityPrintRecord(**defaults)


# ---------------------------------------------------------------------------
# CommunityPrintRecord dataclass
# ---------------------------------------------------------------------------


class TestCommunityPrintRecord:
    def test_to_dict(self) -> None:
        record = _make_record()
        d = record.to_dict()
        assert d["geometric_signature"] == "sig_abc123"
        assert d["printer_model"] == "ender3"
        assert d["outcome"] == "success"
        assert isinstance(d, dict)


class TestCommunityInsight:
    def test_to_dict(self) -> None:
        insight = CommunityInsight(
            geometric_signature="sig_abc",
            total_prints=10,
            success_rate=0.8,
            top_printer_models=[],
            top_materials=[],
            recommended_settings=None,
            common_failures=[],
            average_print_time_seconds=3600,
            confidence="medium",
        )
        d = insight.to_dict()
        assert d["total_prints"] == 10
        assert d["confidence"] == "medium"


class TestCommunityStats:
    def test_to_dict(self) -> None:
        stats = CommunityStats(
            total_records=100,
            unique_models=50,
            unique_printers=5,
            unique_materials=3,
            overall_success_rate=0.85,
            last_updated=time.time(),
        )
        d = stats.to_dict()
        assert d["total_records"] == 100


# ---------------------------------------------------------------------------
# contribute_print
# ---------------------------------------------------------------------------


class TestContributePrint:
    def test_contribute_and_retrieve(self) -> None:
        record = _make_record()
        contribute_print(record)

        insight = get_community_insight(record.geometric_signature)
        assert insight is not None
        assert insight.total_prints == 1

    def test_contribute_multiple_records(self) -> None:
        sig = "sig_multi"
        contribute_print(_make_record(geometric_signature=sig))
        contribute_print(_make_record(geometric_signature=sig, printer_model="voron"))

        insight = get_community_insight(sig)
        assert insight is not None
        assert insight.total_prints == 2

    def test_invalid_outcome_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid outcome"):
            contribute_print(_make_record(outcome="amazing"))

    def test_invalid_grade_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid quality_grade"):
            contribute_print(_make_record(quality_grade="Z"))

    def test_contribute_with_failure_mode(self) -> None:
        record = _make_record(
            outcome="failed",
            quality_grade="F",
            failure_mode="spaghetti",
        )
        contribute_print(record)

        insight = get_community_insight(record.geometric_signature)
        assert insight is not None
        assert len(insight.common_failures) == 1
        assert insight.common_failures[0]["mode"] == "spaghetti"


# ---------------------------------------------------------------------------
# get_community_insight
# ---------------------------------------------------------------------------


class TestGetCommunityInsight:
    def test_no_data_returns_none(self) -> None:
        insight = get_community_insight("nonexistent_sig")
        assert insight is None

    def test_success_rate_calculation(self) -> None:
        sig = "sig_rate"
        contribute_print(_make_record(geometric_signature=sig, outcome="success"))
        contribute_print(_make_record(geometric_signature=sig, outcome="success"))
        contribute_print(_make_record(geometric_signature=sig, outcome="failed", quality_grade="F"))

        insight = get_community_insight(sig)
        assert insight is not None
        assert abs(insight.success_rate - 2.0 / 3.0) < 0.01

    def test_top_printer_models(self) -> None:
        sig = "sig_printers"
        contribute_print(_make_record(geometric_signature=sig, printer_model="ender3"))
        contribute_print(_make_record(geometric_signature=sig, printer_model="ender3"))
        contribute_print(_make_record(geometric_signature=sig, printer_model="voron"))

        insight = get_community_insight(sig)
        assert insight is not None
        assert len(insight.top_printer_models) >= 2
        top = insight.top_printer_models[0]
        assert top["model"] == "ender3"
        assert top["count"] == 2

    def test_top_materials(self) -> None:
        sig = "sig_mats"
        contribute_print(_make_record(geometric_signature=sig, material="PLA"))
        contribute_print(_make_record(geometric_signature=sig, material="PLA"))
        contribute_print(_make_record(geometric_signature=sig, material="PETG"))

        insight = get_community_insight(sig)
        assert insight is not None
        assert insight.top_materials[0]["material"] == "PLA"

    def test_recommended_settings(self) -> None:
        sig = "sig_settings"
        contribute_print(
            _make_record(
                geometric_signature=sig,
                outcome="success",
                settings={"speed": 50, "temp": 200},
            )
        )
        contribute_print(
            _make_record(
                geometric_signature=sig,
                outcome="success",
                settings={"speed": 60, "temp": 210},
            )
        )

        insight = get_community_insight(sig)
        assert insight is not None
        assert insight.recommended_settings is not None
        assert "speed" in insight.recommended_settings

    def test_confidence_low(self) -> None:
        sig = "sig_low"
        contribute_print(_make_record(geometric_signature=sig))

        insight = get_community_insight(sig)
        assert insight is not None
        assert insight.confidence == "low"

    def test_confidence_medium(self) -> None:
        sig = "sig_med"
        for i in range(10):
            contribute_print(
                _make_record(
                    geometric_signature=sig,
                    printer_model=f"printer_{i}",
                )
            )

        insight = get_community_insight(sig)
        assert insight is not None
        assert insight.confidence == "medium"

    def test_confidence_high(self) -> None:
        sig = "sig_high"
        for i in range(25):
            contribute_print(
                _make_record(
                    geometric_signature=sig,
                    printer_model=f"printer_{i % 5}",
                )
            )

        insight = get_community_insight(sig)
        assert insight is not None
        assert insight.confidence == "high"

    def test_average_print_time(self) -> None:
        sig = "sig_time"
        contribute_print(
            _make_record(
                geometric_signature=sig,
                print_time_seconds=1000,
            )
        )
        contribute_print(
            _make_record(
                geometric_signature=sig,
                print_time_seconds=2000,
            )
        )

        insight = get_community_insight(sig)
        assert insight is not None
        assert insight.average_print_time_seconds == 1500


# ---------------------------------------------------------------------------
# get_community_stats
# ---------------------------------------------------------------------------


class TestGetCommunityStats:
    def test_empty_stats(self) -> None:
        stats = get_community_stats()
        assert stats.total_records == 0
        assert stats.overall_success_rate == 0.0

    def test_populated_stats(self) -> None:
        contribute_print(
            _make_record(
                geometric_signature="sig_a",
                printer_model="ender3",
                material="PLA",
                outcome="success",
            )
        )
        contribute_print(
            _make_record(
                geometric_signature="sig_b",
                printer_model="voron",
                material="PETG",
                outcome="failed",
                quality_grade="F",
            )
        )

        stats = get_community_stats()
        assert stats.total_records == 2
        assert stats.unique_models == 2
        assert stats.unique_printers == 2
        assert stats.unique_materials == 2
        assert stats.overall_success_rate == 0.5
        assert stats.last_updated > 0


# ---------------------------------------------------------------------------
# search_community
# ---------------------------------------------------------------------------


class TestSearchCommunity:
    def test_search_all(self) -> None:
        contribute_print(_make_record(geometric_signature="sig_s1"))
        contribute_print(_make_record(geometric_signature="sig_s2"))

        results = search_community()
        assert len(results) == 2

    def test_search_by_printer(self) -> None:
        contribute_print(
            _make_record(
                geometric_signature="sig_sp",
                printer_model="ender3",
            )
        )
        contribute_print(
            _make_record(
                geometric_signature="sig_sp2",
                printer_model="voron",
            )
        )

        results = search_community(printer_model="ender3")
        assert len(results) == 1

    def test_search_by_material(self) -> None:
        contribute_print(
            _make_record(
                geometric_signature="sig_sm",
                material="PLA",
            )
        )
        contribute_print(
            _make_record(
                geometric_signature="sig_sm2",
                material="PETG",
            )
        )

        results = search_community(material="PLA")
        assert len(results) == 1

    def test_search_min_success_rate(self) -> None:
        sig = "sig_rate_filter"
        contribute_print(
            _make_record(
                geometric_signature=sig,
                outcome="success",
            )
        )
        contribute_print(
            _make_record(
                geometric_signature=sig,
                outcome="failed",
                quality_grade="F",
            )
        )

        # 50% success rate, filter for >60%
        results = search_community(min_success_rate=0.6)
        sigs = [r.geometric_signature for r in results]
        assert sig not in sigs

    def test_search_limit(self) -> None:
        for i in range(5):
            contribute_print(_make_record(geometric_signature=f"sig_lim_{i}"))

        results = search_community(limit=2)
        assert len(results) <= 2


# ---------------------------------------------------------------------------
# opt_in_sharing
# ---------------------------------------------------------------------------


class TestSharingOptIn:
    def test_default_disabled(self) -> None:
        assert is_sharing_enabled() is False

    def test_enable_sharing(self) -> None:
        opt_in_sharing(True)
        assert is_sharing_enabled() is True

    def test_disable_sharing(self) -> None:
        opt_in_sharing(True)
        opt_in_sharing(False)
        assert is_sharing_enabled() is False

    def test_toggle_sharing(self) -> None:
        opt_in_sharing(True)
        assert is_sharing_enabled() is True
        opt_in_sharing(False)
        assert is_sharing_enabled() is False
        opt_in_sharing(True)
        assert is_sharing_enabled() is True
