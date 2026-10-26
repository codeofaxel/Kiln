"""Decoration library plugin — save and reuse decorations across models.

Provides MCP tools to save proven decorations (photos, SVGs, QR codes, text)
and re-apply them to new models with one call.  Settings (depth, mode,
material, image_style) are captured from the first successful print and
replayed exactly, eliminating trial-and-error on repeat decorations.

Auto-discovered by :func:`~kiln.plugin_loader.register_all_plugins` --
no manual imports needed.
"""

from __future__ import annotations

import logging
from typing import Any

_logger = logging.getLogger(__name__)


def _not_found_error(name: str) -> dict[str, Any]:
    """The library's not-found answer, redirecting when it's a PRESET.

    A "saved decoration" in Kiln can live in either of two stores: this
    library (keyed by a human name), or kiln-pro's decoration presets
    (keyed by id — what the web's /decorations pages show).  One word over
    two stores, so landing at the wrong door is the expected mistake, not
    an exotic one.  When kiln-pro is installed, resolve the name over
    there and hand back the call that works, instead of pointing at a list
    that cannot contain what was asked for.

    One helper for every door that can miss, so the two answers cannot
    drift apart.  Free installs have no preset store, so the import simply
    isn't there and the plain message stands.
    """
    try:
        from kiln_pro.decoration.decoration_lookup import (  # type: ignore[import]
            crossover_hint,
        )

        hint = crossover_hint(name, asked_store="library")
    except Exception:
        hint = None
    if hint:
        return {"success": False, "error": hint}
    return {
        "success": False,
        "error": f"Decoration not found: {name!r}. Use list_decorations to see available.",
    }


def _memory_refusal(tool: str, answer: dict[str, Any]) -> dict[str, Any]:
    """What a decoration-memory tool says when Kiln's servers gave no plan.

    A plan without decoration memory gets the servers' own Kiln Pro answer;
    anything else (offline, signed out, silent) is worded here, and in every
    case nothing in the decoration's folder changed.
    """
    from kiln.served_answer import classify_answer, clause, fields

    if str(answer.get("code") or "") == "TIER_REQUIRED":
        return {**answer, "status": "error", "success": False, "tool": tool}
    miss = classify_answer(answer)
    reason = clause(
        miss, feature="servers", cannot="reach decoration memory",
    ) if miss is not None else "Kiln's servers gave no answer"
    envelope: dict[str, Any] = {
        "status": "error",
        "success": False,
        "tool": tool,
        "code": str(answer.get("code") or "DECORATION_MEMORY_UNAVAILABLE"),
        "error": (
            f"{tool} is Kiln Pro decoration memory, worked out on Kiln's "
            f"servers. {reason}. Nothing in the decoration's folder changed."
        ),
        **fields(miss),
    }
    for key in ("upgrade_url", "required_tier"):
        if answer.get(key):
            envelope[key] = answer[key]
    return envelope


class _DecorationLibraryPlugin:
    """Save and reuse decorations across models — photos, SVGs, QR codes, text."""

    @property
    def name(self) -> str:
        return "decoration_library"

    @property
    def description(self) -> str:
        return "Save and reuse decorations across models — photos, SVGs, QR codes, text."

    def register(self, mcp: Any) -> None:  # noqa: C901
        """Register decoration library tools with the MCP server."""

        @mcp.tool()
        def save_decoration(
            name: str,
            model_path: str,
            content_type: str = "auto",
            source_path: str = "",
            content_data: str = "",
            depth_mm: float = 0.0,
            mode: str = "emboss",
            image_style: str = "auto",
            material: str = "PLA",
            tags: str = "",
        ) -> dict:
            """Save a proven decoration to the library for reuse on future models.

            Captures the content file (heightmap, SVG, image), settings
            (depth, mode, material), and processing pipeline from the
            ``.kiln_recipe.json`` sidecar so the exact same decoration can
            be applied to new models with ``apply_decoration``.

            :param name: Human-readable name (e.g. "Ash Portrait").
            :param model_path: Path to the model that was just decorated.
            :param content_type: Content type — ``photo``, ``svg``, ``qr``,
                ``text``, or ``auto`` (detect from file extension).
            :param source_path: Path to the original input file (photo, SVG).
            :param content_data: For QR: the data string. For text: the text.
            :param depth_mm: Decoration depth in mm (0 = auto from recipe).
            :param mode: ``emboss`` or ``deboss``.
            :param image_style: Image processing style (coin, portrait, etc.).
            :param material: Material used (e.g. PLA, PETG).
            :param tags: Comma-separated tags for filtering.
            :returns: Dict with saved decoration details and library path.
            """
            import kiln.server as _srv
            if err := _srv._check_auth("cache"):
                return err

            import json
            import os

            if not os.path.isfile(model_path):
                return _srv._error_dict(
                    f"No model at {model_path}: pass the path of the model the decoration was made on.",
                    code="FILE_NOT_FOUND",
                )

            from kiln.decoration_library import (
                _detect_content_type,
            )
            from kiln.decoration_library import (
                save_decoration as _save,
            )

            # --- Auto-extract settings from recipe sidecar ---
            recipe_data: dict[str, Any] = {}
            model_dir = os.path.dirname(os.path.abspath(model_path))
            recipe_path = os.path.join(model_dir, ".kiln_recipe.json")
            if os.path.isfile(recipe_path):
                try:
                    with open(recipe_path, encoding="utf-8") as f:
                        recipe_data = json.load(f)
                    _logger.debug("Loaded recipe sidecar: %s", recipe_path)
                except (json.JSONDecodeError, OSError) as exc:
                    _logger.debug("Failed to read recipe sidecar: %s", exc)

            # Auto-detect content type from source or content files
            if content_type == "auto":
                if source_path:
                    content_type = _detect_content_type(source_path)
                elif content_data:
                    # If content_data looks like a URL or has special chars, assume QR
                    content_type = "qr" if "://" in content_data else "text"
                else:
                    content_type = "photo"

            # Extract depth from recipe if not explicitly provided
            if depth_mm <= 0 and recipe_data:
                depth_mm = float(recipe_data.get("depth_mm", 0.0))

            # Extract material from recipe if default
            if material == "PLA" and recipe_data.get("material"):
                material = recipe_data["material"]

            # Extract image_style from recipe if auto
            if image_style == "auto" and recipe_data.get("image_style"):
                image_style = recipe_data["image_style"]

            # Extract mode from recipe
            if mode == "emboss" and recipe_data.get("mode"):
                mode = recipe_data["mode"]

            # Extract pipeline info from recipe
            pipeline = recipe_data.get("pipeline", {})

            # --- Find content file ---
            content_file = ""
            if content_type in ("photo", "svg"):
                # Look for .dat heightmap first (processed), then source
                if content_type == "photo":
                    for fname in os.listdir(model_dir):
                        if fname.endswith(".dat"):
                            content_file = os.path.join(model_dir, fname)
                            break
                if not content_file and source_path and (
                    os.path.isfile(source_path) or content_type == "svg"
                ):
                    content_file = source_path

            # Parse tags
            tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []

            try:
                decoration = _save(
                    name,
                    content_type=content_type,
                    mode=mode,
                    depth_mm=depth_mm,
                    material=material,
                    image_style=image_style,
                    content_data=content_data,
                    content_path=content_file if content_file else None,
                    source_path=source_path if source_path and os.path.isfile(source_path) else None,
                    processing=pipeline if pipeline else None,
                    tags=tag_list,
                )
            except ValueError as exc:  # what the call asked for cannot make a decoration
                return _srv._error_dict(str(exc), code="INVALID_ARGS")

            from kiln.decoration_library import _decoration_dir

            result = {
                "success": True,
                "decoration": decoration.to_dict(),
                "path": str(_decoration_dir(decoration.slug)),
            }
            try:
                from kiln_pro.plugins.git_render_tools import attach_inspect_bundle

                return attach_inspect_bundle(
                    result, level="full", source_path=model_path,
                )
            except ImportError:
                return result

        @mcp.tool()
        def list_decorations(
            content_type: str = "",
            category: str = "",
            tag: str = "",
        ) -> dict:
            """List all saved decorations in the library.

            Browse the decoration library to find reusable decorations.
            Filter by content type, category, or tag.

            :param content_type: Filter by type — ``photo``, ``svg``, ``qr``,
                ``text``, ``procedural_texture``, ``ai_texture``
                (empty = show all).
            :param category: Filter by category — ``surface`` (photo/svg/qr/text)
                or ``texture`` (procedural/AI textures).  Empty = show all.
            :param tag: Filter by tag (empty = show all).
            :returns: Dict with decoration count and list.

            This is the decoration LIBRARY: keyed by name, and it ADAPTS —
            each recorded success stores proven settings for THAT material,
            so applying picks the depth and mode your prints proved for
            whatever you are printing in now.  It keeps no version history.

            Kiln's other kind of saved decoration is a decoration PRESET
            (kiln-pro; what the web's /decorations pages show): keyed by an
            id because it has versions, branches and signed releases, and
            applied at the exact settings its version recorded rather than
            adapting to the material.  Listed by ``list_decoration_presets``,
            applied by ``apply_decoration_preset``.  The library adapts, the
            preset remembers — if what you want isn't here, look there.

            The response declares its own SCOPE.  The library is
            local-only at every tier — the web's /decorations pages are
            the decoration PRESET store above, not a cloud copy of this
            one — so a complete local read here is the whole library,
            and ``scope`` says exactly that.
            """
            from kiln.decoration_library import (
                list_decorations as _list,
            )
            from kiln.store_scope import DECORATION_LIBRARY, scoped_store_response

            decorations = _list(
                content_type=content_type or None,
                category=category or None,
                tag=tag or None,
            )
            result = {
                "success": True,
                "count": len(decorations),
                "decorations": [d.to_dict() for d in decorations],
            }
            # Cross-store hint: when kiln-pro's decoration PRESETS hold
            # entries, say so — an agent that only checks here would
            # otherwise conclude a saved logo preset doesn't exist.  A
            # hint, not a merge: the two stores stay separate (the
            # library adapts, the preset remembers), and without
            # kiln-pro this listing is exactly what it always was.
            # Complementary to the scope block below, which describes
            # THIS store; the hint names the other one.
            try:
                from kiln_pro.design_versions.decoration_presets import (
                    DecorationPresetStore,
                )

                _presets = DecorationPresetStore()
                try:
                    n = len(_presets.list_presets())
                finally:
                    _presets.close()
                if n:
                    result["presets_hint"] = (
                        f"{n} decoration preset(s) also saved — the "
                        "versioned kind the web's /decorations pages "
                        "show. List them with list_decoration_presets."
                    )
            except Exception:
                # No kiln-pro, or its store is unavailable — the hint
                # must never break a free listing.
                pass
            return scoped_store_response(
                result,
                store=DECORATION_LIBRARY,
                items_key="decorations",
                filters={
                    "content_type": content_type or None,
                    "category": category or None,
                    "tag": tag or None,
                },
            )

        @mcp.tool()
        def apply_decoration(
            name: str,
            model_path: str,
            material: str = "",
            face: str = "auto",
            printer_id: str = "",
        ) -> dict:
            """Apply a saved decoration to a new model — proven settings, one call.

            Loads a previously saved decoration and applies it to the target
            model using the exact settings that worked before.  Automatically
            resolves depth, mode, and image style from the proven recipe.

            This is the magic tool — decorations that took many iterations
            to perfect can be replayed on any model in one call.

            :param name: Decoration name or slug.
            :param model_path: Path to the target model (STL or OBJ).
            :param material: Override material (empty = use proven or detect
                from printer).
            :param face: Which face to decorate (auto, top, bottom, etc.).
            :param printer_id: Optional printer ID for material detection.
            :returns: Dict with decorated model path and settings used.
            """
            import kiln.server as _srv
            if err := _srv._check_auth("generate"):
                return err

            import os

            from kiln.decoration_library import (
                get_content_file_path,
                resolve_decoration_settings,
            )
            from kiln.decoration_library import (
                get_decoration as _get,
            )

            if not os.path.isfile(model_path):
                return {"success": False, "error": f"Model not found: {model_path}"}

            decoration = _get(name)
            if decoration is None:
                return _not_found_error(name)

            # Detect material from printer if not specified
            resolved_material = material
            if not resolved_material and printer_id:
                try:
                    from kiln.server import _resolve_adapter

                    adapter = _resolve_adapter(printer_id)
                    state = adapter.get_state()
                    if hasattr(state, "active_material") and state.active_material:
                        resolved_material = state.active_material
                except Exception as exc:
                    _logger.debug("Could not detect material from printer: %s", exc)

            settings = resolve_decoration_settings(
                decoration, material=resolved_material,
            )

            # Determine content argument for decorate_surface
            content: str = ""
            if decoration.content_type in ("qr", "text"):
                content = decoration.content_data
                if decoration.content_type == "text" and not content.startswith("text:"):
                    content = f"text:{content}"
            else:
                # photo or svg — use the stored content file
                content_path = get_content_file_path(decoration)
                if content_path:
                    content = content_path
                elif decoration.content_data:
                    content = decoration.content_data
                else:
                    return {
                        "success": False,
                        "error": (
                            f"No content file found for decoration {name!r}. "
                            "The content file may have been deleted from the library."
                        ),
                    }

            # Call decorate_surface
            try:
                from kiln.server import decorate_surface as _decorate

                result = _decorate(
                    model_path=model_path,
                    content=content,
                    face=face,
                    depth_mm=settings["depth_mm"],
                    mode=settings["mode"],
                    material=settings["material"],
                    image_style=settings["image_style"],
                    content_type=decoration.content_type,
                )
            except Exception as exc:
                _logger.debug("decorate_surface failed: %s", exc)
                return {
                    "success": False,
                    "error": f"Failed to apply decoration: {exc}",
                }

            if isinstance(result, dict):
                result["decoration_used"] = name
                result["settings_source"] = settings.get("source", "proven")
                for key in ("note", "upgrade_url"):
                    if settings.get(key):
                        result.setdefault(f"settings_{key}", settings[key])
            try:
                from kiln_pro.plugins.git_render_tools import attach_inspect_bundle

                return attach_inspect_bundle(result, level="quick", self_check_before=model_path)
            except ImportError:
                return result

        @mcp.tool()
        def decoration_info(
            name: str,
        ) -> dict:
            """Get full details about a saved decoration.

            Shows the decoration's proven settings, content paths, tags,
            processing pipeline, and library location.

            :param name: Decoration name or slug.
            :returns: Dict with full decoration details and file paths.
            """
            from kiln.decoration_library import (
                _decoration_dir,
                get_content_file_path,
                get_source_file_path,
            )
            from kiln.decoration_library import (
                get_decoration as _get,
            )

            decoration = _get(name)
            if decoration is None:
                return _not_found_error(name)

            info = decoration.to_dict()
            info["success"] = True
            info["content_path"] = get_content_file_path(decoration) or ""
            info["source_path"] = get_source_file_path(decoration) or ""
            info["library_path"] = str(_decoration_dir(decoration.slug))
            return info

        @mcp.tool()
        def decoration_quota_status() -> dict:
            """Check your decoration quota — how many decorations you've used this month.

            Free-tier users get 3 decorations per calendar month.  Every paid
            tier has unlimited decorations.

            Returns used count, limit, remaining, tier, and current month.
            """
            try:
                from kiln.decoration_quota import (
                    decoration_quota_status as _status,
                )

                result = _status()
                result["success"] = True
                return result
            except Exception as exc:
                _logger.debug("decoration_quota_status failed: %s", exc)
                return {
                    "success": False,
                    "error": f"Failed to check decoration quota: {exc}",
                }

        # ------------------------------------------------------------------
        # Decoration memory (Kiln Pro): what printed well, versions, history
        # and rollback.  Worked out on Kiln's servers and written into the
        # decoration's own folder here (kiln.decoration_library).
        # ------------------------------------------------------------------

        @mcp.tool()
        def record_decoration_success(
            name: str,
            material: str,
            depth_mm: float,
            mode: str = "emboss",
            image_style: str = "auto",
            source_job_id: str = "",
        ) -> dict:
            """Record a successful print with a saved decoration (Kiln Pro).

            After a decoration prints well, call this with the settings that
            worked.  ``apply_decoration`` then replays them on that material.

            :param name: Decoration name or slug.
            :param material: Material used (e.g. ``"PLA"``, ``"PETG"``).
            :param depth_mm: Depth in mm that printed well.
            :param mode: ``"emboss"`` or ``"deboss"``.
            :param image_style: Image style that worked (e.g. ``"coin"``).
            :param source_job_id: The print's job id when known: the same
                print reported twice (here and through
                ``record_print_outcome``) counts once.
            :returns: Dict with the updated decoration.
            """
            import kiln.server as _srv
            if err := _srv._check_auth("cache"):
                return err

            from kiln import _pro_decoration_bridge as bridge
            from kiln.decoration_library import record_decoration_success as _record

            try:
                dec = _record(
                    name, material=material, depth_mm=depth_mm, mode=mode,
                    image_style=image_style, source_job_id=source_job_id or None,
                )
            except bridge.MemoryUnavailable as miss:
                return _memory_refusal("record_decoration_success", miss.answer)
            except ValueError as exc:
                return {"status": "error", "error": str(exc), "code": "VALIDATION_ERROR"}
            return {
                "status": "success",
                "decoration": dec.to_dict(),
                "message": (
                    f"Recorded success for '{dec.name}' on {material} "
                    f"(depth={depth_mm}mm, mode={mode})."
                ),
            }

        @mcp.tool()
        def iterate_decoration(
            name: str,
            content_path: str = "",
            depth_mm: float = 0.0,
            mode: str = "",
            image_style: str = "",
            notes: str = "",
        ) -> dict:
            """Create a new version of a saved decoration, archiving the current one (Kiln Pro).

            Use this when tuning a decoration -- a new depth, mode, style or
            content file -- and the previous version stays available for
            ``rollback_decoration``.

            :param name: Decoration name or slug.
            :param content_path: New content file (empty = keep current).
            :param depth_mm: New depth in mm (0 = keep current).
            :param mode: New mode (empty = keep current).
            :param image_style: New image style (empty = keep current).
            :param notes: Free-text notes for this iteration.
            :returns: Dict with the new version and what changed.
            """
            import kiln.server as _srv
            if err := _srv._check_auth("cache"):
                return err

            from kiln import _pro_decoration_bridge as bridge
            from kiln.decoration_library import iterate_decoration as _iterate

            try:
                dec = _iterate(
                    name, content_path=content_path or None,
                    depth_mm=depth_mm if depth_mm > 0 else None,
                    mode=mode or None, image_style=image_style or None, notes=notes,
                )
            except bridge.MemoryUnavailable as miss:
                return _memory_refusal("iterate_decoration", miss.answer)
            except FileNotFoundError as exc:
                return {"status": "error", "error": str(exc), "code": "FILE_NOT_FOUND"}
            except ValueError as exc:
                return {"status": "error", "error": str(exc), "code": "VALIDATION_ERROR"}
            return {
                "status": "success",
                "decoration": dec.to_dict(),
                "version": dec.version,
                "changes": dec.changes,
            }

        @mcp.tool()
        def decoration_history(name: str) -> dict:
            """Get every version of a saved decoration (Kiln Pro).

            Each version with when it was made, what changed, the materials
            it has printed well on and its print count.

            :param name: Decoration name or slug.
            :returns: Dict with the version history and the current version.
            """
            from kiln import _pro_decoration_bridge as bridge
            from kiln.decoration_library import decoration_history as _history
            from kiln.decoration_library import get_decoration as _get

            current = _get(name)
            if current is None:
                return _not_found_error(name)
            try:
                versions = _history(name)
            except bridge.MemoryUnavailable as miss:
                return _memory_refusal("decoration_history", miss.answer)
            except ValueError as exc:
                return {"status": "error", "error": str(exc), "code": "VALIDATION_ERROR"}
            return {
                "status": "success",
                "name": name,
                "versions": versions,
                "current_version": current.version,
            }

        @mcp.tool()
        def rollback_decoration(name: str, version: int) -> dict:
            """Restore an earlier version of a saved decoration (Kiln Pro).

            The current version is archived and the earlier one comes back
            as a NEW version number, so history is never rewritten.

            :param name: Decoration name or slug.
            :param version: Version number to restore.
            :returns: Dict with the restored decoration.
            """
            import kiln.server as _srv
            if err := _srv._check_auth("cache"):
                return err

            from kiln import _pro_decoration_bridge as bridge
            from kiln.decoration_library import rollback_decoration as _rollback

            try:
                dec = _rollback(name, version=version)
            except bridge.MemoryUnavailable as miss:
                return _memory_refusal("rollback_decoration", miss.answer)
            except ValueError as exc:
                return {"status": "error", "error": str(exc), "code": "VALIDATION_ERROR"}
            return {
                "status": "success",
                "decoration": dec.to_dict(),
                "version": dec.version,
                "restored_from": version,
                "message": f"Restored '{dec.name}' from v{version} as v{dec.version}.",
            }

        _logger.debug("Registered decoration library tools")


plugin = _DecorationLibraryPlugin()
