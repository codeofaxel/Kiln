"""Tests for design version control (kiln.design_versions)."""

from __future__ import annotations

import os
import threading
import time

import pytest

from kiln.design_versions import DesignVersionStore

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def store(tmp_path):
    """Create a DesignVersionStore backed by a temp database."""
    db_path = str(tmp_path / "test_versions.db")
    s = DesignVersionStore(db_path=db_path)
    yield s
    s.close()


SCAD_V1 = """\
// Phone stand v1
module phone_stand(width=60, depth=80, angle=65) {
    difference() {
        cube([width, depth, 3]);
        translate([0, 0, 3])
            rotate([angle, 0, 0])
                cube([width, depth, 100]);
    }
}
phone_stand();
"""

SCAD_V2 = """\
// Phone stand v2 — added cable slot
module phone_stand(width=60, depth=80, angle=65, slot_width=12) {
    difference() {
        cube([width, depth, 3]);
        translate([0, 0, 3])
            rotate([angle, 0, 0])
                cube([width, depth, 100]);
        // Cable pass-through
        translate([width/2 - slot_width/2, depth/2, -1])
            cube([slot_width, 5, 5]);
    }
}
phone_stand();
"""

SCAD_V3 = """\
// Phone stand v3 — rounded edges
module phone_stand(width=60, depth=80, angle=65, slot_width=12, fillet=2) {
    minkowski() {
        difference() {
            cube([width - 2*fillet, depth - 2*fillet, 3 - fillet]);
            translate([0, 0, 3])
                rotate([angle, 0, 0])
                    cube([width, depth, 100]);
            translate([width/2 - slot_width/2, depth/2, -1])
                cube([slot_width, 5, 5]);
        }
        sphere(r=fillet, $fn=16);
    }
}
phone_stand();
"""


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSaveAndRetrieve:
    def test_save_and_get_version(self, store):
        v = store.save_version("d1", SCAD_V1, prompt="phone stand")
        assert v.version_id
        assert v.design_id == "d1"
        assert v.scad_source == SCAD_V1
        assert v.prompt == "phone stand"
        assert v.parent_version_id is None
        assert v.diff_from_prev is None

        fetched = store.get_version(v.version_id)
        assert fetched is not None
        assert fetched.version_id == v.version_id
        assert fetched.scad_source == SCAD_V1

    def test_get_nonexistent_version(self, store):
        assert store.get_version("does-not-exist") is None

    def test_save_with_parameters(self, store):
        params = {"width": 60, "depth": 80, "angle": 65}
        v = store.save_version("d1", SCAD_V1, parameters=params)
        assert v.parameters == params

        fetched = store.get_version(v.version_id)
        assert fetched.parameters == params

    def test_save_with_notes(self, store):
        v = store.save_version("d1", SCAD_V1, notes="initial prototype")
        assert v.notes == "initial prototype"

    def test_default_parameters_empty_dict(self, store):
        v = store.save_version("d1", SCAD_V1)
        assert v.parameters == {}


class TestAutoIncrement:
    def test_version_count_increments(self, store):
        v1 = store.save_version("d1", SCAD_V1)
        v2 = store.save_version("d1", SCAD_V2)
        v3 = store.save_version("d1", SCAD_V3)

        assert v1.parent_version_id is None
        assert v2.parent_version_id == v1.version_id
        assert v3.parent_version_id == v2.version_id

    def test_independent_designs_have_separate_chains(self, store):
        va = store.save_version("design_a", SCAD_V1)
        vb = store.save_version("design_b", SCAD_V2)

        assert va.parent_version_id is None
        assert vb.parent_version_id is None


class TestDiff:
    def test_diff_from_prev_computed(self, store):
        store.save_version("d1", SCAD_V1)
        v2 = store.save_version("d1", SCAD_V2)

        assert v2.diff_from_prev is not None
        assert "cable slot" in v2.diff_from_prev.lower() or "slot_width" in v2.diff_from_prev

    def test_first_version_has_no_diff(self, store):
        v = store.save_version("d1", SCAD_V1)
        assert v.diff_from_prev is None

    def test_diff_between_arbitrary_versions(self, store):
        v1 = store.save_version("d1", SCAD_V1)
        store.save_version("d1", SCAD_V2)
        v3 = store.save_version("d1", SCAD_V3)

        diff = store.diff_versions(v1.version_id, v3.version_id)
        assert "---" in diff
        assert "+++" in diff
        assert len(diff) > 0

    def test_diff_identical_versions(self, store):
        v1 = store.save_version("d1", SCAD_V1)
        v2 = store.save_version("d2", SCAD_V1)
        diff = store.diff_versions(v1.version_id, v2.version_id)
        assert diff == ""

    def test_diff_nonexistent_version_a(self, store):
        v1 = store.save_version("d1", SCAD_V1)
        with pytest.raises(ValueError, match="not found"):
            store.diff_versions("bogus", v1.version_id)

    def test_diff_nonexistent_version_b(self, store):
        v1 = store.save_version("d1", SCAD_V1)
        with pytest.raises(ValueError, match="not found"):
            store.diff_versions(v1.version_id, "bogus")


class TestRollback:
    def test_rollback_creates_new_version(self, store):
        v1 = store.save_version("d1", SCAD_V1, prompt="v1")
        store.save_version("d1", SCAD_V2, prompt="v2")

        rolled = store.rollback("d1", v1.version_id)
        assert rolled.scad_source == SCAD_V1
        assert rolled.version_id != v1.version_id
        assert "Rollback" in rolled.notes

        # Should now be the latest
        latest = store.get_latest("d1")
        assert latest.version_id == rolled.version_id

    def test_rollback_to_nonexistent_version(self, store):
        store.save_version("d1", SCAD_V1)
        with pytest.raises(ValueError, match="not found"):
            store.rollback("d1", "nonexistent")

    def test_rollback_wrong_design(self, store):
        v1 = store.save_version("design_a", SCAD_V1)
        store.save_version("design_b", SCAD_V2)

        with pytest.raises(ValueError, match="design_a"):
            store.rollback("design_b", v1.version_id)


class TestListVersions:
    def test_list_newest_first(self, store):
        store.save_version("d1", SCAD_V1)
        time.sleep(0.01)
        store.save_version("d1", SCAD_V2)
        time.sleep(0.01)
        store.save_version("d1", SCAD_V3)

        versions = store.list_versions("d1")
        assert len(versions) == 3
        assert versions[0].created_at >= versions[1].created_at
        assert versions[1].created_at >= versions[2].created_at

    def test_list_respects_limit(self, store):
        for i in range(5):
            store.save_version("d1", f"// version {i}")

        versions = store.list_versions("d1", limit=3)
        assert len(versions) == 3

    def test_list_empty_design(self, store):
        versions = store.list_versions("nonexistent")
        assert versions == []


class TestGetLatest:
    def test_get_latest(self, store):
        store.save_version("d1", SCAD_V1)
        v2 = store.save_version("d1", SCAD_V2)

        latest = store.get_latest("d1")
        assert latest is not None
        assert latest.version_id == v2.version_id

    def test_get_latest_empty(self, store):
        assert store.get_latest("nonexistent") is None


class TestDeleteVersion:
    def test_delete_existing(self, store):
        v = store.save_version("d1", SCAD_V1)
        assert store.delete_version(v.version_id) is True
        assert store.get_version(v.version_id) is None

    def test_delete_nonexistent(self, store):
        assert store.delete_version("bogus") is False


class TestSearch:
    def test_search_by_prompt(self, store):
        store.save_version("d1", SCAD_V1, prompt="phone stand for desk")
        store.save_version("d2", SCAD_V2, prompt="cable organizer")

        results = store.search_versions("phone")
        assert len(results) == 1
        assert results[0].design_id == "d1"

    def test_search_by_notes(self, store):
        store.save_version("d1", SCAD_V1, notes="first attempt")
        store.save_version("d2", SCAD_V2, notes="refined design")

        results = store.search_versions("refined")
        assert len(results) == 1
        assert results[0].design_id == "d2"

    def test_search_no_matches(self, store):
        store.save_version("d1", SCAD_V1, prompt="phone stand")
        results = store.search_versions("xyzzy_not_found")
        assert results == []

    def test_search_respects_limit(self, store):
        for i in range(10):
            store.save_version(f"d{i}", f"// v{i}", prompt="common term")
        results = store.search_versions("common", limit=3)
        assert len(results) == 3


class TestSerialization:
    def test_to_dict(self, store):
        v = store.save_version(
            "d1", SCAD_V1,
            prompt="phone stand",
            parameters={"width": 60},
            notes="initial",
        )
        d = v.to_dict()
        assert isinstance(d, dict)
        assert d["version_id"] == v.version_id
        assert d["design_id"] == "d1"
        assert d["scad_source"] == SCAD_V1
        assert d["prompt"] == "phone stand"
        assert d["parameters"] == {"width": 60}
        assert d["notes"] == "initial"
        assert isinstance(d["created_at"], float)

    def test_to_dict_keys(self, store):
        v = store.save_version("d1", SCAD_V1)
        d = v.to_dict()
        expected_keys = {
            "version_id", "design_id", "scad_source", "prompt",
            "parameters", "diff_from_prev", "created_at",
            "parent_version_id", "notes",
        }
        assert expected_keys.issubset(set(d.keys()))


class TestThreadSafety:
    def test_concurrent_saves(self, store):
        errors = []
        results = []
        lock = threading.Lock()

        def save_versions(start_idx):
            try:
                for i in range(5):
                    v = store.save_version(
                        "d1",
                        f"// concurrent version {start_idx}_{i}",
                        prompt=f"thread {start_idx}",
                    )
                    with lock:
                        results.append(v.version_id)
            except Exception as exc:
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=save_versions, args=(t,)) for t in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"Thread errors: {errors}"
        assert len(results) == 20
        # All version IDs should be unique
        assert len(set(results)) == 20


class TestDatabaseCreation:
    def test_auto_creates_database(self, tmp_path):
        db_path = str(tmp_path / "subdir" / "nested" / "versions.db")
        s = DesignVersionStore(db_path=db_path)
        try:
            v = s.save_version("d1", SCAD_V1)
            assert v.version_id
            assert os.path.exists(db_path)
        finally:
            s.close()

    def test_reopens_existing_database(self, tmp_path):
        db_path = str(tmp_path / "versions.db")
        s1 = DesignVersionStore(db_path=db_path)
        v = s1.save_version("d1", SCAD_V1)
        s1.close()

        s2 = DesignVersionStore(db_path=db_path)
        try:
            fetched = s2.get_version(v.version_id)
            assert fetched is not None
            assert fetched.scad_source == SCAD_V1
        finally:
            s2.close()


class TestLargeSource:
    def test_large_scad_source(self, store):
        large_source = "// big file\n" + ("cube([1,1,1]);\n" * 10_000)
        v = store.save_version("d1", large_source)
        fetched = store.get_version(v.version_id)
        assert fetched.scad_source == large_source

    def test_diff_large_source(self, store):
        src_a = "line_a\n" * 5000
        src_b = "line_b\n" * 5000
        store.save_version("d1", src_a)
        v2 = store.save_version("d1", src_b)
        assert v2.diff_from_prev is not None
        assert len(v2.diff_from_prev) > 0


class TestInputValidation:
    def test_empty_design_id_rejected(self, store):
        with pytest.raises(ValueError, match="design_id must not be empty"):
            store.save_version("", SCAD_V1)

    def test_blank_design_id_rejected(self, store):
        with pytest.raises(ValueError, match="design_id must not be empty"):
            store.save_version("   ", SCAD_V1)

    def test_empty_scad_source_now_allowed_for_mesh_only_versions(self, store):
        # Empty source is allowed (mesh-only imports use this path) —
        # the version is stored with scad_source as the literal empty
        # string.  None (canonical mesh-only shape) is also allowed and
        # is exercised in test_null_source_version.py.
        v = store.save_version("d1", "")
        assert v.scad_source == ""

    def test_null_byte_in_design_id_rejected(self, store):
        with pytest.raises(ValueError, match="null bytes"):
            store.save_version("d1\x00bad", SCAD_V1)

    def test_null_byte_in_scad_source_rejected(self, store):
        with pytest.raises(ValueError, match="null bytes"):
            store.save_version("d1", "cube();\x00DROP TABLE")

    def test_null_byte_in_prompt_rejected(self, store):
        with pytest.raises(ValueError, match="null bytes"):
            store.save_version("d1", SCAD_V1, prompt="good\x00bad")

    def test_null_byte_in_notes_rejected(self, store):
        with pytest.raises(ValueError, match="null bytes"):
            store.save_version("d1", SCAD_V1, notes="ok\x00evil")


class TestUnicode:
    def test_unicode_in_source(self, store):
        src = "// Schale mit Griff \u2014 \u00e4\u00f6\u00fc\u00df\nmodule cup() { sphere(r=10); }"
        v = store.save_version("d1", src)
        fetched = store.get_version(v.version_id)
        assert fetched.scad_source == src

    def test_unicode_in_prompt_and_notes(self, store):
        v = store.save_version(
            "d1", SCAD_V1,
            prompt="\u8bbe\u8ba1\u4e00\u4e2a\u624b\u673a\u652f\u67b6",
            notes="\u2615 caf\u00e9 holder \u2764",
        )
        fetched = store.get_version(v.version_id)
        assert fetched.prompt == "\u8bbe\u8ba1\u4e00\u4e2a\u624b\u673a\u652f\u67b6"
        assert fetched.notes == "\u2615 caf\u00e9 holder \u2764"

    def test_search_unicode(self, store):
        store.save_version("d1", SCAD_V1, prompt="\u8bbe\u8ba1\u4e00\u4e2a\u624b\u673a\u652f\u67b6")
        results = store.search_versions("\u624b\u673a")
        assert len(results) == 1


class TestLikeEscaping:
    def test_search_with_percent_literal(self, store):
        store.save_version("d1", SCAD_V1, prompt="100% done")
        store.save_version("d2", SCAD_V2, prompt="something else")
        results = store.search_versions("100%")
        assert len(results) == 1
        assert results[0].design_id == "d1"

    def test_search_with_underscore_literal(self, store):
        store.save_version("d1", SCAD_V1, prompt="var_name")
        store.save_version("d2", SCAD_V2, prompt="varXname")
        results = store.search_versions("var_name")
        assert len(results) == 1
        assert results[0].design_id == "d1"


class TestLimitClamping:
    def test_negative_limit_clamped(self, store):
        store.save_version("d1", SCAD_V1)
        versions = store.list_versions("d1", limit=-5)
        assert len(versions) == 1

    def test_zero_limit_clamped(self, store):
        store.save_version("d1", SCAD_V1)
        versions = store.list_versions("d1", limit=0)
        assert len(versions) == 1

    def test_search_negative_limit_clamped(self, store):
        store.save_version("d1", SCAD_V1, prompt="findme")
        results = store.search_versions("findme", limit=-1)
        assert len(results) == 1
