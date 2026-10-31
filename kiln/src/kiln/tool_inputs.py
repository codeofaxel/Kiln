"""Every tool input is described, once, where it was written: the docstring.

An MCP host shows a model each tool's input schema.  A parameter with no
``description`` there is a name and a type and nothing else, so the model
guesses what to pass (a printer's name or its id, millimetres or metres),
and a directory grading the server marks the tool incomplete.  Kiln's tools
already describe their inputs, in the ``Args:`` block or the ``:param x:``
lines of their docstrings; the schema the SDK builds from the signature
never read them.

This module reads them at registration, through the one place every tool
passes: the tool manager's ``add_tool``.  Public Kiln's own tools, kiln-pro's
plugins (registered on the same server) and the stubs a plain install builds
all go through it, so one hook describes every door and a tool added next
year inherits it.  The manifest kiln-pro generates and the hosted connector
both read ``tool.parameters``, so they carry the descriptions too.

A parameter the docstring does not describe stays undescribed: this module
never invents a sentence for it.  ``undescribed(tool)`` names such
parameters so a test can refuse them; the fix is the docstring.

Usage, once, before any tool registers (``kiln.server`` does this)::

    from kiln import tool_inputs
    tool_inputs.install(mcp)
"""

from __future__ import annotations

import functools
import re
from typing import Any

#: The Google-style heading that opens a block of parameter descriptions.
_ARGS_HEADING = re.compile(r"^\s*(Args|Arguments|Parameters)\s*:\s*$")

#: Any Google-style section heading ("Returns:", "Raises:", "Example:"),
#: which ends an ``Args:`` block.  One or two capitalised words, a colon,
#: nothing after it.
_SECTION_HEADING = re.compile(r"^\s*[A-Z][A-Za-z]*(?: [A-Z][A-Za-z]*)?:\s*$")

#: One item of an ``Args:`` block: ``name: text``, ``name (type): text`` or
#: ``*args: text``.  ``text`` may be empty when it continues on the next line.
_ARGS_ITEM = re.compile(r"^\s*\*{0,2}([A-Za-z_]\w*)\s*(?:\([^)]*\))?\s*:(?:\s+(.*)|\s*)$")

#: A reST field: ``:param name: text`` or ``:param type name: text``.
_REST_PARAM = re.compile(r"^\s*:param\s+(?:[^:]*\s)?([A-Za-z_]\w*)\s*:\s*(.*)$")

#: Any reST field (``:returns:``, ``:raises X:``), which ends a ``:param``'s
#: continuation lines.
_REST_FIELD = re.compile(r"^\s*:[A-Za-z]+(?:\s[^:]*)?:")

_INSTALLED = "_kiln_tool_inputs_installed"


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def _join(parts: list[str]) -> str:
    return " ".join(p.strip() for p in parts if p.strip()).strip()


def _parse_google(lines: list[str], start: int, out: dict[str, str]) -> int:
    """Read an ``Args:`` block whose heading is at ``start``; return the next index."""
    i = start + 1
    base: int | None = None
    name: str | None = None
    parts: list[str] = []

    def flush() -> None:
        if name is not None:
            out[name] = _join(parts)

    while i < len(lines):
        line = lines[i]
        if not line.strip():
            # A blank line ends the block unless the next text is still
            # indented as an item or its continuation.
            j = i + 1
            while j < len(lines) and not lines[j].strip():
                j += 1
            if j >= len(lines) or base is None or _indent(lines[j]) < base:
                break
            if _SECTION_HEADING.match(lines[j]):
                break
            i = j
            continue
        if _SECTION_HEADING.match(line) and not _ARGS_ITEM.match(line):
            break
        indent = _indent(line)
        if base is None:
            base = indent
        if indent < base:
            break
        item = _ARGS_ITEM.match(line) if indent == base else None
        if item:
            flush()
            name = item.group(1)
            parts = [item.group(2) or ""]
        elif name is not None:
            parts.append(line)
        i += 1
    flush()
    return i


def _parse_rest(lines: list[str], out: dict[str, str]) -> None:
    """Read every ``:param name:`` field and its indented continuation lines."""
    name: str | None = None
    base = 0
    parts: list[str] = []

    def flush() -> None:
        if name is not None:
            out[name] = _join(parts)

    for line in lines:
        field = _REST_PARAM.match(line)
        if field:
            flush()
            name = field.group(1)
            base = _indent(line)
            parts = [field.group(2)]
            continue
        if name is None:
            continue
        if not line.strip() or _REST_FIELD.match(line) or _indent(line) <= base:
            flush()
            name = None
            continue
        parts.append(line)
    flush()


def parse(doc: str | None) -> dict[str, str]:
    """The parameter descriptions a docstring states, by parameter name.

    Reads Google-style ``Args:`` / ``Arguments:`` / ``Parameters:`` blocks and
    reST ``:param name:`` fields, in either or both styles, with their
    continuation lines joined into one sentence run.  A parameter stated with
    no text is absent from the result, the same as one never stated.
    """
    out: dict[str, str] = {}
    if not doc:
        return out
    lines = doc.expandtabs().splitlines()
    i = 0
    while i < len(lines):
        if _ARGS_HEADING.match(lines[i]):
            i = _parse_google(lines, i, out)
            continue
        i += 1
    _parse_rest(lines, out)
    return {name: text for name, text in out.items() if text}


def _properties(tool: Any) -> dict[str, Any]:
    schema = getattr(tool, "parameters", None)
    if not isinstance(schema, dict):
        return {}
    properties = schema.get("properties")
    return properties if isinstance(properties, dict) else {}


def describe(tool: Any) -> list[str]:
    """Write the docstring's parameter descriptions into ``tool``'s schema.

    A property that already carries a description (an ``Annotated`` field,
    a stub built from the manifest) keeps it.  Returns the parameters still
    undescribed afterwards, so the caller can say so.
    """
    stated = parse(getattr(getattr(tool, "fn", None), "__doc__", None))
    missing: list[str] = []
    for name, prop in _properties(tool).items():
        if not isinstance(prop, dict):
            continue
        if str(prop.get("description") or "").strip():
            continue
        text = stated.get(name)
        if text:
            prop["description"] = text
        else:
            missing.append(name)
    return missing


def undescribed(tool: Any) -> list[str]:
    """The parameters of ``tool`` whose schema states no description."""
    return [
        name
        for name, prop in _properties(tool).items()
        if isinstance(prop, dict) and not str(prop.get("description") or "").strip()
    ]


def install(mcp: Any) -> bool:
    """Describe every tool on ``mcp``, now and as later ones register.

    Idempotent.  Returns False when ``mcp`` has no tool registry this module
    recognises (then nothing is described, and the caller should know).
    """
    manager = getattr(mcp, "_tool_manager", None)
    tools = getattr(manager, "_tools", None)
    add_tool = getattr(manager, "add_tool", None)
    if manager is None or not isinstance(tools, dict) or add_tool is None:
        return False
    for tool in list(tools.values()):
        describe(tool)
    if getattr(add_tool, _INSTALLED, False):
        return True

    @functools.wraps(add_tool)
    def add_tool_described(*args: Any, **kwargs: Any) -> Any:
        tool = add_tool(*args, **kwargs)
        describe(tool)
        return tool

    setattr(add_tool_described, _INSTALLED, True)
    manager.add_tool = add_tool_described
    return True
