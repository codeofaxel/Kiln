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

Acceptance is by SHAPE (a short lowercase token), not by a closed set:
a launcher that embeds Kiln may declare a door of its own — via
:func:`set_surface` or the ``KILN_SURFACE`` environment variable —
without this file having to know it exists.  The aggregation side
whitelists what it renders, so an unrecognised token can never mint a
row there; locally it is carried as-is, which beats collapsing a real
door into "unknown".

A process that never declares — the bridge supervisor, a test, kiln
imported as a library — reads ``"unknown"``.  That absence is honest
and must stay distinguishable from the real surfaces downstream.
"""

from __future__ import annotations

import logging
import os
import re

_logger = logging.getLogger(__name__)

#: The doors THIS repo declares.  Shared words with kiln-pro's
#: ``PRESENCE_SURFACES`` mean the same doors; do not repurpose a word
#: here without checking that side.
KNOWN_SURFACES = frozenset({"cli", "mcp", "web"})

#: What ``get_surface`` answers when no entry point declared itself.
UNKNOWN = "unknown"

# A plausible surface token: short, lowercase, no path/format fuzz.
# "unknown" deliberately doesn't get declared through set_surface — it
# is the absence of a declaration, not a door.
_SURFACE_RE = re.compile(r"^[a-z][a-z0-9_]{1,15}$")

#: Environment override, read at every resolve.  This is how a launcher
#: that spawns ``kiln serve`` as a child declares the child's real door
#: without patching it: the entry points still self-declare, but an
#: explicit ``KILN_SURFACE`` outranks them — the launcher knows what it
#: is; the child only knows how it was exec'd.
_ENV_VAR = "KILN_SURFACE"

_surface: str | None = None


def set_surface(surface: str) -> None:
    """Declare this process's surface.  Entry points only.

    Later declarations win, deliberately: ``kiln serve`` enters through
    the CLI door before the server's own ``main`` runs (see module
    docstring).  A malformed value is dropped, not raised on —
    telemetry plumbing never breaks a process start.
    """
    global _surface  # noqa: PLW0603
    if not isinstance(surface, str) or not _SURFACE_RE.match(surface) \
            or surface == UNKNOWN:
        _logger.debug("set_surface(%r): not a surface token, ignored", surface)
        return
    _surface = surface


def get_surface() -> str:
    """The resolved surface, or ``"unknown"`` when nothing declared.

    Precedence: the ``KILN_SURFACE`` environment variable (a launcher's
    statement about this process), then the entry point's own
    declaration, then ``"unknown"``.
    """
    env = os.environ.get(_ENV_VAR, "").strip().lower()
    if env and _SURFACE_RE.match(env) and env != UNKNOWN:
        return env
    return _surface or UNKNOWN


def reset_surface() -> None:
    """Forget the declaration.  Test isolation only."""
    global _surface  # noqa: PLW0603
    _surface = None
