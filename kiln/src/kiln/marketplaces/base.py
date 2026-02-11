"""Abstract base for 3D model marketplace adapters.

Every marketplace backend (Thingiverse, MyMiniFactory, Cults3D, etc.)
implements :class:`MarketplaceAdapter` so that the rest of the system
can search, browse, and download models through a uniform interface.
"""

from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class MarketplaceError(Exception):
    """Base exception for marketplace API errors."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class MarketplaceAuthError(MarketplaceError):
    """Raised when credentials are missing or invalid."""


class MarketplaceNotFoundError(MarketplaceError):
    """Raised when a requested resource does not exist."""


class MarketplaceRateLimitError(MarketplaceError):
    """Raised when the API rate limit has been exceeded."""


# ---------------------------------------------------------------------------
# Shared dataclasses
# ---------------------------------------------------------------------------


@dataclass
class ModelFile:
    """A downloadable file attached to a marketplace model."""

    id: str
    name: str
    size_bytes: int = 0
    download_url: str = ""
    thumbnail_url: Optional[str] = None
    date: Optional[str] = None
    file_type: str = ""  # "stl", "gcode", "3mf", "obj", etc.

    @property
    def is_printable(self) -> bool:
        """Whether this file can be sent directly to a printer."""
        ext = self.name.rsplit(".", 1)[-1].lower() if "." in self.name else ""
        return ext in {"gcode", "gco", "g"}

    @property
    def needs_slicing(self) -> bool:
        """Whether this file needs to be sliced before printing."""
        ext = self.name.rsplit(".", 1)[-1].lower() if "." in self.name else ""
        return ext in {"stl", "3mf", "obj", "step", "stp"}

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["is_printable"] = self.is_printable
        d["needs_slicing"] = self.needs_slicing
        return d


@dataclass
class ModelSummary:
    """Lightweight model summary returned from search / browse endpoints."""

    id: str
    name: str
    url: str
    creator: str
    source: str  # "thingiverse", "myminifactory", "cults3d"
    thumbnail: Optional[str] = None
    like_count: int = 0
    download_count: int = 0
    license: str = ""
    is_free: bool = True
    price_cents: int = 0  # in USD cents, 0 = free
    has_printable_files: bool = False  # has .gcode files
    has_sliceable_files: bool = True  # has .stl / .3mf files
    can_download: bool = True  # False for metadata-only sources (e.g. Cults3D)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ModelDetail:
    """Full details for a single model."""

    id: str
    name: str
    url: str
    creator: str
    source: str
    description: str = ""
    instructions: str = ""
    license: str = ""
    thumbnail: Optional[str] = None
    like_count: int = 0
    download_count: int = 0
    category: Optional[str] = None
    tags: List[str] = field(default_factory=list)
    file_count: int = 0
    is_free: bool = True
    price_cents: int = 0
    can_download: bool = True  # False for metadata-only sources

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Abstract adapter
# ---------------------------------------------------------------------------


class MarketplaceAdapter(ABC):
    """Abstract base class for marketplace backends.

    Subclasses must implement :meth:`search`, :meth:`get_details`,
    and :meth:`get_files`.  :meth:`download_file` has a default
    implementation that raises :class:`MarketplaceError` for
    metadata-only adapters.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Short identifier for this marketplace (e.g. ``"thingiverse"``)."""

    @property
    @abstractmethod
    def display_name(self) -> str:
        """Human-readable marketplace name (e.g. ``"Thingiverse"``)."""

    @property
    def supports_download(self) -> bool:
        """Whether this adapter can download model files.

        Returns ``True`` by default.  Override to ``False`` for
        metadata-only adapters like Cults3D.
        """
        return True

    @abstractmethod
    def search(
        self,
        query: str,
        *,
        page: int = 1,
        per_page: int = 20,
        sort: str = "relevant",
    ) -> List[ModelSummary]:
        """Search for models by keyword."""

    @abstractmethod
    def get_details(self, model_id: str) -> ModelDetail:
        """Get full details for a single model."""

    @abstractmethod
    def get_files(self, model_id: str) -> List[ModelFile]:
        """List downloadable files for a model."""

    def download_file(
        self,
        file_id: str,
        dest_dir: str,
        *,
        file_name: str | None = None,
    ) -> str:
        """Download a model file to a local directory.

        Returns the absolute path to the downloaded file.

        The default implementation raises :class:`MarketplaceError`.
        Adapters that support downloads must override this method.
        """
        raise MarketplaceError(
            f"{self.display_name} does not support direct file downloads.",
        )


# ---------------------------------------------------------------------------
# Resumable download helper
# ---------------------------------------------------------------------------


def resumable_download(
    session: Any,
    url: str,
    out_path: Path,
    *,
    params: Dict[str, str] | None = None,
    timeout: int = 120,
    chunk_size: int = 65536,
    max_retries: int = 3,
) -> str:
    """Download a file with resume support via HTTP Range headers.

    Writes to a ``.part`` temp file and renames on completion.  If a
    partial ``.part`` file exists from a previous interrupted download,
    resumes from where it left off.

    Args:
        session: A ``requests.Session`` to use for the download.
        url: The download URL.
        out_path: Final destination path.
        params: Optional query params to attach to the request.
        timeout: Request timeout in seconds.
        chunk_size: Bytes per chunk (default 64 KB).
        max_retries: Number of resume attempts on failure.

    Returns:
        Absolute path to the downloaded file.

    Raises:
        MarketplaceError: If the download fails after all retries.
    """
    part_path = out_path.with_suffix(out_path.suffix + ".part")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # If final file already exists and has content, return it
    if out_path.exists() and out_path.stat().st_size > 0:
        return str(out_path.resolve())

    for attempt in range(max_retries):
        headers: Dict[str, str] = {}
        existing_bytes = 0

        if part_path.exists():
            existing_bytes = part_path.stat().st_size
            if existing_bytes > 0:
                headers["Range"] = f"bytes={existing_bytes}-"
                _logger.info(
                    "Resuming download at byte %d (attempt %d/%d)",
                    existing_bytes, attempt + 1, max_retries,
                )

        try:
            resp = session.get(
                url,
                params=params,
                headers=headers,
                timeout=timeout,
                stream=True,
            )

            # If server doesn't support Range, start fresh
            if resp.status_code == 200 and existing_bytes > 0:
                _logger.debug("Server returned 200 (no range support), restarting")
                existing_bytes = 0
                mode = "wb"
            elif resp.status_code == 206:
                mode = "ab"
            elif resp.status_code == 416:
                # Range not satisfiable — file is already complete
                if part_path.exists():
                    part_path.rename(out_path)
                    return str(out_path.resolve())
                mode = "wb"
            else:
                resp.raise_for_status()
                mode = "wb"

            with open(part_path, mode) as fh:
                for chunk in resp.iter_content(chunk_size=chunk_size):
                    fh.write(chunk)

            # Verify we got content and file integrity
            if part_path.exists() and part_path.stat().st_size > 0:
                actual_size = part_path.stat().st_size
                # Check Content-Length if the server provided it
                expected_size = resp.headers.get("Content-Length")
                if expected_size is not None:
                    try:
                        expected = int(expected_size) + existing_bytes
                        if actual_size < expected:
                            _logger.warning(
                                "Incomplete download: got %d bytes, expected %d",
                                actual_size, expected,
                            )
                            # Don't rename — leave .part for resume on next attempt
                            continue
                    except (ValueError, TypeError):
                        pass  # Malformed Content-Length, skip check
                part_path.rename(out_path)
                _logger.info(
                    "Downloaded %s (%d bytes)",
                    out_path, out_path.stat().st_size,
                )
                return str(out_path.resolve())

        except Exception as exc:
            _logger.warning(
                "Download attempt %d/%d failed: %s", attempt + 1, max_retries, exc,
            )
            if attempt == max_retries - 1:
                raise MarketplaceError(
                    f"Download failed after {max_retries} attempts: {exc}",
                ) from exc

    raise MarketplaceError("Download failed: no data received.")
