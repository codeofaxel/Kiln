"""A saved goal's id under either of its two argument names.

``design_session`` returns and takes a ``goal_id``; every make tool takes
that same id as ``brief_id``.  The tool dispatcher calls :func:`as_declared`
before it judges a call's arguments, so an id handed over under the other
tool's name reaches the argument the called tool declares.  The reply of
such a call carries :data:`RESULT_KEY`, naming the argument the tool takes.

What stays refused: two different ids in one call, either name on a tool
that takes no goal, and every argument a tool does not declare.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from kiln.front_door import GOAL_PARAM

logger = logging.getLogger(__name__)

#: The two argument names for one saved goal's id.
GOAL_ID_NAMES: tuple[str, str] = ("goal_id", GOAL_PARAM)

#: The structured key the reply of a renamed call carries.
RESULT_KEY = "argument_note"

_UNSET = (None, "")


def as_declared(
    declared: set[str] | None, arguments: Any
) -> tuple[Any, tuple[str, str] | None]:
    """``arguments`` with the goal's id under the name the tool declares.

    Returns the arguments and ``(name passed, name declared)`` when a rename
    happened, else the arguments untouched and ``None``.
    """
    if declared is None or not isinstance(arguments, dict):
        return arguments, None
    takes = [name for name in GOAL_ID_NAMES if name in declared]
    if len(takes) != 1:
        return arguments, None
    wanted = takes[0]
    other = GOAL_ID_NAMES[1] if wanted == GOAL_ID_NAMES[0] else GOAL_ID_NAMES[0]
    if other not in arguments:
        return arguments, None
    given, held = arguments[other], arguments.get(wanted)
    if given not in _UNSET and held not in _UNSET and given != held:
        return arguments, None
    renamed = {key: value for key, value in arguments.items() if key != other}
    renamed[wanted] = held if held not in _UNSET else given
    return renamed, (other, wanted)


def note(tool: str, passed: str, wanted: str) -> str:
    return (
        f"`{passed}` was read as `{wanted}`: both name the same saved goal, "
        f"and {tool} takes it as `{wanted}`."
    )


def install(mcp: Any, declared_arguments: Callable[[str], set[str] | None]) -> bool:
    """Add the note to the reply of a call whose goal argument was renamed."""

    def _attach(inner: Any, ctx: Any, name: str | None, arguments: dict | None) -> None:
        try:
            if not name:
                return
            _, renamed = as_declared(declared_arguments(name), arguments)
            if renamed is None or arguments.get(renamed[0]) in _UNSET:
                return
            from kiln.local_stage import _result_as_dict
            from kiln.mcp_compat import result_structured_content, set_result_structured_content

            sc = result_structured_content(inner)
            sc = dict(sc) if isinstance(sc, dict) else (_result_as_dict(inner) or {})
            if not sc:
                return
            sc[RESULT_KEY] = note(name, *renamed)
            set_result_structured_content(inner, sc)
        except Exception:  # noqa: BLE001 - a note never breaks a result
            logger.debug("goal argument note not attached", exc_info=True)

    try:
        from kiln.mcp_compat import wrap_call_tool_result

        return bool(wrap_call_tool_result(mcp, _attach))
    except Exception:  # noqa: BLE001 - optional surface, never fatal
        logger.debug("goal argument note hook not installed", exc_info=True)
        return False
