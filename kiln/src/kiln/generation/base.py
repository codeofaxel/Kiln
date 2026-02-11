"""Abstract base for 3D model generation providers.

Every generation backend (Meshy, OpenSCAD, Tripo3D, etc.) implements
:class:`GenerationProvider` so that the rest of the system can generate
3D-printable models from text descriptions through a uniform interface.

Workflow::

    1. generate(prompt)        -> GenerationJob (async, returns job ID)
    2. get_job_status(job_id)  -> GenerationJob (poll for completion)
    3. download_result(job_id) -> GenerationResult (local file path)
"""

from __future__ import annotations

import enum
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class GenerationStatus(enum.Enum):
    """Lifecycle states for a generation job."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class GenerationError(Exception):
    """Base exception for model generation errors."""

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


class GenerationAuthError(GenerationError):
    """Raised when an API key is missing or invalid."""


class GenerationTimeoutError(GenerationError):
    """Raised when a generation job exceeds the maximum wait time."""


class GenerationValidationError(GenerationError):
    """Raised when a generated mesh fails validation checks."""


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class GenerationJob:
    """State of a generation job."""

    id: str
    provider: str
    prompt: str
    status: GenerationStatus
    progress: int = 0
    created_at: float = 0.0
    format: str = "stl"
    style: Optional[str] = None
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value
        return data


@dataclass
class GenerationResult:
    """Outcome of a completed generation job."""

    job_id: str
    provider: str
    local_path: str
    format: str
    file_size_bytes: int
    prompt: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class MeshValidationResult:
    """Outcome of mesh validation checks."""

    valid: bool
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    triangle_count: int = 0
    vertex_count: int = 0
    is_manifold: bool = False
    bounding_box: Optional[Dict[str, float]] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------


class GenerationProvider(ABC):
    """Abstract base for 3D model generation backends.

    Concrete providers must implement :meth:`generate`,
    :meth:`get_job_status`, and :meth:`download_result`.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Machine-readable identifier (e.g. ``"meshy"``)."""

    @property
    @abstractmethod
    def display_name(self) -> str:
        """Human-readable name (e.g. ``"Meshy"``)."""

    @abstractmethod
    def generate(
        self,
        prompt: str,
        *,
        format: str = "stl",
        style: str | None = None,
        **kwargs: Any,
    ) -> GenerationJob:
        """Submit a text-to-3D generation job.

        Args:
            prompt: Text description of the desired 3D model.
            format: Desired output format (``"stl"``, ``"obj"``, ``"glb"``).
            style: Optional style hint (provider-specific).
            **kwargs: Provider-specific options.

        Returns:
            A :class:`GenerationJob` with the job ID and initial status.

        Raises:
            GenerationError: If submission fails.
            GenerationAuthError: If credentials are missing or invalid.
        """

    @abstractmethod
    def get_job_status(self, job_id: str) -> GenerationJob:
        """Poll the status of a generation job.

        Args:
            job_id: Job ID returned by :meth:`generate`.

        Returns:
            Updated :class:`GenerationJob` with current status and progress.

        Raises:
            GenerationError: If the status check fails.
        """

    @abstractmethod
    def download_result(
        self,
        job_id: str,
        output_dir: str = "/tmp/kiln_generated",
    ) -> GenerationResult:
        """Download the generated model to local storage.

        Args:
            job_id: Job ID of a completed generation job.
            output_dir: Directory to save the model file.

        Returns:
            A :class:`GenerationResult` with the local file path.

        Raises:
            GenerationError: If the download fails or the job is not complete.
        """

    def list_styles(self) -> List[str]:
        """Return available style options for this provider.

        Returns an empty list by default.  Override in providers that
        support style selection.
        """
        return []
