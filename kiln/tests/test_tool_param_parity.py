"""In-suite backstop for the tool-door param-parity gate.

The gate (scripts/audit_tool_param_parity.py) catches the class found on
2026-07-28: an engine accepts a value-bearing axis param (material,
printer_id) that its @mcp.tool() door never exposes or forwards, so agent
calls run pinned to generic defaults and the tuned analysis is unreachable
through the door that matters.

Two halves, per the house rule that a gate that can't fail is theater:
- the audit runs clean on the real tree (no unclassified findings, no
  stale ledger entries);
- the matcher provably CAN produce a finding, driven on synthetic input.
"""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path

_SCRIPT = (
    Path(__file__).resolve().parents[2] / "scripts" / "audit_tool_param_parity.py"
)


def _load_audit_module():
    spec = importlib.util.spec_from_file_location("audit_tool_param_parity", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_real_tree_is_clean():
    """No unclassified findings and no stale ledger entries on the tree."""
    mod = _load_audit_module()
    findings = mod.run_audit()
    keyed = {(f["tool"], f["param"]) for f in findings}
    known = set(mod.PARAM_PARITY_EXEMPT) | set(mod.PARAM_PARITY_BASELINE)

    unclassified = keyed - known
    assert not unclassified, (
        "New param-parity findings — expose and forward the param, or "
        f"classify each in the script's ledgers: {sorted(unclassified)}"
    )

    stale = known - keyed
    assert not stale, (
        "Stale ledger entries — the finding no longer exists; remove: "
        f"{sorted(stale)}"
    )


def test_gate_can_fail_on_a_starved_door():
    """The matcher flags a synthetic tool that pins a watch param."""
    mod = _load_audit_module()
    src = (
        "def starved_tool(file_path: str) -> dict:\n"
        "    from kiln.printability import analyze_printability as _analyze\n"
        "    return _analyze(file_path)\n"
    )
    tool = ast.parse(src).body[0]
    findings = mod.audit_tool(tool, {})
    flagged = {(f["tool"], f["param"]) for f in findings}
    assert ("starved_tool", "material") in flagged
    assert ("starved_tool", "printer_id") in flagged


def test_forwarding_clears_the_finding():
    """A door that forwards the watch params produces no finding."""
    mod = _load_audit_module()
    src = (
        "def wired_tool(file_path: str, material: str = 'pla',\n"
        "               printer_id: str = '') -> dict:\n"
        "    from kiln.printability import analyze_printability as _analyze\n"
        "    return _analyze(file_path, material=material,\n"
        "                    printer_id=printer_id or None)\n"
    )
    tool = ast.parse(src).body[0]
    assert mod.audit_tool(tool, {}) == []


def test_cli_exit_zero_on_clean_tree():
    """The script's main() agrees with run_audit on the real tree."""
    mod = _load_audit_module()
    assert mod.main([]) == 0
