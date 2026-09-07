"""A tool's served description may not promise access below the tier it gates.

THE BUG THIS WAS BUILT FOR
==========================
``fleet_analytics`` carried ``@requires_tier(LicenseTier.BUSINESS)`` and a
docstring ending "Requires Kiln Pro or Business license."  The docstring is
not a comment: every MCP client receives it verbatim from ``tools/list``, so
a Pro customer could read the served text, buy Pro, call the tool, and be
refused by the decorator directly above the sentence they had just read.

Nothing in either repo could see it.  The tier gates ask whether the gate is
WIRED (declared == enforced) or whether a REFUSAL names the right tier; the
served-surface gate sweeps the same text for leaked internals by word
pattern and never reads ``requires_tier`` at all.  The axis nobody held was
the one a customer actually meets first: the PROMISE in the description
versus the FLOOR in the decorator.

WHAT THIS HOLDS
===============
Every ``@mcp.tool()`` whose registration carries ``requires_tier`` is swept
from source — no roster, so a tool written tomorrow is judged the first time
it is saved.  Its docstring is read for tier PROMISES: requirement-shaped
phrases ("Requires X license", "X tier", a trailing "(X)").  The lowest tier
any promise names must be the tier the decorator enforces.

A promise BELOW the floor fails: that is the defect, and it is the direction
that costs a customer money.  A promise ABOVE the floor also fails, as a
false paywall that suppresses a tool the caller has already paid for.
Silence never fails — a description that names no tier cannot mislead
anyone, and demanding one everywhere would only teach authors to paste a
line they do not maintain.

WHAT IT CANNOT SEE
==================
Only tools gated by the ``requires_tier`` DECORATOR: a tool that gates
in-body, meters a quota, or resolves its tier from the bundled paid-tool
manifest has no decorator here to disagree with, and the kiln-pro
capability-floor ledger owns that half.  It reads prose in a docstring, so a
tier promised in a parameter description or a returned message is a
different surface.  And it judges the tier a tool NAMES, never whether the
decorator's tier was the right product decision.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "kiln"

#: The product ladder, lowest first.  A promise is judged by rank, so
#: "Pro or Business" is a promise of PRO — the cheapest door it names.
TIER_ORDER = ["free", "pro", "business", "enterprise"]

_TIERS = "free|pro|business|enterprise"

#: Requirement-shaped prose, and only that.  A tier word on its own is not a
#: promise: "AMS 2 Pro" is a Bambu product, "tier is one of pro, business,
#: enterprise" is a parameter's value list, and failing either would push
#: authors to delete honest words rather than fix a claim.
_PROMISE_SHAPES = (
    # "Requires Kiln Pro or Business license", "Requires Enterprise license"
    re.compile(rf"requires?\b[^.\n]{{0,40}}?\b({_TIERS})\b[^.\n]{{0,40}}", re.I),
    # "Business+ tier", "Pro tier", "Business-tier"
    re.compile(rf"\b({_TIERS})\+?[-\s]tier\b", re.I),
)

_TIER_WORD = re.compile(rf"\b({_TIERS})\b", re.I)


def _is_tool(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    for dec in fn.decorator_list:
        target = dec.func if isinstance(dec, ast.Call) else dec
        if (getattr(target, "attr", None) or getattr(target, "id", None)) == "tool":
            return True
    return False


def _required_tier(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> str | None:
    """The tier ``requires_tier`` enforces on this tool, lowercased."""
    for dec in fn.decorator_list:
        if not isinstance(dec, ast.Call) or not dec.args:
            continue
        target = dec.func
        name = getattr(target, "attr", None) or getattr(target, "id", None)
        if name != "requires_tier":
            continue
        tier = getattr(dec.args[0], "attr", None)
        if tier:
            return tier.lower()
    return None


def gated_tools() -> list[tuple[str, str, str, str]]:
    """Every ``requires_tier``-decorated tool: (name, tier, docstring, path)."""
    found: list[tuple[str, str, str, str]] = []
    for path in sorted(SRC.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not _is_tool(node):
                continue
            tier = _required_tier(node)
            if tier is None:
                continue
            found.append(
                (node.name, tier, ast.get_docstring(node) or "",
                 path.relative_to(SRC).as_posix()),
            )
    return found


def promised_tiers(text: str) -> set[str]:
    """Every tier named by a requirement-shaped phrase in *text*."""
    named: set[str] = set()
    for shape in _PROMISE_SHAPES:
        for match in shape.finditer(text):
            named.update(w.lower() for w in _TIER_WORD.findall(match.group(0)))
    return named


def test_the_sweep_finds_the_gated_tools():
    """A sweep that silently found nothing would pass forever."""
    tools = gated_tools()
    assert len(tools) >= 30, (
        f"only {len(tools)} requires_tier tools found — the sweep is looking "
        "in the wrong place, and an empty sweep passes every assertion below"
    )
    by_name = {t[0]: t[1] for t in tools}
    assert by_name.get("fleet_analytics") == "business"


@pytest.mark.parametrize("name,tier,doc,path", gated_tools(), ids=lambda v: str(v)[:40])
def test_served_description_promises_the_tier_it_enforces(name, tier, doc, path):
    """The tier a description names must be the tier the decorator requires."""
    named = promised_tiers(doc)
    if not named:
        return  # silence cannot drift

    floor = TIER_ORDER.index(tier)
    cheapest = min(named, key=TIER_ORDER.index)
    dearest = max(named, key=TIER_ORDER.index)

    assert TIER_ORDER.index(cheapest) >= floor, (
        f"{name} ({path}) is gated at {tier.upper()} but its served "
        f"description promises {cheapest.upper()} — a customer can read it, "
        f"buy {cheapest.upper()}, and still be refused. Name the tier the "
        "decorator enforces."
    )
    assert TIER_ORDER.index(dearest) <= floor, (
        f"{name} ({path}) is gated at {tier.upper()} but its served "
        f"description demands {dearest.upper()} — a false paywall that hides "
        "a tool the caller has already paid for."
    )
