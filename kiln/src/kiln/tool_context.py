"""Which MCP tool is running right now, and what it was called with.

Set by the tool-dispatch chokepoint in ``kiln.server`` for the duration of
one call (and by kiln-pro's REST door, which calls tool functions directly)
and read by anything that wants to attribute an event to the tool the caller
actually reached for — the tier-denial counter being the first.  Gates are
called with human prose (``check_business("This team feature")``) and a
counter keyed by that prose ranks sentences instead of doors; the tool name
is the stable key every other per-tool map on the machine already uses, so a
denial can be read against the calls beside it.

The arguments ride along for the make doors' goal join
(``kiln.front_door.join_goal``): a make that named no goal is kept in a
session of its own, and a product generator's session records what the
caller asked for (its material) rather than the generator's default.  They
are a read-only snapshot, reset with the name when the call ends.

Kept in its own tiny module so kiln-pro can import it without booting the
server, and so a missing attribute on an older public install degrades to
``None`` rather than an ImportError at the gate.
"""

from __future__ import annotations

from collections.abc import Mapping
from contextvars import ContextVar, Token
from types import MappingProxyType
from typing import Any

_Call = tuple[str, Mapping[str, Any]]

_current_call: ContextVar[_Call | None] = ContextVar("kiln_current_tool_call", default=None)


def current_tool_name() -> str | None:
    """The tool being dispatched on this context, or ``None`` outside one."""
    call = _current_call.get()
    return call[0] if call else None


def current_tool_arguments() -> dict[str, Any]:
    """A copy of the arguments the running tool was called with, or ``{}``."""
    call = _current_call.get()
    return dict(call[1]) if call else {}


def enter_tool(name: str, arguments: Mapping[str, Any] | None = None) -> Token[_Call | None]:
    """Mark ``name`` as the running tool; pair with :func:`leave_tool`."""
    return _current_call.set((name, MappingProxyType(dict(arguments or {}))))


def leave_tool(token: Token[_Call | None]) -> None:
    _current_call.reset(token)
