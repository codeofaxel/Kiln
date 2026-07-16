#!/usr/bin/env python3
"""Regenerate kiln/src/kiln/data/design_knowledge/load_tables.json.

The safe-load table is DERIVED data — every number re-derives from the
cantilever bending formula, so the file can never again drift from the
physics (the 2026-07-16 audit found hand-authored values ~4x above the
raw breaking load of any compact section):

    safe_load_N = tensile_capacity x Z / L
    tensile_capacity = sigma_t x FDM_PRINT_FACTOR / SAFETY_FACTOR   [N/mm^2]
    Z = A^1.5 / 6      (section modulus of a SQUARE section of area A)

Assumptions, stated where users can see them (the tool's reasoning
echoes them too):

- SQUARE cross-section. An area-only API cannot know the real shape; a
  square is the stated basis, and wide-flat sections are weaker — the
  shape-aware path is kiln-pro's design_for_load.
- FDM_PRINT_FACTOR 0.9: printed parts under-perform datasheet bars.
- SAFETY_FACTOR 3.0: the general-purpose margin the tool discloses.
- Orientation: along_layers (stress within the layer planes) serves the
  full table value; across_layers (load pulls the layer interfaces
  apart) is derated by 0.4 — the WORST per-material interlayer ratio in
  the researched set (ABS/nylon), so the single free-tier pair is
  conservative for every material. Per-material anisotropy depth lives
  in Kiln Pro.

sigma_t values mirror the kiln-pro engineering catalogue (2026-07-16);
kiln-pro's physics-truth gate cross-checks free output against that
catalogue in CI, so drift between the two goes red there.

Usage:
    python3 kiln/scripts/generate_load_tables.py            # rewrite the JSON
    python3 kiln/scripts/generate_load_tables.py --check    # exit 2 if the file differs
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

OUT_PATH = (
    Path(__file__).resolve().parent.parent
    / "src" / "kiln" / "data" / "design_knowledge" / "load_tables.json"
)

# Catalogue tensile strength, MPa (= N/mm^2).
SIGMA_T_MPA = {
    "pla": 50.0,
    "petg": 50.0,
    "abs": 33.0,
    "nylon": 55.0,
    "polycarbonate": 60.0,
}

SAFETY_FACTOR = 3.0
FDM_PRINT_FACTOR = 0.9          # printed vs datasheet-bar strength
ACROSS_LAYER_DERATE = 0.4       # worst researched interlayer (Z/XY) ratio
CANTILEVER_LENGTHS_MM = [25, 50, 100, 150, 200]
CROSS_SECTIONS_MM2 = [12, 24, 36, 48, 60]


def _floor2(x: float) -> float:
    """Round DOWN to 2 decimals — rounding must never add capacity."""
    return math.floor(x * 100.0) / 100.0


def tensile_capacity(material: str) -> float:
    return round(SIGMA_T_MPA[material] * FDM_PRINT_FACTOR / SAFETY_FACTOR, 1)


def safe_load_n(material: str, area_mm2: float, length_mm: float) -> float:
    z_mm3 = area_mm2 ** 1.5 / 6.0
    return _floor2(tensile_capacity(material) * z_mm3 / length_mm)


def build() -> dict:
    doc: dict = {
        "_meta": {
            "version": "2.0.0",
            "domain": "fdm",
            "description": (
                "Per-material safe-load lookup for cantilevered FDM parts, "
                "DERIVED (not hand-authored): safe_load_N = "
                "tensile_capacity_n_per_mm2 x (A^1.5/6) / L, where "
                "tensile_capacity = catalogue tensile x 0.9 FDM print factor "
                "/ 3.0 safety factor. SQUARE cross-section basis — wide or "
                "flat sections are weaker. Regenerate with "
                "kiln/scripts/generate_load_tables.py; never hand-edit values."
            ),
            "_orientation_convention": (
                "layer_orientation_derating keys: 'across_layers' = the load "
                "pulls the layer interfaces apart (build/Z direction — the "
                "WEAK case for FDM; factor 0.4, the worst per-material "
                "interlayer ratio so one conservative pair covers every "
                "material). 'along_layers' = the load acts within the layer "
                "planes (the STRONG case; factor 1.0). Per-material "
                "anisotropy depth lives in Kiln Pro."
            ),
            "_split_note": (
                "Safety-floor profile for AI-controlled FDM printing.  "
                "Engineering depth (per-material SME caveats) is available "
                "in Kiln Pro; see https://kiln3d.com/pricing."
            ),
        },
    }
    for material in SIGMA_T_MPA:
        doc[material] = {
            "tensile_capacity_n_per_mm2": tensile_capacity(material),
            "cross_section_vs_load": [
                {
                    "cantilever_length_mm": length,
                    "entries": [
                        {
                            "cross_section_mm2": area,
                            "max_load_n": safe_load_n(material, area, length),
                        }
                        for area in CROSS_SECTIONS_MM2
                    ],
                }
                for length in CANTILEVER_LENGTHS_MM
            ],
            "layer_orientation_derating": {
                "across_layers": ACROSS_LAYER_DERATE,
                "along_layers": 1.0,
            },
        }
    return doc


def main(argv: list[str]) -> int:
    rendered = json.dumps(build(), indent=2) + "\n"
    if "--check" in argv:
        current = OUT_PATH.read_text(encoding="utf-8")
        if current != rendered:
            print("load_tables.json differs from its generator — regenerate "
                  "with kiln/scripts/generate_load_tables.py")
            return 2
        print("load_tables.json matches its generator.")
        return 0
    OUT_PATH.write_text(rendered, encoding="utf-8")
    print(f"wrote {OUT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
