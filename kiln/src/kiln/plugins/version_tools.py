"""Design version control tools plugin.

Provides MCP tools for tracking version history of parametric designs —
save revisions, view diffs, rollback to earlier versions, and search
through design history.

Versions are stored as JSON sidecar files (`.kiln_recipe.vN.json`) under
``~/.kiln/designs/<design_id>/``.  The :mod:`kiln.design_recipe` module
is the single source of truth; the legacy SQLite store
(:mod:`kiln.design_versions`) is no longer used here.

Auto-discovered by :func:`~kiln.plugin_loader.register_all_plugins` —
no manual imports needed.
"""

from __future__ import annotations

import logging
from typing import Any

_logger = logging.getLogger(__name__)

_DESIGNS_ROOT = "~/.kiln/designs"


def _design_dir(design_id: str) -> str:
    """Return the absolute path to the directory for *design_id*."""
    import os

    return os.path.expanduser(f"{_DESIGNS_ROOT}/{design_id}")


def _ensure_design_dir(design_id: str) -> str:
    """Create and return the design directory, creating it if needed."""
    import os

    path = _design_dir(design_id)
    os.makedirs(path, exist_ok=True)
    return path


def _versioned_recipe_path(design_dir: str, version: int) -> str:
    """Return the path of a versioned recipe snapshot file."""
    import os

    return os.path.join(design_dir, f".kiln_recipe.v{version}.json")


def _load_versioned_recipe(design_dir: str, version: int) -> Any:
    """Load a specific versioned recipe or raise ValueError if not found."""
    import os

    from kiln.design_recipe import load_recipe

    path = _versioned_recipe_path(design_dir, version)
    if not os.path.isfile(path):
        raise ValueError(f"Version {version} not found in {design_dir}")
    return load_recipe(path), path


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
            brief_id: str = "",
            intent_hash: str = "",
        ) -> dict:
            """Save a new version of a parametric design.

            Automatically computes a unified diff from the previous version,
            increments the version number, and persists a versioned recipe
            sidecar (``~/.kiln/designs/<design_id>/.kiln_recipe.vN.json``).

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
                parent_version_id: Unused in this implementation; kept for
                    backward compatibility.  The parent is always the most
                    recent existing version.
                brief_id: Optional saved-goal id from ``design_session``.
                    When supplied, the new version's recipe records the
                    link so the audit's "matches what you asked for" gate,
                    the brief failure_history wiring, and the
                    ``compare_design_versions`` intent diff all join back
                    to the goal.  When omitted, an earlier version's
                    ``brief_id`` (read from the parent recipe) is
                    inherited automatically.
                intent_hash: Optional content hash of the brief's derived
                    intent payload, paired with ``brief_id``.  Same
                    inheritance fall-back as ``brief_id``.

            Returns:
                The saved version record including version number, diff, and
                parent information.  Pro users also get provenance,
                mesh_fingerprint, and mesh_diff with regression warnings.
            """
            import difflib

            from kiln.design_recipe import (
                create_new_version,
                create_recipe,
                find_recipe,
                load_recipe,
                save_recipe,
            )

            try:
                design_directory = _ensure_design_dir(design_id)
                existing_path = find_recipe(design_directory)

                if existing_path is None:
                    # First version — create a fresh recipe
                    recipe = create_recipe(
                        name=design_id,
                        parts=[],
                        source_scad=scad_source,
                        parameters=parameters or {},
                        notes=notes,
                    )
                    recipe.design_id = design_id
                    recipe.prompt = prompt or None
                    recipe.provenance = provenance or None
                    recipe.stl_path = stl_path or None
                    # Saved-goal provenance (A4): caller-supplied wins.
                    recipe.brief_id = brief_id or None
                    recipe.intent_hash = intent_hash or None
                    diff_text: str | None = None
                else:
                    parent = load_recipe(existing_path)
                    prev_scad = parent.source_scad or ""
                    diff_lines = list(
                        difflib.unified_diff(
                            prev_scad.splitlines(keepends=True),
                            scad_source.splitlines(keepends=True),
                            fromfile=f"v{parent.version}",
                            tofile=f"v{parent.version + 1}",
                        )
                    )
                    diff_text = "".join(diff_lines) if diff_lines else ""
                    recipe = create_new_version(
                        parent,
                        existing_path,
                        changes={"scad_source": "updated"} if diff_text else None,
                        notes=notes,
                    )
                    recipe.source_scad = scad_source
                    recipe.parameters = parameters or parent.parameters
                    recipe.design_id = design_id
                    recipe.prompt = prompt or parent.prompt
                    recipe.provenance = provenance or None
                    recipe.stl_path = stl_path or None
                    # Saved-goal provenance (A4): caller-supplied wins;
                    # otherwise inherit from the parent recipe so an
                    # iteration of a brief-attached design doesn't
                    # silently lose the goal link.
                    recipe.brief_id = brief_id or parent.brief_id
                    recipe.intent_hash = intent_hash or parent.intent_hash

                saved_path = save_recipe(recipe, design_directory)
                result: dict = {
                    "ok": True,
                    "version": {
                        "design_id": design_id,
                        "version": recipe.version,
                        "path": saved_path,
                        "versioned_path": _versioned_recipe_path(
                            design_directory, recipe.version
                        ),
                        "created": recipe.created,
                        "prompt": recipe.prompt,
                        "notes": recipe.notes,
                        "parameters": recipe.parameters,
                        "diff_from_prev": diff_text,
                        "parent_version": recipe.parent_version,
                        "changes": recipe.changes,
                        "stl_path": recipe.stl_path,
                        "provenance": recipe.provenance,
                        "brief_id": recipe.brief_id,
                        "intent_hash": recipe.intent_hash,
                    },
                }

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

                    # Register the saved version with the branch system so
                    # it becomes a branch-linked, pushable commit — the
                    # same registration external-mesh imports perform. This
                    # is LOCAL only: it advances the on-device branch head
                    # so the version can be pushed later; it does NOT push.
                    # ``getattr`` so an older kiln-pro that predates this
                    # hook degrades to recipe-only, exactly like free tier
                    # (a missing attribute is not an error here).
                    register = getattr(
                        pro_features, "register_saved_version", None
                    )
                    if register is not None:
                        registration = register(
                            design_id=design_id,
                            scad_source=scad_source,
                            stl_path=recipe.stl_path or stl_path or None,
                            prompt=recipe.prompt or "",
                            parameters=recipe.parameters,
                            notes=recipe.notes or "",
                        )
                        if registration:
                            result["version"]["branch"] = registration
                except ImportError:
                    pass  # Free tier — no provenance enrichment or branch registration

                try:
                    from kiln_pro.plugins.git_render_tools import (
                        attach_inspect_bundle,
                    )

                    return attach_inspect_bundle(
                        result,
                        level="full",
                        source_path=recipe.stl_path or stl_path or None,
                    )
                except ImportError:
                    return result
            except Exception as exc:
                _logger.exception("save_design_version failed")
                return {"ok": False, "error": str(exc)}

        @mcp.tool()
        def list_design_versions(design_id: str, limit: int = 20) -> dict:
            """List version history for a design, newest first.

            Args:
                design_id: The design whose versions to list.
                limit: Maximum number of versions to return (default 20).

            Returns:
                A list of version records ordered by version number descending.
            """
            from kiln.design_recipe import list_recipe_versions

            try:
                design_directory = _design_dir(design_id)
                versions = list_recipe_versions(design_directory)
                # newest first, then apply limit
                versions = list(reversed(versions))[:limit]
                return {
                    "ok": True,
                    "design_id": design_id,
                    "count": len(versions),
                    "versions": versions,
                }
            except Exception as exc:
                _logger.exception("list_design_versions failed")
                return {"ok": False, "error": str(exc)}

        @mcp.tool()
        def diff_design_versions(version_id_a: str, version_id_b: str) -> dict:
            """Compute a unified diff between two design versions.

            Version IDs are interpreted as ``<design_id>:<version_number>``
            (e.g. ``my-coaster:2``).  If no colon is present the string is
            treated as a plain version number and the tool will attempt to
            locate a design that contains that version.

            Args:
                version_id_a: The "from" version in ``design_id:N`` format.
                version_id_b: The "to" version in ``design_id:N`` format.

            Returns:
                A unified diff string showing changes from version A to B.
            """
            import difflib

            try:
                design_id_a, ver_a = _parse_version_ref(version_id_a)
                design_id_b, ver_b = _parse_version_ref(version_id_b)

                dir_a = _design_dir(design_id_a)
                dir_b = _design_dir(design_id_b)

                recipe_a, _ = _load_versioned_recipe(dir_a, ver_a)
                recipe_b, _ = _load_versioned_recipe(dir_b, ver_b)

                source_a = recipe_a.source_scad or ""
                source_b = recipe_b.source_scad or ""

                diff_lines = list(
                    difflib.unified_diff(
                        source_a.splitlines(keepends=True),
                        source_b.splitlines(keepends=True),
                        fromfile=version_id_a,
                        tofile=version_id_b,
                    )
                )
                return {"ok": True, "diff": "".join(diff_lines)}
            except ValueError as exc:
                return {"ok": False, "error": str(exc)}
            except Exception as exc:
                _logger.exception("diff_design_versions failed")
                return {"ok": False, "error": str(exc)}

        @mcp.tool()
        def rollback_design_version(design_id: str, to_version_id: str) -> dict:
            """Rollback a design to a previous version.

            Creates a *new* version whose source matches the target version,
            preserving full history.  The new version's notes record the
            rollback origin.

            Args:
                design_id: The design to rollback.
                to_version_id: The version number (integer) or
                    ``design_id:N`` ref to restore.

            Returns:
                The newly created rollback version record.
            """
            from kiln.design_recipe import (
                create_new_version,
                find_recipe,
                load_recipe,
                save_recipe,
            )

            try:
                design_directory = _ensure_design_dir(design_id)

                # Parse the target version number
                _, target_ver = _parse_version_ref(to_version_id, default_design_id=design_id)
                target_recipe, target_path = _load_versioned_recipe(
                    design_directory, target_ver
                )

                # Load current recipe as parent for the new rollback version
                existing_path = find_recipe(design_directory)
                if existing_path is None:
                    return {
                        "ok": False,
                        "error": f"No recipe found for design '{design_id}'",
                    }
                current = load_recipe(existing_path)

                rollback_notes = f"Rollback to v{target_ver}"
                new_recipe = create_new_version(
                    current,
                    existing_path,
                    changes={"rollback": f"restored from v{target_ver}"},
                    notes=rollback_notes,
                )
                # Copy source and parameters from the target version
                new_recipe.source_scad = target_recipe.source_scad
                new_recipe.parameters = dict(target_recipe.parameters)

                saved_path = save_recipe(new_recipe, design_directory)
                result = {
                    "ok": True,
                    "version": {
                        "design_id": design_id,
                        "version": new_recipe.version,
                        "path": saved_path,
                        "versioned_path": _versioned_recipe_path(
                            design_directory, new_recipe.version
                        ),
                        "created": new_recipe.created,
                        "notes": new_recipe.notes,
                        "restored_from_version": target_ver,
                        "parent_version": new_recipe.parent_version,
                        "changes": new_recipe.changes,
                    },
                }
                try:
                    from kiln_pro.plugins.git_render_tools import (
                        attach_inspect_bundle,
                    )

                    return attach_inspect_bundle(
                        result,
                        level="full",
                        source_path=target_recipe.stl_path or None,
                    )
                except ImportError:
                    return result
            except ValueError as exc:
                return {"ok": False, "error": str(exc)}
            except Exception as exc:
                _logger.exception("rollback_design_version failed")
                return {"ok": False, "error": str(exc)}

        @mcp.tool()
        def get_design_version(version_id: str) -> dict:
            """Retrieve a single design version by its ID.

            Use this to inspect the full source code, parameters, and notes
            for a specific version when you already know the version reference.

            Args:
                version_id: ``design_id:N`` reference (e.g. ``my-coaster:3``)
                    or a plain integer version number if the design_id is
                    unambiguous.

            Returns:
                The version record including source_scad, prompt,
                parameters, notes, and parent_version.
                Returns an error if the version does not exist.
            """

            try:
                design_id, ver = _parse_version_ref(version_id)
                design_directory = _design_dir(design_id)
                recipe, path = _load_versioned_recipe(design_directory, ver)
                return {
                    "ok": True,
                    "version": {
                        "design_id": design_id,
                        "version": recipe.version,
                        "path": path,
                        "created": recipe.created,
                        "prompt": recipe.prompt,
                        "notes": recipe.notes,
                        "source_scad": recipe.source_scad,
                        "parameters": recipe.parameters,
                        "parent_version": recipe.parent_version,
                        "changes": recipe.changes,
                        "stl_path": recipe.stl_path,
                        "provenance": recipe.provenance,
                    },
                }
            except ValueError as exc:
                return {"ok": False, "error": str(exc)}
            except Exception as exc:
                _logger.exception("get_design_version failed")
                return {"ok": False, "error": str(exc)}

        @mcp.tool()
        def search_design_versions(query: str, limit: int = 10) -> dict:
            """Search design versions by prompt, notes, or design name.

            Scans all design directories under ``~/.kiln/designs/`` for
            versioned recipe files whose prompt, notes, or name fields
            contain the query string (case-insensitive).

            Args:
                query: The search term (literal substring, not regex).
                limit: Maximum number of results to return (default 10).

            Returns:
                A list of matching version records, newest first.
            """
            import os

            from kiln.design_recipe import load_recipe

            try:
                root = os.path.expanduser(_DESIGNS_ROOT)
                matches: list[dict[str, Any]] = []
                q = query.lower()

                if not os.path.isdir(root):
                    return {"ok": True, "query": query, "count": 0, "versions": []}

                for design_id in sorted(os.listdir(root)):
                    design_directory = os.path.join(root, design_id)
                    if not os.path.isdir(design_directory):
                        continue
                    for fname in os.listdir(design_directory):
                        if not (fname.startswith(".kiln_recipe.v") and fname.endswith(".json")):
                            continue
                        path = os.path.join(design_directory, fname)
                        try:
                            recipe = load_recipe(path)
                        except Exception:
                            continue
                        searchable = " ".join(
                            filter(
                                None,
                                [
                                    recipe.name or "",
                                    recipe.prompt or "",
                                    recipe.notes or "",
                                ],
                            )
                        ).lower()
                        if q in searchable:
                            matches.append(
                                {
                                    "design_id": design_id,
                                    "version": recipe.version,
                                    "path": os.path.abspath(path),
                                    "created": recipe.created,
                                    "name": recipe.name,
                                    "prompt": recipe.prompt,
                                    "notes": recipe.notes,
                                    "parameters": recipe.parameters,
                                }
                            )
                        if len(matches) >= limit:
                            break
                    if len(matches) >= limit:
                        break

                # sort newest versions first (by created timestamp, desc)
                matches.sort(key=lambda r: r.get("created", ""), reverse=True)
                matches = matches[:limit]

                return {
                    "ok": True,
                    "query": query,
                    "count": len(matches),
                    "versions": matches,
                }
            except Exception as exc:
                _logger.exception("search_design_versions failed")
                return {"ok": False, "error": str(exc)}


def _parse_version_ref(
    ref: str, *, default_design_id: str | None = None
) -> tuple[str, int]:
    """Parse a version reference into ``(design_id, version_number)``.

    Accepts ``"design_id:N"`` or a plain integer string when
    *default_design_id* is provided.

    :raises ValueError: If the reference cannot be parsed.
    """
    if ":" in ref:
        parts = ref.split(":", 1)
        design_id = parts[0].strip()
        try:
            version = int(parts[1].strip())
        except ValueError as exc:
            raise ValueError(f"Invalid version number in ref '{ref}'") from exc
        return design_id, version
    if default_design_id is not None:
        try:
            return default_design_id, int(ref.strip())
        except ValueError as exc:
            raise ValueError(
                f"Cannot parse version number from '{ref}'. "
                "Use 'design_id:N' format."
            ) from exc
    raise ValueError(
        f"Version ref '{ref}' must be in 'design_id:N' format "
        "(e.g. 'my-coaster:3')."
    )


def register_plugin(mcp: Any) -> None:
    """Entry point for plugin auto-discovery."""
    _VersionToolsPlugin().register(mcp)


plugin = _VersionToolsPlugin()
