"""Pure-math 3-D vector helpers (no numpy).

Consolidated from duplicated implementations across colored_renderer,
emboss_generator, surface_intelligence, support_assessment, and printability.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

Vec3 = tuple[float, float, float]

# Accept tuples *or* lists — callers use both.
_In = Sequence[float]


def sub(a: _In, b: _In) -> Vec3:
    """Vector subtraction: a - b."""
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def add(a: _In, b: _In) -> Vec3:
    """Vector addition: a + b."""
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def scale(v: _In, s: float) -> Vec3:
    """Scalar multiply."""
    return (v[0] * s, v[1] * s, v[2] * s)


def dot(a: _In, b: _In) -> float:
    """Dot product."""
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def cross(a: _In, b: _In) -> Vec3:
    """Cross product."""
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def length(v: _In) -> float:
    """Euclidean magnitude."""
    return math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2])


def normalize(v: _In) -> Vec3:
    """Unit vector (returns zero vector for near-zero input)."""
    ln = length(v)
    if ln < 1e-12:
        return (0.0, 0.0, 0.0)
    return (v[0] / ln, v[1] / ln, v[2] / ln)


def face_normal(v0: _In, v1: _In, v2: _In) -> Vec3:
    """Unit normal of a triangle from its three vertices."""
    edge1 = sub(v1, v0)
    edge2 = sub(v2, v0)
    return normalize(cross(edge1, edge2))
