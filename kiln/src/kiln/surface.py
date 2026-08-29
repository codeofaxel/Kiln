"""Which door this process came in through: "cli", "mcp", or "web".

The CLI and the MCP server share every engine chokepoint (slicing,
printing, generation), so an event recorded there cannot tell which
surface the user was on — and per-call-site guessing is exactly the
CLI/MCP copy-paste drift this module exists to avoid (the CLI's own
``_map_printer_hint_to_profile_id`` fork went six months without
learning a single Bambu model because nothing shared).  The surface is
a fact about where the PROCESS started, so it is resolved once, at the
entry point, and read everywhere else.

Entry points that declare themselves:

* ``kiln.cli.main:main`` — the ``kiln`` / ``kiln3d`` console scripts
  and ``python -m kiln`` — declares ``"cli"``.
* ``kiln.server:main`` — reached by ``kiln serve``, by
  ``python -m kiln.server``, and by the mcpb bundle's launcher —
  declares ``"mcp"``.  ``kiln serve`` passes through the CLI door
  first, which is why the LAST declaration wins: the server's own
  entry point corrects the surface before any tool can dispatch.

``"web"`` is in the vocabulary but never set in this repo: the web app
reports through kiln-pro's beacons, which already use that word, and
the shared words must mean the same doors on both sides.  (kiln-pro's
presence vocabulary also has ``"mcp_connector"`` — the HOSTED remote
connector, a different door from the local MCP server this module
calls ``"mcp"``.)

A process that never declares — the bridge supervisor, a test, kiln
imported as a library — reads ``"unknown"``.  That absence is honest
and must stay distinguishable from the three real surfaces downstream.
"""

from __future__ import annotations

import logging

_logger = logging.getLogger(__name__)

#: The closed surface vocabulary.  Shared words with kiln-pro's
#: ``PRESENCE_SURFACES`` mean the same doors; do not add a value here
#: without checking that side.
SURFACES = frozenset({"cli", "mcp", "web"})

#: What ``get_surface`` answers when no entry point declared itself.
UNKNOWN = "unknown"

_surface: str | None = None


def set_surface(surface: str) -> None:
    """Declare this process's surface.  Entry points only.

    Later declarations win, deliberately: ``kiln serve`` enters through
    the CLI door before the server's own ``main`` runs (see module
    docstring).  A value outside :data:`SURFACES` is dropped, not
    raised on — telemetry plumbing never breaks a process start.
    """
    global _surface  # noqa: PLW0603
    if surface not in SURFACES:
        _logger.debug("set_surface(%r): not in %s, ignored", surface, sorted(SURFACES))
        return
    _surface = surface


def get_surface() -> str:
    """The declared surface, or ``"unknown"`` when nothing declared."""
    return _surface or UNKNOWN


def reset_surface() -> None:
    """Forget the declaration.  Test isolation only."""
    global _surface  # noqa: PLW0603
    _surface = None
