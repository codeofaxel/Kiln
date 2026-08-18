"""Design rebuild tool plugin — re-execute a saved design's build.

Exposes :func:`kiln.design_rebuild.rebuild_design_from_recipe` as an MCP
tool.  The engine and its reasoning live in that module; this file is the
tool contract — the docstring an agent reads, and the preview wiring.

Auto-discovered by :func:`~kiln.plugin_loader.register_all_plugins` —
no manual imports needed.
"""

from __future__ import annotations

import logging
from typing import Any

_logger = logging.getLogger(__name__)



class _DesignRebuildPlugin:
    """Rebuild a saved design from its recipe."""

    @property
    def name(self) -> str:
        return "design_rebuild"

    @property
    def description(self) -> str:
        return "Rebuild a saved design from its recipe (parametric re-derive or mesh re-slice)"

    def register(self, mcp: Any) -> None:
        """Register the rebuild tool with the MCP server."""

        @mcp.tool()
        def rebuild_design(recipe_path: str, brief_id: str = "") -> dict:
            """Re-execute the full build pipeline from a saved design recipe.

            Two modes, chosen by what the recipe carries — the result names
            which one ran:

            - **Parametric** (the recipe has OpenSCAD source): the geometry
              is RE-DERIVED.  The recipe's numeric parameters are applied to
              the source and recompiled, so wall thicknesses and fastener
              holes come out exactly as designed.  To resize a parametric
              design, change the parameter — with ``update_scad_parameter``
              on the source, or on the recipe's parameters — and rebuild.
              Never scale the mesh instead: 20% larger turns a 3mm wall into
              3.6mm and leaves an M3 clearance hole that fits nothing.
            - **Mesh** (no source): the recorded part meshes are re-sliced
              exactly as they are.  A geometry change needs a mesh edit first
              (``rescale_model``, ``thicken_mesh_walls``, ...), then a rebuild.

            Every parameter is itemized as applied, absent from the source,
            or skipped with a reason — an edit this tool cannot honor is
            reported, never silently dropped.

            The artifact is a print-ready 3MF where a Bambu printer is
            registered and the merged G-code otherwise (send that with
            ``upload_file``); ``wrapped`` says which one you got.

            AGENT DISPLAY CONTRACT: on success this returns a rendered
            preview of the rebuilt design. Display the image inline to the
            user — do not summarize it in text or drop it silently.

            Example: rebuild_design("prints/bracket/")

            :param recipe_path: The design directory, or the recipe file.
            :param brief_id: Optional saved-goal id. When supplied it is
                recorded on the recipe before the rebuild, so the output
                carries the goal in its provenance; when omitted the
                recipe's existing goal is kept, so iterating a
                goal-attached design keeps it automatically.
            :returns: Dict with the print artifact, the mode used, and
                per-part or per-parameter results.
            """
            from kiln.design_rebuild import (
                apply_brief_to_recipe,
                rebuild_design_from_recipe,
            )

            apply_brief_to_recipe(recipe_path, brief_id)
            result = rebuild_design_from_recipe(recipe_path)
            try:
                from kiln_pro.plugins.git_render_tools import (
                    _DEFAULT_STL_KEYS,
                    attach_inspect_bundle,
                )

                # The parametric mode's re-derived mesh rides on
                # ``compiled_stl``, which the default keys do not know —
                # without it a rebuild on a machine with no Bambu adapter
                # previews NOTHING at the moment the user changed geometry.
                # Prepended to the real default list rather than a copy of
                # it, so the two cannot drift.
                return attach_inspect_bundle(
                    result, level="quick",
                    stl_keys=("compiled_stl", *_DEFAULT_STL_KEYS),
                )
            except ImportError:
                return result

        _logger.debug("design_rebuild: registered rebuild_design")


def get_plugin() -> _DesignRebuildPlugin:
    """Plugin entry point for auto-discovery."""
    return _DesignRebuildPlugin()


plugin = _DesignRebuildPlugin()
