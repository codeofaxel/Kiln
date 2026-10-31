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

Every kind also states ``openWorldHint`` as an explicit boolean, because a
host that reads an unset hint has to guess.  It is ``False`` by default: a
Kiln tool works on Kiln's own catalogue, the person's own account, designs
and printers -- bounded places, even when Kiln's servers do the work.  Pass
``open_world=True`` for a tool that reaches public or open-ended places Kiln
does not own (searching outside model sites, a destination the caller
names).  Being networked is not the test; where the call can land is.

Usage::

    @mcp.tool(annotations=read_only("Estimate print cost"))
    def estimate_print_cost_from_mesh(...): ...

    @mcp.tool(annotations=read_only("Search all marketplaces", open_world=True))
    def search_all_models(...): ...
"""

from __future__ import annotations

from mcp.types import ToolAnnotations


def read_only(title: str, *, open_world: bool = False) -> ToolAnnotations:
    """A tool that changes nothing the user owns."""
    return ToolAnnotations(
        title=title, readOnlyHint=True, destructiveHint=False, openWorldHint=open_world
    )


def creates(title: str, *, open_world: bool = False) -> ToolAnnotations:
    """A tool that mints a new artifact without altering or spending anything."""
    return ToolAnnotations(
        title=title, readOnlyHint=False, destructiveHint=False, openWorldHint=open_world
    )


def destructive(title: str, *, open_world: bool = False) -> ToolAnnotations:
    """A tool that spends, cancels, or alters something — hosts confirm first."""
    return ToolAnnotations(
        title=title, readOnlyHint=False, destructiveHint=True, openWorldHint=open_world
    )


def served_kind(block: object, name: str) -> ToolAnnotations:
    """The kind a served tool's manifest entry states, rebuilt through the helpers.

    kiln-pro's manifest carries each tool's annotations as its definition
    stated them (``title`` and the three hints), so the stub an install
    without kiln-pro registers says what the tool itself says.  An entry
    that states no readable kind is registered as one that asks first: a
    tool whose effect is unknown is never offered as read-only.
    """
    if isinstance(block, dict):
        title = block.get("title")
        hints = [block.get(key) for key in ("readOnlyHint", "destructiveHint", "openWorldHint")]
        if isinstance(title, str) and title.strip() and all(isinstance(h, bool) for h in hints):
            ro, de, ow = hints
            if not (ro and de):
                make = read_only if ro else destructive if de else creates
                return make(title, open_world=ow)
    return destructive(name.replace("_", " ").capitalize())
