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
:class:`Tripo3DProvider`
    Cloud-based text-to-3D via the Tripo3D API.
:class:`StabilityProvider`
    Cloud-based text-to-3D via the Stability AI API.

Registry
--------
:class:`GenerationRegistry`
    Universal provider registry with auto-discovery.
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
from kiln.generation.registry import GenerationRegistry
from kiln.generation.stability import StabilityProvider
from kiln.generation.tripo3d import Tripo3DProvider
from kiln.generation.validation import convert_to_stl, validate_mesh

__all__ = [
    "GenerationAuthError",
    "GenerationError",
    "GenerationJob",
    "GenerationProvider",
    "GenerationRegistry",
    "GenerationResult",
    "GenerationStatus",
    "GenerationTimeoutError",
    "GenerationValidationError",
    "MeshValidationResult",
    "MeshyProvider",
    "OpenSCADProvider",
    "StabilityProvider",
    "Tripo3DProvider",
    "convert_to_stl",
    "validate_mesh",
]
