"""``kiln.mcp_compat`` is the only module allowed to name an SDK major.

The MCP Python SDK 2.0 (2026-07-28) did not move ``mcp.server.fastmcp``, it
DELETED it.  Kiln's package metadata asks for ``mcp>=1.0``, so every install
that resolved after that date gets 2.x, and any direct import of the v1 path
is an ImportError on a real user's machine — while CI, pinned by
``requirements-lock.txt`` to 1.28.x, sees none of it.

``mcp_compat`` exists precisely so one module absorbs that difference, and
its docstring has said "never import these directly" since it was written.
``local_stage`` did anyway, for ``FunctionResource``.  Because resource
registration is wrapped in ``except Exception``, the failure surfaced as
``install()`` returning every flag False and one warning in a log nobody
reads — the 3D stage simply was not there.

A docstring is not a gate.  This is the gate.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

#: Only this module may name an SDK-specific import path.
_THE_DOOR = "mcp_compat.py"

#: Both major-specific roots.  ``fastmcp`` is gone in 2.x and ``mcpserver``
#: does not exist in 1.x, so a direct import of EITHER is a break on the
#: other major — the symmetry matters, and only checking the old name would
#: miss a v2-only import written next year.
_SDK_SPECIFIC_ROOTS = ("mcp.server.fastmcp", "mcp.server.mcpserver")

_SRC = Path(__file__).resolve().parent.parent / "src" / "kiln"


def _direct_imports(tree: ast.AST) -> list[tuple[str, int]]:
    """Every import naming an SDK-major-specific module, at any nesting.

    Walks the whole tree rather than module scope only: the import that
    caused this was inside a function, which is exactly where it survives a
    boot smoke test and fails at the one moment it is needed.
    """
    hits: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module]
        for name in names:
            if any(
                name == root or name.startswith(f"{root}.")
                for root in _SDK_SPECIFIC_ROOTS
            ):
                hits.append((name, node.lineno))
    return hits


def test_no_module_reaches_around_the_compat_shim():
    offenders = []
    for path in sorted(_SRC.rglob("*.py")):
        if path.name == _THE_DOOR:
            continue
        for name, lineno in _direct_imports(
            ast.parse(path.read_text(encoding="utf-8"))
        ):
            offenders.append(f"{path.relative_to(_SRC)}:{lineno} imports {name}")

    assert not offenders, (
        "these import an SDK-major-specific MCP path directly; import from "
        "kiln.mcp_compat instead, which resolves it for whichever SDK is "
        "installed (mcp.server.fastmcp does not exist under mcp>=2.0):\n  "
        + "\n  ".join(offenders)
    )


def test_no_module_reaches_for_the_v1_lowlevel_attribute():
    """``mcp_compat.lowlevel_server()`` exists for this, and was bypassed too.

    SDK 2.0 renamed ``._mcp_server`` to ``._lowlevel_server``.  ``local_stage``
    reached for the v1 name in three places, so on mcp 2.x the token hook that
    mints artifact tokens raised ``AttributeError`` — caught, warned, and the
    stage got geometry for nothing.  An attribute is not an import, so the
    import check above cannot see it; this is the same defect wearing
    different syntax.
    """
    offenders = []
    for path in sorted(_SRC.rglob("*.py")):
        if path.name == _THE_DOOR:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in (
                "_mcp_server",
                "_lowlevel_server",
            ):
                offenders.append(
                    f"{path.relative_to(_SRC)}:{node.lineno} reads .{node.attr}"
                )

    assert not offenders, (
        "these read an SDK-major-specific lowlevel handle directly; call "
        "kiln.mcp_compat.lowlevel_server(mcp) instead, which returns it on "
        "either SDK:\n  " + "\n  ".join(offenders)
    )


def test_the_shim_itself_still_names_both_majors():
    """Guard the guard.

    If someone 'cleans up' mcp_compat down to one major, the test above goes
    green while every install on the other major breaks — the failure this
    file exists to prevent, with the evidence removed.
    """
    names = {
        name
        for name, _ in _direct_imports(
            ast.parse((_SRC / _THE_DOOR).read_text(encoding="utf-8"))
        )
    }
    for root in _SDK_SPECIFIC_ROOTS:
        assert any(n == root or n.startswith(f"{root}.") for n in names), (
            f"{_THE_DOOR} no longer imports {root}; it is the only module that "
            "may, and dropping a major silently breaks every install on it"
        )


def test_function_resource_resolves_on_the_installed_sdk():
    """The specific symbol that was missing, on whichever SDK is present.

    Cheap, and it is the difference between 'the shim mentions it' and 'the
    shim can actually produce it here'.
    """
    from kiln.mcp_compat import MCP_SDK_MAJOR, FunctionResource

    assert MCP_SDK_MAJOR in (1, 2)
    assert isinstance(FunctionResource, type)


def test_the_stage_registers_its_resource_on_the_installed_sdk():
    """The behaviour, not just the import.

    ``install()`` never raises by design, so the only way to tell a working
    stage from a dead one is to read the flags it returns.  Pinned here
    because the dead version looked exactly like a healthy server that had
    the stage switched off.
    """
    from kiln import local_stage
    from kiln.mcp_compat import FastMCP

    if not local_stage.enabled():
        pytest.skip("local stage disabled in this environment")

    out = local_stage.install(FastMCP("stage-probe"))
    assert out["resource"] is True, (
        f"stage resource did not register on the installed SDK: {out}"
    )
