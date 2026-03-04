"""Material catalog tools plugin — brand database, search, purchase URLs.

Registers MCP tools for querying the curated material catalog of 50+
filament and resin products: search by brand/type, get purchase links,
find compatible materials, and fuzzy-match spool metadata.

Auto-discovered by :func:`~kiln.plugin_loader.register_all_plugins` —
no manual imports needed.
"""

from __future__ import annotations

import logging
from typing import Any

_logger = logging.getLogger(__name__)


class _MaterialCatalogToolsPlugin:
    """Material catalog tools — brand database, search, compatibility.

    Tools:
        - search_material_catalog
        - get_material_info
        - list_material_catalog
        - get_compatible_materials
        - get_material_purchase_urls
        - find_material_match
    """

    @property
    def name(self) -> str:
        return "material_catalog_tools"

    @property
    def description(self) -> str:
        return "Material brand catalog, search, and purchase URL tools"

    def register(self, mcp: Any) -> None:
        """Register material catalog tools with the MCP server."""

        @mcp.tool()
        def search_material_catalog(query: str) -> dict:
            """Search the material catalog by brand, type, or keyword.

            Performs a case-insensitive multi-token search across vendor
            names, material types, and notes.  All tokens must match for
            an entry to be returned.

            Args:
                query: Search text (e.g. ``"Hatchbox PLA"``, ``"PETG"``).
            """
            import kiln.server as _srv

            try:
                from kiln.material_catalog import search_catalog

                if not query or not query.strip():
                    return _srv._error_dict("Query cannot be empty", code="VALIDATION_ERROR")

                results = search_catalog(query)
                return {
                    "success": True,
                    "results": [r.to_dict() for r in results],
                    "count": len(results),
                    "query": query,
                }
            except Exception as exc:
                _logger.exception("Unexpected error in search_material_catalog")
                return _srv._error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")

        @mcp.tool()
        def get_material_info(material_id: str) -> dict:
            """Get detailed information for a specific material by ID.

            Returns the full catalog entry including vendor, type, family,
            variants, price range, weight options, and purchase sources.

            Args:
                material_id: Catalog ID (e.g. ``"hatchbox_pla"``, ``"esun_petg"``).
            """
            import kiln.server as _srv

            try:
                from kiln.material_catalog import get_material_entry

                entry = get_material_entry(material_id)
                if entry is None:
                    return _srv._error_dict(
                        f"Material '{material_id}' not found in catalog",
                        code="NOT_FOUND",
                    )
                return {"success": True, "material": entry.to_dict()}
            except Exception as exc:
                _logger.exception("Unexpected error in get_material_info")
                return _srv._error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")

        @mcp.tool()
        def list_material_catalog() -> dict:
            """List all material IDs in the catalog.

            Returns a sorted list of every material identifier available
            in the built-in catalog database.
            """
            import kiln.server as _srv

            try:
                from kiln.material_catalog import list_catalog_ids

                ids = list_catalog_ids()
                return {
                    "success": True,
                    "material_ids": ids,
                    "count": len(ids),
                }
            except Exception as exc:
                _logger.exception("Unexpected error in list_material_catalog")
                return _srv._error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")

        @mcp.tool()
        def get_compatible_materials(material_family: str) -> dict:
            """Find all materials in a given family (e.g. PLA, PETG, resin).

            Returns every catalog entry sharing the same material family,
            useful for finding compatible substitutes.

            Args:
                material_family: Family name (e.g. ``"pla"``, ``"petg"``, ``"resin"``).
            """
            import kiln.server as _srv

            try:
                from kiln.material_catalog import (
                    get_compatible_materials as _get_compat,
                )

                results = _get_compat(material_family)
                return {
                    "success": True,
                    "family": material_family.lower(),
                    "materials": [r.to_dict() for r in results],
                    "count": len(results),
                }
            except Exception as exc:
                _logger.exception("Unexpected error in get_compatible_materials")
                return _srv._error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")

        @mcp.tool()
        def get_material_purchase_urls(
            material_id: str,
            color: str | None = None,
        ) -> dict:
            """Get purchase URLs for a material.

            Returns Amazon search links and manufacturer URLs.  If a colour
            is specified, it is substituted into the Amazon search template.

            Args:
                material_id: Catalog ID (e.g. ``"hatchbox_pla"``).
                color: Optional colour for URL personalisation.
            """
            import kiln.server as _srv

            try:
                from kiln.material_catalog import get_purchase_urls

                urls = get_purchase_urls(material_id, color=color)
                if not urls:
                    return _srv._error_dict(
                        f"Material '{material_id}' not found in catalog",
                        code="NOT_FOUND",
                    )
                return {
                    "success": True,
                    "material_id": material_id,
                    "color": color,
                    "urls": urls,
                }
            except Exception as exc:
                _logger.exception("Unexpected error in get_material_purchase_urls")
                return _srv._error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")

        @mcp.tool()
        def find_material_match(
            vendor: str | None = None,
            material_type: str | None = None,
            color: str | None = None,
        ) -> dict:
            """Fuzzy-match a catalog entry from spool metadata.

            Matches against vendor and material type (case-insensitive
            substring).  If multiple entries match and a colour is provided,
            prefers entries whose variants include that colour.

            Args:
                vendor: Brand name (e.g. ``"Hatchbox"``).
                material_type: Product name (e.g. ``"PLA"``, ``"PLA+"``).
                color: Optional colour preference for tie-breaking.
            """
            import kiln.server as _srv

            try:
                from kiln.material_catalog import find_matching_entry

                entry = find_matching_entry(
                    vendor=vendor,
                    material_type=material_type,
                    color=color,
                )
                if entry is None:
                    return {
                        "success": True,
                        "match": None,
                        "message": "No matching material found in catalog",
                    }
                return {
                    "success": True,
                    "match": entry.to_dict(),
                }
            except Exception as exc:
                _logger.exception("Unexpected error in find_material_match")
                return _srv._error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")

        _logger.debug("Registered material catalog tools")


plugin = _MaterialCatalogToolsPlugin()
