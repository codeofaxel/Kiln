"""Tests for kiln.materials -- material tracking and spool inventory."""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock

import pytest

from kiln.materials import LoadedMaterial, MaterialTracker, MaterialWarning, Spool
from kiln.persistence import KilnDB


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db(tmp_path):
    """Create a real KilnDB backed by a temp file."""
    db_path = str(tmp_path / "test_materials.db")
    _db = KilnDB(db_path=db_path)
    yield _db
    _db.close()


@pytest.fixture()
def bus():
    """Return a MagicMock standing in for EventBus."""
    return MagicMock()


@pytest.fixture()
def tracker(db, bus):
    """MaterialTracker wired to a real DB and mock bus."""
    return MaterialTracker(db=db, event_bus=bus)


@pytest.fixture()
def tracker_no_bus(db):
    """MaterialTracker with DB but no event bus."""
    return MaterialTracker(db=db, event_bus=None)


@pytest.fixture()
def tracker_no_db():
    """MaterialTracker with no DB and no event bus."""
    return MaterialTracker(db=None, event_bus=None)


# ===================================================================
# LoadedMaterial dataclass
# ===================================================================


class TestLoadedMaterial:
    def test_creation_defaults(self):
        mat = LoadedMaterial(printer_name="voron")
        assert mat.printer_name == "voron"
        assert mat.tool_index == 0
        assert mat.material_type == "unknown"
        assert mat.color is None
        assert mat.spool_id is None
        assert isinstance(mat.loaded_at, float)
        assert mat.remaining_grams is None

    def test_creation_all_fields(self):
        mat = LoadedMaterial(
            printer_name="prusa",
            tool_index=1,
            material_type="PLA",
            color="red",
            spool_id="abc123",
            loaded_at=1000.0,
            remaining_grams=750.0,
        )
        assert mat.printer_name == "prusa"
        assert mat.tool_index == 1
        assert mat.material_type == "PLA"
        assert mat.color == "red"
        assert mat.spool_id == "abc123"
        assert mat.loaded_at == 1000.0
        assert mat.remaining_grams == 750.0

    def test_to_dict(self):
        mat = LoadedMaterial(
            printer_name="voron",
            tool_index=0,
            material_type="PETG",
            color="blue",
            spool_id="s1",
            loaded_at=500.0,
            remaining_grams=200.0,
        )
        d = mat.to_dict()
        assert isinstance(d, dict)
        assert d["printer_name"] == "voron"
        assert d["tool_index"] == 0
        assert d["material_type"] == "PETG"
        assert d["color"] == "blue"
        assert d["spool_id"] == "s1"
        assert d["loaded_at"] == 500.0
        assert d["remaining_grams"] == 200.0


# ===================================================================
# Spool dataclass
# ===================================================================


class TestSpool:
    def test_creation_defaults(self):
        spool = Spool(id="s1", material_type="PLA")
        assert spool.id == "s1"
        assert spool.material_type == "PLA"
        assert spool.color is None
        assert spool.brand is None
        assert spool.weight_grams == 1000.0
        assert spool.remaining_grams == 1000.0
        assert spool.cost_usd is None
        assert spool.purchase_date is None
        assert spool.notes == ""

    def test_creation_all_fields(self):
        spool = Spool(
            id="s2",
            material_type="ABS",
            color="black",
            brand="Hatchbox",
            weight_grams=750.0,
            remaining_grams=500.0,
            cost_usd=24.99,
            purchase_date=1700000000.0,
            notes="good spool",
        )
        assert spool.brand == "Hatchbox"
        assert spool.weight_grams == 750.0
        assert spool.remaining_grams == 500.0
        assert spool.cost_usd == 24.99
        assert spool.notes == "good spool"

    def test_to_dict(self):
        spool = Spool(id="s3", material_type="TPU", color="white")
        d = spool.to_dict()
        assert isinstance(d, dict)
        assert d["id"] == "s3"
        assert d["material_type"] == "TPU"
        assert d["color"] == "white"
        assert d["weight_grams"] == 1000.0


# ===================================================================
# MaterialWarning dataclass
# ===================================================================


class TestMaterialWarning:
    def test_creation_defaults(self):
        w = MaterialWarning(
            printer_name="voron",
            expected="PLA",
            loaded="ABS",
        )
        assert w.printer_name == "voron"
        assert w.expected == "PLA"
        assert w.loaded == "ABS"
        assert w.severity == "warning"
        assert w.message == ""

    def test_creation_with_message(self):
        w = MaterialWarning(
            printer_name="prusa",
            expected="PETG",
            loaded="PLA",
            severity="error",
            message="Wrong material loaded",
        )
        assert w.severity == "error"
        assert w.message == "Wrong material loaded"

    def test_to_dict(self):
        w = MaterialWarning(
            printer_name="voron",
            expected="PLA",
            loaded="ABS",
            message="mismatch",
        )
        d = w.to_dict()
        assert isinstance(d, dict)
        assert d["printer_name"] == "voron"
        assert d["expected"] == "PLA"
        assert d["loaded"] == "ABS"
        assert d["severity"] == "warning"
        assert d["message"] == "mismatch"


# ===================================================================
# MaterialTracker -- no DB
# ===================================================================


class TestTrackerNoDB:
    def test_get_material_returns_none(self, tracker_no_db):
        assert tracker_no_db.get_material("voron") is None

    def test_get_all_materials_returns_empty(self, tracker_no_db):
        assert tracker_no_db.get_all_materials("voron") == []

    def test_check_match_no_db_returns_none(self, tracker_no_db):
        assert tracker_no_db.check_match("voron", "PLA") is None

    def test_deduct_usage_no_db_returns_none(self, tracker_no_db):
        assert tracker_no_db.deduct_usage("voron", 50.0) is None

    def test_list_spools_no_db(self, tracker_no_db):
        assert tracker_no_db.list_spools() == []

    def test_get_spool_no_db(self, tracker_no_db):
        assert tracker_no_db.get_spool("nonexistent") is None

    def test_remove_spool_no_db(self, tracker_no_db):
        assert tracker_no_db.remove_spool("nonexistent") is False


# ===================================================================
# MaterialTracker.set_material
# ===================================================================


class TestSetMaterial:
    def test_returns_loaded_material(self, tracker):
        mat = tracker.set_material("voron", "pla", color="red")
        assert isinstance(mat, LoadedMaterial)
        assert mat.printer_name == "voron"
        assert mat.material_type == "PLA"  # uppercased
        assert mat.color == "red"
        assert mat.tool_index == 0

    def test_uppercases_material_type(self, tracker):
        mat = tracker.set_material("prusa", "petg")
        assert mat.material_type == "PETG"

    def test_persists_to_db(self, tracker, db):
        tracker.set_material("voron", "abs", color="black", tool_index=1)
        row = db.get_material("voron", 1)
        assert row is not None
        assert row["material_type"] == "ABS"
        assert row["color"] == "black"

    def test_publishes_material_loaded_event(self, tracker, bus):
        from kiln.events import EventType

        tracker.set_material("voron", "pla", color="white")
        bus.publish.assert_called_once()
        call_args = bus.publish.call_args
        assert call_args[0][0] == EventType.MATERIAL_LOADED
        assert call_args[1]["source"] == "printer:voron"

    def test_with_spool_id_and_remaining(self, tracker, db):
        mat = tracker.set_material(
            "voron", "pla", spool_id="sp1", remaining_grams=800.0,
        )
        assert mat.spool_id == "sp1"
        assert mat.remaining_grams == 800.0
        row = db.get_material("voron", 0)
        assert row["spool_id"] == "sp1"
        assert row["remaining_grams"] == 800.0

    def test_set_material_replaces_existing(self, tracker, db):
        tracker.set_material("voron", "pla", color="red")
        tracker.set_material("voron", "abs", color="black")
        row = db.get_material("voron", 0)
        assert row["material_type"] == "ABS"
        assert row["color"] == "black"


# ===================================================================
# MaterialTracker.get_material
# ===================================================================


class TestGetMaterial:
    def test_returns_stored_material(self, tracker):
        tracker.set_material("voron", "pla", color="red")
        mat = tracker.get_material("voron")
        assert isinstance(mat, LoadedMaterial)
        assert mat.material_type == "PLA"
        assert mat.color == "red"

    def test_returns_none_for_unknown_printer(self, tracker):
        assert tracker.get_material("nonexistent") is None

    def test_different_tool_indices(self, tracker):
        tracker.set_material("voron", "pla", color="red", tool_index=0)
        tracker.set_material("voron", "abs", color="black", tool_index=1)
        mat0 = tracker.get_material("voron", tool_index=0)
        mat1 = tracker.get_material("voron", tool_index=1)
        assert mat0.material_type == "PLA"
        assert mat1.material_type == "ABS"

    def test_different_tool_index_returns_none(self, tracker):
        tracker.set_material("voron", "pla", tool_index=0)
        assert tracker.get_material("voron", tool_index=1) is None


# ===================================================================
# MaterialTracker.get_all_materials
# ===================================================================


class TestGetAllMaterials:
    def test_returns_all_slots(self, tracker):
        tracker.set_material("voron", "pla", tool_index=0)
        tracker.set_material("voron", "abs", tool_index=1)
        tracker.set_material("voron", "petg", tool_index=2)
        mats = tracker.get_all_materials("voron")
        assert len(mats) == 3
        types = {m.material_type for m in mats}
        assert types == {"PLA", "ABS", "PETG"}

    def test_returns_empty_for_unknown_printer(self, tracker):
        assert tracker.get_all_materials("nonexistent") == []

    def test_does_not_include_other_printers(self, tracker):
        tracker.set_material("voron", "pla", tool_index=0)
        tracker.set_material("prusa", "abs", tool_index=0)
        mats = tracker.get_all_materials("voron")
        assert len(mats) == 1
        assert mats[0].material_type == "PLA"


# ===================================================================
# MaterialTracker.check_match
# ===================================================================


class TestCheckMatch:
    def test_matching_material_returns_none(self, tracker):
        tracker.set_material("voron", "PLA")
        assert tracker.check_match("voron", "PLA") is None

    def test_case_insensitive_match_returns_none(self, tracker):
        tracker.set_material("voron", "pla")
        assert tracker.check_match("voron", "Pla") is None

    def test_mismatch_returns_warning(self, tracker):
        tracker.set_material("voron", "pla")
        warning = tracker.check_match("voron", "abs")
        assert isinstance(warning, MaterialWarning)
        assert warning.expected == "ABS"
        assert warning.loaded == "PLA"
        assert warning.printer_name == "voron"
        assert "mismatch" in warning.message.lower()

    def test_no_material_loaded_returns_none(self, tracker):
        assert tracker.check_match("voron", "PLA") is None

    def test_publishes_mismatch_event(self, tracker, bus):
        from kiln.events import EventType

        tracker.set_material("voron", "pla")
        bus.reset_mock()
        tracker.check_match("voron", "abs")
        bus.publish.assert_called_once()
        call_args = bus.publish.call_args
        assert call_args[0][0] == EventType.MATERIAL_MISMATCH
        assert call_args[1]["source"] == "printer:voron"

    def test_match_does_not_publish_event(self, tracker, bus):
        tracker.set_material("voron", "pla")
        bus.reset_mock()
        tracker.check_match("voron", "pla")
        bus.publish.assert_not_called()

    def test_mismatch_warning_severity(self, tracker):
        tracker.set_material("voron", "pla")
        warning = tracker.check_match("voron", "abs")
        assert warning.severity == "warning"


# ===================================================================
# MaterialTracker.deduct_usage
# ===================================================================


class TestDeductUsage:
    def test_reduces_remaining_grams(self, tracker, db):
        tracker.set_material("voron", "pla", remaining_grams=500.0)
        result = tracker.deduct_usage("voron", 100.0)
        assert result == 400.0
        row = db.get_material("voron", 0)
        assert row["remaining_grams"] == 400.0

    def test_cannot_go_below_zero(self, tracker):
        tracker.set_material("voron", "pla", remaining_grams=50.0)
        result = tracker.deduct_usage("voron", 200.0)
        assert result == 0.0

    def test_no_material_returns_none(self, tracker):
        assert tracker.deduct_usage("voron", 10.0) is None

    def test_no_remaining_grams_returns_none(self, tracker):
        tracker.set_material("voron", "pla")  # remaining_grams=None
        assert tracker.deduct_usage("voron", 10.0) is None

    def test_linked_spool_also_deducted(self, tracker, db):
        spool = tracker.add_spool("pla", color="red", weight_grams=1000.0)
        tracker.set_material(
            "voron", "pla", spool_id=spool.id, remaining_grams=1000.0,
        )
        tracker.deduct_usage("voron", 200.0)
        spool_row = db.get_spool(spool.id)
        assert spool_row["remaining_grams"] == 800.0

    def test_spool_low_event_below_10_percent(self, tracker, bus):
        from kiln.events import EventType

        spool = tracker.add_spool("pla", weight_grams=1000.0)
        tracker.set_material(
            "voron", "pla", spool_id=spool.id, remaining_grams=100.0,
        )
        bus.reset_mock()
        # Spool starts at 1000. Deduct 910 -> spool remaining = 90 = 9% -> SPOOL_LOW
        tracker.deduct_usage("voron", 910.0)
        event_types = [c[0][0] for c in bus.publish.call_args_list]
        assert EventType.SPOOL_LOW in event_types

    def test_spool_empty_event_at_zero(self, tracker, bus):
        from kiln.events import EventType

        spool = tracker.add_spool("pla", weight_grams=100.0)
        tracker.set_material(
            "voron", "pla", spool_id=spool.id, remaining_grams=100.0,
        )
        bus.reset_mock()
        tracker.deduct_usage("voron", 100.0)
        event_types = [c[0][0] for c in bus.publish.call_args_list]
        assert EventType.SPOOL_EMPTY in event_types

    def test_multiple_deductions(self, tracker):
        tracker.set_material("voron", "pla", remaining_grams=500.0)
        tracker.deduct_usage("voron", 100.0)
        tracker.deduct_usage("voron", 150.0)
        result = tracker.deduct_usage("voron", 50.0)
        assert result == 200.0

    def test_deduct_different_tool_index(self, tracker):
        tracker.set_material("voron", "pla", remaining_grams=300.0, tool_index=1)
        result = tracker.deduct_usage("voron", 50.0, tool_index=1)
        assert result == 250.0

    def test_deduct_wrong_tool_index_returns_none(self, tracker):
        tracker.set_material("voron", "pla", remaining_grams=300.0, tool_index=0)
        assert tracker.deduct_usage("voron", 50.0, tool_index=1) is None


# ===================================================================
# MaterialTracker -- spool operations
# ===================================================================


class TestAddSpool:
    def test_creates_with_generated_id(self, tracker):
        spool = tracker.add_spool("pla", color="red")
        assert isinstance(spool, Spool)
        assert isinstance(spool.id, str)
        assert len(spool.id) == 12  # os.urandom(6).hex() -> 12 hex chars

    def test_uppercases_material_type(self, tracker):
        spool = tracker.add_spool("petg")
        assert spool.material_type == "PETG"

    def test_default_weight(self, tracker):
        spool = tracker.add_spool("pla")
        assert spool.weight_grams == 1000.0
        assert spool.remaining_grams == 1000.0

    def test_custom_weight(self, tracker):
        spool = tracker.add_spool("abs", weight_grams=750.0)
        assert spool.weight_grams == 750.0
        assert spool.remaining_grams == 750.0

    def test_persists_to_db(self, tracker, db):
        spool = tracker.add_spool("pla", color="blue", brand="Hatchbox")
        row = db.get_spool(spool.id)
        assert row is not None
        assert row["material_type"] == "PLA"
        assert row["color"] == "blue"
        assert row["brand"] == "Hatchbox"

    def test_cost_and_notes(self, tracker):
        spool = tracker.add_spool("pla", cost_usd=19.99, notes="test spool")
        assert spool.cost_usd == 19.99
        assert spool.notes == "test spool"

    def test_purchase_date_set(self, tracker):
        before = time.time()
        spool = tracker.add_spool("pla")
        after = time.time()
        assert before <= spool.purchase_date <= after

    def test_unique_ids(self, tracker):
        ids = {tracker.add_spool("pla").id for _ in range(20)}
        assert len(ids) == 20


class TestRemoveSpool:
    def test_remove_existing_returns_true(self, tracker):
        spool = tracker.add_spool("pla")
        assert tracker.remove_spool(spool.id) is True

    def test_remove_nonexistent_returns_false(self, tracker):
        assert tracker.remove_spool("nonexistent") is False

    def test_remove_actually_deletes(self, tracker):
        spool = tracker.add_spool("pla")
        tracker.remove_spool(spool.id)
        assert tracker.get_spool(spool.id) is None


class TestListSpools:
    def test_returns_all_spools(self, tracker):
        tracker.add_spool("pla", color="red")
        tracker.add_spool("abs", color="black")
        tracker.add_spool("petg", color="blue")
        spools = tracker.list_spools()
        assert len(spools) == 3
        types = {s.material_type for s in spools}
        assert types == {"PLA", "ABS", "PETG"}

    def test_empty_inventory(self, tracker):
        assert tracker.list_spools() == []

    def test_returns_spool_objects(self, tracker):
        tracker.add_spool("pla")
        spools = tracker.list_spools()
        assert all(isinstance(s, Spool) for s in spools)


class TestGetSpool:
    def test_returns_spool(self, tracker):
        added = tracker.add_spool("pla", color="red")
        spool = tracker.get_spool(added.id)
        assert isinstance(spool, Spool)
        assert spool.id == added.id
        assert spool.material_type == "PLA"
        assert spool.color == "red"

    def test_returns_none_for_missing(self, tracker):
        assert tracker.get_spool("nonexistent") is None


# ===================================================================
# MaterialTracker -- no event bus
# ===================================================================


class TestTrackerNoBus:
    def test_set_material_works_without_bus(self, tracker_no_bus):
        mat = tracker_no_bus.set_material("voron", "pla")
        assert mat.material_type == "PLA"

    def test_check_match_works_without_bus(self, tracker_no_bus):
        tracker_no_bus.set_material("voron", "pla")
        warning = tracker_no_bus.check_match("voron", "abs")
        assert isinstance(warning, MaterialWarning)

    def test_deduct_usage_works_without_bus(self, tracker_no_bus):
        spool = tracker_no_bus.add_spool("pla", weight_grams=1000.0)
        tracker_no_bus.set_material(
            "voron", "pla", spool_id=spool.id, remaining_grams=500.0,
        )
        result = tracker_no_bus.deduct_usage("voron", 100.0)
        assert result == 400.0


# ===================================================================
# Thread safety
# ===================================================================


class TestThreadSafety:
    def test_concurrent_set_material(self, tracker):
        """Multiple threads setting materials on different tool indices."""
        errors = []

        def set_mat(idx):
            try:
                tracker.set_material(
                    "voron", "pla", color=f"color-{idx}", tool_index=idx,
                )
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=set_mat, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        assert len(errors) == 0
        mats = tracker.get_all_materials("voron")
        assert len(mats) == 10

    def test_concurrent_deduct_usage(self, tracker, db):
        """Multiple threads deducting from the same material."""
        tracker.set_material("voron", "pla", remaining_grams=1000.0)
        errors = []

        def deduct():
            try:
                tracker.deduct_usage("voron", 1.0)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=deduct) for _ in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        assert len(errors) == 0
        row = db.get_material("voron", 0)
        assert row["remaining_grams"] >= 0.0

    def test_concurrent_add_spool(self, tracker):
        """Multiple threads adding spools concurrently."""
        results = []
        errors = []

        def add():
            try:
                spool = tracker.add_spool("pla")
                results.append(spool.id)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=add) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        assert len(errors) == 0
        assert len(set(results)) == 20  # all unique IDs
