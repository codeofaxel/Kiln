"""A saved goal's id under its argument name, and under the earlier one.

``design_session`` returns a ``goal_id`` and every tool that takes a saved
goal declares it under that name.  ``brief_id`` is the earlier spelling of
the same argument.  The tool dispatcher calls :func:`as_declared` before it
judges a call's arguments, so an id sent under the earlier spelling reaches
the argument the called tool declares.  The reply of such a call carries
:data:`RESULT_KEY`, naming the argument the tool takes.

What stays refused: two different ids in one call (:func:`conflict` words
it), either name on a tool that takes no goal, and every argument a tool
does not declare.  Every door that runs a tool applies the same rule: the
dispatcher, the relay from Kiln's servers to this computer, and the hosted
tool route.

A tool that still declares the earlier spelling is read the other way
round, so a plugin written against it keeps working.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from kiln.front_door import EARLIER_GOAL_PARAM, GOAL_PARAM

logger = logging.getLogger(__name__)

#: The argument name for a saved goal's id, then its earlier spelling.
GOAL_ID_NAMES: tuple[str, str] = (GOAL_PARAM, EARLIER_GOAL_PARAM)

#: The structured key the reply of a renamed call carries.
RESULT_KEY = "argument_note"

_UNSET = (None, "")


def declared_by(tool: Any) -> set[str] | None:
    """The argument names a registered tool declares, or None when unreadable."""
    properties = (getattr(tool, "parameters", None) or {}).get("properties")
    return set(properties) if isinstance(properties, dict) else None


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


def conflict(tool: str, declared: set[str] | None, arguments: Any) -> str | None:
    """The refusal for a call that names two different goals, else ``None``."""
    if declared is None or not isinstance(arguments, dict):
        return None
    takes = [name for name in GOAL_ID_NAMES if name in declared]
    if len(takes) != 1:
        return None
    first, second = (arguments.get(name) for name in GOAL_ID_NAMES)
    if first in _UNSET or second in _UNSET or first == second:
        return None
    return (
        f"{tool} was given two different goals: {GOAL_ID_NAMES[0]} {first!r} and "
        f"{GOAL_ID_NAMES[1]} {second!r}. Both arguments name one saved goal's id, "
        f"and {tool} takes it as {takes[0]}. Pass one of them."
    )


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
