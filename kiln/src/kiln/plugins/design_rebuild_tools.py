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

from kiln.tool_annotations import destructive

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

        @mcp.tool(annotations=destructive("Rebuild a saved design"))
        def rebuild_design(
            recipe_path: str, goal_id: str = "", soft_nozzle_confirmed: bool = False,
            filament_map: list[int] | None = None, mixed_nozzle_experiment: bool = False,
        ) -> dict:
            """Re-execute the full build pipeline from a saved design recipe.

            Two modes, chosen by what the recipe carries — the result names
            which one ran:

            - **Parametric** (the recipe has OpenSCAD source): the geometry
              is RE-DERIVED.  The recipe's numeric parameters are applied to
              the source and recompiled, so wall thicknesses and fastener
              holes come out exactly as designed.  To resize a parametric
              design, change the parameter — with ``update_scad_parameter``
              on the source, or on the recipe's parameters — and rebuild.
              Never scale the mesh instead: it scales every feature with
              the body, so a 3mm wall becomes 3.6mm and a 3.4mm M3
              clearance hole becomes 4.08mm — no longer the fit it was
              dimensioned for.
            - **Mesh** (no source): the recorded part meshes are re-sliced
              exactly as they are.  A geometry change needs a mesh edit first
              (``rescale_model``, ``thicken_mesh_walls``, ...), then a rebuild.

            Every parameter is itemized as applied, absent from the source,
            or skipped with a reason — an edit this tool cannot honor is
            reported, never silently dropped.

            The artifact is a print-ready 3MF where a Bambu printer is
            registered and the merged G-code otherwise (send that with
            ``upload_file``); ``wrapped`` says which one you got.

            The slicer's own time estimate for the rebuilt slice is kept on
            the version, and ``print_time`` says it ("About 3h 1m, the
            slicer's estimate.") when the slice states one.

            When the render pipeline is available this also returns an
            inline preview of the rebuilt design; display that image to the
            user rather than summarizing it. The rebuild itself never
            depends on it.

            Example: rebuild_design("prints/bracket/")

            :param recipe_path: The design directory, or the recipe file.
            :param goal_id: Optional saved-goal id. When supplied it is
                recorded on the recipe before the rebuild, so the output
                carries the goal in its provenance; when omitted the
                recipe's existing goal is kept, so iterating a
                goal-attached design keeps it automatically.
            :param mixed_nozzle_experiment: The person has said yes, for this print, to a slice
                refused with ``MULTI_NOZZLE_MIXED_SIZES`` (nozzles of different sizes at once, an
                experiment). Only on their yes, each time; ``slice_model`` says what follows.
            :param soft_nozzle_confirmed: The owner has said to print anyway
                a slice refused with ``ABRASIVE_ON_SOFT_NOZZLE`` (an abrasive
                filament through a nozzle on record that is not
                wear-resistant).  Only on their word, never on your own
                judgement.
            :param filament_map: Only for a printer with more than one
                nozzle: the nozzle (1 for the first) each filament prints
                from, one number per filament in filament order.  Leave
                unset to let the slicer choose.
            :returns: Dict with the print artifact, the mode used, and
                per-part or per-parameter results.
            """
            import kiln.server as _srv
            if err := _srv._check_auth("generate"):
                return err

            from kiln.design_rebuild import (
                apply_brief_to_recipe,
                rebuild_design_from_recipe,
            )

            apply_brief_to_recipe(recipe_path, goal_id)
            from kiln.slicer import owner_asks

            asks = owner_asks(soft_nozzle_confirmed, filament_map, mixed_nozzle_experiment)
            result = rebuild_design_from_recipe(recipe_path, asks=asks)
            # A parameter change can lift a feature off its host; the
            # re-derived mesh gets the compile door's own check.
            from kiln.fusion_check import attach_fusion_report

            attach_fusion_report(
                result, result.get("compiled_stl") or result.get("stl_path"),
                refuse_floating=True,
            )
            from kiln.front_door import join_goal

            join_goal(goal_id, result, result.get("compiled_stl") or result.get("stl_path"))
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
