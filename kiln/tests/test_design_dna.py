"""Tests for Design DNA — parametric source storage in design cache."""

from __future__ import annotations

import os

import pytest

from kiln.design_cache import CachedDesign, DesignCache


@pytest.fixture()
def cache(tmp_path):
    """Create a fresh DesignCache in a temp directory."""
    return DesignCache(cache_dir=str(tmp_path / "cache"))


@pytest.fixture()
def sample_stl(tmp_path):
    """Create a minimal STL file for testing."""
    stl = tmp_path / "cube.stl"
    stl.write_text("solid cube\nendsolid cube\n")
    return str(stl)


SAMPLE_SCAD = """\
// Parametric cube
width = 20;
height = 10;
depth = 15;
cube([width, height, depth]);
"""

SAMPLE_PROMPT = "Generate a parametric cube with adjustable width, height, and depth"


class TestDesignDNAFields:
    """CachedDesign has scad_source, generation_prompt, provider fields."""

    def test_default_values_are_none(self):
        d = CachedDesign(
            id="test",
            file_name="cube.stl",
            file_path="/tmp/cube.stl",
            file_hash="abc123",
            file_size_bytes=100,
            printer_type="fdm",
            file_format="stl",
        )
        assert d.scad_source is None
        assert d.generation_prompt is None
        assert d.provider is None

    def test_fields_set_explicitly(self):
        d = CachedDesign(
            id="test",
            file_name="cube.stl",
            file_path="/tmp/cube.stl",
            file_hash="abc123",
            file_size_bytes=100,
            printer_type="fdm",
            file_format="stl",
            scad_source=SAMPLE_SCAD,
            generation_prompt=SAMPLE_PROMPT,
            provider="openscad",
        )
        assert d.scad_source == SAMPLE_SCAD
        assert d.generation_prompt == SAMPLE_PROMPT
        assert d.provider == "openscad"

    def test_to_dict_includes_dna_fields(self):
        d = CachedDesign(
            id="test",
            file_name="cube.stl",
            file_path="/tmp/cube.stl",
            file_hash="abc123",
            file_size_bytes=100,
            printer_type="fdm",
            file_format="stl",
            scad_source="cube([1,1,1]);",
            generation_prompt="a cube",
            provider="gemini",
        )
        result = d.to_dict()
        assert result["scad_source"] == "cube([1,1,1]);"
        assert result["generation_prompt"] == "a cube"
        assert result["provider"] == "gemini"


class TestAddWithSource:
    """DesignCache.add() stores Design DNA fields."""

    def test_add_with_scad_source(self, cache, sample_stl):
        entry = cache.add(
            sample_stl,
            scad_source=SAMPLE_SCAD,
            generation_prompt=SAMPLE_PROMPT,
            provider="openscad",
        )
        assert entry.scad_source == SAMPLE_SCAD
        assert entry.generation_prompt == SAMPLE_PROMPT
        assert entry.provider == "openscad"

    def test_add_without_source_defaults_none(self, cache, sample_stl):
        entry = cache.add(sample_stl)
        assert entry.scad_source is None
        assert entry.generation_prompt is None
        assert entry.provider is None

    def test_source_persists_across_get(self, cache, sample_stl):
        entry = cache.add(
            sample_stl,
            scad_source=SAMPLE_SCAD,
            generation_prompt=SAMPLE_PROMPT,
            provider="openscad",
        )
        retrieved = cache.get(entry.id)
        assert retrieved is not None
        assert retrieved.scad_source == SAMPLE_SCAD
        assert retrieved.generation_prompt == SAMPLE_PROMPT
        assert retrieved.provider == "openscad"

    def test_source_persists_across_search(self, cache, sample_stl):
        cache.add(
            sample_stl,
            scad_source=SAMPLE_SCAD,
            provider="openscad",
            tags=["test"],
        )
        results = cache.search(tags=["test"])
        assert len(results) == 1
        assert results[0].scad_source == SAMPLE_SCAD
        assert results[0].provider == "openscad"


class TestGetSource:
    """DesignCache.get_source() returns DNA fields."""

    def test_get_source_returns_fields(self, cache, sample_stl):
        entry = cache.add(
            sample_stl,
            scad_source=SAMPLE_SCAD,
            generation_prompt=SAMPLE_PROMPT,
            provider="openscad",
        )
        result = cache.get_source(entry.id)
        assert result is not None
        assert result["scad_source"] == SAMPLE_SCAD
        assert result["generation_prompt"] == SAMPLE_PROMPT
        assert result["provider"] == "openscad"

    def test_get_source_missing_design(self, cache):
        assert cache.get_source("nonexistent") is None

    def test_get_source_no_source_attached(self, cache, sample_stl):
        entry = cache.add(sample_stl)
        result = cache.get_source(entry.id)
        assert result is not None
        assert result["scad_source"] is None
        assert result["generation_prompt"] is None
        assert result["provider"] is None


class TestUpdateSource:
    """DesignCache.update_source() attaches source to existing designs."""

    def test_update_all_fields(self, cache, sample_stl):
        entry = cache.add(sample_stl)
        assert entry.scad_source is None

        ok = cache.update_source(
            entry.id,
            scad_source=SAMPLE_SCAD,
            generation_prompt=SAMPLE_PROMPT,
            provider="openscad",
        )
        assert ok is True

        retrieved = cache.get(entry.id)
        assert retrieved.scad_source == SAMPLE_SCAD
        assert retrieved.generation_prompt == SAMPLE_PROMPT
        assert retrieved.provider == "openscad"

    def test_update_partial_fields(self, cache, sample_stl):
        entry = cache.add(
            sample_stl,
            scad_source="old_code();",
            provider="old_provider",
        )
        cache.update_source(entry.id, scad_source="new_code();")

        retrieved = cache.get(entry.id)
        assert retrieved.scad_source == "new_code();"
        assert retrieved.provider == "old_provider"  # Unchanged

    def test_update_nonexistent_design(self, cache):
        ok = cache.update_source("nonexistent", scad_source="code();")
        assert ok is False

    def test_update_with_no_args(self, cache, sample_stl):
        entry = cache.add(sample_stl)
        ok = cache.update_source(entry.id)
        assert ok is True  # Design exists, nothing to update


class TestSearchByProvider:
    """DesignCache.search_by_provider() filters by generation provider."""

    def test_search_finds_matching_provider(self, cache, tmp_path):
        stl1 = tmp_path / "a.stl"
        stl1.write_text("solid a\nendsolid a\n")
        stl2 = tmp_path / "b.stl"
        stl2.write_text("solid b\nendsolid b\n")

        cache.add(str(stl1), provider="openscad", scad_source="cube();")
        cache.add(str(stl2), provider="gemini", scad_source="sphere();")

        results = cache.search_by_provider("openscad")
        assert len(results) == 1
        assert results[0].provider == "openscad"
        assert results[0].scad_source == "cube();"

    def test_search_empty_for_unknown_provider(self, cache, sample_stl):
        cache.add(sample_stl, provider="openscad")
        results = cache.search_by_provider("nonexistent")
        assert results == []

    def test_search_respects_limit(self, cache, tmp_path):
        for i in range(5):
            f = tmp_path / f"model_{i}.stl"
            f.write_text(f"solid m{i}\nendsolid m{i}\n")
            cache.add(str(f), provider="openscad")

        results = cache.search_by_provider("openscad", limit=3)
        assert len(results) == 3


class TestSchemaMigration:
    """Design DNA columns are added safely to existing databases."""

    def test_double_init_does_not_fail(self, tmp_path):
        """Creating two DesignCache instances on the same DB should not error."""
        cache_dir = str(tmp_path / "cache")
        c1 = DesignCache(cache_dir=cache_dir)
        c2 = DesignCache(cache_dir=cache_dir)
        c1.close()
        c2.close()

    def test_old_schema_gets_migrated(self, tmp_path):
        """A DB created without DNA columns gets them added on next open."""
        import sqlite3

        cache_dir = str(tmp_path / "cache")
        os.makedirs(cache_dir, exist_ok=True)
        db_path = os.path.join(cache_dir, "designs.db")

        # Create a DB with the old schema (no DNA columns).
        conn = sqlite3.connect(db_path)
        conn.executescript("""
            CREATE TABLE designs (
                id TEXT PRIMARY KEY,
                file_name TEXT NOT NULL,
                file_path TEXT NOT NULL,
                file_hash TEXT NOT NULL,
                file_size_bytes INTEGER NOT NULL,
                printer_type TEXT NOT NULL,
                file_format TEXT NOT NULL,
                tags_json TEXT NOT NULL DEFAULT '[]',
                source TEXT,
                filament_type TEXT,
                estimated_print_time_s REAL,
                dimensions_json TEXT,
                slicer_used TEXT,
                created_at REAL NOT NULL,
                last_used_at REAL NOT NULL,
                use_count INTEGER NOT NULL DEFAULT 0,
                metadata_json TEXT NOT NULL DEFAULT '{}'
            );
        """)
        conn.close()

        # Open with DesignCache — should add DNA columns.
        cache = DesignCache(cache_dir=cache_dir)

        # Verify columns exist by querying them.
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(designs)").fetchall()]
        conn.close()
        cache.close()

        assert "scad_source" in cols
        assert "generation_prompt" in cols
        assert "provider" in cols
