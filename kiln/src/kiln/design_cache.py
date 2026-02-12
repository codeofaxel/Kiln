"""Design/file cache for Kiln multi-printer manufacturing.

Agents save generated or downloaded manufacturing files locally with
tagged metadata (printer type, format, source, dimensions, filament info)
so they can reuse them across jobs without re-downloading or re-generating.

Files are stored in ``~/.kiln/cache/designs/`` (override with
``KILN_CACHE_DIR``).  Each file is stored under its SHA-256 hash
to enable automatic deduplication.

Supported file formats for FDM printing:

- **Model files**: ``.stl``, ``.3mf``, ``.obj``, ``.step``, ``.amf``
- **G-code files**: ``.gcode``, ``.gco``, ``.g``, ``.bgcode``
- **Project files**: ``.3mf`` (PrusaSlicer/OrcaSlicer/BambuStudio projects)
- **Slicer configs**: ``.ini``, ``.json``

Each cached entry may also store FDM-relevant metadata:

- **filament_type**: PLA, PETG, ABS, TPU, ASA, Nylon, etc.
- **estimated_print_time_s**: Estimated print duration in seconds.
- **dimensions_mm**: Bounding box as ``{"x": float, "y": float, "z": float}``.
- **slicer_used**: Name and version of the slicer (e.g. ``"PrusaSlicer 2.7.1"``).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import shutil
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_DEFAULT_CACHE_DIR = os.path.join(str(Path.home()), ".kiln", "cache", "designs")

# ---------------------------------------------------------------------------
# File format -> printer type mapping
# ---------------------------------------------------------------------------

_FILE_FORMAT_TO_PRINTER_TYPE: Dict[str, str] = {
    # Model files (pre-slicing)
    "stl": "fdm",
    "3mf": "fdm",
    "obj": "fdm",
    "step": "fdm",
    "stp": "fdm",
    "amf": "fdm",
    "ply": "fdm",
    # G-code (post-slicing, ready to print)
    "gcode": "fdm",
    "gco": "fdm",
    "g": "fdm",
    "bgcode": "fdm",
    "nc": "fdm",
    # Slicer project / config files
    "ini": "fdm",
    "json": "fdm",
}

_VALID_PRINTER_TYPES = {"fdm"}

# Formats that represent sliced, ready-to-print files.
_GCODE_FORMATS = {"gcode", "gco", "g", "bgcode", "nc"}

# Formats that represent 3-D model geometry (pre-slicing).
_MODEL_FORMATS = {"stl", "3mf", "obj", "step", "stp", "amf", "ply"}


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class CachedDesign:
    """A single cached design file with FDM-relevant metadata.

    The ``metadata`` dict may contain the following FDM-specific keys
    in addition to any arbitrary user-supplied data:

    - ``filament_type`` (str): Material name, e.g. ``"PLA"``.
    - ``estimated_print_time_s`` (float): Estimated print duration in seconds.
    - ``dimensions_mm`` (dict): Bounding box ``{"x": float, "y": float, "z": float}``.
    - ``slicer_used`` (str): Slicer name and version string.
    - ``layer_height_mm`` (float): Layer height in millimetres.
    - ``infill_percent`` (int): Infill density percentage.
    - ``supports`` (bool): Whether support structures are enabled.
    - ``printer_model`` (str): Target printer model name.
    """

    id: str
    file_name: str
    file_path: str
    file_hash: str
    file_size_bytes: int
    printer_type: str
    file_format: str
    tags: List[str] = field(default_factory=list)
    source: Optional[str] = None
    filament_type: Optional[str] = None
    estimated_print_time_s: Optional[float] = None
    dimensions_mm: Optional[Dict[str, float]] = None
    slicer_used: Optional[str] = None
    created_at: float = 0.0
    last_used_at: float = 0.0
    use_count: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Serialise to a plain dict suitable for JSON output."""
        return asdict(self)


@dataclass
class CacheStats:
    """Aggregate statistics for the design cache."""

    total_files: int
    total_size_bytes: int
    cache_hits: int
    cache_misses: int
    by_printer_type: Dict[str, int] = field(default_factory=dict)
    by_format: Dict[str, int] = field(default_factory=dict)
    by_filament_type: Dict[str, int] = field(default_factory=dict)
    oldest_file: Optional[float] = None
    newest_file: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        """Serialise to a plain dict suitable for JSON output."""
        return asdict(self)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _compute_sha256(file_path: str) -> str:
    """Compute the SHA-256 hex digest of a file."""
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _extension_to_format(file_name: str) -> str:
    """Extract the normalised file extension (without dot) from a filename."""
    _, ext = os.path.splitext(file_name)
    return ext.lstrip(".").lower()


def _detect_printer_type(file_format: str) -> Optional[str]:
    """Return the printer type for a known file format, or ``None``."""
    return _FILE_FORMAT_TO_PRINTER_TYPE.get(file_format)


def _is_gcode(file_format: str) -> bool:
    """Return ``True`` if the format is a G-code / sliced file."""
    return file_format in _GCODE_FORMATS


def _is_model(file_format: str) -> bool:
    """Return ``True`` if the format is a 3-D model geometry file."""
    return file_format in _MODEL_FORMATS


# ---------------------------------------------------------------------------
# DesignCache
# ---------------------------------------------------------------------------


class DesignCache:
    """Manages a local cache of design files with FDM-specific metadata.

    The cache stores files on disk under hash-based subdirectories for
    automatic deduplication.  Metadata (tags, filament type, print time
    estimates, dimensions, slicer info) is stored in a companion SQLite
    database for fast querying.

    LRU eviction is applied during :meth:`cleanup` — the least-recently-used
    files are removed first when the cache exceeds its size limit.

    Cache hit/miss metrics are tracked per-instance and included in
    :meth:`stats` output.

    :param cache_dir: Directory to store cached files.  Defaults to
        ``KILN_CACHE_DIR`` or ``~/.kiln/cache/designs/``.
    :param max_size_mb: Default maximum cache size in megabytes for
        cleanup operations.
    """

    def __init__(
        self,
        *,
        cache_dir: Optional[str] = None,
        max_size_mb: float = 500.0,
    ) -> None:
        self._cache_dir = cache_dir or os.environ.get(
            "KILN_CACHE_DIR", _DEFAULT_CACHE_DIR
        )
        self._max_size_mb = max_size_mb
        self._lock = threading.Lock()

        # Cache hit metrics.
        self._hits = 0
        self._misses = 0

        os.makedirs(self._cache_dir, exist_ok=True)

        # SQLite metadata store alongside the cache directory.
        db_path = os.path.join(self._cache_dir, "designs.db")
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._ensure_schema()

    @property
    def cache_dir(self) -> str:
        """The filesystem path of the cache directory."""
        return self._cache_dir

    @property
    def hits(self) -> int:
        """Total number of cache hits since initialisation."""
        return self._hits

    @property
    def misses(self) -> int:
        """Total number of cache misses since initialisation."""
        return self._misses

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def _ensure_schema(self) -> None:
        """Create the designs table if it does not already exist."""
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS designs (
                    id                      TEXT PRIMARY KEY,
                    file_name               TEXT NOT NULL,
                    file_path               TEXT NOT NULL,
                    file_hash               TEXT NOT NULL,
                    file_size_bytes         INTEGER NOT NULL,
                    printer_type            TEXT NOT NULL,
                    file_format             TEXT NOT NULL,
                    tags_json               TEXT NOT NULL DEFAULT '[]',
                    source                  TEXT,
                    filament_type           TEXT,
                    estimated_print_time_s  REAL,
                    dimensions_json         TEXT,
                    slicer_used             TEXT,
                    created_at              REAL NOT NULL,
                    last_used_at            REAL NOT NULL,
                    use_count               INTEGER NOT NULL DEFAULT 0,
                    metadata_json           TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_designs_hash
                    ON designs(file_hash);
                CREATE INDEX IF NOT EXISTS idx_designs_printer_type
                    ON designs(printer_type);
                CREATE INDEX IF NOT EXISTS idx_designs_format
                    ON designs(file_format);
                CREATE INDEX IF NOT EXISTS idx_designs_filament
                    ON designs(filament_type);
                CREATE INDEX IF NOT EXISTS idx_designs_created
                    ON designs(created_at);
                CREATE INDEX IF NOT EXISTS idx_designs_last_used
                    ON designs(last_used_at);
                """
            )
            self._conn.commit()

    # ------------------------------------------------------------------
    # Row conversion
    # ------------------------------------------------------------------

    def _row_to_cached_design(self, row: sqlite3.Row) -> CachedDesign:
        """Convert a database row to a :class:`CachedDesign` instance."""
        d = dict(row)
        dimensions_raw = d.get("dimensions_json")
        dimensions = json.loads(dimensions_raw) if dimensions_raw else None
        return CachedDesign(
            id=d["id"],
            file_name=d["file_name"],
            file_path=d["file_path"],
            file_hash=d["file_hash"],
            file_size_bytes=d["file_size_bytes"],
            printer_type=d["printer_type"],
            file_format=d["file_format"],
            tags=json.loads(d["tags_json"]),
            source=d["source"],
            filament_type=d["filament_type"],
            estimated_print_time_s=d["estimated_print_time_s"],
            dimensions_mm=dimensions,
            slicer_used=d["slicer_used"],
            created_at=d["created_at"],
            last_used_at=d["last_used_at"],
            use_count=d["use_count"],
            metadata=json.loads(d["metadata_json"]),
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add(
        self,
        file_path: str,
        *,
        printer_type: Optional[str] = None,
        tags: Optional[List[str]] = None,
        source: Optional[str] = None,
        filament_type: Optional[str] = None,
        estimated_print_time_s: Optional[float] = None,
        dimensions_mm: Optional[Dict[str, float]] = None,
        slicer_used: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> CachedDesign:
        """Add a file to the cache.

        Computes SHA-256, copies to cache dir, records metadata.
        If a file with the same hash already exists, the existing
        entry is returned without re-copying (deduplication).

        :param file_path: Path to the design file on disk.
        :param printer_type: Printer type (``"fdm"``).
            Auto-detected from file extension if not provided.
        :param tags: Descriptive tags for search.
        :param source: Where the file came from (URL, local path, etc.).
        :param filament_type: Filament material, e.g. ``"PLA"``, ``"PETG"``.
        :param estimated_print_time_s: Estimated print duration in seconds.
        :param dimensions_mm: Bounding box ``{"x": float, "y": float, "z": float}``.
        :param slicer_used: Slicer name and version, e.g. ``"PrusaSlicer 2.7.1"``.
        :param metadata: Arbitrary extra data (layer height, infill, etc.).
        :returns: The :class:`CachedDesign` for the cached file.
        :raises FileNotFoundError: If *file_path* does not exist.
        :raises ValueError: If *printer_type* cannot be determined.
        """
        if not os.path.isfile(file_path):
            raise FileNotFoundError(f"Design file not found: {file_path}")

        file_name = os.path.basename(file_path)
        file_format = _extension_to_format(file_name)

        # Resolve printer type: explicit > auto-detect.
        resolved_type = printer_type or _detect_printer_type(file_format)
        if resolved_type is None:
            raise ValueError(
                f"Cannot determine printer_type for format {file_format!r}. "
                f"Provide printer_type explicitly."
            )
        if resolved_type not in _VALID_PRINTER_TYPES:
            raise ValueError(
                f"Invalid printer_type {resolved_type!r}. "
                f"Must be one of: {sorted(_VALID_PRINTER_TYPES)}"
            )

        file_hash = _compute_sha256(file_path)

        # Dedup check -- return existing entry if the same content is cached.
        existing = self.get_by_hash(file_hash)
        if existing is not None:
            self._hits += 1
            logger.info(
                "File already cached (hash=%s, id=%s)",
                file_hash[:12],
                existing.id,
            )
            return existing

        self._misses += 1

        # Copy file into cache directory under hash-based subdirectory.
        hash_dir = os.path.join(self._cache_dir, file_hash[:2])
        os.makedirs(hash_dir, exist_ok=True)
        dest_path = os.path.join(hash_dir, file_name)
        shutil.copy2(file_path, dest_path)
        file_size = os.path.getsize(dest_path)

        design_id = secrets.token_hex(8)
        now = time.time()

        entry = CachedDesign(
            id=design_id,
            file_name=file_name,
            file_path=dest_path,
            file_hash=file_hash,
            file_size_bytes=file_size,
            printer_type=resolved_type,
            file_format=file_format,
            tags=tags or [],
            source=source,
            filament_type=filament_type,
            estimated_print_time_s=estimated_print_time_s,
            dimensions_mm=dimensions_mm,
            slicer_used=slicer_used,
            created_at=now,
            last_used_at=now,
            use_count=0,
            metadata=metadata or {},
        )

        with self._lock:
            self._conn.execute(
                """
                INSERT INTO designs
                    (id, file_name, file_path, file_hash, file_size_bytes,
                     printer_type, file_format, tags_json, source,
                     filament_type, estimated_print_time_s, dimensions_json,
                     slicer_used, created_at, last_used_at, use_count,
                     metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry.id,
                    entry.file_name,
                    entry.file_path,
                    entry.file_hash,
                    entry.file_size_bytes,
                    entry.printer_type,
                    entry.file_format,
                    json.dumps(entry.tags),
                    entry.source,
                    entry.filament_type,
                    entry.estimated_print_time_s,
                    json.dumps(entry.dimensions_mm) if entry.dimensions_mm else None,
                    entry.slicer_used,
                    entry.created_at,
                    entry.last_used_at,
                    entry.use_count,
                    json.dumps(entry.metadata),
                ),
            )
            self._conn.commit()

        logger.info(
            "Cached design %s (%s, %d bytes, %s, filament=%s)",
            design_id,
            file_name,
            file_size,
            resolved_type,
            filament_type or "unknown",
        )
        return entry

    def get(self, design_id: str) -> Optional[CachedDesign]:
        """Look up a cached design by ID.

        Increments the cache hit counter on success or miss counter
        on failure.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM designs WHERE id = ?", (design_id,)
            ).fetchone()
        if row is None:
            self._misses += 1
            return None
        self._hits += 1
        return self._row_to_cached_design(row)

    def get_by_hash(self, file_hash: str) -> Optional[CachedDesign]:
        """Look up by file hash (deduplication)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM designs WHERE file_hash = ?", (file_hash,)
            ).fetchone()
        if row is None:
            return None
        return self._row_to_cached_design(row)

    def search(
        self,
        *,
        printer_type: Optional[str] = None,
        file_format: Optional[str] = None,
        filament_type: Optional[str] = None,
        slicer_used: Optional[str] = None,
        tags: Optional[List[str]] = None,
        query: Optional[str] = None,
        limit: int = 50,
    ) -> List[CachedDesign]:
        """Search cached designs with filters.

        :param printer_type: Filter by printer type.
        :param file_format: Filter by file format extension.
        :param filament_type: Filter by filament material.
        :param slicer_used: Filter by slicer name (substring match).
        :param tags: Filter entries that contain ALL of these tags.
        :param query: Free-text search against file name and tags.
        :param limit: Maximum results to return.
        """
        clauses: List[str] = []
        params: List[Any] = []

        if printer_type is not None:
            clauses.append("printer_type = ?")
            params.append(printer_type)
        if file_format is not None:
            clauses.append("file_format = ?")
            params.append(file_format)
        if filament_type is not None:
            clauses.append("filament_type = ?")
            params.append(filament_type)
        if slicer_used is not None:
            clauses.append("slicer_used LIKE ?")
            params.append(f"%{slicer_used}%")
        if query is not None:
            clauses.append("(file_name LIKE ? OR tags_json LIKE ?)")
            pattern = f"%{query}%"
            params.extend([pattern, pattern])

        where = ""
        if clauses:
            where = "WHERE " + " AND ".join(clauses)

        params.append(limit)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM designs {where} "
                "ORDER BY last_used_at DESC LIMIT ?",
                params,
            ).fetchall()

        results = [self._row_to_cached_design(r) for r in rows]

        # Tag filtering is done in Python since tags are JSON-encoded.
        if tags:
            tag_set = set(tags)
            results = [r for r in results if tag_set.issubset(set(r.tags))]

        return results

    def record_use(self, design_id: str) -> None:
        """Increment use count and update last_used_at."""
        now = time.time()
        with self._lock:
            self._conn.execute(
                "UPDATE designs SET use_count = use_count + 1, "
                "last_used_at = ? WHERE id = ?",
                (now, design_id),
            )
            self._conn.commit()

    def remove(self, design_id: str) -> bool:
        """Remove a design from cache (file + metadata).

        Returns ``True`` if the entry was found and deleted.
        """
        entry = self.get(design_id)
        if entry is None:
            return False

        # Remove file from disk.
        try:
            if os.path.isfile(entry.file_path):
                os.remove(entry.file_path)
                # Clean up hash subdirectory if empty.
                parent = os.path.dirname(entry.file_path)
                if os.path.isdir(parent) and not os.listdir(parent):
                    os.rmdir(parent)
        except OSError as exc:
            logger.warning(
                "Could not remove cached file %s: %s", entry.file_path, exc
            )

        with self._lock:
            self._conn.execute("DELETE FROM designs WHERE id = ?", (design_id,))
            self._conn.commit()

        logger.info("Deleted cached design %s (%s)", design_id, entry.file_name)
        return True

    def stats(self) -> CacheStats:
        """Get cache statistics including hit/miss metrics."""
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS cnt, "
                "COALESCE(SUM(file_size_bytes), 0) AS total_size, "
                "MIN(created_at) AS oldest, MAX(created_at) AS newest "
                "FROM designs"
            ).fetchone()

            total_files = row["cnt"]
            total_size = row["total_size"]
            oldest = row["oldest"]
            newest = row["newest"]

            # By printer type.
            type_rows = self._conn.execute(
                "SELECT printer_type, COUNT(*) AS cnt FROM designs "
                "GROUP BY printer_type"
            ).fetchall()
            by_printer_type = {r["printer_type"]: r["cnt"] for r in type_rows}

            # By format.
            fmt_rows = self._conn.execute(
                "SELECT file_format, COUNT(*) AS cnt FROM designs "
                "GROUP BY file_format"
            ).fetchall()
            by_format = {r["file_format"]: r["cnt"] for r in fmt_rows}

            # By filament type.
            filament_rows = self._conn.execute(
                "SELECT filament_type, COUNT(*) AS cnt FROM designs "
                "WHERE filament_type IS NOT NULL "
                "GROUP BY filament_type"
            ).fetchall()
            by_filament_type = {
                r["filament_type"]: r["cnt"] for r in filament_rows
            }

        return CacheStats(
            total_files=total_files,
            total_size_bytes=total_size,
            cache_hits=self._hits,
            cache_misses=self._misses,
            by_printer_type=by_printer_type,
            by_format=by_format,
            by_filament_type=by_filament_type,
            oldest_file=oldest,
            newest_file=newest,
        )

    def cleanup(
        self,
        *,
        max_age_days: float = 90,
        max_size_mb: Optional[float] = None,
    ) -> int:
        """Remove old/excess files using LRU eviction.  Returns count removed.

        First removes files older than *max_age_days* (based on
        ``last_used_at``), then if the cache still exceeds
        *max_size_mb*, removes least-recently-used files until it fits.
        """
        removed = 0
        cutoff = time.time() - (max_age_days * 86400)

        # Phase 1: remove by age.
        with self._lock:
            old_rows = self._conn.execute(
                "SELECT id FROM designs WHERE last_used_at < ? "
                "ORDER BY last_used_at ASC",
                (cutoff,),
            ).fetchall()
        for row in old_rows:
            if self.remove(row["id"]):
                removed += 1

        # Phase 2: LRU eviction by size if needed.
        effective_max = max_size_mb if max_size_mb is not None else self._max_size_mb
        max_bytes = effective_max * 1024 * 1024

        with self._lock:
            current_size = self._conn.execute(
                "SELECT COALESCE(SUM(file_size_bytes), 0) FROM designs"
            ).fetchone()[0]

        if current_size > max_bytes:
            # Remove least-recently-used files until under limit.
            with self._lock:
                lru_rows = self._conn.execute(
                    "SELECT id, file_size_bytes FROM designs "
                    "ORDER BY last_used_at ASC"
                ).fetchall()
            for row in lru_rows:
                if current_size <= max_bytes:
                    break
                if self.remove(row["id"]):
                    current_size -= row["file_size_bytes"]
                    removed += 1

        return removed

    def export_design(self, design_id: str, destination: str) -> str:
        """Copy a cached design to a destination path.

        :param design_id: ID of the cached design.
        :param destination: Destination file path or directory.
        :returns: The final destination path.
        :raises FileNotFoundError: If the design is not found in cache.
        """
        entry = self.get(design_id)
        if entry is None:
            raise FileNotFoundError(f"Design not found in cache: {design_id}")

        if os.path.isdir(destination):
            dest_path = os.path.join(destination, entry.file_name)
        else:
            os.makedirs(os.path.dirname(destination) or ".", exist_ok=True)
            dest_path = destination

        shutil.copy2(entry.file_path, dest_path)
        logger.info("Exported design %s to %s", design_id, dest_path)
        return dest_path

    def close(self) -> None:
        """Close the database connection."""
        self._conn.close()


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_design_cache: Optional[DesignCache] = None


def get_design_cache() -> DesignCache:
    """Return the module-level :class:`DesignCache` singleton.

    Lazily created on first call using the default or env-configured
    cache directory.
    """
    global _design_cache
    if _design_cache is None:
        _design_cache = DesignCache()
    return _design_cache


__all__ = [
    "CachedDesign",
    "CacheStats",
    "DesignCache",
    "get_design_cache",
]
