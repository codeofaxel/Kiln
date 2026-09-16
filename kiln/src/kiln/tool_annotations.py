"""Tool annotations every Kiln surface inherits.

MCP hosts decide from ``ToolAnnotations`` whether a call needs the user's
confirmation: a read-only tool can run unprompted, a destructive one always
asks.  Anthropic's Connectors Directory review requires every served tool to
carry a ``title`` and the applicable ``readOnlyHint`` / ``destructiveHint``.

Annotate at the definition, not in a wire-layer table, so the local server,
the MCPB bundle, and the hosted connector all say the same thing about a
tool.  Three kinds cover the surface:

* ``read_only``   — computes, lists, estimates, searches, or renders a
                    preview.  Nothing the user owns changes.
* ``creates``     — mints a new artifact (a design, a mesh, a drawing).
                    Not destructive: nothing existing is altered or spent.
* ``destructive`` — spends money or a quota, cancels, or changes a running
                    print.  Hosts prompt before every call.

Usage::

    @mcp.tool(annotations=read_only("Estimate print cost"))
    def estimate_print_cost_from_mesh(...): ...
"""

from __future__ import annotations

from mcp.types import ToolAnnotations


def read_only(title: str) -> ToolAnnotations:
    """A tool that changes nothing the user owns."""
    return ToolAnnotations(title=title, readOnlyHint=True, destructiveHint=False)


def creates(title: str) -> ToolAnnotations:
    """A tool that mints a new artifact without altering or spending anything."""
    return ToolAnnotations(title=title, readOnlyHint=False, destructiveHint=False)


def destructive(title: str) -> ToolAnnotations:
    """A tool that spends, cancels, or alters something — hosts confirm first."""
    return ToolAnnotations(title=title, readOnlyHint=False, destructiveHint=True)
