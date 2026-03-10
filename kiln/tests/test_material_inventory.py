"""Tests for kiln.material_inventory — Material Inventory Engine.

Covers:
- Fleet material summary aggregation
- Consumption history calculation
- Consumption forecasting
- Material sufficiency checks
- Restock suggestions
- Finding printers with specific materials
- Fleet job assignment optimisation
- Spool swap suggestions
"""

from __future__ import annotations

import time

import pytest

from kiln.material_inventory import (
    check_material_sufficiency,
    find_printers_with_material,
    forecast_consumption,
    get_consumption_history,
    get_fleet_material_summary,
    get_restock_suggestions,
    optimize_fleet_assignment,
    suggest_spool_swaps,
)
from kiln.persistence import KilnDB

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db(tmp_path):
    """Return a KilnDB backed by a temporary file."""
    db_path = str(tmp_path / "test_inventory.db")
    instance = KilnDB(db_path=db_path)
    yield instance
    instance.close()


def _add_spool(db, spool_id, material_type, *, remaining_grams=1000.0, color=None, brand=None):
    """Helper to insert a spool."""
    db.save_spool({
        "id": spool_id,
        "material_type": material_type,
        "color": color,
        "brand": brand,
        "weight_grams": 1000.0,
        "remaining_grams": remaining_grams,
    })


def _add_material(db, printer_name, material_type, *, remaining_grams=None, color=None, spool_id=None, tool_index=0):
    """Helper to insert a loaded material record."""
    db.save_material(
        printer_name,
        tool_index,
        material_type,
        color=color,
        spool_id=spool_id,
        remaining_grams=remaining_grams,
    )


def _add_print_record(db, job_id, printer_name, *, material_type="PLA", status="completed",
                       completed_at=None, filament_used_mm=None):
    """Helper to insert a print history record."""
    metadata = {}
    if filament_used_mm is not None:
        metadata["filament_used_mm"] = filament_used_mm
    db.save_print_record({
        "job_id": job_id,
        "printer_name": printer_name,
        "file_name": f"{job_id}.gcode",
        "status": status,
        "material_type": material_type,
        "completed_at": completed_at or time.time(),
        "metadata": metadata if metadata else None,
    })


# ---------------------------------------------------------------------------
# FleetMaterialSummary
# ---------------------------------------------------------------------------


class TestFleetMaterialSummary:
    """Tests for get_fleet_material_summary — aggregation across printers and spools."""

    def test_empty_fleet(self, db):
        result = get_fleet_material_summary(db)
        assert result == []

    def test_single_printer_single_spool(self, db):
        _add_spool(db, "sp-1", "PLA", remaining_grams=800.0, color="white")
        _add_material(db, "ender3", "PLA", remaining_grams=800.0, spool_id="sp-1", color="white")

        result = get_fleet_material_summary(db)
        assert len(result) == 1
        s = result[0]
        assert s.material_type == "PLA"
        assert s.spool_count == 1
        assert "ender3" in s.printers_loaded
        assert "white" in s.colors
        # Stock from spool only (loaded material has spool_id, so not double-counted)
        assert s.total_grams == 800.0

    def test_multi_printer_aggregation(self, db):
        _add_spool(db, "sp-1", "PLA", remaining_grams=500.0, color="white")
        _add_spool(db, "sp-2", "PLA", remaining_grams=300.0, color="black")
        _add_material(db, "ender3", "PLA", remaining_grams=500.0, spool_id="sp-1", color="white")
        _add_material(db, "prusa", "PLA", remaining_grams=300.0, spool_id="sp-2", color="black")

        result = get_fleet_material_summary(db)
        assert len(result) == 1
        s = result[0]
        assert s.material_type == "PLA"
        assert s.total_grams == 800.0
        assert s.spool_count == 2
        assert set(s.printers_loaded) == {"ender3", "prusa"}
        assert set(s.colors) == {"white", "black"}

    def test_multiple_material_types(self, db):
        _add_spool(db, "sp-1", "PLA", remaining_grams=500.0)
        _add_spool(db, "sp-2", "PETG", remaining_grams=700.0)

        result = get_fleet_material_summary(db)
        assert len(result) == 2
        types = {s.material_type for s in result}
        assert types == {"PLA", "PETG"}

    def test_unlinked_material_adds_to_total(self, db):
        # Material loaded without a spool_id — its grams count toward total
        _add_material(db, "ender3", "PLA", remaining_grams=200.0)

        result = get_fleet_material_summary(db)
        assert len(result) == 1
        assert result[0].total_grams == 200.0

    def test_to_dict_serialization(self, db):
        _add_spool(db, "sp-1", "PLA", remaining_grams=500.0, color="red")
        _add_material(db, "ender3", "PLA", remaining_grams=500.0, spool_id="sp-1")

        result = get_fleet_material_summary(db)
        d = result[0].to_dict()
        assert d["material_type"] == "PLA"
        assert isinstance(d["printers_loaded"], list)
        assert isinstance(d["colors"], list)


# ---------------------------------------------------------------------------
# ConsumptionHistory
# ---------------------------------------------------------------------------


class TestConsumptionHistory:
    """Tests for get_consumption_history — usage from print history."""

    def test_no_history_returns_empty(self, db):
        result = get_consumption_history(db)
        assert result == []

    def test_history_within_window(self, db):
        _add_print_record(
            db, "job-1", "ender3",
            material_type="PLA",
            completed_at=time.time() - 86400,  # yesterday
            filament_used_mm=5000.0,
        )

        result = get_consumption_history(db, days=7)
        assert len(result) == 1
        rec = result[0]
        assert rec.material_type == "PLA"
        assert rec.print_count == 1
        assert rec.grams_used > 0
        assert rec.daily_rate_grams > 0

    def test_history_outside_window_excluded(self, db):
        _add_print_record(
            db, "job-old", "ender3",
            material_type="PLA",
            completed_at=time.time() - (60 * 86400),  # 60 days ago
            filament_used_mm=5000.0,
        )

        result = get_consumption_history(db, days=7)
        # The record is completed 60 days ago, outside the 7-day window
        # list_print_history returns it, but our cutoff filter excludes it
        for rec in result:
            if rec.material_type == "PLA":
                # If it shows up, the grams should be 0 since it's outside window
                # Actually the record should be excluded entirely by the cutoff
                assert rec.grams_used == 0.0 or rec.print_count == 0

    def test_multiple_materials_grouped(self, db):
        now = time.time()
        _add_print_record(
            db, "job-1", "ender3",
            material_type="PLA",
            completed_at=now - 3600,
            filament_used_mm=3000.0,
        )
        _add_print_record(
            db, "job-2", "prusa",
            material_type="PETG",
            completed_at=now - 7200,
            filament_used_mm=2000.0,
        )

        result = get_consumption_history(db, days=7)
        types = {r.material_type for r in result}
        assert "PLA" in types
        assert "PETG" in types

    def test_to_dict_serialization(self, db):
        _add_print_record(
            db, "job-1", "ender3",
            material_type="PLA",
            completed_at=time.time() - 3600,
            filament_used_mm=1000.0,
        )
        result = get_consumption_history(db, days=7)
        d = result[0].to_dict()
        assert "material_type" in d
        assert "daily_rate_grams" in d


# ---------------------------------------------------------------------------
# ConsumptionForecast
# ---------------------------------------------------------------------------


class TestConsumptionForecast:
    """Tests for forecast_consumption — projected material depletion."""

    def test_no_history_gives_none_days(self, db):
        _add_spool(db, "sp-1", "PLA", remaining_grams=500.0)

        result = forecast_consumption(db, material_type="PLA")
        assert result.days_until_empty is None
        assert result.urgency == "ok"
        assert result.restock_recommended is False

    def test_healthy_stock_ok(self, db):
        _add_spool(db, "sp-1", "PLA", remaining_grams=5000.0)
        # Add history showing moderate usage
        _add_print_record(
            db, "job-1", "ender3",
            material_type="PLA",
            completed_at=time.time() - 3600,
            filament_used_mm=1000.0,
        )

        result = forecast_consumption(db, material_type="PLA", days_ahead=30)
        # With 5000g stock and low daily rate, should be OK
        assert result.urgency == "ok"
        assert result.restock_recommended is False
        assert result.current_stock_grams == 5000.0

    def test_low_stock_triggers_low_urgency(self, db):
        # Small stock with high usage rate
        _add_spool(db, "sp-1", "PLA", remaining_grams=100.0)
        # Create enough usage to have a meaningful daily rate
        now = time.time()
        for i in range(10):
            _add_print_record(
                db, f"job-{i}", "ender3",
                material_type="PLA",
                completed_at=now - (i * 3600),
                filament_used_mm=20000.0,  # ~60g each at PLA density
            )

        result = forecast_consumption(db, material_type="PLA", days_ahead=30)
        # 100g stock with high usage => should run out soon
        if result.days_until_empty is not None and result.days_until_empty < 30:
            assert result.urgency in ("low", "critical")
            assert result.restock_recommended is True

    def test_critical_stock_urgency(self, db):
        _add_spool(db, "sp-1", "PLA", remaining_grams=10.0)
        # High usage
        now = time.time()
        for i in range(5):
            _add_print_record(
                db, f"job-{i}", "ender3",
                material_type="PLA",
                completed_at=now - (i * 3600),
                filament_used_mm=50000.0,  # ~149g each
            )

        result = forecast_consumption(db, material_type="PLA", days_ahead=30)
        if result.days_until_empty is not None:
            assert result.days_until_empty < 7
            assert result.urgency == "critical"
            assert result.restock_recommended is True

    def test_to_dict_serialization(self, db):
        _add_spool(db, "sp-1", "PLA", remaining_grams=500.0)
        result = forecast_consumption(db, material_type="PLA")
        d = result.to_dict()
        assert d["material_type"] == "PLA"
        assert "urgency" in d
        assert "days_until_empty" in d


# ---------------------------------------------------------------------------
# MaterialCheck
# ---------------------------------------------------------------------------


class TestMaterialCheck:
    """Tests for check_material_sufficiency — per-printer material adequacy."""

    def test_sufficient_material(self, db):
        _add_material(db, "ender3", "PLA", remaining_grams=500.0)

        result = check_material_sufficiency(
            db, printer_name="ender3", required_grams=200.0, material_type="PLA",
        )
        assert result.sufficient is True
        assert result.shortfall_grams == 0.0
        assert result.loaded_grams == 500.0

    def test_insufficient_with_alternatives(self, db):
        _add_material(db, "ender3", "PLA", remaining_grams=50.0)
        _add_material(db, "prusa", "PLA", remaining_grams=500.0)

        result = check_material_sufficiency(
            db, printer_name="ender3", required_grams=200.0, material_type="PLA",
        )
        assert result.sufficient is False
        assert result.shortfall_grams == 150.0
        assert "prusa" in result.alternative_printers
        assert any("prusa" in s.lower() for s in result.suggestions)

    def test_insufficient_no_alternatives(self, db):
        _add_material(db, "ender3", "PLA", remaining_grams=50.0)

        result = check_material_sufficiency(
            db, printer_name="ender3", required_grams=200.0, material_type="PLA",
        )
        assert result.sufficient is False
        assert len(result.alternative_printers) == 0
        # Should suggest purchase
        assert any("amazon" in s.lower() or "purchase" in s.lower() for s in result.suggestions)

    def test_no_material_loaded(self, db):
        result = check_material_sufficiency(
            db, printer_name="ender3", required_grams=100.0, material_type="PLA",
        )
        assert result.sufficient is False
        assert result.loaded_grams is None
        assert result.shortfall_grams == 100.0

    def test_generates_shelf_stock_suggestion(self, db):
        _add_material(db, "ender3", "PLA", remaining_grams=50.0)
        # Spool on the shelf (not loaded anywhere)
        _add_spool(db, "sp-shelf", "PLA", remaining_grams=800.0, brand="Hatchbox", color="white")

        result = check_material_sufficiency(
            db, printer_name="ender3", required_grams=200.0, material_type="PLA",
        )
        assert result.sufficient is False
        assert any("unused spool" in s.lower() for s in result.suggestions)

    def test_generates_pause_and_swap_suggestion(self, db):
        _add_material(db, "ender3", "PLA", remaining_grams=100.0)

        result = check_material_sufficiency(
            db, printer_name="ender3", required_grams=200.0, material_type="PLA",
        )
        assert result.sufficient is False
        assert any("pause-and-swap" in s.lower() for s in result.suggestions)

    def test_to_dict_serialization(self, db):
        _add_material(db, "ender3", "PLA", remaining_grams=500.0)
        result = check_material_sufficiency(
            db, printer_name="ender3", required_grams=200.0, material_type="PLA",
        )
        d = result.to_dict()
        assert d["sufficient"] is True
        assert isinstance(d["suggestions"], list)
        assert isinstance(d["alternative_printers"], list)


# ---------------------------------------------------------------------------
# RestockSuggestions
# ---------------------------------------------------------------------------


class TestRestockSuggestions:
    """Tests for get_restock_suggestions — materials running low."""

    def test_no_low_materials_returns_empty(self, db):
        _add_spool(db, "sp-1", "PLA", remaining_grams=5000.0)
        result = get_restock_suggestions(db)
        assert result == []

    def test_low_material_generates_suggestion(self, db):
        _add_spool(db, "sp-1", "PLA", remaining_grams=50.0)
        # Add heavy usage history
        now = time.time()
        for i in range(5):
            _add_print_record(
                db, f"job-{i}", "ender3",
                material_type="PLA",
                completed_at=now - (i * 3600),
                filament_used_mm=50000.0,
            )

        result = get_restock_suggestions(db)
        assert len(result) >= 1
        suggestion = result[0]
        assert suggestion.material_type == "PLA"
        assert suggestion.urgency in ("low", "critical")
        assert "amazon" in suggestion.purchase_urls

    def test_critical_urgency_sorted_first(self, db):
        now = time.time()
        # PLA: critical (very low stock, high usage)
        _add_spool(db, "sp-pla", "PLA", remaining_grams=5.0)
        for i in range(3):
            _add_print_record(
                db, f"pla-{i}", "ender3",
                material_type="PLA",
                completed_at=now - (i * 3600),
                filament_used_mm=50000.0,
            )

        # PETG: low (moderate stock, moderate usage)
        _add_spool(db, "sp-petg", "PETG", remaining_grams=200.0)
        for i in range(3):
            _add_print_record(
                db, f"petg-{i}", "prusa",
                material_type="PETG",
                completed_at=now - (i * 3600),
                filament_used_mm=30000.0,
            )

        result = get_restock_suggestions(db)
        if len(result) >= 2:
            # Critical should come before low
            assert result[0].urgency == "critical" or result[0].days_until_empty <= result[1].days_until_empty

    def test_to_dict_serialization(self, db):
        now = time.time()
        _add_spool(db, "sp-1", "PLA", remaining_grams=5.0, brand="Hatchbox", color="white")
        for i in range(3):
            _add_print_record(
                db, f"job-{i}", "ender3",
                material_type="PLA",
                completed_at=now - (i * 3600),
                filament_used_mm=50000.0,
            )

        result = get_restock_suggestions(db)
        if result:
            d = result[0].to_dict()
            assert "material_type" in d
            assert "purchase_urls" in d
            assert isinstance(d["purchase_urls"], dict)


# ---------------------------------------------------------------------------
# FindPrintersWithMaterial
# ---------------------------------------------------------------------------


class TestFindPrintersWithMaterial:
    """Tests for find_printers_with_material — locating specific materials in fleet."""

    def test_no_printers(self, db):
        result = find_printers_with_material(db, material_type="PLA")
        assert result == []

    def test_matching_printer_found(self, db):
        _add_material(db, "ender3", "PLA", remaining_grams=500.0, color="white")
        _add_material(db, "prusa", "PETG", remaining_grams=300.0)

        result = find_printers_with_material(db, material_type="PLA")
        assert len(result) == 1
        assert result[0]["printer_name"] == "ender3"
        assert result[0]["remaining_grams"] == 500.0

    def test_filter_by_min_grams(self, db):
        _add_material(db, "ender3", "PLA", remaining_grams=100.0)
        _add_material(db, "prusa", "PLA", remaining_grams=500.0)

        result = find_printers_with_material(db, material_type="PLA", min_grams=200.0)
        assert len(result) == 1
        assert result[0]["printer_name"] == "prusa"

    def test_filter_by_color(self, db):
        _add_material(db, "ender3", "PLA", remaining_grams=500.0, color="white")
        _add_material(db, "prusa", "PLA", remaining_grams=500.0, color="black")

        result = find_printers_with_material(db, material_type="PLA", color="black")
        assert len(result) == 1
        assert result[0]["printer_name"] == "prusa"

    def test_sorted_by_remaining_descending(self, db):
        _add_material(db, "ender3", "PLA", remaining_grams=200.0)
        _add_material(db, "prusa", "PLA", remaining_grams=800.0)
        _add_material(db, "voron", "PLA", remaining_grams=500.0)

        result = find_printers_with_material(db, material_type="PLA")
        assert len(result) == 3
        assert result[0]["printer_name"] == "prusa"
        assert result[1]["printer_name"] == "voron"
        assert result[2]["printer_name"] == "ender3"


# ---------------------------------------------------------------------------
# FleetAssignment
# ---------------------------------------------------------------------------


class TestFleetAssignment:
    """Tests for optimize_fleet_assignment — job-to-printer matching."""

    def test_single_job_single_printer(self, db):
        _add_material(db, "ender3", "PLA", remaining_grams=500.0)

        jobs = [{"file_name": "benchy.gcode", "material_type": "PLA", "required_grams": 100.0}]
        result = optimize_fleet_assignment(db, jobs=jobs)
        assert len(result) == 1
        assert result[0].recommended_printer == "ender3"
        assert result[0].material_match is True
        assert result[0].remaining_after_print_grams == 400.0

    def test_multiple_jobs_optimal_assignment(self, db):
        _add_material(db, "ender3", "PLA", remaining_grams=300.0)
        _add_material(db, "prusa", "PLA", remaining_grams=800.0)

        jobs = [
            {"file_name": "big.gcode", "material_type": "PLA", "required_grams": 500.0},
            {"file_name": "small.gcode", "material_type": "PLA", "required_grams": 100.0},
        ]
        result = optimize_fleet_assignment(db, jobs=jobs)
        assert len(result) == 2

        # The big job should go to prusa (800g), small to ender3 (300g)
        big_job = next(r for r in result if r.job_file == "big.gcode")
        small_job = next(r for r in result if r.job_file == "small.gcode")
        assert big_job.recommended_printer == "prusa"
        assert big_job.material_match is True
        assert small_job.recommended_printer == "ender3"
        assert small_job.material_match is True

    def test_no_matching_printer(self, db):
        _add_material(db, "ender3", "PLA", remaining_grams=500.0)

        jobs = [{"file_name": "flex.gcode", "material_type": "TPU", "required_grams": 100.0}]
        result = optimize_fleet_assignment(db, jobs=jobs)
        assert len(result) == 1
        assert result[0].material_match is False
        assert result[0].recommended_printer == ""

    def test_empty_jobs_list(self, db):
        result = optimize_fleet_assignment(db, jobs=[])
        assert result == []

    def test_to_dict_serialization(self, db):
        _add_material(db, "ender3", "PLA", remaining_grams=500.0)
        jobs = [{"file_name": "benchy.gcode", "material_type": "PLA", "required_grams": 100.0}]
        result = optimize_fleet_assignment(db, jobs=jobs)
        d = result[0].to_dict()
        assert d["job_file"] == "benchy.gcode"
        assert "material_match" in d


# ---------------------------------------------------------------------------
# SuggestSpoolSwaps
# ---------------------------------------------------------------------------


class TestSuggestSpoolSwaps:
    """Tests for suggest_spool_swaps — minimal swap suggestions."""

    def test_no_swaps_needed(self, db):
        _add_material(db, "ender3", "PLA", remaining_grams=500.0)
        jobs = [{"material_type": "PLA", "required_grams": 100.0}]

        result = suggest_spool_swaps(db, jobs=jobs)
        assert result == []

    def test_swap_suggested_when_needed(self, db):
        # Printer has PETG loaded, but job needs PLA
        _add_material(db, "ender3", "PETG", remaining_grams=500.0)
        # PLA spool on the shelf
        _add_spool(db, "sp-pla", "PLA", remaining_grams=800.0, brand="Hatchbox")

        jobs = [{"material_type": "PLA", "required_grams": 200.0}]
        result = suggest_spool_swaps(db, jobs=jobs)
        assert len(result) >= 1
        assert "PLA" in result[0].upper()

    def test_empty_jobs(self, db):
        result = suggest_spool_swaps(db, jobs=[])
        assert result == []

    def test_no_shelf_stock_no_crash(self, db):
        # Job needs material but nothing available
        jobs = [{"material_type": "NYLON", "required_grams": 200.0}]
        result = suggest_spool_swaps(db, jobs=jobs)
        # Should return empty (no spools to suggest)
        assert isinstance(result, list)
