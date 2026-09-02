#!/usr/bin/env python3
"""Dump every MCP tool, prompt, and resource description the server hands to clients.

Everything an MCP client receives from ``tools/list`` — the tool name, its
description (the docstring), and every parameter's description — is as
public as the README: the hosted connector, the desktop app, and any
``pip install kiln3d`` user can read it verbatim.  This script writes the
whole surface to one file so it can be swept for wording that should not
be there (internal module paths, private data-file names, thresholds and
formulas, research provenance) without starting a client.

    # what a free user sees: public tools + discovery stubs for paid tools
    python scripts/dump_tool_descriptions.py --without-pro -o /tmp/tools_free.txt

    # what a kiln-pro install / the hosted connector serves
    python scripts/dump_tool_descriptions.py -o /tmp/tools_full.txt

    # machine-readable copy for other gates
    python scripts/dump_tool_descriptions.py --json /tmp/tools.json

Run with the project's virtualenv Python and ``PYTHONPATH=kiln/src`` so the
registry you dump is the checkout you are auditing, not an installed copy.
Exit 0 always; this is a dump, not a gate.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _block_pro_import() -> None:
    """Make ``import kiln_pro`` raise ImportError for the rest of the process.

    Setting a module's ``sys.modules`` entry to ``None`` is the documented
    way to force ImportError, which is exactly the branch a free install
    takes: the server then registers discovery stubs from the bundled
    manifest instead of the real paid tools.
    """
    sys.modules["kiln_pro"] = None  # type: ignore[assignment]


def _tool_schema(tool) -> dict:
    params = getattr(tool, "parameters", None)
    if isinstance(params, dict):
        return params
    meta = getattr(tool, "fn_metadata", None)
    model = getattr(meta, "arg_model", None)
    if model is not None and hasattr(model, "model_json_schema"):
        try:
            return model.model_json_schema()
        except Exception:  # noqa: BLE001 — a schema that will not render is still a tool
            return {}
    return {}


def collect() -> dict:
    import kiln
    from kiln.server import mcp

    # Plugins (public and, when importable, kiln-pro) register at import
    # time; a second call is a no-op but guards the mid-load retry path.
    try:
        from kiln.server import _ensure_internal_tool_plugins_registered

        _ensure_internal_tool_plugins_registered()
    except Exception:  # noqa: BLE001 — registry is whatever import produced
        pass

    tools = []
    for tool in sorted(mcp._tool_manager.list_tools(), key=lambda t: t.name):
        schema = _tool_schema(tool)
        props = schema.get("properties", {}) if isinstance(schema, dict) else {}
        required = set(schema.get("required", []) if isinstance(schema, dict) else [])
        params = []
        for pname, pdef in props.items():
            if not isinstance(pdef, dict):
                pdef = {}
            ptype = pdef.get("type")
            if ptype is None and "anyOf" in pdef:
                ptype = "|".join(
                    str(a.get("type", "?")) for a in pdef["anyOf"] if isinstance(a, dict)
                )
            params.append(
                {
                    "name": pname,
                    "type": ptype,
                    "required": pname in required,
                    "default": pdef.get("default"),
                    "description": pdef.get("description") or "",
                }
            )
        fn = getattr(tool, "fn", None)
        tools.append(
            {
                "name": tool.name,
                "module": getattr(fn, "__module__", "") or "",
                "description": tool.description or "",
                "parameters": params,
            }
        )

    prompts = []
    try:
        for p in sorted(mcp._prompt_manager.list_prompts(), key=lambda p: p.name):
            prompts.append(
                {
                    "name": p.name,
                    "description": p.description or "",
                    "arguments": [
                        {"name": a.name, "description": a.description or ""}
                        for a in (p.arguments or [])
                    ],
                }
            )
    except Exception:  # noqa: BLE001 — prompts are optional
        pass

    resources = []
    try:
        for r in sorted(mcp._resource_manager.list_resources(), key=lambda r: str(r.uri)):
            resources.append(
                {"uri": str(r.uri), "name": r.name, "description": r.description or ""}
            )
        for t in sorted(mcp._resource_manager.list_templates(), key=lambda t: t.uri_template):
            resources.append(
                {
                    "uri": t.uri_template,
                    "name": t.name,
                    "description": t.description or "",
                }
            )
    except Exception:  # noqa: BLE001 — resources are optional
        pass

    return {
        "kiln_source": str(Path(kiln.__file__).resolve().parent),
        "kiln_pro_importable": sys.modules.get("kiln_pro") is not None
        and "kiln_pro" in sys.modules,
        "instructions": getattr(mcp, "instructions", None) or "",
        "tool_count": len(tools),
        "tools": tools,
        "prompts": prompts,
        "resources": resources,
    }


def render(data: dict) -> str:
    out: list[str] = []
    out.append(f"# kiln source: {data['kiln_source']}")
    out.append(f"# kiln_pro importable: {data['kiln_pro_importable']}")
    out.append(f"# tools: {data['tool_count']}  prompts: {len(data['prompts'])}  resources: {len(data['resources'])}")
    out.append("")
    out.append("=" * 78)
    out.append("SERVER INSTRUCTIONS (static handshake text)")
    out.append("=" * 78)
    out.append(data["instructions"])
    out.append("")
    for t in data["tools"]:
        out.append("=" * 78)
        out.append(f"TOOL {t['name']}    [{t['module']}]")
        out.append("=" * 78)
        out.append(t["description"].rstrip())
        if t["parameters"]:
            out.append("")
            out.append("PARAMETERS:")
            for p in t["parameters"]:
                req = "required" if p["required"] else f"default={p['default']!r}"
                line = f"  - {p['name']} ({p['type']}, {req})"
                if p["description"]:
                    line += f": {p['description']}"
                out.append(line)
        out.append("")
    for p in data["prompts"]:
        out.append("=" * 78)
        out.append(f"PROMPT {p['name']}")
        out.append("=" * 78)
        out.append(p["description"].rstrip())
        for a in p["arguments"]:
            out.append(f"  - {a['name']}: {a['description']}")
        out.append("")
    for r in data["resources"]:
        out.append("=" * 78)
        out.append(f"RESOURCE {r['uri']}    ({r['name']})")
        out.append("=" * 78)
        out.append(r["description"].rstrip())
        out.append("")
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("-o", "--out", type=Path, help="write the text dump here (default: stdout)")
    parser.add_argument("--json", type=Path, help="also write a JSON copy here")
    parser.add_argument(
        "--without-pro",
        action="store_true",
        help="block `import kiln_pro` so the dump shows what a free install serves",
    )
    args = parser.parse_args(argv)

    if args.without_pro:
        _block_pro_import()

    data = collect()
    text = render(data)
    if args.out:
        args.out.write_text(text, encoding="utf-8")
        print(
            f"wrote {args.out} — {data['tool_count']} tools, "
            f"{len(data['prompts'])} prompts, {len(data['resources'])} resources "
            f"(kiln_pro importable: {data['kiln_pro_importable']}; source {data['kiln_source']})"
        )
    else:
        sys.stdout.write(text)
    if args.json:
        args.json.write_text(json.dumps(data, indent=1, default=str), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
