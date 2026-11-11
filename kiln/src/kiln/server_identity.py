"""What Kiln's MCP servers call themselves, and the logo they offer.

Every server Kiln builds passes :func:`identity_kwargs` to its constructor,
so an app that reads the initialize reply sees one display name ("Kiln")
and one logo at every door.  ``SERVER_NAME`` is the machine identifier and
stays lowercase; ``SERVER_TITLE`` is what a person is shown.
"""

from __future__ import annotations

import inspect
from typing import Any

SERVER_NAME = "kiln"
SERVER_TITLE = "Kiln"
WEBSITE_URL = "https://kiln3d.com"

#: (address, media type, sizes) for each logo file, raster first.
ICON_SOURCES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("https://kiln3d.com/assets/kiln-mark-1024.png", "image/png", ("1024x1024",)),
    ("https://kiln3d.com/assets/kiln-favicon-256.svg", "image/svg+xml", ("any",)),
)


def _kiln_version() -> str:
    try:
        from kiln import __version__
    except ImportError:
        return ""
    return str(__version__ or "")


def icons(sources: Any = ICON_SOURCES) -> list[Any]:
    """The logo list in the SDK's own type; empty when the SDK has none."""
    try:
        from mcp.types import Icon
    except ImportError:
        return []
    built = []
    for src, mime_type, sizes in sources:
        try:
            built.append(Icon(src=src, mimeType=mime_type, sizes=list(sizes)))
        except Exception:  # noqa: BLE001 - a logo never stops a server starting
            return []
    return built


def identity_kwargs(server_cls: Any, *, icon_sources: Any = ICON_SOURCES) -> dict[str, Any]:
    """Display name, version, website and logo, limited to what ``server_cls`` accepts."""
    try:
        accepted = inspect.signature(server_cls.__init__).parameters
    except (TypeError, ValueError):
        return {}
    offered: dict[str, Any] = {"title": SERVER_TITLE, "website_url": WEBSITE_URL}
    version = _kiln_version()
    if version:
        offered["version"] = version
    logo = icons(icon_sources)
    if logo:
        offered["icons"] = logo
    return {key: value for key, value in offered.items() if key in accepted}
