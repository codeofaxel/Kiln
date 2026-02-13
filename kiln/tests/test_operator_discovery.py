"""Tests for kiln.operator_discovery -- operator marketplace discovery engine.

Covers:
- OperatorListing, DiscoveryQuery, DiscoveryResult dataclasses and to_dict
- Register / remove listings
- Search with individual filter types (material, capability, location, lead
  time, quality, success rate, verified_only)
- Combined filters
- Sorting by composite score (quality * success_rate), verified first
- Empty results
- Stats aggregation
- Accepting orders toggle
- Input validation (all boundary conditions)
- Duplicate listing update
- Thread safety
- Singleton accessor
"""

from __future__ import annotations

import threading
import time
from unittest.mock import patch

import pytest

from kiln.operator_discovery import (
    DiscoveryEngine,
    DiscoveryQuery,
    DiscoveryResult,
    DiscoveryValidationError,
    OperatorListing,
    _validate_listing,
    get_discovery_engine,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_listing(**overrides: object) -> OperatorListing:
    """Create a valid OperatorListing with sensible defaults."""
    defaults = dict(
        operator_id="op-1",
        display_name="TestFarm",
        materials=["PLA"],
        printer_models=["Prusa MK4"],
        capabilities=["enclosure"],
        location="Austin, TX",
        min_lead_time_hours=2.0,
        avg_quality_score=4.0,
        success_rate=0.9,
        verified=False,
        accepting_orders=True,
    )
    defaults.update(overrides)
    return OperatorListing(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Dataclass to_dict
# ---------------------------------------------------------------------------


class TestOperatorListingToDict:
    """OperatorListing.to_dict round-trip."""

    def test_to_dict_keys(self) -> None:
        listing = _make_listing()
        d = listing.to_dict()
        expected_keys = {
            "operator_id",
            "display_name",
            "materials",
            "printer_models",
            "capabilities",
            "location",
            "min_lead_time_hours",
            "avg_quality_score",
            "success_rate",
            "verified",
            "accepting_orders",
        }
        assert set(d.keys()) == expected_keys

    def test_to_dict_values(self) -> None:
        listing = _make_listing(
            operator_id="x",
            materials=["PLA", "PETG"],
            verified=True,
        )
        d = listing.to_dict()
        assert d["operator_id"] == "x"
        assert d["materials"] == ["PLA", "PETG"]
        assert d["verified"] is True

    def test_to_dict_returns_new_lists(self) -> None:
        listing = _make_listing(materials=["PLA"])
        d = listing.to_dict()
        d["materials"].append("ABS")
        assert listing.materials == ["PLA"]


class TestDiscoveryQueryToDict:
    """DiscoveryQuery.to_dict round-trip."""

    def test_default_query(self) -> None:
        q = DiscoveryQuery()
        d = q.to_dict()
        assert d["material"] is None
        assert d["verified_only"] is False

    def test_populated_query(self) -> None:
        q = DiscoveryQuery(material="PLA", verified_only=True)
        d = q.to_dict()
        assert d["material"] == "PLA"
        assert d["verified_only"] is True


class TestDiscoveryResultToDict:
    """DiscoveryResult.to_dict round-trip."""

    def test_empty_result(self) -> None:
        q = DiscoveryQuery()
        r = DiscoveryResult(
            operators=[], total_matches=0, query=q, searched_at=1000.0
        )
        d = r.to_dict()
        assert d["operators"] == []
        assert d["total_matches"] == 0
        assert d["searched_at"] == 1000.0
        assert d["query"]["material"] is None

    def test_result_with_operators(self) -> None:
        listing = _make_listing()
        q = DiscoveryQuery(material="PLA")
        r = DiscoveryResult(
            operators=[listing], total_matches=1, query=q, searched_at=1000.0
        )
        d = r.to_dict()
        assert len(d["operators"]) == 1
        assert d["operators"][0]["operator_id"] == "op-1"


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class TestValidation:
    """Input validation for OperatorListing fields."""

    def test_empty_operator_id(self) -> None:
        with pytest.raises(DiscoveryValidationError, match="operator_id"):
            _validate_listing(_make_listing(operator_id=""))

    def test_whitespace_operator_id(self) -> None:
        with pytest.raises(DiscoveryValidationError, match="operator_id"):
            _validate_listing(_make_listing(operator_id="   "))

    def test_operator_id_too_long(self) -> None:
        with pytest.raises(DiscoveryValidationError, match="operator_id"):
            _validate_listing(_make_listing(operator_id="x" * 101))

    def test_empty_display_name(self) -> None:
        with pytest.raises(DiscoveryValidationError, match="display_name"):
            _validate_listing(_make_listing(display_name=""))

    def test_display_name_too_long(self) -> None:
        with pytest.raises(DiscoveryValidationError, match="display_name"):
            _validate_listing(_make_listing(display_name="x" * 201))

    def test_empty_materials_list(self) -> None:
        with pytest.raises(DiscoveryValidationError, match="materials"):
            _validate_listing(_make_listing(materials=[]))

    def test_empty_string_material(self) -> None:
        with pytest.raises(DiscoveryValidationError, match="material"):
            _validate_listing(_make_listing(materials=[""]))

    def test_material_too_long(self) -> None:
        with pytest.raises(DiscoveryValidationError, match="material"):
            _validate_listing(_make_listing(materials=["x" * 51]))

    def test_empty_printer_models(self) -> None:
        with pytest.raises(DiscoveryValidationError, match="printer_models"):
            _validate_listing(_make_listing(printer_models=[]))

    def test_quality_score_too_low(self) -> None:
        with pytest.raises(DiscoveryValidationError, match="avg_quality_score"):
            _validate_listing(_make_listing(avg_quality_score=-0.1))

    def test_quality_score_too_high(self) -> None:
        with pytest.raises(DiscoveryValidationError, match="avg_quality_score"):
            _validate_listing(_make_listing(avg_quality_score=5.1))

    def test_quality_score_boundary_zero(self) -> None:
        _validate_listing(_make_listing(avg_quality_score=0.0))

    def test_quality_score_boundary_five(self) -> None:
        _validate_listing(_make_listing(avg_quality_score=5.0))

    def test_success_rate_too_low(self) -> None:
        with pytest.raises(DiscoveryValidationError, match="success_rate"):
            _validate_listing(_make_listing(success_rate=-0.01))

    def test_success_rate_too_high(self) -> None:
        with pytest.raises(DiscoveryValidationError, match="success_rate"):
            _validate_listing(_make_listing(success_rate=1.01))

    def test_success_rate_boundary_zero(self) -> None:
        _validate_listing(_make_listing(success_rate=0.0))

    def test_success_rate_boundary_one(self) -> None:
        _validate_listing(_make_listing(success_rate=1.0))

    def test_negative_lead_time(self) -> None:
        with pytest.raises(DiscoveryValidationError, match="min_lead_time_hours"):
            _validate_listing(_make_listing(min_lead_time_hours=-1.0))

    def test_zero_lead_time_ok(self) -> None:
        _validate_listing(_make_listing(min_lead_time_hours=0.0))

    def test_valid_listing_passes(self) -> None:
        _validate_listing(_make_listing())


# ---------------------------------------------------------------------------
# Register / remove
# ---------------------------------------------------------------------------


class TestRegisterRemove:
    """Registering and removing operator listings."""

    def test_register_and_get(self) -> None:
        engine = DiscoveryEngine()
        listing = _make_listing()
        engine.register_listing(listing)
        assert engine.get_listing("op-1") is listing

    def test_get_missing_returns_none(self) -> None:
        engine = DiscoveryEngine()
        assert engine.get_listing("nope") is None

    def test_remove_existing(self) -> None:
        engine = DiscoveryEngine()
        engine.register_listing(_make_listing())
        assert engine.remove_listing("op-1") is True
        assert engine.get_listing("op-1") is None

    def test_remove_nonexistent(self) -> None:
        engine = DiscoveryEngine()
        assert engine.remove_listing("nope") is False

    def test_register_invalid_raises(self) -> None:
        engine = DiscoveryEngine()
        with pytest.raises(DiscoveryValidationError):
            engine.register_listing(_make_listing(operator_id=""))

    def test_duplicate_update(self) -> None:
        engine = DiscoveryEngine()
        engine.register_listing(_make_listing(avg_quality_score=3.0))
        engine.register_listing(_make_listing(avg_quality_score=4.5))
        listing = engine.get_listing("op-1")
        assert listing is not None
        assert listing.avg_quality_score == 4.5


# ---------------------------------------------------------------------------
# Search — individual filters
# ---------------------------------------------------------------------------


class TestSearchFilters:
    """Search with a single filter active at a time."""

    def setup_method(self) -> None:
        self.engine = DiscoveryEngine()
        self.engine.register_listing(
            _make_listing(
                operator_id="a",
                materials=["PLA", "PETG"],
                capabilities=["enclosure", "multi_material"],
                location="Austin, TX",
                min_lead_time_hours=2.0,
                avg_quality_score=4.0,
                success_rate=0.9,
                verified=True,
            )
        )
        self.engine.register_listing(
            _make_listing(
                operator_id="b",
                materials=["ABS", "Nylon"],
                capabilities=["large_format"],
                location="Denver, CO",
                min_lead_time_hours=8.0,
                avg_quality_score=3.0,
                success_rate=0.7,
                verified=False,
            )
        )

    def test_filter_material_match(self) -> None:
        result = self.engine.search(DiscoveryQuery(material="PLA"))
        assert result.total_matches == 1
        assert result.operators[0].operator_id == "a"

    def test_filter_material_case_insensitive(self) -> None:
        result = self.engine.search(DiscoveryQuery(material="pla"))
        assert result.total_matches == 1

    def test_filter_material_no_match(self) -> None:
        result = self.engine.search(DiscoveryQuery(material="TPU"))
        assert result.total_matches == 0

    def test_filter_capability_match(self) -> None:
        result = self.engine.search(DiscoveryQuery(capability="large_format"))
        assert result.total_matches == 1
        assert result.operators[0].operator_id == "b"

    def test_filter_capability_case_insensitive(self) -> None:
        result = self.engine.search(DiscoveryQuery(capability="ENCLOSURE"))
        assert result.total_matches == 1
        assert result.operators[0].operator_id == "a"

    def test_filter_location_substring(self) -> None:
        result = self.engine.search(DiscoveryQuery(location="Austin"))
        assert result.total_matches == 1
        assert result.operators[0].operator_id == "a"

    def test_filter_location_case_insensitive(self) -> None:
        result = self.engine.search(DiscoveryQuery(location="denver"))
        assert result.total_matches == 1

    def test_filter_location_no_match(self) -> None:
        result = self.engine.search(DiscoveryQuery(location="Chicago"))
        assert result.total_matches == 0

    def test_filter_max_lead_time(self) -> None:
        result = self.engine.search(DiscoveryQuery(max_lead_time_hours=4.0))
        assert result.total_matches == 1
        assert result.operators[0].operator_id == "a"

    def test_filter_min_quality_score(self) -> None:
        result = self.engine.search(DiscoveryQuery(min_quality_score=3.5))
        assert result.total_matches == 1
        assert result.operators[0].operator_id == "a"

    def test_filter_min_success_rate(self) -> None:
        result = self.engine.search(DiscoveryQuery(min_success_rate=0.85))
        assert result.total_matches == 1
        assert result.operators[0].operator_id == "a"

    def test_filter_verified_only(self) -> None:
        result = self.engine.search(DiscoveryQuery(verified_only=True))
        assert result.total_matches == 1
        assert result.operators[0].operator_id == "a"

    def test_no_filters_returns_all(self) -> None:
        result = self.engine.search(DiscoveryQuery())
        assert result.total_matches == 2


# ---------------------------------------------------------------------------
# Search — combined filters
# ---------------------------------------------------------------------------


class TestSearchCombined:
    """Search with multiple filters simultaneously."""

    def setup_method(self) -> None:
        self.engine = DiscoveryEngine()
        self.engine.register_listing(
            _make_listing(
                operator_id="a",
                materials=["PLA"],
                capabilities=["enclosure"],
                location="Austin, TX",
                avg_quality_score=4.5,
                success_rate=0.95,
                verified=True,
            )
        )
        self.engine.register_listing(
            _make_listing(
                operator_id="b",
                materials=["PLA"],
                capabilities=["enclosure"],
                location="Denver, CO",
                avg_quality_score=3.0,
                success_rate=0.6,
                verified=False,
            )
        )
        self.engine.register_listing(
            _make_listing(
                operator_id="c",
                materials=["ABS"],
                capabilities=["large_format"],
                location="Austin, TX",
                avg_quality_score=4.8,
                success_rate=0.99,
                verified=True,
            )
        )

    def test_material_and_location(self) -> None:
        result = self.engine.search(
            DiscoveryQuery(material="PLA", location="Austin")
        )
        assert result.total_matches == 1
        assert result.operators[0].operator_id == "a"

    def test_material_and_quality_and_verified(self) -> None:
        result = self.engine.search(
            DiscoveryQuery(
                material="PLA", min_quality_score=4.0, verified_only=True
            )
        )
        assert result.total_matches == 1
        assert result.operators[0].operator_id == "a"

    def test_all_filters_no_match(self) -> None:
        result = self.engine.search(
            DiscoveryQuery(
                material="PLA",
                capability="large_format",
                location="Austin",
                verified_only=True,
            )
        )
        assert result.total_matches == 0

    def test_location_with_none_location_operator(self) -> None:
        self.engine.register_listing(
            _make_listing(
                operator_id="d",
                materials=["PLA"],
                location=None,
            )
        )
        result = self.engine.search(DiscoveryQuery(location="Austin"))
        # "d" has no location so should not match
        ids = [op.operator_id for op in result.operators]
        assert "d" not in ids


# ---------------------------------------------------------------------------
# Search — sorting
# ---------------------------------------------------------------------------


class TestSearchSorting:
    """Verify composite-score sorting and verified-first tiebreaker."""

    def test_sorted_by_composite_score(self) -> None:
        engine = DiscoveryEngine()
        # score: 4.0 * 0.5 = 2.0
        engine.register_listing(
            _make_listing(
                operator_id="low",
                avg_quality_score=4.0,
                success_rate=0.5,
            )
        )
        # score: 3.0 * 0.9 = 2.7
        engine.register_listing(
            _make_listing(
                operator_id="mid",
                avg_quality_score=3.0,
                success_rate=0.9,
            )
        )
        # score: 5.0 * 1.0 = 5.0
        engine.register_listing(
            _make_listing(
                operator_id="high",
                avg_quality_score=5.0,
                success_rate=1.0,
            )
        )
        result = engine.search(DiscoveryQuery())
        ids = [op.operator_id for op in result.operators]
        assert ids == ["high", "mid", "low"]

    def test_verified_operators_first(self) -> None:
        engine = DiscoveryEngine()
        # Unverified with higher composite score
        engine.register_listing(
            _make_listing(
                operator_id="unverified-high",
                avg_quality_score=5.0,
                success_rate=1.0,
                verified=False,
            )
        )
        # Verified with lower composite score
        engine.register_listing(
            _make_listing(
                operator_id="verified-low",
                avg_quality_score=2.0,
                success_rate=0.5,
                verified=True,
            )
        )
        result = engine.search(DiscoveryQuery())
        ids = [op.operator_id for op in result.operators]
        assert ids[0] == "verified-low"
        assert ids[1] == "unverified-high"

    def test_verified_then_score_within_group(self) -> None:
        engine = DiscoveryEngine()
        engine.register_listing(
            _make_listing(
                operator_id="v-low",
                avg_quality_score=2.0,
                success_rate=0.5,
                verified=True,
            )
        )
        engine.register_listing(
            _make_listing(
                operator_id="v-high",
                avg_quality_score=5.0,
                success_rate=1.0,
                verified=True,
            )
        )
        engine.register_listing(
            _make_listing(
                operator_id="u-high",
                avg_quality_score=5.0,
                success_rate=1.0,
                verified=False,
            )
        )
        result = engine.search(DiscoveryQuery())
        ids = [op.operator_id for op in result.operators]
        assert ids == ["v-high", "v-low", "u-high"]


# ---------------------------------------------------------------------------
# Empty results
# ---------------------------------------------------------------------------


class TestEmptyResults:
    """Queries against an empty engine."""

    def test_search_empty_engine(self) -> None:
        engine = DiscoveryEngine()
        result = engine.search(DiscoveryQuery(material="PLA"))
        assert result.total_matches == 0
        assert result.operators == []

    def test_result_has_timestamp(self) -> None:
        engine = DiscoveryEngine()
        before = time.time()
        result = engine.search(DiscoveryQuery())
        after = time.time()
        assert before <= result.searched_at <= after

    def test_result_carries_query(self) -> None:
        engine = DiscoveryEngine()
        q = DiscoveryQuery(material="PLA", verified_only=True)
        result = engine.search(q)
        assert result.query is q


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


class TestStats:
    """get_stats aggregation."""

    def test_stats_empty(self) -> None:
        engine = DiscoveryEngine()
        stats = engine.get_stats()
        assert stats["total_operators"] == 0
        assert stats["verified_count"] == 0
        assert stats["accepting_count"] == 0
        assert stats["material_coverage"] == {}
        assert stats["avg_quality"] == 0.0

    def test_stats_populated(self) -> None:
        engine = DiscoveryEngine()
        engine.register_listing(
            _make_listing(
                operator_id="a",
                materials=["PLA", "PETG"],
                avg_quality_score=4.0,
                verified=True,
                accepting_orders=True,
            )
        )
        engine.register_listing(
            _make_listing(
                operator_id="b",
                materials=["PLA", "ABS"],
                avg_quality_score=3.0,
                verified=False,
                accepting_orders=False,
            )
        )
        stats = engine.get_stats()
        assert stats["total_operators"] == 2
        assert stats["verified_count"] == 1
        assert stats["accepting_count"] == 1
        assert stats["material_coverage"] == {"PLA": 2, "PETG": 1, "ABS": 1}
        assert stats["avg_quality"] == pytest.approx(3.5)


# ---------------------------------------------------------------------------
# Accepting orders toggle
# ---------------------------------------------------------------------------


class TestAcceptingOrders:
    """update_accepting_orders toggle."""

    def test_toggle_off(self) -> None:
        engine = DiscoveryEngine()
        engine.register_listing(_make_listing(accepting_orders=True))
        engine.update_accepting_orders("op-1", accepting=False)
        listing = engine.get_listing("op-1")
        assert listing is not None
        assert listing.accepting_orders is False

    def test_toggle_on(self) -> None:
        engine = DiscoveryEngine()
        engine.register_listing(_make_listing(accepting_orders=False))
        engine.update_accepting_orders("op-1", accepting=True)
        listing = engine.get_listing("op-1")
        assert listing is not None
        assert listing.accepting_orders is True

    def test_toggle_nonexistent_raises(self) -> None:
        engine = DiscoveryEngine()
        with pytest.raises(KeyError, match="not found"):
            engine.update_accepting_orders("nope", accepting=True)


# ---------------------------------------------------------------------------
# Thread safety
# ---------------------------------------------------------------------------


class TestThreadSafety:
    """Concurrent register/search must not corrupt state."""

    def test_concurrent_register_and_search(self) -> None:
        engine = DiscoveryEngine()
        errors: list[Exception] = []

        def _register(start: int) -> None:
            try:
                for i in range(start, start + 50):
                    engine.register_listing(
                        _make_listing(
                            operator_id=f"op-{i}",
                            avg_quality_score=3.0,
                            success_rate=0.8,
                        )
                    )
            except Exception as exc:
                errors.append(exc)

        def _search() -> None:
            try:
                for _ in range(50):
                    engine.search(DiscoveryQuery(material="PLA"))
            except Exception as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=_register, args=(0,)),
            threading.Thread(target=_register, args=(50,)),
            threading.Thread(target=_search),
            threading.Thread(target=_search),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert errors == [], f"Thread errors: {errors}"
        # All 100 listings should be present
        stats = engine.get_stats()
        assert stats["total_operators"] == 100

    def test_concurrent_register_and_remove(self) -> None:
        engine = DiscoveryEngine()
        errors: list[Exception] = []

        # Pre-populate
        for i in range(50):
            engine.register_listing(
                _make_listing(operator_id=f"op-{i}")
            )

        def _remove() -> None:
            try:
                for i in range(50):
                    engine.remove_listing(f"op-{i}")
            except Exception as exc:
                errors.append(exc)

        def _register() -> None:
            try:
                for i in range(50, 100):
                    engine.register_listing(
                        _make_listing(operator_id=f"op-{i}")
                    )
            except Exception as exc:
                errors.append(exc)

        t1 = threading.Thread(target=_remove)
        t2 = threading.Thread(target=_register)
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        assert errors == []
        # The 50 originals should be removed, 50 new ones added
        stats = engine.get_stats()
        assert stats["total_operators"] == 50


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------


class TestSingleton:
    """get_discovery_engine returns the same instance."""

    def test_singleton_identity(self) -> None:
        # Reset module-level singleton for isolation
        import kiln.operator_discovery as mod

        mod._engine = None
        try:
            e1 = get_discovery_engine()
            e2 = get_discovery_engine()
            assert e1 is e2
        finally:
            mod._engine = None

    def test_singleton_thread_safe(self) -> None:
        import kiln.operator_discovery as mod

        mod._engine = None
        results: list[DiscoveryEngine] = []

        def _get() -> None:
            results.append(get_discovery_engine())

        threads = [threading.Thread(target=_get) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        mod._engine = None
        assert len(results) == 10
        assert all(r is results[0] for r in results)
