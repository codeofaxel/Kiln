"""OpenSCAD parametric library for Kiln 3D.

This module loads all .scad files in this directory and concatenates them into
a single string. The resulting source is prepended to Gemini-generated OpenSCAD
code before compilation, making every library module available in scope without
`use` or `include` statements.
"""

from __future__ import annotations

from pathlib import Path


def get_library_source() -> str:
    """Return concatenated OpenSCAD library code to prepend to generated code."""
    scad_dir = Path(__file__).parent
    parts: list[str] = []

    # Sort for deterministic ordering across platforms.
    for scad_file in sorted(scad_dir.glob("*.scad")):
        parts.append(f"// === BEGIN {scad_file.name} ===")
        parts.append(scad_file.read_text(encoding="utf-8"))
        parts.append(f"// === END {scad_file.name} ===\n")

    return "\n".join(parts)
