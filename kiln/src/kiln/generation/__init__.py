"""Text-to-model generation providers and mesh validation.

Provides :class:`GenerationProvider` implementations for generating
3D-printable models from text descriptions, plus a mesh validation
pipeline for checking print readiness.

Providers
---------
:class:`MeshyProvider`
    Cloud-based text-to-3D via the Meshy API.
:class:`OpenSCADProvider`
    Local parametric generation via OpenSCAD CLI.
"""

from kiln.generation.base import (
    GenerationAuthError,
    GenerationError,
    GenerationJob,
    GenerationProvider,
    GenerationResult,
    GenerationStatus,
    GenerationTimeoutError,
    GenerationValidationError,
    MeshValidationResult,
)
from kiln.generation.meshy import MeshyProvider
from kiln.generation.openscad import OpenSCADProvider
from kiln.generation.validation import convert_to_stl, validate_mesh

__all__ = [
    "GenerationAuthError",
    "GenerationError",
    "GenerationJob",
    "GenerationProvider",
    "GenerationResult",
    "GenerationStatus",
    "GenerationTimeoutError",
    "GenerationValidationError",
    "MeshValidationResult",
    "convert_to_stl",
    "MeshyProvider",
    "OpenSCADProvider",
    "validate_mesh",
]
