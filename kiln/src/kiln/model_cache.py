"""Local model cache/library for Kiln.

Agents save generated or downloaded models locally with tagged metadata
(source, prompt, dimensions, print history) so they can reuse them
across jobs without re-downloading or re-generating.

Files are stored in ``~/.kiln/model_cache/`` (override with
``KILN_MODEL_CACHE_DIR``).  Each file is stored under its SHA-256 hash
to enable automatic deduplication.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import shutil
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from kiln.persistence import KilnDB

logger = logging.getLogger(__name__)

_DEFAULT_CACHE_DIR = os.path.join(str(Path.home()), ".kiln", "model_cache")


@dataclass
class ModelCacheEntry:
    """A single cached 3D model file with metadata."""

    cache_id: str
    file_name: str
    file_path: str
    file_hash: str
    file_size_bytes: int
    source: str
    source_id: Optional[str] = None
    prompt: Optional[str] = None
    tags: List[str] = field(default_factory=list)
    dimensions: Optional[Dict[str, float]] = None
    print_count: int = 0
    last_printed_at: Optional[float] = None
    created_at: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Serialise to a plain dict suitable for JSON output."""
        d = asdict(self)
        return d


def _compute_sha256(file_path: str) -> str:
    """Compute the SHA-256 hex digest of a file."""
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


class ModelCache:
    """Manages a local cache of 3D model files with metadata.

    Parameters:
        db: The :class:`KilnDB` instance for metadata storage.
        cache_dir: Directory to store cached model files.  Defaults to
            ``KILN_MODEL_CACHE_DIR`` or ``~/.kiln/model_cache/``.
    """

    def __init__(self, db: KilnDB, cache_dir: Optional[str] = None) -> None:
        self._db = db
        self._cache_dir = cache_dir or os.environ.get(
            "KILN_MODEL_CACHE_DIR", _DEFAULT_CACHE_DIR
        )
        os.makedirs(self._cache_dir, exist_ok=True)

    @property
    def cache_dir(self) -> str:
        """The filesystem path of the cache directory."""
        return self._cache_dir

    def add(
        self,
        file_path: str,
        *,
        source: str,
        source_id: Optional[str] = None,
        prompt: Optional[str] = None,
        tags: Optional[List[str]] = None,
        dimensions: Optional[Dict[str, float]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> ModelCacheEntry:
        """Add a model file to the cache.

        The file is copied into the cache directory under a subdirectory
        named after its SHA-256 hash (enabling automatic deduplication).
        If a file with the same hash already exists, the existing entry
        is returned without re-copying.

        Args:
            file_path: Path to the model file on disk.
            source: Origin of the model (e.g. ``"thingiverse"``,
                ``"meshy"``, ``"upload"``).
            source_id: Marketplace thing ID, generation job ID, etc.
            prompt: For AI-generated models, the prompt used.
            tags: Descriptive tags for search.
            dimensions: Bounding box in mm: ``{"x": ..., "y": ..., "z": ...}``.
            metadata: Arbitrary extra data.

        Returns:
            The :class:`ModelCacheEntry` for the cached file.

        Raises:
            FileNotFoundError: If *file_path* does not exist.
            ValueError: If *source* is empty.
        """
        if not os.path.isfile(file_path):
            raise FileNotFoundError(f"Model file not found: {file_path}")
        if not source:
            raise ValueError("source is required")

        file_hash = _compute_sha256(file_path)

        # Dedup check — return existing entry if the same file is cached.
        existing = self.get_by_hash(file_hash)
        if existing is not None:
            logger.info(
                "File already cached (hash=%s, cache_id=%s)",
                file_hash[:12],
                existing.cache_id,
            )
            return existing

        # Copy file into cache directory.
        file_name = os.path.basename(file_path)
        hash_dir = os.path.join(self._cache_dir, file_hash[:16])
        os.makedirs(hash_dir, exist_ok=True)
        dest_path = os.path.join(hash_dir, file_name)
        shutil.copy2(file_path, dest_path)
        file_size = os.path.getsize(dest_path)

        cache_id = secrets.token_hex(8)
        now = time.time()

        entry = ModelCacheEntry(
            cache_id=cache_id,
            file_name=file_name,
            file_path=dest_path,
            file_hash=file_hash,
            file_size_bytes=file_size,
            source=source,
            source_id=source_id,
            prompt=prompt,
            tags=tags or [],
            dimensions=dimensions,
            print_count=0,
            last_printed_at=None,
            created_at=now,
            metadata=metadata or {},
        )

        self._db.save_cache_entry(entry)
        logger.info("Cached model %s (%s, %d bytes)", cache_id, file_name, file_size)
        return entry

    def get(self, cache_id: str) -> Optional[ModelCacheEntry]:
        """Return a cache entry by ID, or ``None`` if not found."""
        return self._db.get_cache_entry(cache_id)

    def get_by_hash(self, file_hash: str) -> Optional[ModelCacheEntry]:
        """Return a cache entry by file hash (dedup check), or ``None``."""
        return self._db.get_cache_entry_by_hash(file_hash)

    def search(
        self,
        *,
        query: Optional[str] = None,
        source: Optional[str] = None,
        tags: Optional[List[str]] = None,
        limit: int = 20,
    ) -> List[ModelCacheEntry]:
        """Search cached models by name, tags, source, or prompt text.

        Args:
            query: Free-text search against file name, prompt, and tags.
            source: Filter by source (e.g. ``"thingiverse"``).
            tags: Filter entries that contain ALL of these tags.
            limit: Maximum results to return.
        """
        return self._db.search_cache(
            query=query, source=source, tags=tags, limit=limit,
        )

    def record_print(self, cache_id: str) -> None:
        """Increment print_count and update last_printed_at for a cached model."""
        self._db.record_cache_print(cache_id)

    def delete(self, cache_id: str) -> bool:
        """Remove a model from the cache directory and DB.

        Returns ``True`` if the entry was found and deleted.
        """
        entry = self.get(cache_id)
        if entry is None:
            return False

        # Remove file from disk.
        try:
            if os.path.isfile(entry.file_path):
                os.remove(entry.file_path)
                # Clean up hash directory if empty.
                parent = os.path.dirname(entry.file_path)
                if os.path.isdir(parent) and not os.listdir(parent):
                    os.rmdir(parent)
        except OSError as exc:
            logger.warning("Could not remove cached file %s: %s", entry.file_path, exc)

        self._db.delete_cache_entry(cache_id)
        logger.info("Deleted cached model %s (%s)", cache_id, entry.file_name)
        return True

    def list_all(self, *, limit: int = 50, offset: int = 0) -> List[ModelCacheEntry]:
        """List all cached models, newest first."""
        return self._db.list_cache_entries(limit=limit, offset=offset)


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_model_cache: Optional[ModelCache] = None


def get_model_cache() -> ModelCache:
    """Return the module-level :class:`ModelCache` singleton.

    Lazily created on first call, using the default :func:`get_db`
    instance and the default or env-configured cache directory.
    """
    global _model_cache
    if _model_cache is None:
        from kiln.persistence import get_db
        _model_cache = ModelCache(db=get_db())
    return _model_cache
