"""OpenAI function-calling schema converter for Kiln MCP tools.

Introspects the MCP tools registered on the FastMCP server instance and
converts them to OpenAI-compatible function-calling schemas.  Supports
tier-based filtering so that weaker models receive a smaller, simpler
tool set.

All imports from :mod:`kiln.server` are lazy to avoid circular imports
and heavy module initialisation at import time.

Usage::

    from kiln.tool_schema import get_all_tool_schemas, get_tool_function

    schemas = get_all_tool_schemas(tier="standard")
    fn = get_tool_function("printer_status")
    result = fn()
"""

from __future__ import annotations

import inspect
import re
import types
import typing
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from kiln.tool_tiers import TIERS, get_tier


# ---------------------------------------------------------------------------
# Internal registry (lazily populated)
# ---------------------------------------------------------------------------

_TOOL_REGISTRY: Dict[str, Callable] = {}
_SCHEMA_CACHE: Dict[str, dict] = {}
_loaded = False


def _ensure_loaded() -> None:
    """Populate the tool registry from the FastMCP server instance.

    Deferred until first access to avoid importing ``kiln.server``
    (and all its dependencies) at module-import time.
    """
    global _loaded
    if _loaded:
        return

    from kiln.server import mcp as _mcp_instance

    for tool in _mcp_instance._tool_manager.list_tools():
        _TOOL_REGISTRY[tool.name] = tool.fn
        _SCHEMA_CACHE[tool.name] = _build_openai_schema(tool)

    _loaded = True


# ---------------------------------------------------------------------------
# Python type → JSON Schema type mapping
# ---------------------------------------------------------------------------

_PYTHON_TO_JSON_SCHEMA: Dict[type, str] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    dict: "object",
}


def _python_type_to_json_schema(annotation: Any) -> Tuple[dict, bool]:
    """Convert a Python type annotation to a JSON Schema fragment.

    Returns:
        A tuple of ``(schema_dict, is_optional)`` where *is_optional*
        is ``True`` when the annotation includes ``None`` (e.g.
        ``str | None`` or ``Optional[str]``).
    """
    # Unwrap typing.Optional / Union with None
    origin = typing.get_origin(annotation)

    # Handle `X | None` (Python 3.10+ union syntax) and `Optional[X]`
    if origin is Union or origin is types.UnionType:
        args = typing.get_args(annotation)
        non_none = [a for a in args if a is not type(None)]
        if len(non_none) == 1:
            inner_schema, _ = _python_type_to_json_schema(non_none[0])
            return inner_schema, True
        # Multi-type union without None — fall back to first type
        if non_none:
            inner_schema, _ = _python_type_to_json_schema(non_none[0])
            return inner_schema, type(None) in args
        return {"type": "string"}, True

    # list[X] or List[X]
    if origin in (list, List):
        args = typing.get_args(annotation)
        if args:
            item_schema, _ = _python_type_to_json_schema(args[0])
            return {"type": "array", "items": item_schema}, False
        return {"type": "array"}, False

    # dict[K, V] or Dict[K, V]
    if origin in (dict, Dict):
        return {"type": "object"}, False

    # Plain types
    if annotation in _PYTHON_TO_JSON_SCHEMA:
        return {"type": _PYTHON_TO_JSON_SCHEMA[annotation]}, False

    # Fallback
    return {"type": "string"}, False


# ---------------------------------------------------------------------------
# Docstring parsing
# ---------------------------------------------------------------------------

def _parse_docstring(doc: str | None) -> Tuple[str, Dict[str, str]]:
    """Extract the description and per-parameter docs from a docstring.

    Returns:
        A tuple of ``(description, param_docs)`` where *description* is
        the first paragraph and *param_docs* maps parameter names to
        their descriptions extracted from the ``Args:`` section.
    """
    if not doc:
        return "", {}

    lines = doc.strip().splitlines()

    # --- Description: everything before the first blank line or "Args:" ---
    desc_lines: list[str] = []
    rest_start = len(lines)
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped == "" or stripped.lower().startswith("args:"):
            rest_start = i
            break
        desc_lines.append(stripped)
    description = " ".join(desc_lines)

    # --- Parameter docs from "Args:" section ---
    param_docs: Dict[str, str] = {}
    in_args = False
    current_param: str | None = None
    current_desc_parts: list[str] = []

    saw_blank = False
    for line in lines[rest_start:]:
        stripped = line.strip()

        if stripped.lower() == "args:":
            in_args = True
            saw_blank = False
            continue

        if not in_args:
            continue

        # End of Args section (Returns:, Raises:, blank line after param, etc.)
        if stripped.lower().startswith(("returns:", "raises:", "yields:")):
            break

        # A blank line inside Args may separate the last param from
        # trailing prose.  Track it and break if the next non-blank line
        # is not a parameter definition or an indented continuation.
        if not stripped:
            saw_blank = True
            continue

        if saw_blank:
            saw_blank = False
            # Check whether this looks like a new parameter.  If not,
            # we've left the Args block (trailing prose after a blank).
            param_match_check = re.match(
                r"^(\w+)\s*(?:\([^)]*\))?\s*:\s*(.+)", stripped
            )
            if not param_match_check:
                break

        # New parameter line: "name: description" or "name (type): description"
        param_match = re.match(r"^(\w+)\s*(?:\([^)]*\))?\s*:\s*(.+)", stripped)
        if param_match:
            # Save previous parameter
            if current_param:
                param_docs[current_param] = " ".join(current_desc_parts)
            current_param = param_match.group(1)
            current_desc_parts = [param_match.group(2).strip()]
        elif current_param and stripped:
            # Continuation line for current parameter
            current_desc_parts.append(stripped)

    # Save last parameter
    if current_param:
        param_docs[current_param] = " ".join(current_desc_parts)

    return description, param_docs


# ---------------------------------------------------------------------------
# Schema construction
# ---------------------------------------------------------------------------

def _build_openai_schema(tool: Any) -> dict:
    """Build an OpenAI function-calling schema from a FastMCP Tool object.

    Uses the tool's stored JSON Schema ``parameters`` as the authoritative
    source (FastMCP already introspects the function signature), then
    enriches it with parameter descriptions parsed from the docstring.
    """
    description, param_docs = _parse_docstring(tool.fn.__doc__)

    # FastMCP's tool.parameters is already a JSON Schema dict with
    # "type": "object", "properties": {...}, "required": [...]
    parameters = dict(tool.parameters)  # shallow copy

    # Enrich property descriptions from docstring Args: section
    props = parameters.get("properties", {})
    for pname, pdoc in param_docs.items():
        if pname in props:
            prop = dict(props[pname])  # copy to avoid mutation
            if "description" not in prop or not prop["description"]:
                prop["description"] = pdoc
            props[pname] = prop
    parameters["properties"] = props

    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": description or tool.description,
            "parameters": parameters,
        },
    }


def _build_schema_from_function(fn: Callable) -> dict:
    """Build an OpenAI function-calling schema directly from a function.

    Fallback path when a FastMCP Tool object is not available.  Uses
    ``inspect`` to introspect the function signature and type hints.
    """
    sig = inspect.signature(fn)
    try:
        hints = typing.get_type_hints(fn)
    except Exception:
        hints = {}

    description, param_docs = _parse_docstring(fn.__doc__)

    properties: Dict[str, Any] = {}
    required: List[str] = []

    for pname, param in sig.parameters.items():
        if pname in ("self", "cls"):
            continue

        annotation = hints.get(pname, param.annotation)
        if annotation is inspect.Parameter.empty:
            schema_fragment: dict = {"type": "string"}
            is_optional = False
        else:
            schema_fragment, is_optional = _python_type_to_json_schema(annotation)

        prop: Dict[str, Any] = dict(schema_fragment)
        if pname in param_docs:
            prop["description"] = param_docs[pname]

        # Default values
        if param.default is not inspect.Parameter.empty:
            if param.default is not None:
                prop["default"] = param.default
        elif not is_optional:
            required.append(pname)

        properties[pname] = prop

    # Return type is excluded per OpenAI spec (the schema describes inputs).
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": properties,
    }
    if required:
        parameters["required"] = required

    return {
        "type": "function",
        "function": {
            "name": fn.__name__,
            "description": description,
            "parameters": parameters,
        },
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def mcp_tool_to_openai_schema(func: Callable) -> dict:
    """Convert a single MCP tool function to an OpenAI function-calling schema.

    If the function is already registered in the FastMCP server the
    pre-built schema is returned.  Otherwise, the schema is built by
    introspecting the function directly.

    Args:
        func: The MCP tool function to convert.

    Returns:
        An OpenAI-compatible function-calling schema dict.
    """
    _ensure_loaded()
    name = func.__name__

    if name in _SCHEMA_CACHE:
        return _SCHEMA_CACHE[name]

    return _build_schema_from_function(func)


def get_all_tool_schemas(tier: str = "full") -> List[dict]:
    """Get OpenAI function-calling schemas for all tools in a tier.

    Args:
        tier: Tool tier name — ``"essential"``, ``"standard"``, or
            ``"full"``.  Defaults to ``"full"`` (all tools).

    Returns:
        A list of OpenAI function-calling schema dicts, one per tool.

    Raises:
        KeyError: If *tier* is not a recognised tier name.
    """
    _ensure_loaded()
    tool_names = get_tier(tier)
    schemas = []
    for name in tool_names:
        if name in _SCHEMA_CACHE:
            schemas.append(_SCHEMA_CACHE[name])
    return schemas


def _find_tier_for_tool(name: str) -> str | None:
    """Return the lowest tier that contains *name*, or ``None``."""
    for tier_name in ("essential", "standard", "full"):
        if name in TIERS.get(tier_name, []):
            return tier_name
    return None


def _suggest_alternatives(name: str, current_tier: str) -> list[str]:
    """Suggest tools in *current_tier* that are related to *name*.

    Uses simple keyword overlap on tool names (split on ``_``) to find
    tools in the agent's tier that serve a similar purpose.
    """
    keywords = set(name.split("_"))
    tier_tools = TIERS.get(current_tier, [])
    scored = []
    for tool in tier_tools:
        overlap = keywords & set(tool.split("_"))
        if overlap and tool != name:
            scored.append((len(overlap), tool))
    scored.sort(key=lambda x: -x[0])
    return [t for _, t in scored[:3]]


def get_tool_function(name: str, *, tier: str | None = None) -> Callable:
    """Look up a tool function by name.

    Args:
        name: The tool function name (e.g. ``"printer_status"``).
        tier: Optional current tier context.  When provided and *name*
            is a valid tool but not in this tier, the error message
            includes the required tier and suggests alternatives.

    Returns:
        The callable tool function.

    Raises:
        KeyError: If *name* is not a registered tool or not in the
            specified tier.
    """
    _ensure_loaded()
    try:
        fn = _TOOL_REGISTRY[name]
    except KeyError:
        # Tool doesn't exist at all — give a tier-aware hint if possible.
        if tier is not None:
            required_tier = _find_tier_for_tool(name)
            if required_tier is not None:
                alternatives = _suggest_alternatives(name, tier)
                alt_str = (
                    f" Alternatives in your tier: {', '.join(alternatives)}"
                    if alternatives
                    else ""
                )
                raise KeyError(
                    f"Tool {name!r} requires the {required_tier!r} tier "
                    f"(you are on {tier!r}).{alt_str}"
                ) from None
        raise KeyError(
            f"Unknown tool {name!r}. "
            f"Available tools: {len(_TOOL_REGISTRY)}"
        ) from None
    return fn


def get_tool_registry() -> Dict[str, Callable]:
    """Return a copy of the tool name to function mapping.

    The registry is lazily populated on first call.

    Returns:
        A dict mapping tool names to their callable functions.
    """
    _ensure_loaded()
    return dict(_TOOL_REGISTRY)


# ---------------------------------------------------------------------------
# Safety-annotated schemas
# ---------------------------------------------------------------------------

_SAFETY_DATA: Optional[dict] = None


def _load_tool_safety() -> dict:
    """Load tool safety classifications from the bundled JSON data file."""
    global _SAFETY_DATA
    if _SAFETY_DATA is not None:
        return _SAFETY_DATA

    import json
    from importlib import resources

    try:
        data_pkg = resources.files("kiln") / "data" / "tool_safety.json"
        raw = data_pkg.read_text(encoding="utf-8")
    except (FileNotFoundError, TypeError):
        # Fallback for editable installs or non-standard layouts.
        import os

        fallback = os.path.join(
            os.path.dirname(__file__), "data", "tool_safety.json"
        )
        with open(fallback, "r") as fh:
            raw = fh.read()

    data = json.loads(raw)
    _SAFETY_DATA = data.get("classifications", {})
    return _SAFETY_DATA


def get_annotated_tool_schemas(tier: str = "full") -> List[dict]:
    """Get OpenAI schemas enriched with safety-level metadata.

    Each schema dict gains a ``safety`` key under ``function`` containing
    the tool's safety classification from ``data/tool_safety.json``.

    Args:
        tier: Tool tier name (``"essential"``, ``"standard"``, ``"full"``).

    Returns:
        A list of annotated OpenAI function-calling schema dicts.
    """
    import copy

    schemas = get_all_tool_schemas(tier)
    safety = _load_tool_safety()
    annotated = []
    for schema in schemas:
        schema = copy.deepcopy(schema)
        name = schema.get("function", {}).get("name", "")
        if name in safety:
            schema["function"]["safety"] = safety[name]
        else:
            schema["function"]["safety"] = {"level": "safe"}
        annotated.append(schema)
    return annotated
