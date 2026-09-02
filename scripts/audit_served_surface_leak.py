#!/usr/bin/env python3
"""Served-surface leak gate — what a stranger reads without opening the source.

Every MCP client receives the tool descriptions, parameter docs, prompt and
resource descriptions, and the server instructions verbatim; ``kiln --help``
prints every command docstring; the bundled paid-tool manifest registers a
discovery stub on every free install; and the ``_meta`` notes in the shipped
knowledge files ride along in the wheel.  None of that is source code, so the
comment-and-docstring gates never saw it — which is how tool descriptions
shipped with internal patent docket numbers, private module paths, the
research bibliography behind curated values, and the internal label for the
paid data.

This gate builds the FREE-install view of the server in-process (kiln-pro
blocked, so the discovery stubs appear exactly as a free user gets them),
walks the CLI, reads the paid-tool manifest and the data ``_meta`` strings,
and fails on any served text that names how the paid depth is built rather
than what a tier unlocks.

    python scripts/audit_served_surface_leak.py            # exit 0 clean, 2 leak, 3 no registry
    python scripts/audit_served_surface_leak.py --dump /tmp/served.txt

Run with the project's virtualenv Python and ``PYTHONPATH=kiln/src`` so the
registry judged is the checkout under test.  The kiln-pro repo carries the
twin gate for the descriptions only a kiln-pro install serves
(``scripts/audit_served_docstrings.py``); keep the rule tables aligned.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS = _ROOT / "scripts"
_SRC = _ROOT / "kiln" / "src"
_PKG = _SRC / "kiln"
_DATA = _PKG / "data"
_MANIFEST = _PKG / "pro_tool_manifest.json"

sys.path.insert(0, str(_SCRIPTS))
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

# ── The rules ──────────────────────────────────────────────────────────────
# Each pattern names HOW the paid depth is built (a leak), never WHAT a tier
# unlocks (fine).  Tier names, outcomes, and the pricing URL never trip these.
#
# DO NOT reword the literals below to avoid the words they catch.  Here the
# word IS the rule, not prose about the rule: a cleanup pass that "tidies"
# them turns the matching check into a no-op, and a blinded gate reports
# clean forever without ever saying it stopped looking.  The sibling gate
# exempts this file for exactly that reason (``_SELF`` in
# ``audit_moat_comment_leak.py``).  Change a pattern only to make it catch
# MORE, and prove the new shape fails before trusting it.
RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    # Branding the curated data in text a client reads.
    ("private-tier self-label", re.compile(r"\bmoat\b", re.IGNORECASE)),
    # A private module / file path or overlay filename.
    ("private module or file path",
     re.compile(r"\bkiln_pro[./][\w./-]+|\b\w+_pro_overlay\.json\b")),
    # Internal patent docket numbers, claim/priority shorthand, bug tags.
    # "patent pending" is a public statement and stays allowed.
    ("internal docket or claim shorthand",
     re.compile(r"\bKILN-\d{3}\b|\bpatent\b(?!\s+pending)|\bclaims?\s+\d+\b|"
                r"\bpriorit(?:y|ies)\s+\d+\b|\bbug-X\d\b|crown[- ]jewel",
                re.IGNORECASE)),
    # Naming the withheld paid fields is a field inventory, not an outcome.
    ("paid field inventory",
     re.compile(r"\b(?:agent_notes|agent_guidance|failure_modes|general_rules|"
                r"common_issues|use_case_ratings|break_in_tips|"
                r"cycle_life_estimates|co_print)\b")),
    # The sourcing playbook behind curated values.  Public standards (ISO,
    # ASTM) are textbook and do not trip; named references and dated
    # datasheet citations do.
    ("research bibliography",
     re.compile(r"cnc kitchen|matweb|ces edupack|\bspringer\b|shigley|"
                r"datasheet[- ](?:grounded|derived)|vendor datasheets?|"
                r"tds-derived|data ?sheets?\s*\(20\d\d", re.IGNORECASE)),
    # Infrastructure vendor and storage internals.  Case-sensitive on
    # purpose: an operator env-var name like KILN_CLOUD_SUPABASE_SECRET is the
    # config interface and may be named; prose about the vendor may not.
    ("infrastructure internals",
     re.compile(r"(?<![A-Z_])Supabase\b|\bRLS\b|service[- ]role\s+key")),
)

# Consciously reviewed served texts that trip a rule for a non-leak reason.
# Keyed by (surface id, rule name).  Add a row only with a reason comment.
_ALLOWLIST: frozenset[tuple[str, str]] = frozenset()


def _persona_rules():
    """The public-language gate's persona / process-shorthand rules."""
    try:
        from check_public_language import find_violations
    except Exception:  # noqa: BLE001 — the gate still runs without it
        return None
    return find_violations


def judge(surface: str, text: str, find_violations=None) -> list[tuple[str, str, str]]:
    """Return ``(rule, surface, excerpt)`` for every leak in *text*."""
    hits: list[tuple[str, str, str]] = []
    if not text:
        return hits
    for rule, pattern in RULES:
        if (surface, rule) in _ALLOWLIST:
            continue
        for m in pattern.finditer(text):
            start = max(0, m.start() - 50)
            excerpt = " ".join(text[start:m.end() + 50].split())
            hits.append((rule, surface, excerpt))
    if find_violations is not None:
        for f in find_violations(text, source=surface):
            hits.append((f"persona/process: {f.rule}", surface, f.text[:120]))
    return hits


# ── Surfaces ───────────────────────────────────────────────────────────────

def registry_texts() -> list[tuple[str, str]]:
    """Every description the free-install MCP server hands to a client."""
    import dump_tool_descriptions as dump

    dump._block_pro_import()
    data = dump.collect()
    out: list[tuple[str, str]] = [("server:instructions", data["instructions"])]
    for t in data["tools"]:
        out.append((f"tool:{t['name']}", t["description"]))
        for p in t["parameters"]:
            if p["description"]:
                out.append((f"tool:{t['name']}.{p['name']}", p["description"]))
    for p in data["prompts"]:
        out.append((f"prompt:{p['name']}", p["description"]))
        for a in p["arguments"]:
            out.append((f"prompt:{p['name']}.{a['name']}", a["description"]))
    for r in data["resources"]:
        out.append((f"resource:{r['uri']}", r["description"]))
    return out


def cli_texts() -> list[tuple[str, str]]:
    """``--help`` for every command reachable from ``kiln``."""
    import click

    from kiln.cli.main import cli

    out: list[tuple[str, str]] = []

    def walk(cmd, path):
        ctx = click.Context(cmd, info_name=path[-1])
        try:
            out.append(("cli:" + " ".join(path), cmd.get_help(ctx)))
        except Exception as exc:  # noqa: BLE001 — a broken help is its own bug
            out.append(("cli:" + " ".join(path), f"<help failed: {exc}>"))
        if isinstance(cmd, click.Group):
            for name in sorted(cmd.list_commands(ctx)):
                sub = cmd.get_command(ctx, name)
                if sub is not None:
                    walk(sub, path + [name])

    walk(cli, ["kiln"])
    return out


def _strings(node, path: str):
    if isinstance(node, str):
        yield path, node
    elif isinstance(node, dict):
        for k, v in node.items():
            yield from _strings(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _strings(v, f"{path}[{i}]")


def manifest_texts() -> list[tuple[str, str]]:
    """Descriptions, parameter docs, nudges, and tier copy in the bundled manifest."""
    if not _MANIFEST.is_file():
        return []
    m = json.loads(_MANIFEST.read_text(encoding="utf-8"))
    out: list[tuple[str, str]] = []
    for path, s in _strings(m.get("tiers", {}), "manifest:tiers"):
        out.append((path, s))
    for t in m.get("tools", []):
        name = t.get("name", "?")
        out.append((f"manifest:{name}", t.get("description", "") or ""))
        for pname, pdef in (t.get("parameters") or {}).get("properties", {}).items():
            if isinstance(pdef, dict) and pdef.get("description"):
                out.append((f"manifest:{name}.{pname}", pdef["description"]))
        for path, s in _strings(t.get("upgrade_nudge") or {}, f"manifest:{name}.upgrade_nudge"):
            out.append((path, s))
    return out


def data_meta_texts() -> list[tuple[str, str]]:
    """Every string under an underscore key in the shipped knowledge files."""
    out: list[tuple[str, str]] = []
    for f in sorted(_DATA.rglob("*.json")):
        if "scad_libraries" in f.parts:
            continue
        try:
            doc = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        rel = f.relative_to(_PKG).as_posix()

        def walk(node, path, under_meta):
            if isinstance(node, dict):
                for k, v in node.items():
                    walk(v, f"{path}.{k}", under_meta or str(k).startswith("_"))
            elif isinstance(node, list):
                for i, v in enumerate(node):
                    walk(v, f"{path}[{i}]", under_meta)
            elif isinstance(node, str) and under_meta:
                out.append((f"data:{rel}{path}", node))

        walk(doc, "", False)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dump", type=Path, help="write every judged text here")
    parser.add_argument(
        "--static-only", action="store_true",
        help="judge only the manifest and data notes (no server import)",
    )
    args = parser.parse_args(argv)

    surfaces: list[tuple[str, str]] = []
    surfaces += manifest_texts()
    surfaces += data_meta_texts()
    if not args.static_only:
        try:
            surfaces += registry_texts()
            surfaces += cli_texts()
        except Exception as exc:  # noqa: BLE001 — report, never pass vacuously
            print(f"served-surface leak gate: could not load the registry/CLI: {exc}")
            return 3

    if args.dump:
        args.dump.write_text(
            "\n".join(f"=== {s}\n{t}\n" for s, t in surfaces), encoding="utf-8"
        )

    fv = _persona_rules()
    hits: list[tuple[str, str, str]] = []
    for surface, text in surfaces:
        hits.extend(judge(surface, text, fv))

    counts = {}
    for s, _ in surfaces:
        counts[s.split(":")[0]] = counts.get(s.split(":")[0], 0) + 1
    summary = ", ".join(f"{k} {v}" for k, v in sorted(counts.items()))
    if hits:
        print(f"served-surface leak gate: {len(hits)} leak(s) across {summary}\n")
        for rule, surface, excerpt in hits:
            print(f"  [{rule}] {surface}\n      …{excerpt}…")
        print(
            "\nFix the wording at the source (docstring, help text, manifest "
            "generator, or data note): say what the tier unlocks, not how it is "
            "built.  Allowlist a row only for a reviewed non-leak."
        )
        return 2
    print(f"served-surface leak gate: clean ({summary} texts judged)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
