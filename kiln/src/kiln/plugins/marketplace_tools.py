"""Marketplace tool plugin — search, browse, and download from 3D model marketplaces.

Migrated from server.py to reduce its size.  Each module exposes a
module-level ``plugin`` variable implementing the
:class:`~kiln.plugin_loader.ToolPlugin` protocol.

To migrate tools from server.py:
1. Create a new module in ``kiln/plugins/``.
2. Define a class with ``name``, ``description``, and ``register(mcp)``.
3. In ``register()``, define tool functions decorated with ``@mcp.tool()``.
4. Assign an instance to ``plugin`` at module level.
5. Remove the original tool definition from server.py.

The :func:`~kiln.plugin_loader.register_all_plugins` loader discovers
this module automatically — no manual imports needed.
"""

from __future__ import annotations

import logging
from typing import Any

_logger = logging.getLogger(__name__)


class _MarketplaceToolsPlugin:
    """Marketplace search, browse, download, status, and diagnostics tools.

    Covers:
    - Multi-marketplace unified search (search_all_models)
    - Thingiverse-specific search/browse/details/files/download
    - Marketplace status and diagnostics
    """

    @property
    def name(self) -> str:
        return "marketplace_tools"

    @property
    def description(self) -> str:
        return "Marketplace search, browse, download, status, and diagnostics"

    def register(self, mcp: Any) -> None:
        """Register marketplace tools with the MCP server."""

        # ---------------------------------------------------------------
        # Multi-marketplace unified search
        # ---------------------------------------------------------------

        @mcp.tool()
        def search_all_models(
            query: str,
            page: int = 1,
            per_page: int = 10,
            sort: str = "relevant",
            sources: list[str] | None = None,
        ) -> dict:
            """Search across all connected 3D model marketplaces simultaneously.

            Searches Thingiverse, MyMiniFactory, Cults3D, and MakerWorld in
            parallel and returns interleaved results from all sources.  Note
            that MakerWorld returns a search URL (no direct API access) while
            other sources return actual model results.

            Args:
                query: Search keywords (e.g. "raspberry pi case", "benchy").
                page: Page number (1-based, default 1).
                per_page: Results per source (default 10).
                sort: Sort order — "relevant", "popular", or "newest".
                sources: Optional list to restrict search (e.g. ["thingiverse",
                    "myminifactory"]).  Omit to search all connected sources.

            Each result includes a ``source`` field identifying the marketplace.
            Results also include ``is_free``, ``has_printable_files`` (has G-code),
            and ``has_sliceable_files`` (has STL/3MF) hints.

            Use ``model_details`` with the ``id`` to inspect, ``model_files``
            to see downloadable files, and ``download_model`` to save locally.
            """
            from kiln.marketplaces.base import MarketplaceError
            from kiln.server import (
                _MARKETPLACE_SETUP_GUIDE,
                _THINGIVERSE_DEPRECATION_NOTICE,
                _error_dict,
                _init_marketplace_registry,
                _marketplace_registry,
                logger,
            )

            try:
                if _marketplace_registry.count == 0:
                    _init_marketplace_registry()

                if _marketplace_registry.count == 0:
                    return _error_dict(
                        _MARKETPLACE_SETUP_GUIDE,
                        code="NO_MARKETPLACES",
                    )

                results = _marketplace_registry.search_all(
                    query,
                    page=page,
                    per_page=per_page,
                    sort=sort,
                    sources=sources,
                )
                resp = {
                    "success": True,
                    "query": query,
                    "models": [r.to_dict() for r in results.models],
                    "count": len(results.models),
                    "page": page,
                    "sources": _marketplace_registry.connected,
                    "searched": results.searched,
                    "skipped": results.skipped,
                    "failed": results.failed,
                    "health_summary": results.summary,
                }
                # Surface deprecation notice when Thingiverse results are included.
                _tv_sources = results.searched or _marketplace_registry.connected
                if "thingiverse" in _tv_sources:
                    resp["deprecation_notices"] = {
                        "thingiverse": _THINGIVERSE_DEPRECATION_NOTICE,
                    }
                return resp
            except MarketplaceError as exc:
                return _error_dict(f"Failed to search models: {exc}. Check marketplace credentials are configured.")
            except Exception as exc:
                logger.exception("Unexpected error in search_all_models")
                return _error_dict(f"Unexpected error in search_all_models: {exc}", code="INTERNAL_ERROR")

        # ---------------------------------------------------------------
        # Thingiverse-specific tools
        # ---------------------------------------------------------------

        @mcp.tool()
        def search_models(
            query: str,
            page: int = 1,
            per_page: int = 10,
            sort: str = "relevant",
        ) -> dict:
            """Search Thingiverse for 3D-printable models.

            Args:
                query: Search keywords (e.g. "raspberry pi case", "benchy").
                page: Page number for pagination (1-based, default 1).
                per_page: Results per page (default 10, max 100).
                sort: Sort order — "relevant", "popular", "newest", or "makes".

            Returns a list of model summaries including name, creator, thumbnail,
            and download/like counts.  Use ``model_details`` with the ``id`` to
            get full information, and ``model_files`` to see downloadable files.
            """
            from kiln.marketplaces.thingiverse import ThingiverseError
            from kiln.server import (
                _THINGIVERSE_DEPRECATION_NOTICE,
                _error_dict,
                _get_thingiverse,
                logger,
            )

            try:
                client = _get_thingiverse()
                results = client.search(query, page=page, per_page=per_page, sort=sort)
                return {
                    "success": True,
                    "query": query,
                    "models": [r.to_dict() for r in results],
                    "count": len(results),
                    "page": page,
                    "deprecation_notice": _THINGIVERSE_DEPRECATION_NOTICE,
                }
            except (ThingiverseError, RuntimeError) as exc:
                return _error_dict(f"Failed to search Thingiverse: {exc}. Check that KILN_THINGIVERSE_TOKEN is set.")
            except Exception as exc:
                logger.exception("Unexpected error in search_models")
                return _error_dict(f"Unexpected error in search_models: {exc}", code="INTERNAL_ERROR")

        @mcp.tool()
        def model_details(thing_id: int) -> dict:
            """Get full details for a Thingiverse model.

            Args:
                thing_id: Numeric thing ID (from ``search_models`` results).

            Returns comprehensive metadata including description, instructions,
            license, tags, and file count.
            """
            from kiln.marketplaces.thingiverse import (
                ThingiverseError,
                ThingiverseNotFoundError,
            )
            from kiln.server import (
                _THINGIVERSE_DEPRECATION_NOTICE,
                _error_dict,
                _get_thingiverse,
                logger,
            )

            try:
                client = _get_thingiverse()
                thing = client.get_thing(thing_id)
                return {
                    "success": True,
                    "model": thing.to_dict(),
                    "deprecation_notice": _THINGIVERSE_DEPRECATION_NOTICE,
                }
            except ThingiverseNotFoundError:
                return _error_dict(f"Model {thing_id} not found.", code="NOT_FOUND")
            except (ThingiverseError, RuntimeError) as exc:
                return _error_dict(f"Failed to get model details: {exc}. Check that KILN_THINGIVERSE_TOKEN is set.")
            except Exception as exc:
                logger.exception("Unexpected error in model_details")
                return _error_dict(f"Unexpected error in model_details: {exc}", code="INTERNAL_ERROR")

        @mcp.tool()
        def model_files(thing_id: int) -> dict:
            """List downloadable files for a Thingiverse model.

            Args:
                thing_id: Numeric thing ID.

            Returns a list of files with name, size, and download URL.
            Use ``download_model`` with the ``file_id`` to save a file locally.
            """
            from kiln.marketplaces.thingiverse import (
                ThingiverseError,
                ThingiverseNotFoundError,
            )
            from kiln.server import (
                _THINGIVERSE_DEPRECATION_NOTICE,
                _error_dict,
                _get_thingiverse,
                logger,
            )

            try:
                client = _get_thingiverse()
                files = client.get_files(thing_id)
                return {
                    "success": True,
                    "thing_id": thing_id,
                    "files": [f.to_dict() for f in files],
                    "count": len(files),
                    "deprecation_notice": _THINGIVERSE_DEPRECATION_NOTICE,
                }
            except ThingiverseNotFoundError:
                return _error_dict(f"Model {thing_id} not found.", code="NOT_FOUND")
            except (ThingiverseError, RuntimeError) as exc:
                return _error_dict(f"Failed to list model files: {exc}. Check that KILN_THINGIVERSE_TOKEN is set.")
            except Exception as exc:
                logger.exception("Unexpected error in model_files")
                return _error_dict(f"Unexpected error in model_files: {exc}", code="INTERNAL_ERROR")

        @mcp.tool()
        def download_model(
            file_id: int | None = None,
            dest_dir: str | None = None,
            file_name: str | None = None,
            model_id: str | None = None,
            source: str = "thingiverse",
            download_all: bool = False,
        ) -> dict:
            """Download model file(s) from a marketplace to local storage.

            **Community models are unverified.** Always preview dimensions and
            validate the mesh (``validate_generated_mesh``) before printing.
            Models with high download counts and positive ratings are generally
            safer.  AI-generated or untested designs can damage delicate printer
            hardware — prefer proven blueprints when possible.

            Args:
                file_id: Numeric file ID (from ``model_files`` results).  If
                    omitted and ``model_id`` is provided, downloads all files
                    for the model.
                dest_dir: Local directory to save the file in (default:
                    the system temp directory).
                file_name: Override the saved file name (single-file mode only).
                    Defaults to the original name from the marketplace.
                model_id: Model/thing ID.  When ``file_id`` is omitted,
                    all files for this model are downloaded.
                source: Marketplace source — ``"thingiverse"`` (default),
                    ``"myminifactory"``, etc.
                download_all: When True, downloads all files for the model
                    regardless of whether ``file_id`` is provided.

            After downloading, validate with ``validate_generated_mesh``, then
            upload to a printer with ``upload_file`` and print with ``start_print``.
            """
            import os
            import tempfile
            from pathlib import Path

            from kiln.marketplaces.base import (
                MarketplaceError,
            )
            from kiln.marketplaces.base import (
                MarketplaceNotFoundError as MktNotFoundError,
            )
            from kiln.marketplaces.thingiverse import (
                ThingiverseError,
                ThingiverseNotFoundError,
            )
            from kiln.server import (
                _THINGIVERSE_DEPRECATION_NOTICE,
                _check_auth,
                _check_disk_space,
                _error_dict,
                _get_thingiverse,
                _init_marketplace_registry,
                _marketplace_registry,
                logger,
            )

            if dest_dir is None:
                dest_dir = os.path.join(tempfile.gettempdir(), "kiln_downloads")

            if err := _check_auth("files"):
                return err

            # --- Path traversal guard ------------------------------------------
            # Constrain dest_dir to safe locations so an agent cannot write to
            # arbitrary directories like /etc or ~/.ssh.
            _resolved = Path(dest_dir).resolve()
            _allowed_roots = (
                Path(tempfile.gettempdir()).resolve(),
                Path.home().resolve(),
                Path.cwd().resolve(),
            )
            if not any(
                _resolved == root or _resolved.is_relative_to(root)
                for root in _allowed_roots
            ):
                return _error_dict(
                    "dest_dir must be within /tmp/, home directory, or current "
                    f"working directory. Got: {dest_dir}",
                    code="INVALID_PATH",
                )
            # -------------------------------------------------------------------

            if disk_err := _check_disk_space(dest_dir):
                return disk_err
            try:
                # Multi-file download: model_id provided without file_id, or download_all
                if (file_id is None or download_all) and model_id is not None:
                    if _marketplace_registry.count == 0:
                        _init_marketplace_registry()

                    mkt = _marketplace_registry.get(source)
                    if not mkt.supports_download:
                        return _error_dict(
                            f"{mkt.display_name} does not support direct downloads.",
                            code="UNSUPPORTED",
                        )

                    files = mkt.get_files(str(model_id))
                    if not files:
                        return _error_dict(
                            f"No files found for model {model_id} on {source}.",
                            code="NOT_FOUND",
                        )

                    downloaded: list[dict] = []
                    errors: list[dict] = []
                    for mf in files:
                        try:
                            path = mkt.download_file(
                                mf.id,
                                dest_dir,
                                file_name=None,
                            )
                            downloaded.append(
                                {
                                    "file_id": mf.id,
                                    "file_name": mf.name,
                                    "local_path": path,
                                }
                            )
                        except (MarketplaceError, RuntimeError) as exc:
                            errors.append(
                                {
                                    "file_id": mf.id,
                                    "file_name": mf.name,
                                    "error": str(exc),
                                }
                            )

                    dl_resp = {
                        "success": len(downloaded) > 0,
                        "model_id": model_id,
                        "source": source,
                        "downloaded": downloaded,
                        "errors": errors,
                        "total_files": len(files),
                        "downloaded_count": len(downloaded),
                        "verification_status": "unverified",
                        "safety_notice": (
                            "These are community-uploaded models and have NOT been "
                            "verified for print safety or quality. Validate each mesh "
                            "with validate_generated_mesh before printing. Prefer "
                            "proven models with high download counts."
                        ),
                        "message": (f"Downloaded {len(downloaded)}/{len(files)} files from {source} to {dest_dir}"),
                    }
                    if source == "thingiverse":
                        dl_resp["deprecation_notice"] = _THINGIVERSE_DEPRECATION_NOTICE
                    return dl_resp

                # Single-file download (legacy Thingiverse path)
                if file_id is None:
                    return _error_dict(
                        "Either file_id or model_id must be provided.",
                        code="INVALID_INPUT",
                    )
                client = _get_thingiverse()
                path = client.download_file(file_id, dest_dir, file_name=file_name)
                return {
                    "success": True,
                    "file_id": file_id,
                    "local_path": path,
                    "verification_status": "unverified",
                    "safety_notice": (
                        "This is a community-uploaded model and has NOT been "
                        "verified for print safety or quality. Validate the mesh "
                        "with validate_generated_mesh before printing. Prefer "
                        "proven models with high download counts."
                    ),
                    "deprecation_notice": _THINGIVERSE_DEPRECATION_NOTICE,
                    "message": f"Downloaded to {path}",
                }
            except (ThingiverseNotFoundError, MktNotFoundError):
                return _error_dict(
                    f"File {file_id or model_id} not found on {source}.",
                    code="NOT_FOUND",
                )
            except (ThingiverseError, MarketplaceError, RuntimeError) as exc:
                return _error_dict(
                    f"Failed to download model: {exc}. Check marketplace credentials and that the model/file ID is correct."
                )
            except Exception as exc:
                logger.exception("Unexpected error in download_model")
                return _error_dict(f"Unexpected error in download_model: {exc}", code="INTERNAL_ERROR")

        @mcp.tool()
        def browse_models(
            browse_type: str = "popular",
            page: int = 1,
            per_page: int = 10,
            category: str | None = None,
        ) -> dict:
            """Browse Thingiverse models by popularity, recency, or category.

            Args:
                browse_type: One of "popular", "newest", or "featured".
                page: Page number (1-based, default 1).
                per_page: Results per page (default 10, max 100).
                category: Optional category slug to filter by (e.g. "3d-printing",
                    "art").  Use ``list_categories`` to see available slugs.

            Returns model summaries similar to ``search_models``.
            """
            from kiln.marketplaces.thingiverse import ThingiverseError
            from kiln.server import _error_dict, _get_thingiverse, logger

            try:
                client = _get_thingiverse()

                if category:
                    results = client.category_things(category, page=page, per_page=per_page)
                elif browse_type == "popular":
                    results = client.popular(page=page, per_page=per_page)
                elif browse_type == "newest":
                    results = client.newest(page=page, per_page=per_page)
                elif browse_type == "featured":
                    results = client.featured(page=page, per_page=per_page)
                else:
                    return _error_dict(
                        f"Unknown browse_type: {browse_type!r}.  Supported: 'popular', 'newest', 'featured'.",
                        code="INVALID_ARGS",
                    )

                return {
                    "success": True,
                    "browse_type": browse_type if not category else f"category:{category}",
                    "models": [r.to_dict() for r in results],
                    "count": len(results),
                    "page": page,
                }
            except (ThingiverseError, RuntimeError) as exc:
                return _error_dict(f"Failed to browse models: {exc}. Check that KILN_THINGIVERSE_TOKEN is set.")
            except Exception as exc:
                logger.exception("Unexpected error in browse_models")
                return _error_dict(f"Unexpected error in browse_models: {exc}", code="INTERNAL_ERROR")

        @mcp.tool()
        def list_model_categories() -> dict:
            """List available Thingiverse content categories.

            Returns category names and slugs.  Pass a slug to
            ``browse_models(category=...)`` to browse models in that category.
            """
            from kiln.marketplaces.thingiverse import ThingiverseError
            from kiln.server import _error_dict, _get_thingiverse, logger

            try:
                client = _get_thingiverse()
                cats = client.list_categories()
                return {
                    "success": True,
                    "categories": [c.to_dict() for c in cats],
                    "count": len(cats),
                }
            except (ThingiverseError, RuntimeError) as exc:
                return _error_dict(f"Failed to list categories: {exc}. Check that KILN_THINGIVERSE_TOKEN is set.")
            except Exception as exc:
                logger.exception("Unexpected error in list_model_categories")
                return _error_dict(f"Unexpected error in list_model_categories: {exc}", code="INTERNAL_ERROR")

        # ---------------------------------------------------------------
        # Marketplace status & diagnostics
        # ---------------------------------------------------------------

        @mcp.tool()
        def marketplace_status() -> dict:
            """Check which 3D model marketplaces are connected and available.

            Returns the list of configured marketplace sources, their
            connection status, and whether credentials are present.  Use
            this to verify marketplace access before searching or
            downloading models.
            """

            # Import server internals lazily to avoid circular imports
            try:
                from kiln.server import (
                    _init_marketplace_registry,
                    _marketplace_registry,
                )
            except ImportError:
                return {
                    "success": False,
                    "error": {
                        "code": "IMPORT_ERROR",
                        "message": "Could not access marketplace registry.",
                        "retryable": False,
                    },
                }

            try:
                if _marketplace_registry.count == 0:
                    _init_marketplace_registry()

                import os

                sources = {
                    "thingiverse": bool(os.environ.get("KILN_THINGIVERSE_TOKEN")),
                    "myminifactory": bool(os.environ.get("KILN_MMF_API_KEY")),
                    "cults3d": bool(os.environ.get("KILN_CULTS3D_USERNAME") and os.environ.get("KILN_CULTS3D_API_KEY")),
                }

                return {
                    "success": True,
                    "connected_count": _marketplace_registry.count,
                    "connected": _marketplace_registry.connected,
                    "credentials_configured": {name: configured for name, configured in sources.items()},
                    "message": (
                        f"{_marketplace_registry.count} marketplace(s) connected"
                        if _marketplace_registry.count > 0
                        else (
                            "No marketplaces configured. To enable model search, set API keys for at least one:\n"
                            "1. MyMiniFactory (recommended) — https://myminifactory.com/settings/developer"
                            " → export KILN_MMF_API_KEY=your_key\n"
                            "2. Cults3D (search only) — https://cults3d.com/en/api/keys"
                            " → export KILN_CULTS3D_USERNAME=your_username && export KILN_CULTS3D_API_KEY=your_key\n"
                            "3. Thingiverse (deprecated) — https://www.thingiverse.com/apps/create"
                            " → export KILN_THINGIVERSE_TOKEN=your_token"
                        )
                    ),
                }
            except Exception as exc:
                _logger.exception("Error in marketplace_status")
                return {
                    "success": False,
                    "error": {
                        "code": "INTERNAL_ERROR",
                        "message": f"Unexpected error: {exc}",
                        "retryable": True,
                    },
                }

        @mcp.tool()
        def marketplace_diagnostics() -> dict:
            """Run connectivity checks against all configured marketplaces.

            Performs a lightweight probe (empty search) against each
            connected marketplace and reports which ones are reachable.
            Useful for debugging download failures.
            """
            try:
                from kiln.server import (
                    _init_marketplace_registry,
                    _marketplace_registry,
                )
            except ImportError:
                return {
                    "success": False,
                    "error": {
                        "code": "IMPORT_ERROR",
                        "message": "Could not access marketplace registry.",
                        "retryable": False,
                    },
                }

            try:
                if _marketplace_registry.count == 0:
                    _init_marketplace_registry()

                if _marketplace_registry.count == 0:
                    return {
                        "success": False,
                        "error": {
                            "code": "NO_MARKETPLACES",
                            "message": (
                                "No marketplace credentials configured. To enable model search, set API keys for at least one:\n"
                                "1. MyMiniFactory (recommended) — https://myminifactory.com/settings/developer"
                                " → export KILN_MMF_API_KEY=your_key\n"
                                "2. Cults3D (search only) — https://cults3d.com/en/api/keys"
                                " → export KILN_CULTS3D_USERNAME=your_username && export KILN_CULTS3D_API_KEY=your_key\n"
                                "3. Thingiverse (deprecated) — https://www.thingiverse.com/apps/create"
                                " → export KILN_THINGIVERSE_TOKEN=your_token"
                            ),
                            "retryable": False,
                        },
                    }

                results = _marketplace_registry.search_all(
                    "benchy",
                    page=1,
                    per_page=1,
                )
                return {
                    "success": True,
                    "searched": results.searched,
                    "failed": results.failed,
                    "skipped": results.skipped,
                    "summary": results.summary,
                    "message": (f"Probed {len(results.searched)} marketplace(s). {len(results.failed)} failure(s)."),
                }
            except Exception as exc:
                _logger.exception("Error in marketplace_diagnostics")
                return {
                    "success": False,
                    "error": {
                        "code": "INTERNAL_ERROR",
                        "message": f"Unexpected error: {exc}",
                        "retryable": True,
                    },
                }

        _logger.debug("Registered marketplace tools (search, browse, download, status, diagnostics)")


plugin = _MarketplaceToolsPlugin()
