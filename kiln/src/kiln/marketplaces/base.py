"""Abstract base for 3D model marketplace adapters.

Every marketplace backend (Thingiverse, MyMiniFactory, Cults3D, etc.)
implements :class:`MarketplaceAdapter` so that the rest of the system
can search, browse, and download models through a uniform interface.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


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
