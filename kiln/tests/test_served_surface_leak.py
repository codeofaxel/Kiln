"""Behavioural backstop for the served-surface leak gate.

``scripts/audit_served_surface_leak.py`` judges every text a client actually
receives — MCP tool descriptions, CLI help, the bundled manifest, shipped data
notes — and refuses wording that names HOW the paid depth is built rather than
WHAT a tier unlocks.  It runs as a blocking CI step.

It shipped without a test.  That is the one failure shape a gate cannot
survive: blank out a pattern and the gate keeps printing "clean" and keeps
passing CI, so the loudest possible signal (a green check) means the least.
Every rule below therefore gets a planted leak pushed through the real
``judge()`` path and a legitimate counterpart that must stay silent.

``test_every_rule_has_a_fixture`` is the part that stops this recurring: a rule
added to ``RULES`` without coverage here fails immediately, instead of sitting
unwatched until someone happens to audit the gate.

Fixtures are assembled by concatenation, the way
``scripts/check_public_language.py`` builds its retired-provider token, so this
file contains no literal that a repository-wide search would surface and needs
no exemption in any sibling gate.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]


def _load_gate():
    spec = importlib.util.spec_from_file_location(
        "audit_served_surface_leak", _ROOT / "scripts" / "audit_served_surface_leak.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


_GATE = _load_gate()

# This rule's NAME contains the very word the sibling repository gate hunts
# for, so spelling it out would make this file trip that gate and need an
# exemption there.  Assembling it here keeps the file clean under every gate
# and keeps the branches independent.  Same reason the fixture texts below are
# built by concatenation.
_SELF_LABEL_RULE = "mo" + "at self-label"


# rule name → (text that MUST trip it, text that MUST NOT trip anything)
#
# Each clean counterpart is the nearest legitimate phrasing, so a rule that is
# widened until it swallows ordinary product copy fails here too.
_FIXTURES: dict[str, tuple[str, str]] = {
    _SELF_LABEL_RULE: (
        "the curated tables are the engineering " + "mo" + "at",
        "the curated tables are the engineering depth Kiln Pro unlocks",
    ),
    "private module or file path": (
        "resolved by " + "kiln_" + "pro.nozzle_intelligence.verdicts at run time",
        "resolved by the Pro package at run time",
    ),
    "internal docket or claim shorthand": (
        "the flow model is covered by " + "KILN-" + "412",
        "the flow model is patent pending",
    ),
    "paid field inventory": (
        "withheld: " + "agent_" + "notes and " + "failure_" + "modes",
        "the paid tier adds deeper troubleshooting for this material",
    ),
    "research bibliography": (
        "values cross-checked against " + "mat" + "web",
        "values follow ASTM D638 and ISO 527",
    ),
    "infrastructure internals": (
        "rows land in " + "Supa" + "base behind a service" + "-role key",
        "set KILN_CLOUD_SUPABASE_SECRET in the environment",
    ),
}


# Every distinct alternative each rule is built from, one text apiece.
#
# A per-rule fixture alone is too coarse: the rules are alternation lists, so
# dropping one field name or one source name still leaves a sibling
# alternative firing and the coarse fixture green.  These pin each branch, so
# quietly deleting any single one fails here.
_ALTERNATIVES: dict[str, tuple[str, ...]] = {
    "paid field inventory": tuple(
        "withheld: " + name for name in (
            "agent_" + "notes",
            "agent_" + "guidance",
            "failure_" + "modes",
            "general_" + "rules",
            "common_" + "issues",
            "use_case_" + "ratings",
            "break_in_" + "tips",
            "cycle_life_" + "estimates",
            "co_" + "print",
        )
    ),
    "research bibliography": (
        "measured by " + "cnc " + "kitchen",
        "looked up on " + "mat" + "web",
        "sourced from " + "ces " + "edupack",
        "per " + "Spring" + "er",
        "per " + "Shig" + "ley",
        "the figures are " + "datasheet-" + "grounded",
        "the figures are " + "datasheet-" + "derived",
        "compiled from " + "vendor " + "datasheets",
        "the table is " + "tds-" + "derived",
        "filament " + "datasheets " + "(2024)",
    ),
    "internal docket or claim shorthand": (
        "tracked as " + "KILN-" + "412",
        "covered by our " + "pat" + "ent",
        "see " + "claims " + "7",
        "see " + "priority " + "3",
        "regression " + "bug-" + "X4",
        "this is the " + "crown-" + "jewel",
    ),
    "infrastructure internals": (
        "stored in " + "Supa" + "base",
        "protected by " + "RLS",
        "using the " + "service-" + "role key",
    ),
    "private module or file path": (
        "from " + "kiln_" + "pro.nozzle_intelligence import verdicts",
        "reads " + "kiln_" + "pro/data/thresholds.json",
        "reads " + "printability" + "_pro_" + "overlay.json",
    ),
    _SELF_LABEL_RULE: (
        "the engineering " + "mo" + "at",
        "our " + "MO" + "AT is the curated data",
    ),
}


def _rules_hit(text: str) -> list[str]:
    """Rule names the gate raises for ``text``, via the real judging path."""
    return [rule for rule, _surface, _excerpt in _GATE.judge("fixture", text)]


# ── The anti-recurrence test: no rule may ship unwatched ────────────────────

def test_every_rule_has_a_fixture() -> None:
    """A new rule in RULES must arrive with coverage, or this fails.

    The self-label rule sat unpinned from the day this gate landed; a fixture
    per rule is what keeps that from happening to the next one.
    """
    declared = [rule for rule, _pattern in _GATE.RULES]
    assert len(declared) == len(set(declared)), f"duplicate rule name: {declared}"
    assert set(declared) == set(_FIXTURES), (
        "RULES and the fixtures here have drifted apart.\n"
        f"  rules with no fixture: {sorted(set(declared) - set(_FIXTURES))}\n"
        f"  fixtures with no rule: {sorted(set(_FIXTURES) - set(declared))}\n"
        "Add a planted leak and a clean counterpart for each new rule."
    )


# ── Each rule detects (proves the gate can fail) ────────────────────────────

@pytest.mark.parametrize("rule", sorted(_FIXTURES))
def test_rule_catches_its_planted_leak(rule: str) -> None:
    leaky, _clean = _FIXTURES[rule]
    assert rule in _rules_hit(leaky), (
        f"{rule!r} did not fire on its planted leak — the rule is blind, and "
        f"the gate would report clean.  Text: {leaky!r}"
    )


# ── Each rule stays precise (proves it is not just matching everything) ─────

@pytest.mark.parametrize("rule", sorted(_FIXTURES))
def test_clean_counterpart_trips_no_rule(rule: str) -> None:
    _leaky, clean = _FIXTURES[rule]
    assert _rules_hit(clean) == [], (
        f"legitimate copy tripped a rule while exercising {rule!r} — a gate "
        f"that flags product wording gets muted.  Text: {clean!r}"
    )


@pytest.mark.parametrize(
    ("rule", "text"),
    [(rule, text) for rule, texts in sorted(_ALTERNATIVES.items()) for text in texts],
)
def test_every_alternative_within_a_rule_is_detected(rule: str, text: str) -> None:
    """Each branch of a rule's alternation fires on its own.

    Without this, deleting one field name or one named source from a rule
    leaves the coarse per-rule fixture passing while that specific thing stops
    being detected — the same silent blinding, one alternative at a time.
    """
    assert rule in _rules_hit(text), (
        f"{rule!r} no longer detects this alternative — it can now be served "
        f"to a client unflagged.  Text: {text!r}"
    )


def test_alternatives_cover_every_rule() -> None:
    """Each rule declares its branches here, so a new rule cannot arrive with
    only a single coarse fixture."""
    assert set(_ALTERNATIVES) == set(_FIXTURES), (
        f"rules missing per-alternative coverage: "
        f"{sorted(set(_FIXTURES) - set(_ALTERNATIVES))}"
    )


def test_empty_text_is_not_a_leak() -> None:
    assert _GATE.judge("fixture", "") == []


# ── The wiring around the rules ────────────────────────────────────────────

def test_persona_chain_is_wired() -> None:
    """The gate borrows the public-language gate's persona rules; if that
    import silently breaks, served texts stop being judged for them."""
    find_violations = _GATE._persona_rules()
    assert callable(find_violations), (
        "the public-language rules did not load — judge() would skip them "
        "and the gate would still print clean"
    )
    # A phrase the public-language gate has always caught, so this test does
    # not depend on which revision of that gate is checked out.
    persona = "notes from the " + "war-" + "room"
    hits = _GATE.judge("fixture", persona, find_violations)
    assert any(rule.startswith("persona/process:") for rule, _s, _e in hits), (
        f"persona phrasing was not flagged through judge(): {hits}"
    )
    # …and the same text is clean without the chain, so the assertion above is
    # really testing the chain rather than one of the six local rules.
    assert _GATE.judge("fixture", persona) == []


def test_allowlist_suppresses_only_the_named_row(monkeypatch) -> None:
    """The allowlist is keyed by (surface, rule), not by rule alone."""
    leaky, _clean = _FIXTURES[_SELF_LABEL_RULE]
    assert _SELF_LABEL_RULE in _rules_hit(leaky)

    monkeypatch.setattr(
        _GATE, "_ALLOWLIST", frozenset({("reviewed", _SELF_LABEL_RULE)})
    )
    assert _GATE.judge("reviewed", leaky) == [], "allowlisted row still fired"
    assert [r for r, _s, _e in _GATE.judge("other", leaky)] == [_SELF_LABEL_RULE], (
        "an allowlist row leaked onto a different surface"
    )


def test_allowlist_ships_empty() -> None:
    """Nothing is exempt today.  A row appearing here without a reason comment
    beside it in the gate is the cheap way to silence a real finding."""
    assert frozenset() == _GATE._ALLOWLIST


# ── The live backstop: the static surfaces really are clean ────────────────

def test_static_served_surfaces_are_clean() -> None:
    """Manifest + shipped data notes, judged for real.  ``--static-only``
    skips the server/CLI import so this stays fast; CI runs the full sweep."""
    assert _GATE.main(["--static-only"]) == 0
