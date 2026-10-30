"""Tool names Kiln took away, and the tool that does the job now.

When a tool is renamed or folded into another, its old name may stay
registered as an alias for a while so existing setups keep working.  Two
tables keep that honest:

* ``DEPRECATED`` -- old names still registered, each with the release that
  removes it.  ``kiln/tests/test_retired_tool_names.py`` fails a tool whose
  description calls itself deprecated with no row here, and a row whose
  release has arrived, so "kept for one release" is a date someone is held
  to rather than a sentence in a docstring.
* ``RETIRED`` -- names no tool carries any more.  A call to one is refused
  at the dispatch door with the call that replaces it, so an old setup gets
  a pointer instead of "unknown tool".
"""

from __future__ import annotations

#: Old name -> the call that does the job now.
RETIRED: dict[str, str] = {
    "print_status_lite": 'printer_status(detail="lite")',
    "repair_mesh_advanced": "repair_mesh(close_holes=True)",
    "render_model_preview": 'visualize_model(angles=["isometric"])',
    "recover_texture_detail": "keep_painted_detail",
    # Renamed 2026-10-30: they hand back the file from before a mid-print
    # edit and the steps to go back to it, and change nothing themselves.
    "revert_mid_print": "get_print_before_edit",
    "revert_mid_print_fleet": "get_print_before_edit_fleet",
}

#: Old name still registered -> (the call that replaces it, the release that
#: removes it, as "X.Y.Z").
DEPRECATED: dict[str, tuple[str, str]] = {}


def retired_message(name: str) -> str | None:
    """The refusal for a retired tool name, or None for any other name."""
    replacement = RETIRED.get(name)
    if replacement is None:
        return None
    return f"The {name} tool was removed from Kiln. Call {replacement} instead."


def says_deprecated(description: str | None) -> bool:
    """True when a tool's description opens by calling the tool deprecated."""
    first_line = (description or "").lstrip().split("\n", 1)[0]
    return first_line.lstrip("*_ ").lower().startswith("deprecated")


def replacement_tool(call: str) -> str:
    """The tool name a replacement call names: ``repair_mesh(...)`` -> ``repair_mesh``."""
    return call.split("(", 1)[0].strip()
