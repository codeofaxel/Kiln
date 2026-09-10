"""Argument shapes at the tool door — one helper, every door.

Two things every MCP tool needs and none should re-implement:

**JSON-carrying parameters arrive in three shapes.**  A parameter documented
as "a JSON string" is handed the string, the already-parsed object (agents
pass real objects, and FastMCP's own pre-parse turns a JSON string into one
before pydantic ever sees it), or nothing.  A tool typed ``overrides: str |
None`` raised on the first two — the SDK parses any string whose annotation
is not bare ``str``, then pydantic rejects the dict — so the docstring's own
example never worked (``reslice_with_overrides`` from v1.4.0 through
v1.4.1.1; three printer models on the founder dashboard's failure tile).
Such a parameter is annotated ``str | dict[str, Any] | None`` (or the list
twin) and its body calls :func:`parse_json_object` / :func:`parse_json_array`.

**The chokepoint half.**  An explicit ``null`` for a field that is not
Optional, and a bare string for a ``list[str]`` field, are both things an
agent does every day, and both used to escape pydantic as a stack trace the
agent had to read.  The dispatch wrapper in ``kiln.server`` calls
:func:`coerce_tool_arguments` for the two safe shapes and
:func:`invalid_arguments_envelope` for whatever is left, so a mis-shaped
call is a counted failure with the accepted parameters named, not a raise.
"""

from __future__ import annotations

import json
import types
import typing
from typing import Any

# Kiln's failure envelope, mirrored rather than imported: ``kiln.server``
# imports this module, and a door helper must not boot the server.
_JSON_ERROR_CODE = "VALIDATION_ERROR"


def _error(message: str, code: str = _JSON_ERROR_CODE) -> dict[str, Any]:
    return {
        "success": False,
        "error": {"code": code, "message": message, "retryable": False},
    }


def parse_json_object(
    value: Any, name: str
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """``(parsed, error)`` for a parameter that should carry a JSON object.

    ``None`` / ``""`` → ``(None, None)``: the caller treats it as omitted.
    A dict is returned as-is; a string is parsed.  Anything else — a list, a
    number, a string that is not JSON, JSON that is not an object — is an
    error envelope naming the parameter, never a raise.
    """
    if value is None or value == "":
        return None, None
    if isinstance(value, dict):
        return value, None
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            return None, _error(f"{name} is not valid JSON: {exc}")
        if not isinstance(parsed, dict):
            return None, _error(
                f"{name} must be a JSON object (key-value pairs), "
                f"got {type(parsed).__name__}."
            )
        return parsed, None
    return None, _error(
        f"{name} must be a JSON object (key-value pairs), "
        f"got {type(value).__name__}."
    )


def parse_json_array(
    value: Any, name: str
) -> tuple[list[Any] | None, dict[str, Any] | None]:
    """``(parsed, error)`` for a parameter that should carry a JSON array.

    Same contract as :func:`parse_json_object` with a list in place of a
    dict.  A tuple counts as a list.
    """
    if value is None or value == "":
        return None, None
    if isinstance(value, (list, tuple)):
        return list(value), None
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            return None, _error(f"{name} is not valid JSON: {exc}")
        if not isinstance(parsed, list):
            return None, _error(
                f"{name} must be a JSON array, got {type(parsed).__name__}."
            )
        return parsed, None
    return None, _error(
        f"{name} must be a JSON array, got {type(value).__name__}."
    )


# ---------------------------------------------------------------------------
# Chokepoint coercion
# ---------------------------------------------------------------------------


def _union_members(annotation: Any) -> tuple[Any, ...]:
    origin = typing.get_origin(annotation)
    if origin is typing.Union or origin is types.UnionType:
        return typing.get_args(annotation)
    return (annotation,)


def _allows_none(annotation: Any) -> bool:
    if annotation is Any or annotation is None or annotation is type(None):
        return True
    return any(m is type(None) for m in _union_members(annotation))


def _list_item_type(annotation: Any) -> Any | None:
    """The item type when ``annotation`` (or one union member) is a list."""
    for member in _union_members(annotation):
        origin = typing.get_origin(member)
        if member is list or origin is list:
            args = typing.get_args(member)
            return args[0] if args else Any
    return None


def _accepts_str(annotation: Any) -> bool:
    return any(m is str for m in _union_members(annotation))


def coerce_tool_arguments(
    arg_model: Any, arguments: dict[str, Any] | None
) -> dict[str, Any]:
    """Coerce the two safe shapes an agent sends every day.

    * ``null`` for a field that does not accept ``None`` → the key is dropped
      so the declared default applies (``printer_id: str = ""`` given
      ``null`` used to raise "Input should be a valid string").
    * a bare string for a ``list[...]`` field that does not also accept a
      string → wrapped as a one-element list (``filament_types="PLA"`` for
      ``filament_types: list[str]``).  A string that parses as a JSON array
      is left alone: the SDK's own pre-parse handles it.

    Anything else is returned untouched for pydantic to judge.  Never
    raises; an unreadable model means no coercion, not a broken call.
    """
    if not arguments:
        return dict(arguments or {})
    try:
        fields = arg_model.model_fields
    except Exception:  # noqa: BLE001 — an odd model shape must not block a call
        return dict(arguments)
    out = dict(arguments)
    for key, value in arguments.items():
        field = fields.get(key)
        if field is None:
            continue
        annotation = field.annotation
        if value is None and not _allows_none(annotation):
            if field.is_required():
                continue  # a required field: let pydantic say so
            out.pop(key, None)
            continue
        if (
            isinstance(value, str)
            and _list_item_type(annotation) is not None
            and not _accepts_str(annotation)
        ):
            stripped = value.strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                continue  # JSON array text: the SDK pre-parse owns it
            out[key] = [value]
    return out


def invalid_arguments_envelope(
    tool_name: str, exc: Exception, accepted: list[str] | None
) -> dict[str, Any]:
    """Kiln's failure envelope for a pydantic argument-validation error.

    Names each offending field with pydantic's own reason and the type it
    received, then the parameters the tool does accept — so the agent's
    next call can be right without a second round of guessing.
    """
    problems: list[str] = []
    errors = getattr(exc, "errors", None)
    try:
        rows = errors() if callable(errors) else []
    except Exception:  # noqa: BLE001
        rows = []
    for row in rows or []:
        loc = ".".join(str(p) for p in (row.get("loc") or ())) or "argument"
        msg = str(row.get("msg") or "invalid value")
        got = row.get("input")
        got_type = type(got).__name__ if "input" in row else None
        if row.get("type") == "missing":
            problems.append(f"{loc} is required")
        elif got_type and got_type != "NoneType":
            problems.append(f"{loc}: {msg} (got {got_type})")
        else:
            problems.append(f"{loc}: {msg}")
    if not problems:
        problems.append(str(exc).splitlines()[0][:200])
    accepts = ", ".join(accepted) if accepted else "no arguments"
    message = (
        f"{tool_name} was called with invalid arguments: "
        f"{'; '.join(problems)}. {tool_name} accepts: {accepts}."
    )
    return {
        "success": False,
        "error": {"code": "INVALID_ARGS", "message": message, "retryable": False},
        "invalid_arguments": problems,
        "accepted_arguments": list(accepted or []),
    }
