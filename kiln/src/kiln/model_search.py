"""One model search: Kiln's own public designs first, then every connected site.

``search_models`` and ``search_all_models`` both run :func:`search`.  Kiln's
public designs are asked of the same door an agent would call
(``search_public_designs``), so the order they come in is that door's own.  A
site that is not connected is not searched and is never a refusal on its own.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

#: The name Kiln's own public designs carry as a result's ``source`` and in
#: ``sources``.
KILN = "kiln"

SEARCH_UNAVAILABLE = "Kiln's model search couldn't be reached just now. Try again in a moment."
NO_MODEL_SOURCES = (
    "Kiln has nothing to search from this computer yet. Sign in to Kiln to "
    "search the designs people have made public (the same search as "
    "kiln3d.com/search), or connect a model site."
)


def _kiln_search_door() -> Callable[..., Any] | None:
    """The door that searches Kiln's public designs, or ``None`` when this
    install lists no such door."""
    from kiln.server import mcp

    try:
        tool = mcp._tool_manager.get_tool("search_public_designs")
    except Exception:  # noqa: BLE001 -- no registry yet: no door
        return None
    return getattr(tool, "fn", None) if tool is not None else None


def _as_model(design: dict[str, Any]) -> dict[str, Any] | None:
    """A public design in the shape every search result has."""
    username = design.get("username") or design.get("owner_username")
    slug = design.get("slug")
    if not isinstance(username, str) or not isinstance(slug, str) or not username or not slug:
        return None
    has_file = design.get("has_file") is True or design.get("can_download") is True
    return {
        "id": f"{username}/{slug}",
        "name": str(design.get("name") or slug),
        "url": f"https://kiln3d.com/{username}/{slug}",
        "creator": username,
        "source": KILN,
        "description": design.get("description") or "",
        "thumbnail": design.get("picture") or design.get("thumbnail_url"),
        "like_count": design.get("stars") if isinstance(design.get("stars"), int) else 0,
        "license": design.get("license") or "",
        "is_free": True,
        "price_cents": 0,
        "has_printable_files": False,
        "has_sliceable_files": has_file,
        "can_download": has_file,
        "printed_by": design.get("printed_by"),
        # How to open it: get_public_design(username, slug).
        "open_with": "get_public_design",
        "username": username,
        "slug": slug,
    }


def kiln_public_designs(query: str, page: int) -> dict[str, Any]:
    """Kiln's public designs for *query*: ``{"searched": True, "models": [...]}``,
    or ``{"searched": False, "why": ...}`` when the search gave no answer."""
    door = _kiln_search_door()
    if door is None:
        return {"searched": False, "why": "This version of Kiln lists no public-design search."}
    try:
        answer = door(query=query, page=page)
    except Exception:  # noqa: BLE001 -- no answer is a miss, said below
        logger.warning("model search: Kiln's public designs gave no answer", exc_info=True)
        return {"searched": False, "why": "Kiln's public designs could not be searched just now."}
    if not isinstance(answer, dict) or answer.get("status") == "error" or answer.get("success") is False:
        said = answer.get("error") if isinstance(answer, dict) else None
        if isinstance(said, dict):
            said = said.get("message")
        return {"searched": False, "why": str(said or "Kiln's public designs could not be searched just now.")}
    results = answer.get("results") if isinstance(answer.get("results"), dict) else {}
    models = [m for m in (_as_model(d) for d in results.get("designs") or [] if isinstance(d, dict)) if m]
    return {"searched": True, "models": models, "address": answer.get("address")}


def search(
    query: str, *, page: int = 1, per_page: int = 10, sort: str = "relevant",
    sources: list[str] | None = None,
) -> dict[str, Any]:
    """Search Kiln's public designs, then every connected model site.

    *sources* names the places to search (``"kiln"`` and any site); left out,
    every one is.  Answers the tool's whole reply.
    """
    import kiln.server as srv
    from kiln.runtime_env import is_hosted_multitenant

    wanted = [s.strip().lower() for s in sources or [] if isinstance(s, str) and s.strip()]
    registry = srv._marketplace_registry
    if registry.count == 0:
        srv._init_marketplace_registry()
        registry = srv._marketplace_registry

    models: list[dict[str, Any]] = []
    searched: list[str] = []
    not_searched: dict[str, str] = {}
    address = None
    if not wanted or KILN in wanted:
        kiln_side = kiln_public_designs(query, page)
        if kiln_side["searched"]:
            searched.append(KILN)
            models.extend(kiln_side["models"][: max(per_page, 0) or None])
            address = kiln_side.get("address")
        else:
            not_searched[KILN] = kiln_side["why"]

    site_names = [s for s in wanted if s != KILN]
    skipped: list[str] = []
    failed: list[str] = []
    summary = ""
    if registry.count and (not wanted or site_names):
        results = registry.search_all(
            query, page=page, per_page=per_page, sort=sort, sources=site_names or None,
        )
        models.extend(m.to_dict() for m in results.models)
        searched.extend(results.searched)
        skipped, failed, summary = list(results.skipped), list(results.failed), results.summary

    if not searched:
        # Nothing answered.  On Kiln's servers that is Kiln's to fix; on a
        # person's own computer it says how to get a search.
        if is_hosted_multitenant():
            return srv._error_dict(SEARCH_UNAVAILABLE, code="SEARCH_UNAVAILABLE", retryable=True)
        if failed or skipped:
            return srv._error_dict(
                "None of the model sites answered just now, so nothing was searched. Try again in a moment.",
                code="SEARCH_UNAVAILABLE", retryable=True,
                extra={"failed": failed, "skipped": skipped, "not_searched": not_searched},
            )
        return srv._error_dict(
            NO_MODEL_SOURCES, code="NO_MODEL_SOURCES",
            extra={"not_searched": not_searched, "how_to_connect_a_site": srv._MARKETPLACE_SETUP_GUIDE},
        )

    reply: dict[str, Any] = {
        "success": True,
        "query": query,
        "models": models,
        "count": len(models),
        "page": page,
        "sources": [KILN, *registry.connected],
        "searched": searched,
        "skipped": skipped,
        "failed": failed,
        "health_summary": summary,
    }
    if not_searched:
        reply["not_searched"] = not_searched
    if address:
        reply["kiln_search_address"] = address
    if "thingiverse" in searched:
        reply["deprecation_notices"] = {"thingiverse": srv._THINGIVERSE_DEPRECATION_NOTICE}
    return reply


__all__ = ["KILN", "kiln_public_designs", "search"]
