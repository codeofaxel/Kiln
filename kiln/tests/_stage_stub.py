"""A stand-in stage document for the painter's tests.

The painter (:mod:`kiln.stage_paint`) reads the stage's look -- lamps, sky,
dark-part rule, material, camera, plate -- out of the cached stage document
Kiln's servers serve, and keeps no number of its own.  Tests of its
CONTRACT (declines, letterbox, determinism, the plate's geometry) need some
document to read, and this one is made up for them: every value here is a
stand-in with the right SHAPE, not the stage's.  The stage's own look, and
how faithfully the painter paints it, is pinned beside the stage, where the
look lives.
"""

from __future__ import annotations

import json
from pathlib import Path

#: The dark-part rule's shape, with stand-in numbers.
LIFT = {
    "stage": "#202428",
    "contrastFloor": 3,
    "studioLift": 10,
    "studioCap": 0.5,
    "glossCap": 0.75,
    "curve": 4,
}

#: A rig with the stage's SHAPE: ambient, hemisphere, four directionals, a
#: sky gradient and a bounce card, a studio measure and a gloss.
RIG = {
    "ambient": 0.3,
    "hemisphere": {"sky": "#ffffff", "ground": "#606060", "intensity": 0.6},
    "key": {"color": "#ffffff", "intensity": 0.8, "at": [8, 16, 8]},
    "rim": {"color": "#ffffff", "intensity": 0.3, "at": [-12, 8, -8]},
    "graze": {"color": "#ffffff", "intensity": 0.7, "at": [16, 6, 2]},
    "counter": {"color": "#ffffff", "intensity": 0.4, "at": [-16, 6, 2]},
    "sky": [[0.0, "rgb(120, 120, 120)"], [0.45, "rgb(60, 60, 60)"], [0.78, "rgb(170, 170, 170)"], [1.0, "rgb(240, 240, 240)"]],
    "bounce": {"u": 0.3, "v": 0.3, "radius": 0.05, "core": 0.3, "color": "rgb(140, 140, 140)"},
    "studio": 0.5,
    "gloss": 0.3,
}

#: The rest of the look, stand-in values.  The plate keeps 25 cells across
#: (250 / 10): the centre-cross rounding the plate test pins needs an odd
#: half, as the stage's own plate has.
RECIPE = {
    "model": {"color": "#c8c8c8", "roughness": 0.5, "metalness": 0.0},
    "creaseDeg": 30,
    "studioCanvas": {"width": 256, "height": 128},
    "camera": {"fov": 40, "openingAz": 45, "openingEl": 0.5, "elClamp": [-1.2, 1.45], "stillDistFactor": 3.0},
    "bloom": {"strength": 0.5, "radius": 0.8, "threshold": 0.9},
    "plate": {
        "mm": 250, "cellMm": 10, "pxPerMm": 4,
        "wash": "rgba(40, 40, 40, 0.5)", "grid": "rgba(96, 96, 96, 0.3)", "centre": "rgba(255, 120, 60, 0.2)",
        "border": "rgba(110, 110, 110, 0.35)", "stamp": "rgba(255, 120, 60, 0.2)", "etch": "rgba(160, 170, 180, 0.3)",
    },
    "blob": [[0, 0.5], [0.5, 0.25], [1, 0]],
    "railPx": 48,
}


def document(rig: dict | None = None, lift: dict | None = None, recipe: dict | None = None) -> str:
    """A document text the painter can read its look from: the three
    object literals the real one carries, in a page-shaped wrapper."""
    def literal(name: str, value: dict) -> str:
        return f"var {name} = {json.dumps(value, indent=2)};\n"

    return (
        "<!DOCTYPE html>\n<html><head><meta charset=\"utf-8\"></head><body><script>\n"
        + literal("STAGE_LIFT", lift or LIFT)
        + literal("STAGE_RIG", rig or RIG)
        + literal("STAGE_RECIPE", recipe or RECIPE)
        + "</script></body></html>\n"
    )


def install(monkeypatch, home: Path, text: str | None = None) -> Path:
    """Cache *text* (the stand-in document by default) as the stage document
    under a private ``KILN_HOME`` at *home*, and forget any look the painter
    read before.  Returns the cached file's path."""
    from kiln import stage_cache, stage_paint

    home = Path(home)
    cache = home / "stage_cache"
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / "mesh_viewer.html"
    path.write_text(text if text is not None else document(), encoding="utf-8")
    monkeypatch.setenv("KILN_HOME", str(home))
    stage_cache._reset_for_tests()
    getattr(stage_paint, "_LOOK_MEMO", {}).clear()  # absent on a painter that kept its own numbers
    return path


def background() -> tuple[int, int, int]:
    """The stand-in stage's backdrop, as bytes."""
    h = LIFT["stage"].lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
