"""The record a format conversion leaves behind — the neutral fact half.

Kiln converts meshes between formats in several places (an AI provider's
GLB becomes an STL, a decorated OBJ comes back as an STL), and until
2026-08-28 none of those conversions left any record: the tool result
named the output file and the input format simply vanished from the
story.  A user whose textured GLB became a grey STL had nothing,
anywhere, that said so.

This module is the writing half of the fix, and it deliberately mirrors
the STEP conversion record's division of labour: the ENGINE records a
neutral fact (what format went in, what came out, which tool, what the
destination format cannot carry forward) and stops there.  Judgment —
whether that loss matters for this part, this user, this tier — belongs
to the layers above (kiln-pro's provenance enrichment persists the fact
into a design's life story; the part passport narrates it).

**The fact rides the tool result, in the open.**  Transparency about
what happened to a user's file is not a paid feature — every caller
sees the record on the result that converted their file.  What is paid
(and lives in kiln-pro, per the standing split) is the MEMORY: carrying
the record into a design's provenance, its ancestry, and its passport.

**``lost`` is capability, not measurement.**  The list names what the
destination format cannot carry forward — categories the SOURCE format
can hold.  Whether this particular file used them is not measured here
(v1), and the field name ``lost_capabilities`` says so: a GLB with no
textures still reads "textures" in the list, because STL could not have
carried them either way.  A measured per-file census ("4 color zones
flattened") is the planned enrichment, not this record.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

#: Versioned discriminator, following ``kiln.step_facts.v1``'s pattern.
FORMAT_CONVERSION_KIND = "kiln.format_conversion.v1"

#: What each source format can carry that binary STL cannot.  STL is pure
#: triangles — no color, no materials, no textures, no named objects — so
#: every entry here is a one-way door.  Keyed by source format (lower-case,
#: no dot); conversions to targets other than STL can extend this to a
#: (from, to) key when one exists.
_LOST_TO_STL: dict[str, tuple[str, ...]] = {
    "glb": ("textures", "materials", "vertex_colors", "uv_coordinates", "named_objects"),
    "gltf": ("textures", "materials", "vertex_colors", "uv_coordinates", "named_objects"),
    "obj": ("materials", "uv_coordinates", "named_objects"),
    "3mf": ("colors", "materials", "named_objects", "multi_object_structure"),
    "ply": ("vertex_colors",),
    "dae": ("materials", "uv_coordinates", "named_objects"),
    "off": (),
}


def lost_capabilities(from_format: str, to_format: str) -> list[str]:
    """What ``to_format`` cannot carry forward from ``from_format``.

    Categorical (see module doc) — an unknown pair answers an empty list
    rather than guessing, so the record never claims a loss nobody
    established.
    """
    if to_format == "stl":
        return list(_LOST_TO_STL.get(from_format, ()))
    return []


def format_conversion_record(
    *,
    from_path: str,
    to_path: str,
    tool: str,
    reason: str,
    original_retained: bool = True,
) -> dict[str, Any]:
    """Build the neutral record of one format conversion.

    :param from_path: The file that was converted.  Kept in the record
        (as ``original_path``) when *original_retained* — the converter
        leaves sources on disk beside their outputs, and naming the path
        is what stops the original from being silently orphaned.
    :param to_path: The file the conversion produced.
    :param tool: The tool/door that converted, e.g.
        ``"download_generated_model"`` — the story's "who".
    :param reason: One plain sentence a passport can repeat, e.g.
        ``"converted to STL for slicer compatibility"``.
    :param original_retained: False when the source bytes are already
        gone (the record then honestly carries no path).
    """
    from_format = Path(from_path).suffix.lower().lstrip(".")
    to_format = Path(to_path).suffix.lower().lstrip(".")
    record: dict[str, Any] = {
        "kind": FORMAT_CONVERSION_KIND,
        "from_format": from_format,
        "to_format": to_format,
        "tool": tool,
        "reason": reason,
        # Capability, not measurement — see the module doc.
        "lost_capabilities": lost_capabilities(from_format, to_format),
        "converted_at": datetime.now(timezone.utc).isoformat(),
    }
    if original_retained:
        record["original_path"] = str(from_path)
    return record


def convert_to_stl_recorded(
    input_path: str,
    *,
    tool: str,
    reason: str = "converted to STL for slicer compatibility",
) -> tuple[str, dict[str, Any]]:
    """:func:`kiln.generation.validation.convert_to_stl`, with the receipt.

    Same conversion, same output path convention — plus the record every
    conversion now owes.  ``convert_to_stl`` writes the STL BESIDE the
    source (``with_suffix(".stl")``) and deletes nothing, so the original
    is genuinely retained and the record names it.

    Call sites adopt this instead of the bare converter so a conversion
    without a record becomes impossible to write by accident — the pair
    is the only thing this function returns.
    """
    from kiln.generation.validation import convert_to_stl

    stl_path = convert_to_stl(input_path)
    return stl_path, format_conversion_record(
        from_path=input_path,
        to_path=stl_path,
        tool=tool,
        reason=reason,
    )
