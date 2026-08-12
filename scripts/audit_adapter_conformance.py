#!/usr/bin/env python3
"""Adapter-conformance gate — eight backends, one contract, no silent gaps.

Every printer adapter fills the same three dataclasses: ``PrinterState``
(what the machine is doing), ``JobProgress`` (how far along), and
``PrintResult`` (what happened when we asked it to print).  The abstract
base class enforces that each adapter HAS the methods.  Nothing enforces
what those methods put IN the objects — a field left at its ``None``
default is perfectly valid Python, and each adapter's tests are written
against that adapter's own behaviour, so no test and no reviewer ever
sees the eight lined up side by side.

That blind spot has now cost twice.

  * ``record_print_outcome``'s auto-fire hook was wired into ONE adapter,
    so "seven of the eight reported no prints at all regardless of how
    much their owners printed" — quoted from ``record_print_start``'s own
    docstring, where the fix was recorded and the general hole was not.
  * ``state_age_seconds`` — the reading that ``describe_stale_state``
    turns into the user-facing "these numbers may be stale" warning — is
    populated by two adapters.  On the rest the warning cannot fire, and
    nothing anywhere said so.

Both are the same class: a field one backend populates and the others
leave empty, invisible to the type system, invisible to every test.

WHAT THIS GATE IS, AND IS NOT.  It is a DECLARATION ledger, not an
opinion about what every adapter ought to provide.  Protocols genuinely
differ — a serial printer has no wifi signal, and asking for one is not
a bug.  So the ledger records what is true today, with a reason for each
absence, and the gate fails when reality CHANGES without the ledger
changing with it:

  * a new adapter, or a new contract field, with no rows          -> fail
  * a field the ledger says is ``provided`` that no longer is     -> fail
  * a row for an adapter or field that no longer exists           -> fail
  * a ``deferred`` row past its ISO date                          -> fail

The third failure is the one that earns its keep: it turns "this backend
quietly stopped reporting X" from something nobody can see into a red
build.

    python3 scripts/audit_adapter_conformance.py            # 0 clean, 2 findings
    python3 scripts/audit_adapter_conformance.py --json     # CI format
    python3 scripts/audit_adapter_conformance.py --explain  # the ledger
    python3 scripts/audit_adapter_conformance.py --seed     # ledger stubs

HONEST BOUNDS.  It reads constructor keyword arguments statically, so an
adapter that builds its state through a helper or ``**kwargs`` reads as
providing nothing; ``delegates`` exists for the wrapper case and any
other indirection needs a ledger row saying so.  It proves a field is
ASSIGNED, never that the value is correct — a adapter that sets
``tool_temp_actual=0.0`` unconditionally passes here and is review's
problem.  And it judges the contract dataclasses named below; a new one
is invisible until it is added to ``CONTRACTS``.
"""

from __future__ import annotations

import argparse
import ast
import dataclasses
import datetime as _dt
import json
import pathlib
import sys
from typing import Any

REPO = pathlib.Path(__file__).resolve().parents[1]
SRC = REPO / "kiln" / "src"
ADAPTER_DIR = SRC / "kiln" / "printers"
LEDGER = REPO / "scripts" / "adapter_conformance.yaml"

# The contract objects an adapter is responsible for filling.
CONTRACTS = ("PrinterState", "JobProgress", "PrintResult")

# WATCHED fields — the ones where absence has a CONSEQUENCE a user or a
# safety path can feel.  Everything else on these dataclasses is
# vendor-specific hardware description (fan speeds, wifi signal, nozzle
# type, speed profile): genuinely absent on most protocols, rendered by
# nothing, and demanding a written reason from seven adapters for each
# would produce ninety-odd rubber-stamped justifications.  A ledger
# nobody reads is worse than no ledger, so the gate asks only where the
# answer matters, and `--explain` still prints the full picture.
#
# The value beside each field is why it is watched — the consequence.
WATCHED: dict[str, str] = {
    "PrinterState.connected": "every surface branches on it",
    "PrinterState.state": "the whole product asks 'what is it doing'",
    "PrinterState.tool_temp_actual": "rendered by the monitor",
    "PrinterState.tool_temp_target": "rendered by the monitor",
    "PrinterState.bed_temp_actual": "rendered by the monitor",
    "PrinterState.bed_temp_target": "rendered by the monitor",
    "PrinterState.print_error": "the monitor's error readout",
    "PrinterState.state_age_seconds": (
        "describe_stale_state turns it into the 'these readings may be "
        "stale' warning — absent, that warning can never fire"
    ),
    "PrinterState.last_job_result": "how the last print ended",
    "JobProgress.file_name": "what is printing",
    "JobProgress.completion": "the progress readout",
    "JobProgress.print_time_seconds": (
        "elapsed time — the only number a print-hours capture can use"
    ),
    "JobProgress.print_time_left_seconds": "the ETA readout",
    "PrintResult.success": "callers branch on it",
    "PrintResult.message": "what the user is told",
    "PrintResult.job_id": (
        "the dedupe key record_print_hours_for_job requires; verified "
        "populated by NO adapter, which is why that path can never fire"
    ),
}

# Classifications a ledger row may carry.
PROVIDED = "provided"
NOT_IN_PROTOCOL = "not_in_protocol"   # needs `why`
DELEGATES = "delegates"               # the whole adapter forwards elsewhere
DEFERRED = "deferred"                 # needs `why` + ISO `by`
VALID = {PROVIDED, NOT_IN_PROTOCOL, DELEGATES, DEFERRED}


# --------------------------------------------------------------------------
# Discovery — what exists, read from the code rather than a second list
# --------------------------------------------------------------------------
def contract_fields() -> dict[str, list[str]]:
    """Field names per contract dataclass, from the dataclass itself."""
    sys.path.insert(0, str(SRC))
    from kiln.printers import base  # noqa: PLC0415 — path set above

    out: dict[str, list[str]] = {}
    for name in CONTRACTS:
        cls = getattr(base, name)
        out[name] = [f.name for f in dataclasses.fields(cls)]
    return out


def adapter_modules() -> dict[str, pathlib.Path]:
    """Concrete adapter module stem -> path.

    Anchored on the files, not on an import of every adapter: several pull
    in optional heavy transports (pyserial, paho-mqtt) that need not be
    installed for a bare pre-push hook to run this.
    """
    skip = {"base", "__init__", "print_gate", "progress_motion"}
    out = {}
    for path in sorted(ADAPTER_DIR.glob("*.py")):
        if path.stem in skip:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and any(
                isinstance(b, ast.Name) and b.id.endswith("Adapter")
                for b in node.bases
            ):
                out[path.stem] = path
                break
    return out


def fields_assigned(path: pathlib.Path) -> dict[str, set[str]]:
    """Contract -> field names this module ever passes as a keyword arg."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: dict[str, set[str]] = {c: set() for c in CONTRACTS}
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in found
        ):
            found[node.func.id] |= {k.arg for k in node.keywords if k.arg}
    return found


def delegating_methods(path: pathlib.Path) -> set[str]:
    """Methods whose whole body is ``return self.<attr>.<same name>(...)``.

    A wrapper adapter (Creality forwards to Moonraker or OctoPrint by
    model) constructs nothing itself, so a constructor scan reports it as
    providing NOTHING.  That is a false finding, and this is how the gate
    tells a wrapper apart from a gap.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        body = [n for n in node.body if not isinstance(n, ast.Expr)]
        if len(body) != 1 or not isinstance(body[0], ast.Return):
            continue
        val = body[0].value
        if (
            isinstance(val, ast.Call)
            and isinstance(val.func, ast.Attribute)
            and val.func.attr == node.name
            and isinstance(val.func.value, ast.Attribute)
        ):
            out.add(node.name)
    return out


# --------------------------------------------------------------------------
# Ledger
# --------------------------------------------------------------------------
def load_ledger() -> dict[str, Any]:
    if not LEDGER.is_file():
        return {}
    try:
        import yaml  # noqa: PLC0415
    except ImportError:
        print("PyYAML not installed — cannot read the ledger", file=sys.stderr)
        raise SystemExit(3)
    return yaml.safe_load(LEDGER.read_text(encoding="utf-8")) or {}


def audit() -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    fields = contract_fields()
    modules = adapter_modules()
    ledger = load_ledger()
    rows = ledger.get("adapters") or {}
    today = _dt.date.today().isoformat()

    for stem, path in sorted(modules.items()):
        assigned = fields_assigned(path)
        delegated = delegating_methods(path)
        entry = rows.get(stem)
        if entry is None:
            findings.append({
                "adapter": stem,
                "kind": "unclassified_adapter",
                "detail": (
                    "new adapter with no ledger rows — declare, per contract "
                    "field, what it provides and why anything is absent"
                ),
            })
            continue

        # A whole-adapter delegation claim is checked, not taken on trust.
        if entry.get("delegates_to"):
            if not {"get_state", "get_job"} <= delegated:
                findings.append({
                    "adapter": stem,
                    "kind": "delegation_claim_unsupported",
                    "detail": (
                        f"ledger says it delegates to {entry['delegates_to']}, "
                        "but get_state/get_job do not forward — it builds its "
                        "own objects and owes real rows"
                    ),
                })
            continue

        claims = entry.get("fields") or {}
        for key in WATCHED:
            contract, _, field = key.partition(".")
            claim = claims.get(key)
            really = field in assigned[contract]
            if claim is None:
                findings.append({
                    "adapter": stem, "kind": "unclassified_field",
                    "detail": (
                        f"{key} is {'set' if really else 'never set'} and "
                        "carries no ledger row"
                    ),
                })
                continue
            status = claim if isinstance(claim, str) else claim.get("status")
            why = "" if isinstance(claim, str) else (claim.get("why") or "")
            if status not in VALID:
                findings.append({
                    "adapter": stem, "kind": "bad_status",
                    "detail": f"{key}: {status!r} is not one of {sorted(VALID)}",
                })
                continue
            if status == PROVIDED and not really:
                findings.append({
                    "adapter": stem, "kind": "regression",
                    "detail": (
                        f"{key} is declared provided but is no longer "
                        "assigned anywhere in this adapter"
                    ),
                })
            if status == NOT_IN_PROTOCOL:
                if really:
                    findings.append({
                        "adapter": stem, "kind": "stale_absence",
                        "detail": (
                            f"{key} is declared absent-by-protocol but the "
                            "adapter now sets it — promote it to provided"
                        ),
                    })
                elif not why:
                    findings.append({
                        "adapter": stem, "kind": "missing_reason",
                        "detail": f"{key}: not_in_protocol needs a why",
                    })
            if status == DEFERRED and really:
                # A gap that got closed must not leave its deferral
                # standing: the row would keep excusing an absence that no
                # longer exists, and the field would sit unprotected
                # against regressing back.
                findings.append({
                    "adapter": stem, "kind": "stale_deferral",
                    "detail": (
                        f"{key} is declared deferred but the adapter now sets "
                        "it — promote the row to provided so the gate guards it"
                    ),
                })
            elif status == DEFERRED:
                by = "" if isinstance(claim, str) else str(claim.get("by") or "")
                if not by or not why:
                    findings.append({
                        "adapter": stem, "kind": "missing_reason",
                        "detail": f"{key}: deferred needs both why and an ISO by",
                    })
                elif by < today:
                    findings.append({
                        "adapter": stem, "kind": "past_due",
                        "detail": f"{key}: deferred was due {by}",
                    })

        for key in claims:
            contract, _, field = key.partition(".")
            if contract not in fields or field not in fields[contract]:
                findings.append({
                    "adapter": stem, "kind": "stale_row",
                    "detail": f"{key} names no field on any contract object",
                })
            elif key not in WATCHED:
                findings.append({
                    "adapter": stem, "kind": "unwatched_row",
                    "detail": (
                        f"{key} is a real field but not in WATCHED — either "
                        "watch it (name the consequence) or drop the row"
                    ),
                })

    for stem in rows:
        if stem not in modules:
            findings.append({
                "adapter": stem, "kind": "stale_adapter",
                "detail": "ledger row for an adapter that no longer exists",
            })
    return findings


def seed() -> str:
    """Print ledger stubs reflecting what the code does today."""
    fields = contract_fields()
    lines = ["adapters:"]
    for stem, path in sorted(adapter_modules().items()):
        assigned = fields_assigned(path)
        delegated = delegating_methods(path)
        lines.append(f"  {stem}:")
        if {"get_state", "get_job"} <= delegated:
            lines.append("    delegates_to: FILL-IN  # wrapper; constructs nothing")
            continue
        lines.append("    fields:")
        for key in WATCHED:
            contract, _, field = key.partition(".")
            if field in assigned[contract]:
                lines.append(f"      {key}: provided")
            else:
                lines.append(f"      {key}:")
                lines.append("        status: not_in_protocol")
                lines.append("        why: FILL-IN")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--explain", action="store_true")
    ap.add_argument("--seed", action="store_true")
    args = ap.parse_args()

    if args.seed:
        print(seed())
        return 0
    if args.explain:
        print(json.dumps(load_ledger(), indent=2, sort_keys=True))
        return 0

    findings = audit()
    if args.json:
        print(json.dumps(findings, indent=2))
    elif findings:
        print(f"adapter conformance: {len(findings)} finding(s)\n")
        for f in findings:
            print(f"  [{f['kind']}] {f['adapter']}: {f['detail']}")
        print(
            "\nThe ledger records what each backend really provides.  Fix the "
            "adapter, or declare the absence with a reason."
        )
    else:
        n = len(adapter_modules())
        print(f"adapter conformance: clean — {n} adapters, contracts declared")
    return 2 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
