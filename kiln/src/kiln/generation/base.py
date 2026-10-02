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
import functools
import os
import tempfile
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from typing import Any, ClassVar

from kiln.daily_stats import counts_outside_service, record_generation_provider

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
    style: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
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

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class MeshValidationResult:
    """Outcome of mesh validation checks."""

    valid: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    triangle_count: int = 0
    vertex_count: int = 0
    is_manifold: bool = False
    bounding_box: dict[str, float] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class MeshAnalysis:
    """Detailed geometric and printability analysis of a 3D mesh."""

    triangle_count: int = 0
    vertex_count: int = 0
    is_manifold: bool = False
    bounding_box: dict[str, float] | None = None
    dimensions_mm: dict[str, float] | None = None
    volume_mm3: float = 0.0
    surface_area_mm2: float = 0.0
    center_of_mass: dict[str, float] | None = None
    connected_components: int = 0
    degenerate_triangles: int = 0
    overhang_triangle_count: int = 0
    overhang_percentage: float = 0.0
    max_overhang_angle_deg: float = 0.0
    #: A quick 0-100 mesh check: watertight, loose parts, overhang
    #: angle and share, degenerate faces, size.  It is not the
    #: printability score — :func:`kiln.printability.analyze_printability`
    #: is the one number called that, and it judges an overhang by whether
    #: a slicer can print it, which this check cannot (a 9 mm port ceiling
    #: costs 20 points here and nothing there).  Under one name the two
    #: read as two verdicts on the same part.
    mesh_check_score: int = 0
    printability_issues: list[str] = field(default_factory=list)
    #: Why this file cannot print as a part at all -- unreadable, empty,
    #: every triangle degenerate, or flat (every point in one plane) -- or
    #: ``None`` when it can.  Every print verdict asks this before judging anything else
    #: (:func:`kiln.generation.validation.unprintable_geometry_reason`).
    unprintable_geometry: str | None = None

    def has_geometry(self) -> bool:
        """True when the analysis is about an actual mesh.

        ``analyze_mesh`` never raises — a file it cannot read comes back as
        this dataclass zeroed out, with the reason in
        :attr:`printability_issues`.  Callers that treat such a result as a
        real analysis end up reporting a valid file as an empty, broken
        mesh (0 triangles, "not manifold"), which is a misdiagnosis.  Check
        this first: False means "the INPUT could not be read," not "the
        mesh is bad."
        """
        return self.triangle_count > 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------


def _count_generation(provider: Any, job: Any) -> None:
    """Count one generation the provider ACCEPTED, under its own name.

    A job that came back already failed (a compile error, a rejected
    prompt) is not counted: the question this answers is which providers
    people's models actually come from.
    """
    if getattr(job, "status", None) is GenerationStatus.FAILED:
        return
    record_generation_provider(provider.name)


def _notes_arrival(download: Any) -> Any:
    """Wrap a provider's ``download_result`` so the file it returns gets
    its arrival note (:mod:`kiln.arrival`)."""

    @functools.wraps(download)
    def _download(self: Any, *args: Any, **kwargs: Any) -> Any:
        result = download(self, *args, **kwargs)
        from kiln.arrival import note_generation

        note_generation(self, result)
        return result

    return _download


class GenerationProvider(ABC):
    """Abstract base for 3D model generation backends.

    Concrete providers must implement :meth:`generate`,
    :meth:`get_job_status`, and :meth:`download_result`.

    Every subclass's :meth:`generate` is counted in the daily usage stats
    under the provider's own :attr:`name`, and every file its
    :meth:`download_result` returns gets a note saying where it came from
    (see ``__init_subclass__``), so a provider added later is counted and
    noted with no further wiring.
    """

    #: The provider draws in millimetres, so the size a file arrives at is
    #: the size it was designed at.  Kiln sends Tripo, Meshy and Stability a
    #: prompt and never a size, so each picks its own scale: Tripo's "40 mm
    #: calibration cube" came back 1.0 units across (a live job, 2026-09-30).
    sets_real_size: ClassVar[bool] = False

    #: The model is drawn by a service outside this machine, so where it came
    #: from earns a note.  False for a provider that compiles code here: the
    #: code is its own record.
    drawn_elsewhere: ClassVar[bool] = True

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        # Wrapped here rather than at each caller: the MCP tools, the CLI
        # and the pipelines all reach a provider their own way, and the
        # one thing they share is these methods.
        generate = cls.__dict__.get("generate")
        if callable(generate) and not getattr(generate, "__isabstractmethod__", False):
            cls.generate = counts_outside_service("generation", _count_generation)(generate)
        download = cls.__dict__.get("download_result")
        if callable(download) and not getattr(download, "__isabstractmethod__", False):
            cls.download_result = _notes_arrival(download)

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
        output_dir: str = os.path.join(tempfile.gettempdir(), "kiln_generated"),
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

    def list_styles(self) -> list[str]:
        """Return available style options for this provider.

        Returns an empty list by default.  Override in providers that
        support style selection.
        """
        return []
