"""Model and design cache tools plugin.

Extracts caching-domain MCP tools from server.py into a focused plugin
module.  Covers both the model cache (marketplace downloads, generated
models) and the design cache (versioned design files).

Auto-discovered by :func:`~kiln.plugin_loader.register_all_plugins` --
no manual imports needed.
"""

from __future__ import annotations

import logging
from typing import Any

_logger = logging.getLogger(__name__)


class _CacheToolsPlugin:
    """Model cache and design cache tools.

    Tools:
        - cache_model
        - search_cached_models
        - get_cached_model
        - list_cached_models
        - delete_cached_model
        - cache_design
        - list_cached_designs
        - get_cached_design
    """

    @property
    def name(self) -> str:
        return "cache_tools"

    @property
    def description(self) -> str:
        return "Model cache and design cache tools"

    def register(self, mcp: Any) -> None:  # noqa: PLR0915
        """Register cache tools with the MCP server."""

        import kiln.server as _srv

        # ------------------------------------------------------------------
        # cache_model
        # ------------------------------------------------------------------

        @mcp.tool()
        def cache_model(
            file_path: str,
            source: str,
            source_id: str | None = None,
            prompt: str | None = None,
            tags: str | None = None,
            dimensions: str | None = None,
            metadata: str | None = None,
        ) -> dict:
            """Add a 3D model file to the local cache for reuse across jobs.

            Copies the file into ``~/.kiln/model_cache/`` and stores metadata
            (source, prompt, tags, dimensions) in the database.  Duplicate files
            are detected automatically by SHA-256 hash.

            Args:
                file_path: Path to the model file on disk.
                source: Origin — ``"thingiverse"``, ``"myminifactory"``, ``"meshy"``,
                    ``"openscad"``, ``"upload"``, etc.
                source_id: Marketplace thing ID or generation job ID.
                prompt: For generated models, the text prompt used.
                tags: Comma-separated tags (e.g. ``"benchy,calibration,test"``).
                dimensions: JSON object with bounding box in mm, e.g.
                    ``'{"x": 60, "y": 31, "z": 48}'``.
                metadata: Optional JSON object with extra data.
            """
            if err := _srv._check_auth("cache"):
                return err
            try:
                import json as _json

                from kiln.model_cache import get_model_cache

                tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else None
                dim_dict = _json.loads(dimensions) if dimensions else None
                meta_dict = _json.loads(metadata) if metadata else None

                cache = get_model_cache()
                entry = cache.add(
                    file_path,
                    source=source,
                    source_id=source_id,
                    prompt=prompt,
                    tags=tag_list,
                    dimensions=dim_dict,
                    metadata=meta_dict,
                )
                return {"success": True, "entry": entry.to_dict()}
            except FileNotFoundError as exc:
                return _srv._error_dict(f"Failed to cache model: {exc}", code="NOT_FOUND")
            except (ValueError, _json.JSONDecodeError) as exc:
                return _srv._error_dict(f"Failed to cache model: {exc}", code="VALIDATION_ERROR")
            except Exception as exc:
                _logger.exception("Unexpected error in cache_model")
                return _srv._error_dict(f"Unexpected error in cache_model: {exc}", code="INTERNAL_ERROR")

        # ------------------------------------------------------------------
        # search_cached_models
        # ------------------------------------------------------------------

        @mcp.tool()
        def search_cached_models(
            query: str | None = None,
            source: str | None = None,
            tags: str | None = None,
            limit: int = 20,
        ) -> dict:
            """Search the local model cache by name, source, tags, or prompt text.

            Args:
                query: Free-text search against file name, prompt, and tags.
                source: Filter by source (e.g. ``"thingiverse"``).
                tags: Comma-separated tags to filter by.
                limit: Maximum results (default 20).

            The response declares its ``scope``: this cache is local to
            this machine, so the count is what THIS install has cached,
            not everything the user has ever downloaded or generated.
            """
            if err := _srv._check_auth("cache"):
                return err
            try:
                from kiln.model_cache import get_model_cache
                from kiln.store_scope import MODEL_CACHE, scoped_store_response

                tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else None
                cache = get_model_cache()
                entries = cache.search(query=query, source=source, tags=tag_list, limit=limit)
                return scoped_store_response(
                    {
                        "success": True,
                        "entries": [e.to_dict() for e in entries],
                        "count": len(entries),
                    },
                    store=MODEL_CACHE,
                    items_key="entries",
                )
            except Exception as exc:
                _logger.exception("Unexpected error in search_cached_models")
                return _srv._error_dict(f"Unexpected error in search_cached_models: {exc}", code="INTERNAL_ERROR")

        # ------------------------------------------------------------------
        # get_cached_model
        # ------------------------------------------------------------------

        @mcp.tool()
        def get_cached_model(cache_id: str) -> dict:
            """Return details for a specific cached model.

            Args:
                cache_id: The unique cache ID of the model.
            """
            if err := _srv._check_auth("cache"):
                return err
            try:
                from kiln.model_cache import get_model_cache

                entry = get_model_cache().get(cache_id)
                if entry is None:
                    return _srv._error_dict(f"No cached model with id {cache_id!r}.", code="NOT_FOUND")
                return {"success": True, "entry": entry.to_dict()}
            except Exception as exc:
                _logger.exception("Unexpected error in get_cached_model")
                return _srv._error_dict(f"Unexpected error in get_cached_model: {exc}", code="INTERNAL_ERROR")

        # ------------------------------------------------------------------
        # list_cached_models
        # ------------------------------------------------------------------

        @mcp.tool()
        def list_cached_models(limit: int = 50, offset: int = 0) -> dict:
            """List all models in the local cache, newest first.

            Args:
                limit: Maximum results (default 50).
                offset: Number of entries to skip for pagination.

            The response declares its ``scope``: this cache is local to
            this machine, so the count is what THIS install has cached,
            not everything the user has ever downloaded or generated.
            It is also a page — ``limit``/``offset`` bound it further.
            """
            if err := _srv._check_auth("cache"):
                return err
            try:
                from kiln.model_cache import get_model_cache
                from kiln.store_scope import MODEL_CACHE, scoped_store_response

                entries = get_model_cache().list_all(limit=limit, offset=offset)
                return scoped_store_response(
                    {
                        "success": True,
                        "entries": [e.to_dict() for e in entries],
                        "count": len(entries),
                    },
                    store=MODEL_CACHE,
                    items_key="entries",
                )
            except Exception as exc:
                _logger.exception("Unexpected error in list_cached_models")
                return _srv._error_dict(f"Unexpected error in list_cached_models: {exc}", code="INTERNAL_ERROR")

        # ------------------------------------------------------------------
        # delete_cached_model
        # ------------------------------------------------------------------

        @mcp.tool()
        def delete_cached_model(cache_id: str) -> dict:
            """Remove a model from the local cache (file and metadata).

            Args:
                cache_id: The unique cache ID of the model to delete.
            """
            if err := _srv._check_auth("cache"):
                return err
            try:
                from kiln.model_cache import get_model_cache

                deleted = get_model_cache().delete(cache_id)
                if not deleted:
                    return _srv._error_dict(f"No cached model with id {cache_id!r}.", code="NOT_FOUND")
                return {"success": True, "cache_id": cache_id}
            except Exception as exc:
                _logger.exception("Unexpected error in delete_cached_model")
                return _srv._error_dict(f"Unexpected error in delete_cached_model: {exc}", code="INTERNAL_ERROR")

        # ------------------------------------------------------------------
        # cache_design
        # ------------------------------------------------------------------

        @mcp.tool()
        def cache_design(
            file_path: str,
            *,
            label: str | None = None,
            material: str | None = None,
        ) -> dict:
            """Cache a 3D design file for faster access and version tracking.

            Args:
                file_path: Path to the design file to cache.
                label: Human-readable label for the cached design.
                material: Intended material for this design.
            """
            if err := _srv._check_auth("cache"):
                return err

            try:
                from kiln.design_cache import get_design_cache

                cache = get_design_cache()
                entry = cache.add(
                    file_path,
                    filament_type=(material or "").strip() or None,
                    metadata={"label": label} if label else None,
                )
                return {"success": True, "cached_design": entry.to_dict()}
            except Exception as exc:
                _logger.exception("Error in cache_design")
                return _srv._error_dict(f"Failed to cache design: {exc}", code="CACHE_ERROR")

        # ------------------------------------------------------------------
        # list_cached_designs
        # ------------------------------------------------------------------

        @mcp.tool()
        def list_cached_designs(
            *,
            material: str | None = None,
            limit: int = 50,
        ) -> dict:
            """List cached designs, optionally filtered by material.

            Args:
                material: Filter by material (e.g. "PLA", "PETG").
                limit: Maximum number of results.

            Most recently used designs come first.

            The response declares its ``scope``: which store was read,
            and whether the user's cloud-side designs are included.
            Read ``scope`` before treating ``count`` as everything they
            have saved.
            """
            if err := _srv._check_auth("cache"):
                return err
            try:
                from kiln.design_cache import get_design_cache
                from kiln.store_scope import DESIGN_CACHE, scoped_store_response

                cache = get_design_cache()
                material = (material or "").strip() or None
                designs = cache.search(filament_type=material, limit=limit)
                return scoped_store_response(
                    {
                        "success": True,
                        "designs": [d.to_dict() for d in designs],
                        "count": len(designs),
                    },
                    store=DESIGN_CACHE,
                    items_key="designs",
                )
            except Exception as exc:
                _logger.exception("Error in list_cached_designs")
                return _srv._error_dict(f"Failed to list cached designs: {exc}", code="CACHE_ERROR")

        # ------------------------------------------------------------------
        # get_cached_design
        # ------------------------------------------------------------------

        @mcp.tool()
        def get_cached_design(design_id: str) -> dict:
            """Retrieve a cached design by ID.

            Args:
                design_id: The cached design's identifier.
            """
            if err := _srv._check_auth("cache"):
                return err
            try:
                from kiln.design_cache import get_design_cache

                cache = get_design_cache()
                entry = cache.get(design_id)
                if entry is None:
                    return _srv._error_dict(f"Design {design_id!r} not found", code="NOT_FOUND")
                return {"success": True, "design": entry.to_dict()}
            except Exception as exc:
                _logger.exception("Error in get_cached_design")
                return _srv._error_dict(f"Failed to get cached design: {exc}", code="CACHE_ERROR")


plugin = _CacheToolsPlugin()
