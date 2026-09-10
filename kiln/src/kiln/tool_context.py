"""Which MCP tool is running right now.

Set by the tool-dispatch chokepoint in ``kiln.server`` for the duration of
one call and read by anything that wants to attribute an event to the tool
the caller actually reached for — the tier-denial counter being the first.
Gates are called with human prose (``check_business("This team feature")``)
and a counter keyed by that prose ranks sentences instead of doors; the
tool name is the stable key every other per-tool map on the machine already
uses, so a denial can be read against the calls beside it.

Kept in its own tiny module so kiln-pro can import it without booting the
server, and so a missing attribute on an older public install degrades to
``None`` rather than an ImportError at the gate.
"""

from __future__ import annotations

from contextvars import ContextVar, Token

_current_tool_name: ContextVar[str | None] = ContextVar(
    "kiln_current_tool_name", default=None
)


def current_tool_name() -> str | None:
    """The tool being dispatched on this context, or ``None`` outside one."""
    return _current_tool_name.get()


def enter_tool(name: str) -> Token[str | None]:
    """Mark ``name`` as the running tool; pair with :func:`leave_tool`."""
    return _current_tool_name.set(name)


def leave_tool(token: Token[str | None]) -> None:
    _current_tool_name.reset(token)
