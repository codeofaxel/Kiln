"""Every tool Kiln registers says what it does to the person's things.

MCP hosts read a tool's annotations to decide whether it may run without
asking: a read-only tool can run unprompted, a destructive one always asks.
A tool registered with none leaves the host to guess, and a guess that lets
a print start, a file be overwritten or money be spent without the person
being asked is a safety bug.

So every tool this server registers carries one of the three kinds in
``kiln.tool_annotations``, stated at its definition.  The served tools an
install without kiln-pro lists as stubs carry the kind kiln-pro's own
definition gave them, read from the manifest, so a plain install and Kiln's
servers say the same thing about each tool.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import kiln.server as server

def _problem(annotations: Any) -> str | None:
    """Why *annotations* does not tell a host what the tool does, or None."""
    if annotations is None:
        return "no annotations"
    wire = annotations.model_dump(by_alias=True, exclude_none=True)
    if not str(wire.get("title") or "").strip():
        return "no title"
    for hint in ("readOnlyHint", "destructiveHint", "openWorldHint"):
        if not isinstance(wire.get(hint), bool):
            return f"{hint} not stated"
    if wire["readOnlyHint"] and wire["destructiveHint"]:
        return "read-only and destructive at once"
    return None


def _registered_tools() -> dict[str, Any]:
    server._ensure_internal_tool_plugins_registered()
    return dict(server.mcp._tool_manager._tools)


def test_every_registered_tool_states_its_kind() -> None:
    """Public Kiln's own tools, and the stubs when kiln-pro is absent."""
    unannotated = []
    for name, tool in sorted(_registered_tools().items()):
        module = getattr(tool.fn, "__module__", "") or ""
        if module.startswith("kiln_pro"):
            continue  # kiln-pro's own tests judge kiln-pro's tools
        problem = _problem(tool.annotations)
        if problem:
            unannotated.append(f"{name} ({module}): {problem}")
    assert unannotated == [], (
        f"{len(unannotated)} tools tell a host nothing about what they change. Give each one "
        "read_only / creates / destructive from kiln.tool_annotations at its definition:\n  "
        + "\n  ".join(unannotated)
    )


def test_the_recovery_server_states_its_kind() -> None:
    """The tools Kiln serves when it could not start say they change nothing."""
    from kiln import startup_failure

    safe = startup_failure.build_safe_mode_server(startup_failure.explain(RuntimeError("boom")), None)
    tools = dict(safe._tool_manager._tools)
    assert tools, "the recovery server registered no tools; the check would prove nothing"
    assert {name: _problem(tool.annotations) for name, tool in tools.items()} == dict.fromkeys(tools)


class _Catcher:
    """Takes what the stub registrar hands a server, annotations included."""

    def __init__(self) -> None:
        self.annotations: dict[str, Any] = {}

    def tool(self, *args: Any, **kwargs: Any) -> Any:
        def keep(fn: Any) -> Any:
            self.annotations[kwargs.get("name") or fn.__name__] = kwargs.get("annotations")
            return fn

        return keep


def test_every_served_stub_carries_its_kind_from_the_manifest() -> None:
    """A plain install lists each served tool with kiln-pro's own kind for it."""
    manifest = json.loads(
        (Path(server.__file__).parent / "pro_tool_manifest.json").read_text(encoding="utf-8")
    )
    catcher = _Catcher()
    server._register_pro_tool_stubs(catcher)
    served = [
        entry["name"]
        for entry in manifest["tools"]
        if entry.get("served") is not False and not entry["name"].startswith("_")
    ]
    assert served, "the manifest serves no tools; the check would prove nothing"
    wrong = []
    for name in served:
        registered = catcher.annotations.get(name)
        problem = _problem(registered)
        if problem:
            wrong.append(f"{name}: {problem}")
            continue
        block = next(e for e in manifest["tools"] if e["name"] == name).get("annotations")
        if registered.model_dump(by_alias=True, exclude_none=True) != block:
            wrong.append(f"{name}: the stub's annotations differ from the manifest's")
    assert wrong == [], "\n  ".join(["served stubs that do not say what they change:", *wrong])
    assert _problem(catcher.annotations.get("keep_design")) is None, (
        "keep_design, registered beside the stubs, spends a keep and must say so"
    )
