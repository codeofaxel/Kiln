"""Tests for the nullable ``scad_source`` path on ``DesignVersionStore``.

External mesh files (STL/3MF/OBJ from Thingiverse, MakerWorld, etc.)
register as first-class design versions with ``scad_source=None`` —
the version still participates in the version genealogy via its mesh
fingerprint, but no source code exists to diff or recompile.

This test file pins the contract end-to-end so a future refactor that
re-introduces a NOT NULL constraint or rejects ``None`` fails fast.
"""

from __future__ import annotations

import sqlite3

import pytest

from kiln.design_versions import DesignVersion, DesignVersionStore


@pytest.fixture()
def store(tmp_path):
    s = DesignVersionStore(db_path=str(tmp_path / "dv.db"))
    yield s
    s.close()


# ---------------------------------------------------------------------------
# Save-side contract
# ---------------------------------------------------------------------------


class TestSaveWithNoneSource:
    def test_save_version_accepts_none_source(self, store):
        v = store.save_version(design_id="thingiverse-12345", scad_source=None)
        assert isinstance(v, DesignVersion)
        assert v.scad_source is None
        assert v.design_id == "thingiverse-12345"

    def test_save_version_default_argument_is_none(self, store):
        # Calling save_version without an explicit scad_source argument
        # must work — the canonical mesh-only signature.
        v = store.save_version(design_id="mug")
        assert v.scad_source is None

    def test_round_trip_via_get_version(self, store):
        v = store.save_version(design_id="mug", scad_source=None, notes="STL import")
        loaded = store.get_version(v.version_id)
        assert loaded is not None
        assert loaded.scad_source is None
        assert loaded.notes == "STL import"

    def test_two_none_source_versions_chain_via_parent(self, store):
        v1 = store.save_version(design_id="mug", scad_source=None)
        v2 = store.save_version(design_id="mug", scad_source=None)
        assert v2.parent_version_id == v1.version_id
        # No source on either side → no textual diff.
        assert v2.diff_from_prev is None

    def test_mixed_chain_source_then_no_source(self, store):
        v1 = store.save_version(design_id="mug", scad_source="cube([10,10,10]);\n")
        v2 = store.save_version(design_id="mug", scad_source=None)
        # Diff against a None side is None — diff is meaningless without
        # source on both ends; callers fall back to mesh fingerprint.
        assert v2.diff_from_prev is None
        assert v1.scad_source == "cube([10,10,10]);\n"
        assert v2.scad_source is None


# ---------------------------------------------------------------------------
# Diff helper contract
# ---------------------------------------------------------------------------


class TestDiffHelperHandlesNone:
    def test_diff_with_none_old(self, store):
        # Internal helper — protected from accidentally crashing when a
        # consumer passes a None on either side.
        assert store._compute_diff(None, "cube();\n") is None

    def test_diff_with_none_new(self, store):
        assert store._compute_diff("cube();\n", None) is None

    def test_diff_with_both_none(self, store):
        assert store._compute_diff(None, None) is None

    def test_diff_with_both_present_unchanged(self, store):
        assert store._compute_diff("a\n", "a\n") == ""

    def test_diff_with_both_present_changed(self, store):
        out = store._compute_diff("a\n", "b\n")
        assert out is not None and "+b" in out and "-a" in out


# ---------------------------------------------------------------------------
# Schema migration on legacy databases
# ---------------------------------------------------------------------------


class TestSchemaMigration:
    def test_legacy_not_null_db_migrates_to_nullable(self, tmp_path):
        """Simulate a database created before the nullable migration:
        build the table by hand with NOT NULL on scad_source, insert a
        row, then open it through DesignVersionStore (which runs the
        migration on first access) and verify the new column accepts
        NULL while the old row is preserved."""
        db_path = str(tmp_path / "legacy.db")
        conn = sqlite3.connect(db_path)
        conn.executescript(
            """
            CREATE TABLE design_versions (
                version_id        TEXT PRIMARY KEY,
                design_id         TEXT NOT NULL,
                scad_source       TEXT NOT NULL,
                prompt            TEXT NOT NULL DEFAULT '',
                parameters        TEXT NOT NULL DEFAULT '{}',
                diff_from_prev    TEXT,
                created_at        REAL NOT NULL,
                parent_version_id TEXT,
                notes             TEXT NOT NULL DEFAULT '',
                version_number    INTEGER NOT NULL DEFAULT 1
            );
            INSERT INTO design_versions
                (version_id, design_id, scad_source, created_at)
                VALUES ('legacy_v1', 'old_design', 'cube([1,1,1]);', 0.0);
            """
        )
        conn.commit()
        conn.close()

        # Opening the store triggers the migration.
        store = DesignVersionStore(db_path=db_path)
        try:
            # The legacy row survives the migration.
            v = store.get_version("legacy_v1")
            assert v is not None
            assert v.scad_source == "cube([1,1,1]);"

            # New rows can have NULL scad_source.
            v2 = store.save_version(design_id="new_design", scad_source=None)
            assert v2.scad_source is None

            # The PRAGMA reports the column as nullable now.
            row = next(
                c for c in store._conn.execute(
                    "PRAGMA table_info(design_versions)"
                )
                if c[1] == "scad_source"
            )
            assert row[3] == 0, "scad_source NOT NULL constraint should be dropped"
        finally:
            store.close()

    def test_migration_idempotent_on_already_nullable_db(self, tmp_path):
        """Opening a fresh DB twice — the second call's migration is a
        no-op since the column is already nullable.  Verifies the
        PRAGMA detection short-circuits cleanly."""
        db_path = str(tmp_path / "fresh.db")
        s1 = DesignVersionStore(db_path=db_path)
        s1.save_version(design_id="d1", scad_source=None)
        s1.close()

        s2 = DesignVersionStore(db_path=db_path)
        try:
            assert s2.get_version
            # The fresh row is still there.
            rows = list(
                s2._conn.execute(
                    "SELECT version_id FROM design_versions WHERE design_id = ?",
                    ("d1",),
                )
            )
            assert len(rows) == 1
        finally:
            s2.close()
