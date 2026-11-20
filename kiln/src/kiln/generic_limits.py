"""The general design limits: one figure per quantity, the same for every plastic.

These are the figures the open design guides print for an ordinary rigid
plastic, and they hold on every install, offline included.  A plastic's own
figures, where Kiln has them, come from Kiln's servers through
:mod:`kiln.material_limits`; every check reads its limits there, and a check
that gets no plastic figure reads these.

A wall figure depends on the nozzle: a bigger nozzle makes it thicker, and a
smaller one never takes it below the figure at the reference nozzle.  The
thread pitch follows the layer height.
"""

from __future__ import annotations

#: The nozzle and layer height the fixed figures below are stated for.
REFERENCE_NOZZLE_MM: float = 0.4
REFERENCE_LAYER_MM: float = 0.2

#: Steepest face, from vertical (0 is a vertical wall, 90 a ceiling), that
#: prints with no support.
OVERHANG_UNSUPPORTED_DEG: float = 45.0
#: Longest straight span between two supported edges that closes and holds.
BRIDGE_MAX_MM: float = 10.0
#: Longest span that closes with no visible sag.
BRIDGE_CLEAN_MM: float = 5.0
#: Smallest round hole, axis vertical, that prints open.
HOLE_FORMS_MM: float = 1.5
#: Smallest round hole that prints open and round enough to correct.
HOLE_USABLE_MM: float = 2.0
#: Thinnest free-standing pin that prints true and survives handling.
PIN_MIN_MM: float = 3.0
#: Smallest thread printed as it comes off the plate.
THREAD_MIN_DIAMETER_MM: float = 6.0
#: Layers one thread pitch spans, at the least.
THREAD_PITCH_LAYERS: int = 8

#: Thinnest wall that closes: two full lines, one per face.
WALL_MIN_MM: float = 0.8
#: Thinnest wall recommended for strength and handling: three lines.
WALL_RECOMMENDED_MM: float = 1.2
WALL_MIN_LINES: int = 2
WALL_RECOMMENDED_LINES: int = 3


def stated_nozzle_mm(nozzle_mm: float | None) -> float:
    """*nozzle_mm*, or the reference nozzle when none is stated."""
    try:
        value = float(nozzle_mm) if nozzle_mm is not None else REFERENCE_NOZZLE_MM
    except (TypeError, ValueError):
        return REFERENCE_NOZZLE_MM
    return value if value > 0 else REFERENCE_NOZZLE_MM


def wall_min_mm(nozzle_mm: float | None = None) -> float:
    """The thinnest wall that closes at *nozzle_mm*: two lines, never below
    :data:`WALL_MIN_MM`."""
    return round(max(WALL_MIN_MM, WALL_MIN_LINES * stated_nozzle_mm(nozzle_mm)), 3)


def wall_recommended_mm(nozzle_mm: float | None = None) -> float:
    """The general recommended wall at *nozzle_mm*: three lines, never below
    :data:`WALL_RECOMMENDED_MM`."""
    return round(max(WALL_RECOMMENDED_MM, WALL_RECOMMENDED_LINES * stated_nozzle_mm(nozzle_mm)), 3)


def thread_min_pitch_mm(layer_height_mm: float | None = None) -> float:
    """The finest thread pitch that prints with a recognisable profile at
    *layer_height_mm* (the reference layer when none is stated)."""
    try:
        layer = float(layer_height_mm) if layer_height_mm is not None else REFERENCE_LAYER_MM
    except (TypeError, ValueError):
        layer = REFERENCE_LAYER_MM
    if layer <= 0:
        layer = REFERENCE_LAYER_MM
    return round(THREAD_PITCH_LAYERS * layer, 3)


__all__ = [
    "BRIDGE_CLEAN_MM",
    "BRIDGE_MAX_MM",
    "HOLE_FORMS_MM",
    "HOLE_USABLE_MM",
    "OVERHANG_UNSUPPORTED_DEG",
    "PIN_MIN_MM",
    "REFERENCE_LAYER_MM",
    "REFERENCE_NOZZLE_MM",
    "THREAD_MIN_DIAMETER_MM",
    "THREAD_PITCH_LAYERS",
    "WALL_MIN_LINES",
    "WALL_MIN_MM",
    "WALL_RECOMMENDED_LINES",
    "WALL_RECOMMENDED_MM",
    "stated_nozzle_mm",
    "thread_min_pitch_mm",
    "wall_min_mm",
    "wall_recommended_mm",
]
