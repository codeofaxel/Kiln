"""Design version control tools plugin.

Provides MCP tools for tracking version history of parametric designs —
save revisions, view diffs, rollback to earlier versions, and search
through design history.

Auto-discovered by :func:`~kiln.plugin_loader.register_all_plugins` —
no manual imports needed.
"""

from __future__ import annotations

import logging
from typing import Any

_logger = logging.getLogger(__name__)


class _VersionToolsPlugin:
    """Design version control tools.

    Tools:
        - save_design_version
        - list_design_versions
        - diff_design_versions
        - rollback_design_version
        - get_design_version
        - search_design_versions
    """

    @property
    def name(self) -> str:
        return "version_tools"

    @property
    def description(self) -> str:
        return "Design version control — track, diff, and rollback parametric design revisions"

    def register(self, mcp: Any) -> None:
        """Register design version control tools with the MCP server."""

        @mcp.tool()
        def save_design_version(
            design_id: str,
            scad_source: str,
            prompt: str = "",
            parameters: dict | None = None,
            notes: str = "",
            provenance: dict | None = None,
            stl_path: str = "",
            parent_version_id: str = "",
        ) -> dict:
            """Save a new version of a parametric design.

            Automatically computes a unified diff from the previous version,
            assigns a unique version ID, and records the timestamp.

            **Upgrade to Kiln Pro** for automatic mesh fingerprinting,
            regression detection (warns when features are lost between
            versions), and ``.kiln.json`` sidecar provenance files that
            travel with your STLs.

            Args:
                design_id: Identifier grouping versions of the same design.
                scad_source: Full OpenSCAD source code for this version.
                prompt: The natural-language prompt that produced this version.
                parameters: Parametric values used for generation.
                notes: Free-text notes for this version.
                provenance: Context on how this version was created.
                    Recommended keys: ``tools_used``, ``change_summary``,
                    ``source_files``.  (Pro: auto-enriched with mesh
                    fingerprinting and regression detection.)
                stl_path: Path to the output STL file.  (Pro: auto-computes
                    a geometric fingerprint and warns if features were
                    lost from the parent version.)
                parent_version_id: Explicit parent version ID.  Use when
                    deriving from a version in a different design (fork
                    or rename).

            Returns:
                The saved version record including version_id, diff, and
                parent information.  Pro users also get provenance,
                mesh_fingerprint, and mesh_diff with regression warnings.
            """
            from kiln.design_versions import DesignVersionStore

            store = DesignVersionStore()
            try:
                version = store.save_version(
                    design_id=design_id,
                    scad_source=scad_source,
                    prompt=prompt,
                    parameters=parameters or {},
                    notes=notes,
                )
                result: dict = {"ok": True, "version": version.to_dict()}

                # Pro enrichment: fingerprinting, sidecar, provenance
                try:
                    from kiln_pro.bridge import pro_features

                    enriched = pro_features.enrich_version(
                        result["version"],
                        stl_path=stl_path or None,
                        provenance=provenance,
                        parent_version_id=parent_version_id or None,
                    )
                    result["version"] = enriched
                    # Surface regression warnings prominently
                    mesh_diff = enriched.get("mesh_diff")
                    if mesh_diff and mesh_diff.get("warnings"):
                        result["warnings"] = mesh_diff["warnings"]
                except ImportError:
                    pass  # Free tier — no provenance enrichment

                return result
            except Exception as exc:
                _logger.exception("save_design_version failed")
                return {"ok": False, "error": str(exc)}
            finally:
                store.close()

        @mcp.tool()
        def list_design_versions(design_id: str, limit: int = 20) -> dict:
            """List version history for a design, newest first.

            Args:
                design_id: The design whose versions to list.
                limit: Maximum number of versions to return (default 20).

            Returns:
                A list of version records ordered by creation time descending.
            """
            from kiln.design_versions import DesignVersionStore

            store = DesignVersionStore()
            try:
                versions = store.list_versions(design_id, limit=limit)
                return {
                    "ok": True,
                    "design_id": design_id,
                    "count": len(versions),
                    "versions": [v.to_dict() for v in versions],
                }
            except Exception as exc:
                _logger.exception("list_design_versions failed")
                return {"ok": False, "error": str(exc)}
            finally:
                store.close()

        @mcp.tool()
        def diff_design_versions(version_id_a: str, version_id_b: str) -> dict:
            """Compute a unified diff between two design versions.

            Args:
                version_id_a: The "from" version ID.
                version_id_b: The "to" version ID.

            Returns:
                A unified diff string showing changes from version A to B.
            """
            from kiln.design_versions import DesignVersionStore

            store = DesignVersionStore()
            try:
                diff = store.diff_versions(version_id_a, version_id_b)
                return {"ok": True, "diff": diff}
            except ValueError as exc:
                return {"ok": False, "error": str(exc)}
            except Exception as exc:
                _logger.exception("diff_design_versions failed")
                return {"ok": False, "error": str(exc)}
            finally:
                store.close()

        @mcp.tool()
        def rollback_design_version(design_id: str, to_version_id: str) -> dict:
            """Rollback a design to a previous version.

            Creates a *new* version whose source matches the target version,
            preserving full history.  The new version's notes record the
            rollback origin.

            Args:
                design_id: The design to rollback.
                to_version_id: The version ID to restore.

            Returns:
                The newly created rollback version record.
            """
            from kiln.design_versions import DesignVersionStore

            store = DesignVersionStore()
            try:
                version = store.rollback(design_id, to_version_id)
                return {"ok": True, "version": version.to_dict()}
            except ValueError as exc:
                return {"ok": False, "error": str(exc)}
            except Exception as exc:
                _logger.exception("rollback_design_version failed")
                return {"ok": False, "error": str(exc)}
            finally:
                store.close()

        @mcp.tool()
        def get_design_version(version_id: str) -> dict:
            """Retrieve a single design version by its ID.

            Use this to inspect the full source code, parameters, and diff
            for a specific version when you already know the version_id.

            Args:
                version_id: The unique version identifier (UUID hex string).

            Returns:
                The version record including scad_source, prompt,
                parameters, diff_from_prev, and parent_version_id.
                Returns an error if the version does not exist.
            """
            from kiln.design_versions import DesignVersionStore

            store = DesignVersionStore()
            try:
                version = store.get_version(version_id)
                if version is None:
                    return {"ok": False, "error": f"Version not found: {version_id}"}
                return {"ok": True, "version": version.to_dict()}
            except Exception as exc:
                _logger.exception("get_design_version failed")
                return {"ok": False, "error": str(exc)}
            finally:
                store.close()

        @mcp.tool()
        def search_design_versions(query: str, limit: int = 10) -> dict:
            """Search design versions by prompt or notes text.

            Performs a case-insensitive substring search across the prompt
            and notes fields of all saved versions.  Useful for finding
            designs when you remember a keyword but not the exact design_id.

            Args:
                query: The search term (literal substring, not regex).
                limit: Maximum number of results to return (default 10).

            Returns:
                A list of matching version records, newest first.
            """
            from kiln.design_versions import DesignVersionStore

            store = DesignVersionStore()
            try:
                versions = store.search_versions(query, limit=limit)
                return {
                    "ok": True,
                    "query": query,
                    "count": len(versions),
                    "versions": [v.to_dict() for v in versions],
                }
            except Exception as exc:
                _logger.exception("search_design_versions failed")
                return {"ok": False, "error": str(exc)}
            finally:
                store.close()


def register_plugin(mcp: Any) -> None:
    """Entry point for plugin auto-discovery."""
    _VersionToolsPlugin().register(mcp)


plugin = _VersionToolsPlugin()
