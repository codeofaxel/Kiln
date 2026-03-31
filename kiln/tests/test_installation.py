"""Tests for kiln.installation — anonymous installation ID system."""

from __future__ import annotations

import threading
import uuid
from pathlib import Path

import pytest

import kiln.installation as inst
from kiln.installation import get_installation_headers, get_installation_id


@pytest.fixture(autouse=True)
def _clear_cache():
    """Reset the module-level cache between tests."""
    inst._cached_id = None
    yield
    inst._cached_id = None


@pytest.fixture
def id_path(tmp_path: Path) -> Path:
    return tmp_path / ".kiln" / "installation_id"


# --- Core behavior ---


def test_first_call_generates_uuid(id_path: Path):
    """First call should generate a valid UUID4 and write it to file."""
    result = get_installation_id(path=id_path)

    assert uuid.UUID(result).version == 4
    assert id_path.is_file()
    assert id_path.read_text(encoding="utf-8") == result


def test_idempotent(id_path: Path):
    """Subsequent calls return the same UUID."""
    first = get_installation_id(path=id_path)
    second = get_installation_id(path=id_path)

    assert first == second


def test_reads_existing_file(id_path: Path):
    """If a valid UUID file exists, return it without overwriting."""
    known_id = str(uuid.uuid4())
    id_path.parent.mkdir(parents=True, exist_ok=True)
    id_path.write_text(known_id, encoding="utf-8")

    result = get_installation_id(path=id_path)
    assert result == known_id


# --- Corrupt / missing ---


def test_corrupt_file_triggers_regeneration(id_path: Path):
    """A corrupt file should be replaced with a fresh UUID."""
    id_path.parent.mkdir(parents=True, exist_ok=True)
    id_path.write_text("not-a-uuid", encoding="utf-8")

    result = get_installation_id(path=id_path)
    assert uuid.UUID(result).version == 4
    assert id_path.read_text(encoding="utf-8") == result


def test_empty_file_triggers_regeneration(id_path: Path):
    """An empty file should be treated as corrupt."""
    id_path.parent.mkdir(parents=True, exist_ok=True)
    id_path.write_text("", encoding="utf-8")

    result = get_installation_id(path=id_path)
    assert uuid.UUID(result).version == 4


def test_whitespace_trimmed(id_path: Path):
    """Trailing newlines should be trimmed when reading."""
    known_id = str(uuid.uuid4())
    id_path.parent.mkdir(parents=True, exist_ok=True)
    id_path.write_text(f"  {known_id}\n", encoding="utf-8")

    assert get_installation_id(path=id_path) == known_id


# --- Directory creation ---


def test_missing_directory_gets_created(tmp_path: Path):
    """The ~/.kiln/ directory should be created if it doesn't exist."""
    deep_path = tmp_path / "new_dir" / "sub" / "installation_id"
    result = get_installation_id(path=deep_path)

    assert uuid.UUID(result).version == 4
    assert deep_path.is_file()


# --- Thread safety ---


def test_thread_safety(id_path: Path):
    """Concurrent calls should all get the same ID."""
    results: list[str] = []
    barrier = threading.Barrier(10)

    def worker():
        barrier.wait()
        results.append(get_installation_id(path=id_path))

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(set(results)) == 1
    assert uuid.UUID(results[0]).version == 4


# --- Headers ---


def test_get_installation_headers(id_path: Path):
    """get_installation_headers returns correct format."""
    headers = get_installation_headers(path=id_path)

    assert "X-Kiln-Installation-Id" in headers
    assert uuid.UUID(headers["X-Kiln-Installation-Id"]).version == 4


def test_headers_consistent_with_id(id_path: Path):
    """Headers should use the same ID as get_installation_id."""
    iid = get_installation_id(path=id_path)
    headers = get_installation_headers(path=id_path)

    assert headers["X-Kiln-Installation-Id"] == iid


# --- Cache behavior ---


def test_cache_used_for_default_path(monkeypatch, tmp_path: Path):
    """When using default path, the in-memory cache avoids repeated file I/O."""
    fake_path = tmp_path / ".kiln" / "installation_id"
    monkeypatch.setattr(inst, "_DEFAULT_PATH", fake_path)

    first = get_installation_id()  # No path arg — uses default
    # Delete file to prove cache is being used.
    fake_path.unlink()
    second = get_installation_id()

    assert first == second


def test_cache_not_used_for_custom_path(id_path: Path):
    """When a custom path is given, the cache is bypassed."""
    first = get_installation_id(path=id_path)
    # Overwrite with a different UUID.
    new_id = str(uuid.uuid4())
    id_path.write_text(new_id, encoding="utf-8")
    second = get_installation_id(path=id_path)

    assert second == new_id
    assert first != second
