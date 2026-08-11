"""A material Kiln was TOLD about is never reported as a material it sensed.

The incident (2026-08, a first-time user's Bambu P2S with no AMS): Kiln
answered "PETG loaded" and the agent repeated it as ground truth.  Nothing
had measured anything.  ``preflight_check`` asked
``MaterialTracker.check_match``, whose ``None`` means BOTH "the materials
match" and "no material is recorded", and rendered that single ``None`` as
"Loaded material matches expected (PETG)".  The printer had something else
in the extruder.

These tests pin the three facts that keep that sentence from coming back:

1. The store's verdict has three states, so silence cannot be rendered as a
   match (``MaterialTracker.match_verdict``).
2. The record says who decided it (``LoadedMaterial.determined_by``), so no
   reader has to assume — and a reader is not allowed to hardcode "not
   sensed" either, which the ``observed`` cases below prove.
3. Every door that turns the store into a sentence goes through ONE helper
   (``kiln.server._material_match_report``) and names the right machine.
"""

from __future__ import annotations

import sqlite3
import time
from unittest.mock import MagicMock, patch

import pytest

import kiln.server as server
from kiln.materials import MaterialTracker
from kiln.persistence import KilnDB
from kiln.printers.base import PrinterCapabilities, PrinterState, PrinterStatus
from kiln.registry import PrinterRegistry

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db(tmp_path):
    _db = KilnDB(db_path=str(tmp_path / "provenance.db"))
    yield _db
    _db.close()


@pytest.fixture()
def tracker(db):
    return MaterialTracker(db=db, event_bus=None)


@pytest.fixture()
def empty_registry():
    """A registry with nothing in it, isolated from other tests' state."""
    registry = PrinterRegistry()
    with patch.object(server, "_get_registry", return_value=registry):
        yield registry


def _idle_state() -> PrinterState:
    return PrinterState(
        connected=True,
        state=PrinterStatus.IDLE,
        tool_temp_actual=24.0,
        tool_temp_target=0.0,
        bed_temp_actual=23.0,
        bed_temp_target=0.0,
    )


def _stub_adapter(host: str = "http://p2s.local") -> MagicMock:
    """An adapter that is idle, senses nothing, and has a stable fingerprint."""
    adapter = MagicMock()
    adapter.get_state.return_value = _idle_state()
    adapter.capabilities = PrinterCapabilities(can_detect_filament=False)
    adapter.name = "bambu"
    adapter.serial = ""
    adapter._serial = ""
    adapter.host = host
    return adapter


def _material_check(result: dict) -> dict:
    checks = [c for c in result["checks"] if c["name"] == "material_match"]
    assert len(checks) == 1, f"expected one material_match check, got {checks}"
    return checks[0]


# ===================================================================
# 1. Three states, so silence is not a match
# ===================================================================


class TestMatchVerdict:
    def test_nothing_recorded_is_unknown_not_a_match(self, tracker):
        """The incident, at the layer that caused it."""
        verdict, warning, loaded = tracker.match_verdict("p2s", "PETG")
        assert verdict == "unknown"
        assert warning is None
        assert loaded is None

    def test_no_database_is_also_unknown(self):
        verdict, warning, loaded = MaterialTracker(db=None).match_verdict("p2s", "PETG")
        assert verdict == "unknown"
        assert warning is None
        assert loaded is None

    def test_recorded_and_equal_is_a_match(self, tracker):
        tracker.set_material("p2s", "petg")
        verdict, warning, loaded = tracker.match_verdict("p2s", "PETG")
        assert verdict == "match"
        assert warning is None
        assert loaded is not None and loaded.material_type == "PETG"

    def test_recorded_and_different_is_a_mismatch(self, tracker):
        tracker.set_material("p2s", "PLA")
        verdict, warning, loaded = tracker.match_verdict("p2s", "PETG")
        assert verdict == "mismatch"
        assert warning is not None
        assert warning.expected == "PETG"
        assert warning.loaded == "PLA"
        assert loaded is not None

    def test_mismatch_still_publishes_its_event(self, db):
        """The publish lived inside check_match; it must survive the move."""
        from kiln.events import EventType

        bus = MagicMock()
        t = MaterialTracker(db=db, event_bus=bus)
        t.set_material("p2s", "PLA")
        bus.reset_mock()
        t.match_verdict("p2s", "PETG")
        bus.publish.assert_called_once()
        assert bus.publish.call_args[0][0] == EventType.MATERIAL_MISMATCH

    def test_match_publishes_nothing(self, db):
        bus = MagicMock()
        t = MaterialTracker(db=db, event_bus=bus)
        t.set_material("p2s", "PETG")
        bus.reset_mock()
        t.match_verdict("p2s", "PETG")
        bus.publish.assert_not_called()

    def test_check_match_contract_is_unchanged(self, tracker):
        """The old two-state accessor still answers exactly as it did."""
        assert tracker.check_match("p2s", "PETG") is None  # nothing recorded
        tracker.set_material("p2s", "PETG")
        assert tracker.check_match("p2s", "PETG") is None  # matches
        assert tracker.check_match("p2s", "PLA") is not None  # mismatch


# ===================================================================
# 2. The record says who decided
# ===================================================================


class TestProvenanceOnTheRecord:
    def test_default_is_declared_not_sensed(self, tracker):
        mat = tracker.set_material("p2s", "PETG")
        assert mat.determined_by == "user_reported"
        assert mat.is_sensed is False

    def test_declared_provenance_survives_the_round_trip(self, tracker):
        tracker.set_material("p2s", "PETG")
        loaded = tracker.get_material("p2s")
        assert loaded is not None
        assert loaded.determined_by == "user_reported"
        assert loaded.is_sensed is False

    def test_observed_provenance_survives_the_round_trip(self, tracker):
        """A machine-reported row must read back as sensed, or the readers
        below are just hardcoding 'not sensed' and calling it honesty."""
        tracker.set_material("p2s", "PETG", determined_by="observed")
        loaded = tracker.get_material("p2s")
        assert loaded is not None
        assert loaded.determined_by == "observed"
        assert loaded.is_sensed is True

    def test_get_all_materials_carries_provenance(self, tracker):
        tracker.set_material("p2s", "PETG", determined_by="observed")
        tracker.set_material("p2s", "PLA", tool_index=1)
        by_slot = {m.tool_index: m for m in tracker.get_all_materials("p2s")}
        assert by_slot[0].is_sensed is True
        assert by_slot[1].is_sensed is False

    def test_unknown_provenance_word_is_refused(self, tracker):
        with pytest.raises(ValueError, match="determined_by"):
            tracker.set_material("p2s", "PETG", determined_by="sensor")

    def test_vocabulary_is_read_from_the_store_not_copied(self):
        from kiln.materials import _provenance_vocabulary

        assert _provenance_vocabulary() is not None
        assert _provenance_vocabulary() == frozenset(KilnDB.VALID_DETERMINED_BY)

    def test_row_written_before_the_column_reads_as_declared(self, tracker, db):
        """A NULL provenance is a row somebody typed — never None, and never
        promoted to a reading."""
        db.save_material(
            printer_name="p2s",
            tool_index=0,
            material_type="PETG",
        )
        loaded = tracker.get_material("p2s")
        assert loaded is not None
        assert loaded.determined_by == "user_reported"
        assert loaded.is_sensed is False

    def test_legacy_table_is_migrated(self, tmp_path):
        """A database created before the column exists gains it on open."""
        path = str(tmp_path / "legacy.db")
        conn = sqlite3.connect(path)
        conn.execute(
            """
            CREATE TABLE printer_materials (
                printer_name    TEXT NOT NULL,
                tool_index      INTEGER NOT NULL DEFAULT 0,
                material_type   TEXT NOT NULL,
                color           TEXT,
                spool_id        TEXT,
                loaded_at       REAL NOT NULL,
                remaining_grams REAL,
                PRIMARY KEY (printer_name, tool_index)
            )
            """
        )
        conn.execute(
            "INSERT INTO printer_materials "
            "(printer_name, tool_index, material_type, loaded_at) VALUES (?, ?, ?, ?)",
            ("p2s", 0, "PETG", time.time()),
        )
        conn.commit()
        conn.close()

        legacy = KilnDB(db_path=path)
        try:
            row = legacy.get_material("p2s", 0)
            assert row is not None
            assert "determined_by" in row
            assert row["determined_by"] is None
            loaded = MaterialTracker(db=legacy).get_material("p2s")
            assert loaded is not None
            assert loaded.determined_by == "user_reported"
        finally:
            legacy.close()


# ===================================================================
# 3a. preflight_check — the incident replay
# ===================================================================


class TestPreflightMaterialSentence:
    def test_nothing_recorded_does_not_claim_a_match(self, tracker, empty_registry):
        """THE regression: an empty store used to produce
        'Loaded material matches expected (PETG)'."""
        adapter = _stub_adapter()
        with patch.object(server, "_get_adapter", return_value=adapter), patch.object(
            server, "_get_material_tracker", return_value=tracker
        ):
            result = server.preflight_check(expected_material="PETG")

        check = _material_check(result)
        assert "matches expected" not in check["message"]
        assert "Cannot confirm" in check["message"]
        assert check["sensed"] is False
        assert check["verified_by"] is None
        assert check["advisory"] is True
        # Not knowing is never a reason to block a print.
        assert check["passed"] is True
        assert result["ready"] is True

    def test_declared_material_is_reported_as_declared(self, tracker, empty_registry):
        tracker.set_material("default", "PETG")
        adapter = _stub_adapter()
        with patch.object(server, "_get_adapter", return_value=adapter), patch.object(
            server, "_get_material_tracker", return_value=tracker
        ):
            result = server.preflight_check(expected_material="PETG")

        check = _material_check(result)
        assert check["passed"] is True
        assert check["sensed"] is False
        assert check["verified_by"] == "user_reported"
        assert "no sensor confirmed it" in check["message"]
        assert "PETG" in check["message"]
        assert check.get("advisory") is None

    def test_a_stale_record_says_how_old_it_is(self, tracker, db, empty_registry):
        """A claim from three weeks ago is weaker than one from this morning,
        and the sentence has to let the reader weigh that."""
        tracker.set_material("default", "PETG")
        db._conn.execute(
            "UPDATE printer_materials SET loaded_at = ? WHERE printer_name = ?",
            (time.time() - 21 * 86400, "default"),
        )
        db._conn.commit()
        adapter = _stub_adapter()
        with patch.object(server, "_get_adapter", return_value=adapter), patch.object(
            server, "_get_material_tracker", return_value=tracker
        ):
            result = server.preflight_check(expected_material="PETG")

        assert "recorded 21 days ago" in _material_check(result)["message"]

    def test_a_fresh_record_says_today(self, tracker, empty_registry):
        tracker.set_material("default", "PETG")
        adapter = _stub_adapter()
        with patch.object(server, "_get_adapter", return_value=adapter), patch.object(
            server, "_get_material_tracker", return_value=tracker
        ):
            result = server.preflight_check(expected_material="PETG")

        assert "recorded today" in _material_check(result)["message"]

    def test_sensed_material_is_reported_as_sensed(self, tracker, empty_registry):
        """Proves the sentence is read off the record, not hardcoded."""
        tracker.set_material("default", "PETG", determined_by="observed")
        adapter = _stub_adapter()
        with patch.object(server, "_get_adapter", return_value=adapter), patch.object(
            server, "_get_material_tracker", return_value=tracker
        ):
            result = server.preflight_check(expected_material="PETG")

        check = _material_check(result)
        assert check["sensed"] is True
        assert check["verified_by"] == "observed"
        assert "the printer reported" in check["message"]
        assert "no sensor" not in check["message"]

    def test_mismatch_still_blocks_and_names_its_source(self, tracker, empty_registry):
        tracker.set_material("default", "PLA")
        adapter = _stub_adapter()
        with patch.object(server, "_get_adapter", return_value=adapter), patch.object(
            server, "_get_material_tracker", return_value=tracker
        ):
            result = server.preflight_check(expected_material="PETG")

        check = _material_check(result)
        assert check["passed"] is False
        assert check["sensed"] is False
        assert "mismatch" in check["message"].lower()
        assert result["ready"] is False
        assert any("mismatch" in e.lower() for e in result["errors"])


# ===================================================================
# 3b. check_material_match — the same three states at the tool door
# ===================================================================


class TestCheckMaterialMatchTool:
    def test_nothing_recorded_is_not_a_match(self, tracker, empty_registry):
        with patch.object(server, "_get_material_tracker", return_value=tracker):
            result = server.check_material_match(
                expected_material="PETG", printer_name="p2s"
            )
        assert result["success"] is True
        assert result["match"] is None  # not True, and not False
        assert result["sensed"] is False
        assert result["verified_by"] is None
        assert "Cannot confirm" in result["note"]

    def test_declared_match_says_it_is_declared(self, tracker, empty_registry):
        tracker.set_material("p2s", "PETG")
        with patch.object(server, "_get_material_tracker", return_value=tracker):
            result = server.check_material_match(
                expected_material="PETG", printer_name="p2s"
            )
        assert result["match"] is True
        assert result["sensed"] is False
        assert result["verified_by"] == "user_reported"
        assert "no sensor confirmed it" in result["note"]

    def test_sensed_match_says_the_printer_reported_it(self, tracker, empty_registry):
        tracker.set_material("p2s", "PETG", determined_by="observed")
        with patch.object(server, "_get_material_tracker", return_value=tracker):
            result = server.check_material_match(
                expected_material="PETG", printer_name="p2s"
            )
        assert result["match"] is True
        assert result["sensed"] is True
        assert "the printer reported" in result["note"]

    def test_mismatch_keeps_its_warning(self, tracker, empty_registry):
        tracker.set_material("p2s", "PLA")
        with patch.object(server, "_get_material_tracker", return_value=tracker):
            result = server.check_material_match(
                expected_material="PETG", printer_name="p2s"
            )
        assert result["match"] is False
        assert result["warning"]["expected"] == "PETG"
        assert result["warning"]["loaded"] == "PLA"


# ===================================================================
# 3c. The answer is about the machine under test
# ===================================================================


class TestPrinterIdentity:
    def test_does_not_answer_about_another_machine(self, tracker):
        """``list_names()[0]`` is alphabetical, so 'alpha' used to answer for
        the printer actually under test."""
        registry = PrinterRegistry()
        other = _stub_adapter(host="http://alpha.local")
        under_test = _stub_adapter(host="http://p2s.local")
        registry.register("alpha", other)
        registry.register("zeta", under_test)
        tracker.set_material("alpha", "PLA")
        tracker.set_material("zeta", "PETG")

        with patch.object(server, "_get_registry", return_value=registry), patch.object(
            server, "_get_material_tracker", return_value=tracker
        ):
            assert registry.list_names()[0] == "alpha"  # the old answer
            assert server._material_store_name(under_test) == "zeta"
            result = server._material_match_report("zeta", "PETG")
        assert result["verdict"] == "match"

    def test_one_machine_under_two_labels_finds_its_row(self, tracker):
        """The bootstrap registers the active printer as "default" AND under
        its config.yaml name.  A row written under either is a true statement
        about the same machine."""
        registry = PrinterRegistry()
        adapter = _stub_adapter()
        registry.register("default", adapter)
        registry.register("p2s", adapter)
        tracker.set_material("default", "PETG")

        with patch.object(server, "_get_registry", return_value=registry), patch.object(
            server, "_get_material_tracker", return_value=tracker
        ):
            assert server._material_store_name(adapter) == "default"
            # With nothing recorded anywhere, the name a user recognises wins.
            assert server._printer_labels(adapter)[0] == "p2s"

    def test_unregistered_adapter_falls_back_to_default(self, tracker, empty_registry):
        with patch.object(server, "_get_material_tracker", return_value=tracker):
            assert server._material_store_name(_stub_adapter()) == "default"

    def test_registry_resolves_names_from_an_adapter(self):
        registry = PrinterRegistry()
        adapter = _stub_adapter()
        registry.register("default", adapter)
        registry.register("p2s", adapter)
        registry.register("other", _stub_adapter(host="http://other.local"))
        assert registry.names_for(adapter) == ["default", "p2s"]
        assert registry.aliases_of("p2s") == ["default", "p2s"]
