#!/usr/bin/env python3
"""Tool-door param-parity gate — a tuned engine must be reachable through
its tool door.

Kiln's analysis engines take *value-bearing axis params* — ``material`` and
``printer_id`` — that change the answer: warping / thermal-stress / adhesion
thresholds are material-specific, and per-machine calibration keys off the
printer.  The MCP tool wrapper is the front door an agent actually calls.
When the wrapper does not expose (or forward) an axis param the engine
accepts, every agent call runs pinned to the engine's default — generic PLA,
no printer — and the tuned analysis is unreachable through the door that
matters, silently.

On 2026-07-28 ``analyze_printability`` was found doing exactly this: the
engine had taken ``material`` / ``printer_id`` for months, the tool exposed
neither, so the advertised material-tuned analysis could never fire through
an agent call.  No existing gate could see it: tier gates check enforcement,
autofire gates check previews, nothing compared the tool door's signature to
the engine's.

This gate closes the class.  For every ``@mcp.tool()`` wrapper in public
Kiln, it resolves the engine functions the wrapper calls, and flags any
WATCH param (``material``, ``printer_id``) that the engine accepts but the
call site neither forwards from a same-named tool param nor passes
explicitly — unless the (tool, param) pair is consciously exempted below
with a reason.  The right fix for a flagged tool is to EXPOSE the param and
forward it; the exemption is for doors where the axis genuinely has no
meaning (e.g. the tool operates on geometry alone by design).

    python3 scripts/audit_tool_param_parity.py            # exit 0 clean, 2 findings
    python3 scripts/audit_tool_param_parity.py --json     # CI / machine format
    python3 scripts/audit_tool_param_parity.py --explain  # ledger + reasons

Stale exemptions (entries no longer matching any finding) FAIL the run, so
the allowlist cannot rot into a graveyard of dead rows.

Honest bounds: v1 audits the ``@mcp.tool()`` wrappers (the agent-facing
doors) in ``plugins/`` and ``server.py``.  Engine-to-engine call sites
inside internal pipelines can starve the same params; that sweep is a
documented follow-up, not covered here.
"""

from __future__ import annotations

import argparse
import ast
import importlib
import inspect
import json
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "kiln" / "src" / "kiln"

# The value-bearing axis params.  Extend deliberately: every addition
# re-audits the whole tool surface against a new axis.
WATCH_PARAMS: frozenset[str] = frozenset({"material", "printer_id"})

# (tool_name, param) -> one-line reason the door deliberately omits the
# axis because the VALUE ARRIVES ANOTHER WAY (verified at the call site).
# An entry that stops matching a real finding fails the run as STALE —
# remove it when the door gains the param.
PARAM_PARITY_EXEMPT: dict[tuple[str, str], str] = {
    ("auto_arrange_parts_on_plate", "printer_id"):
        "tool resolves printer_id to plate_width/plate_depth itself "
        "(_resolve_tool_build_volume) — the engine's printer_id is an "
        "alternative route to the same dimensions",
    ("check_print_readiness", "printer_id"):
        "tool resolves printer_id to printer_bed_mm itself — the engine's "
        "printer_id is an alternative route to the same dimensions",
    ("multi_copy_print", "printer_id"):
        "tool resolves the printer profile to bed_width_mm/bed_depth_mm "
        "itself — the engine's printer_id is an alternative route",
    ("multi_copy_print", "material"):
        "single-material execution door: material belongs to the slice "
        "profile / reslice overrides; mixed-material batches route "
        "through multi_material_print and multi_color_copies",
    ("validate_assembly", "material"):
        "per-part materials already feed the signal (load_bearing_signal("
        "material=p.material) per part); the flagged call is the "
        "interface-level one, which carries joint_type by design",
}

# (tool_name, param) -> one-line note.  KNOWN findings owed a product
# decision (expose the axis, or move to EXEMPT with a verified reason).
# Reported as warnings, not failures, so the gate can hold the line on NEW
# findings while the tail is worked down.  A stale entry fails the run.
PARAM_PARITY_BASELINE: dict[tuple[str, str], str] = {
    # (2026-07-28 round 2: the six original baseline rows were resolved —
    # four doors gained the axis param, two moved to EXEMPT with verified
    # reasons.  New findings start here.)
}


def _iter_source_files() -> list[Path]:
    files = sorted((_SRC / "plugins").glob("*.py"))
    server = _SRC / "server.py"
    if server.exists():
        files.append(server)
    return files


def _is_mcp_tool_decorator(node: ast.expr) -> bool:
    """True for ``@mcp.tool()`` / ``@mcp.tool`` decorators."""
    target = node.func if isinstance(node, ast.Call) else node
    return (
        isinstance(target, ast.Attribute)
        and target.attr == "tool"
        and isinstance(target.value, ast.Name)
        and target.value.id == "mcp"
    )


class _ImportMap(ast.NodeVisitor):
    """Collect ``from kiln.X import name [as alias]`` bindings in a scope."""

    def __init__(self) -> None:
        self.bindings: dict[str, tuple[str, str]] = {}  # alias -> (module, name)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module and node.module.startswith("kiln"):
            for a in node.names:
                self.bindings[a.asname or a.name] = (node.module, a.name)
        self.generic_visit(node)


def _engine_signature(module: str, name: str) -> inspect.Signature | None:
    """Resolve the real signature of an imported engine callable.

    Returns None for anything that is not a plain function we can inspect
    (plugin cross-imports, classes, helpers that vanish at runtime).
    """
    try:
        obj = getattr(importlib.import_module(module), name)
        if not inspect.isfunction(obj):
            return None
        return inspect.signature(obj)
    except Exception:
        return None


def _positional_names(sig: inspect.Signature) -> list[str]:
    return [
        p.name
        for p in sig.parameters.values()
        if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
    ]


def audit_tool(
    tool: ast.FunctionDef | ast.AsyncFunctionDef,
    file_bindings: dict[str, tuple[str, str]],
) -> list[dict[str, str]]:
    """Return findings for one @mcp.tool wrapper."""
    tool_params = {a.arg for a in tool.args.args + tool.args.kwonlyargs}

    scope = _ImportMap()
    scope.visit(tool)
    bindings = {**file_bindings, **scope.bindings}

    findings: list[dict[str, str]] = []
    for call in ast.walk(tool):
        if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Name):
            continue
        bound = bindings.get(call.func.id)
        if bound is None:
            continue
        module, name = bound
        if module.startswith("kiln.plugins") or module == "kiln.server":
            continue  # tool-to-tool plumbing, not an engine seam
        sig = _engine_signature(module, name)
        if sig is None:
            continue
        watch_in_engine = WATCH_PARAMS & set(sig.parameters)
        if not watch_in_engine:
            continue
        passed_kw = {kw.arg for kw in call.keywords if kw.arg}
        passed_pos = set(_positional_names(sig)[: len(call.args)])
        for param in sorted(watch_in_engine):
            if param in passed_kw or param in passed_pos:
                continue  # forwarded (whatever the source expression is)
            findings.append(
                {
                    "tool": tool.name,
                    "param": param,
                    "engine": f"{module}.{name}",
                    "exposed_on_tool": str(param in tool_params),
                }
            )
    return findings


def run_audit() -> list[dict[str, str]]:
    import logging

    logging.disable(logging.INFO)  # engine imports register plugins loudly
    findings: list[dict[str, str]] = []
    for path in _iter_source_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        file_imports = _ImportMap()
        # Module-level imports only (walking the whole tree would re-collect
        # per-function imports; those are handled per tool).
        for node in tree.body:
            file_imports.visit(node) if isinstance(node, ast.ImportFrom) else None
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
                _is_mcp_tool_decorator(d) for d in node.decorator_list
            ):
                for f in audit_tool(node, file_imports.bindings):
                    f["file"] = str(path.relative_to(_SRC.parent.parent.parent))
                    findings.append(f)
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--explain", action="store_true")
    args = parser.parse_args(argv)

    findings = run_audit()
    keyed = {(f["tool"], f["param"]): f for f in findings}

    stale_exempt = [k for k in PARAM_PARITY_EXEMPT if k not in keyed]
    stale_baseline = [k for k in PARAM_PARITY_BASELINE if k not in keyed]
    known = set(PARAM_PARITY_EXEMPT) | set(PARAM_PARITY_BASELINE)
    live = {k: f for k, f in keyed.items() if k not in known}
    baselined = {
        k: f for k, f in keyed.items() if k in PARAM_PARITY_BASELINE
    }

    if args.explain:
        print("Watch params:", ", ".join(sorted(WATCH_PARAMS)))
        for (tool_name, param), reason in sorted(PARAM_PARITY_EXEMPT.items()):
            print(f"  exempt   {tool_name}.{param}: {reason}")
        for (tool_name, param), note in sorted(PARAM_PARITY_BASELINE.items()):
            print(f"  baseline {tool_name}.{param}: {note}")
        for (tool_name, param), f in sorted(live.items()):
            print(f"  FINDING  {tool_name}.{param} — engine {f['engine']}")

    stale = stale_exempt + stale_baseline
    if args.json:
        print(
            json.dumps(
                {
                    "findings": sorted(live.values(), key=lambda f: f["tool"]),
                    "baselined": sorted(
                        baselined.values(), key=lambda f: f["tool"]
                    ),
                    "stale_entries": [list(k) for k in stale],
                },
                indent=2,
            )
        )
    else:
        for (tool_name, param), f in sorted(live.items()):
            print(
                f"PARAM-PARITY: {f['file']}: tool `{tool_name}` calls "
                f"{f['engine']} but never passes `{param}` — the tuned axis "
                f"is unreachable through this door. Expose and forward the "
                f"param, or classify it (EXEMPT with a verified reason, or "
                f"BASELINE with the owed decision)."
            )
        for (tool_name, param) in sorted(baselined):
            print(
                f"baseline (owed): {tool_name}.{param} — "
                f"{PARAM_PARITY_BASELINE[(tool_name, param)]}"
            )
        for tool_name, param in stale:
            print(
                f"STALE ENTRY: ({tool_name}, {param}) no longer matches a "
                f"finding — remove it from its ledger."
            )

    if live or stale:
        return 2
    if not args.json:
        print(
            f"Tool-door param parity: clean "
            f"({len(baselined)} baselined finding(s) owed a decision)."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
