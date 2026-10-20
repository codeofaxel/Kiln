"""Every annotated tool states openWorldHint as a boolean.

OpenAI's plugin review requires readOnlyHint, destructiveHint and
openWorldHint to be explicit booleans on every tool; a host reading an unset
hint has to guess.  The three helpers in ``kiln.tool_annotations`` are the
only way a Kiln tool is annotated, so they carry the rule: closed (False) by
default, and ``open_world=True`` for a tool that reaches public places Kiln
does not own -- the marketplace search, browse and detail tools.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

from kiln import tool_annotations
from kiln.plugins import marketplace_tools

_SRC = Path(tool_annotations.__file__).resolve().parent

MARKETPLACE_OPEN_WORLD = {
    "search_all_models",
    "search_models",
    "model_details",
    "browse_models",
    "list_model_categories",
}


def _open_world(annotations: Any) -> Any:
    # mcp 2.x names the field open_world_hint (wire alias openWorldHint); 1.x openWorldHint.
    value = getattr(annotations, "open_world_hint", None)
    return value if value is not None else getattr(annotations, "openWorldHint", None)


@pytest.mark.parametrize("helper", ["read_only", "creates", "destructive"])
def test_each_helper_states_open_world_explicitly(helper: str) -> None:
    make = getattr(tool_annotations, helper)
    assert _open_world(make("A tool")) is False
    assert _open_world(make("A tool", open_world=True)) is True
    wire = make("A tool").model_dump(by_alias=True, exclude_none=True)
    assert wire["openWorldHint"] is False


def test_marketplace_reads_are_declared_open_world() -> None:
    seen: dict[str, Any] = {}

    class _Mcp:
        def tool(self, **kwargs: Any):
            def register(fn: Any) -> Any:
                seen[fn.__name__] = kwargs.get("annotations")
                return fn

            return register

    marketplace_tools.plugin.register(_Mcp())
    for name in sorted(MARKETPLACE_OPEN_WORLD):
        assert _open_world(seen[name]) is True, name


def test_no_tool_is_annotated_around_the_helpers() -> None:
    """A ``ToolAnnotations(...)`` built by hand skips the explicit hint."""
    offenders = []
    for path in sorted(_SRC.rglob("*.py")):
        if path.name == "tool_annotations.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", None)
                if name == "ToolAnnotations":
                    offenders.append(f"{path.relative_to(_SRC)}:{node.lineno}")
    assert offenders == []
