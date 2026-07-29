"""Display-contract tools must not declare a schema their preview breaks.

``attach_inspect_bundle`` (and the autofire helpers built on it) return
``[Image, result]`` so MCP clients render the preview inline.  The
July-2026 MCP SDK builds a structured-output schema from a tool's return
annotation and VALIDATES the return against it: a bare ``dict``
annotation produces no schema (list returns pass as unstructured
content), while a parameterized ``dict[str, Any]`` produces a DictModel
that rejects the list — so the tool errors exactly when it succeeds and
tries to show its result (found live on ``apply_image_texture``,
2026-07-29).  Engine-level pin: no MCP tool whose return can flow
through a composite-returning helper may declare a parameterized dict
return annotation.

The SDK-behavior assumption is pinned by
``test_sdk_skips_schema_for_bare_dict`` so a future SDK that starts
schematizing bare ``dict`` fails HERE, not in the field.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
SRC = REPO / "src" / "kiln"

COMPOSITE_HELPERS = {
    "attach_inspect_bundle",
    "autofire_import",
    "autofire_pr_artifact",
    "attach_product_preview",
}


def _violations_in(root: pathlib.Path) -> list[str]:
    hits: list[str] = []
    for f in sorted(root.rglob("*.py")):
        if "tests" in f.parts or f.name.startswith("test_"):
            continue
        try:
            tree = ast.parse(f.read_text())
        except (SyntaxError, UnicodeDecodeError):
            continue

        # Module-local helpers that pass a composite return through.
        local_composite: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for r in ast.walk(node):
                    if isinstance(r, ast.Return) and isinstance(r.value, ast.Call):
                        name = getattr(
                            r.value.func, "id", getattr(r.value.func, "attr", "")
                        )
                        if name in COMPOSITE_HELPERS:
                            local_composite.add(node.name)

        reach = COMPOSITE_HELPERS | local_composite
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not any(
                "mcp.tool" in ast.unparse(d) for d in node.decorator_list
            ):
                continue
            if node.returns is None:
                continue
            ann = ast.unparse(node.returns)
            if not (ann.startswith("dict[") or ann.startswith("Dict[")):
                continue
            for r in ast.walk(node):
                if isinstance(r, ast.Return) and isinstance(r.value, ast.Call):
                    name = getattr(
                        r.value.func, "id", getattr(r.value.func, "attr", "")
                    )
                    if name in reach:
                        hits.append(f"{f.relative_to(root)}::{node.name} -> {ann}")
                        break
    return hits


class TestDisplayContractReturnShapes:
    def test_no_composite_returning_tool_declares_a_dict_schema(self):
        violations = _violations_in(SRC)
        assert not violations, (
            "These MCP tools return the [Image, result] display composite "
            "but declare a parameterized dict return annotation — the MCP "
            "SDK will reject the composite at output validation, so the "
            "tool fails exactly when it succeeds. Change the annotation to "
            f"bare `dict`: {violations}"
        )

    def test_sdk_skips_schema_for_bare_dict(self):
        """Pin the SDK behavior the rule above depends on."""
        fastmcp_tools = pytest.importorskip("mcp.server.fastmcp.tools.base")

        def bare() -> dict:  # pragma: no cover - signature is the test
            return {}

        tool = fastmcp_tools.Tool.from_function(bare)
        assert tool.output_schema is None, (
            "The installed MCP SDK now builds an output schema for a bare "
            "`dict` annotation — display-contract composite returns "
            "([Image, result]) will fail validation everywhere. The "
            "display contract needs a new wire strategy before shipping "
            "on this SDK."
        )
