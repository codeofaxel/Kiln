"""Tests for the local model cache/library.

Coverage:
- Add/get/search/delete lifecycle
- Deduplication by file hash
- Search by query, source, tags
- Print count tracking
- Cache directory creation
- Edge cases: missing file, empty source, nonexistent ID
"""

from __future__ import annotations

import json
import os
import time

import pytest

from kiln.model_cache import ModelCache, ModelCacheEntry, _compute_sha256
from kiln.persistence import KilnDB


@pytest.fixture()
def db(tmp_path):
    """Create a temporary KilnDB instance."""
    db_path = str(tmp_path / "test.db")
    return KilnDB(db_path=db_path)


@pytest.fixture()
def cache(db, tmp_path):
    """Create a ModelCache with a temporary cache directory."""
    cache_dir = str(tmp_path / "model_cache")
    return ModelCache(db=db, cache_dir=cache_dir)


@pytest.fixture()
def sample_stl(tmp_path):
    """Create a sample STL file for testing."""
    stl_path = tmp_path / "benchy.stl"
    stl_path.write_bytes(b"solid benchy\nfacet normal 0 0 1\nendsolid benchy\n")
    return str(stl_path)


@pytest.fixture()
def another_stl(tmp_path):
    """Create a second STL file with different content."""
    stl_path = tmp_path / "cube.stl"
    stl_path.write_bytes(b"solid cube\nfacet normal 1 0 0\nendsolid cube\n")
    return str(stl_path)


class TestModelCacheEntry:
    """ModelCacheEntry dataclass tests."""

    def test_to_dict_returns_all_fields(self):
        entry = ModelCacheEntry(
            cache_id="abc123",
            file_name="benchy.stl",
            file_path="/tmp/cache/benchy.stl",
            file_hash="deadbeef",
            file_size_bytes=1024,
            source="thingiverse",
            source_id="763622",
            prompt=None,
            tags=["calibration", "test"],
            dimensions={"x": 60.0, "y": 31.0, "z": 48.0},
            print_count=3,
            last_printed_at=1700000000.0,
            created_at=1699000000.0,
            metadata={"license": "CC-BY"},
        )
        d = entry.to_dict()
        assert d["cache_id"] == "abc123"
        assert d["file_name"] == "benchy.stl"
        assert d["source"] == "thingiverse"
        assert d["tags"] == ["calibration", "test"]
        assert d["dimensions"]["x"] == 60.0
        assert d["print_count"] == 3
        assert d["metadata"]["license"] == "CC-BY"

    def test_defaults(self):
        entry = ModelCacheEntry(
            cache_id="x",
            file_name="f.stl",
            file_path="/tmp/f.stl",
            file_hash="h",
            file_size_bytes=0,
            source="upload",
        )
        assert entry.tags == []
        assert entry.dimensions is None
        assert entry.print_count == 0
        assert entry.metadata == {}


class TestComputeSha256:
    """SHA-256 computation tests."""

    def test_hash_is_deterministic(self, sample_stl):
        h1 = _compute_sha256(sample_stl)
        h2 = _compute_sha256(sample_stl)
        assert h1 == h2
        assert len(h1) == 64

    def test_different_files_different_hashes(self, sample_stl, another_stl):
        h1 = _compute_sha256(sample_stl)
        h2 = _compute_sha256(another_stl)
        assert h1 != h2


class TestModelCacheAdd:
    """ModelCache.add() tests."""

    def test_add_copies_file_to_cache(self, cache, sample_stl):
        entry = cache.add(sample_stl, source="upload")
        assert os.path.isfile(entry.file_path)
        assert entry.file_name == "benchy.stl"
        assert entry.source == "upload"
        assert entry.file_size_bytes > 0
        assert len(entry.cache_id) == 16  # secrets.token_hex(8) = 16 chars

    def test_add_with_all_metadata(self, cache, sample_stl):
        entry = cache.add(
            sample_stl,
            source="thingiverse",
            source_id="763622",
            prompt=None,
            tags=["calibration", "benchy"],
            dimensions={"x": 60.0, "y": 31.0, "z": 48.0},
            metadata={"license": "CC-BY"},
        )
        assert entry.source_id == "763622"
        assert entry.tags == ["calibration", "benchy"]
        assert entry.dimensions["z"] == 48.0
        assert entry.metadata["license"] == "CC-BY"

    def test_add_missing_file_raises(self, cache):
        with pytest.raises(FileNotFoundError, match="not found"):
            cache.add("/nonexistent/model.stl", source="upload")

    def test_add_empty_source_raises(self, cache, sample_stl):
        with pytest.raises(ValueError, match="source is required"):
            cache.add(sample_stl, source="")

    def test_add_creates_cache_directory(self, db, tmp_path, sample_stl):
        cache_dir = str(tmp_path / "new_cache_dir")
        assert not os.path.exists(cache_dir)
        cache = ModelCache(db=db, cache_dir=cache_dir)
        assert os.path.isdir(cache_dir)


class TestModelCacheDedup:
    """Deduplication by file hash."""

    def test_duplicate_file_returns_existing_entry(self, cache, sample_stl):
        entry1 = cache.add(sample_stl, source="upload")
        entry2 = cache.add(sample_stl, source="thingiverse")  # same file, different source
        assert entry1.cache_id == entry2.cache_id
        assert entry1.file_hash == entry2.file_hash

    def test_different_files_create_separate_entries(self, cache, sample_stl, another_stl):
        entry1 = cache.add(sample_stl, source="upload")
        entry2 = cache.add(another_stl, source="upload")
        assert entry1.cache_id != entry2.cache_id
        assert entry1.file_hash != entry2.file_hash

    def test_get_by_hash(self, cache, sample_stl):
        entry = cache.add(sample_stl, source="upload")
        found = cache.get_by_hash(entry.file_hash)
        assert found is not None
        assert found.cache_id == entry.cache_id

    def test_get_by_hash_not_found(self, cache):
        assert cache.get_by_hash("nonexistenthash") is None


class TestModelCacheGet:
    """ModelCache.get() tests."""

    def test_get_existing_entry(self, cache, sample_stl):
        entry = cache.add(sample_stl, source="upload")
        found = cache.get(entry.cache_id)
        assert found is not None
        assert found.file_name == "benchy.stl"
        assert found.file_hash == entry.file_hash

    def test_get_nonexistent_returns_none(self, cache):
        assert cache.get("nonexistent_id") is None


class TestModelCacheSearch:
    """ModelCache.search() tests."""

    def test_search_by_file_name(self, cache, sample_stl, another_stl):
        cache.add(sample_stl, source="upload")
        cache.add(another_stl, source="upload")
        results = cache.search(query="benchy")
        assert len(results) == 1
        assert results[0].file_name == "benchy.stl"

    def test_search_by_source(self, cache, sample_stl, another_stl):
        cache.add(sample_stl, source="thingiverse")
        cache.add(another_stl, source="meshy")
        results = cache.search(source="thingiverse")
        assert len(results) == 1
        assert results[0].source == "thingiverse"

    def test_search_by_tags(self, cache, sample_stl, another_stl):
        cache.add(sample_stl, source="upload", tags=["calibration", "benchy"])
        cache.add(another_stl, source="upload", tags=["functional", "box"])
        results = cache.search(tags=["calibration"])
        assert len(results) == 1
        assert "calibration" in results[0].tags

    def test_search_by_prompt(self, cache, sample_stl):
        cache.add(sample_stl, source="meshy", prompt="a small tugboat")
        results = cache.search(query="tugboat")
        assert len(results) == 1

    def test_search_no_results(self, cache, sample_stl):
        cache.add(sample_stl, source="upload")
        results = cache.search(query="nonexistent_term_xyz")
        assert len(results) == 0

    def test_search_limit(self, cache, tmp_path):
        for i in range(5):
            p = tmp_path / f"model_{i}.stl"
            p.write_bytes(f"solid model_{i}\nendsolid\n".encode())
            cache.add(str(p), source="upload")
        results = cache.search(limit=3)
        assert len(results) == 3


class TestModelCachePrintTracking:
    """Print count and last_printed_at tracking."""

    def test_record_print_increments_count(self, cache, sample_stl):
        entry = cache.add(sample_stl, source="upload")
        assert entry.print_count == 0

        cache.record_print(entry.cache_id)
        updated = cache.get(entry.cache_id)
        assert updated.print_count == 1
        assert updated.last_printed_at is not None

    def test_record_print_multiple_times(self, cache, sample_stl):
        entry = cache.add(sample_stl, source="upload")
        cache.record_print(entry.cache_id)
        cache.record_print(entry.cache_id)
        cache.record_print(entry.cache_id)
        updated = cache.get(entry.cache_id)
        assert updated.print_count == 3


class TestModelCacheDelete:
    """ModelCache.delete() tests."""

    def test_delete_removes_entry_and_file(self, cache, sample_stl):
        entry = cache.add(sample_stl, source="upload")
        cached_path = entry.file_path
        assert os.path.isfile(cached_path)

        deleted = cache.delete(entry.cache_id)
        assert deleted is True
        assert cache.get(entry.cache_id) is None
        assert not os.path.isfile(cached_path)

    def test_delete_nonexistent_returns_false(self, cache):
        assert cache.delete("nonexistent_id") is False

    def test_delete_cleans_up_empty_hash_dir(self, cache, sample_stl):
        entry = cache.add(sample_stl, source="upload")
        hash_dir = os.path.dirname(entry.file_path)
        cache.delete(entry.cache_id)
        assert not os.path.exists(hash_dir)


class TestModelCacheListAll:
    """ModelCache.list_all() tests."""

    def test_list_all_empty(self, cache):
        assert cache.list_all() == []

    def test_list_all_returns_entries(self, cache, sample_stl, another_stl):
        cache.add(sample_stl, source="upload")
        cache.add(another_stl, source="upload")
        entries = cache.list_all()
        assert len(entries) == 2

    def test_list_all_with_limit_and_offset(self, cache, tmp_path):
        for i in range(5):
            p = tmp_path / f"model_{i}.stl"
            p.write_bytes(f"solid model_{i}\nendsolid\n".encode())
            cache.add(str(p), source="upload")
        page1 = cache.list_all(limit=2, offset=0)
        page2 = cache.list_all(limit=2, offset=2)
        assert len(page1) == 2
        assert len(page2) == 2
        assert page1[0].cache_id != page2[0].cache_id
