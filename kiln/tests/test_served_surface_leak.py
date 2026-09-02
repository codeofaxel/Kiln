"""CI backstop for the served-surface leak gate.

Everything an MCP client receives — tool and parameter descriptions, prompt
and resource text, the server instructions, ``kiln --help``, the bundled
paid-tool manifest, and the ``_meta`` notes in the shipped knowledge files —
is as public as the README.  ``scripts/audit_served_surface_leak.py`` judges
that text for wording which names HOW the paid depth is built rather than
WHAT a tier unlocks.

A green run proves we did not ship the method.  It proves nothing on its own,
though: a gate whose pattern no longer matches anything also runs green, and
says nothing about having stopped looking.  So the tests below plant a real
self-label into a served surface and assert the gate FAILS on it, then remove
it and assert clean — and pin each rule literal, because rewording one is the
edit that turns this gate into a no-op.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _ROOT / "scripts" / "audit_served_surface_leak.py"


def _load_gate():
    spec = importlib.util.spec_from_file_location("audit_served_surface_leak", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


_GATE = _load_gate()

def _j(*parts: str) -> str:
    """Join fixture fragments into one literal.

    The shapes below are assembled from parts on purpose: the sibling
    leak gates scan ``kiln/tests`` for these very literals, so spelling
    them out whole would make this file trip them and need an exemption
    everywhere.  Same reason ``check_public_language.py`` splits its
    retired-provider token.
    """
    return "".join(parts)


_LABEL = _j("mo", "at")
_PRIV = _j("kiln", "_pro")

#: The exact wording shapes the 2026-09-02 sweep found on served surfaces.
#: Each must stay caught; a rule that stops matching its row is a rule that
#: has been reworded into silence.  Every entry in ``RULES`` needs a row here
#: (pinned by ``test_every_rule_has_a_fixture``), so a rule added later
#: cannot ship unpinned the way the self-label rule did.
_LEAK_SHAPES: tuple[tuple[str, str], ...] = (
    ("private-tier self-label", f"these curated values are the engineering {_LABEL}"),
    ("private module or file path", f"see ``{_PRIV}/_rest/org_admin_authz.py``"),
    ("private module or file path", f"pipes through :mod:`{_PRIV}.recovery.mid_print_engine`"),
    ("internal docket or claim shorthand", "Patent: KILN-021 Priority 8 (crown jewel)."),
    ("internal docket or claim shorthand", "the improved prompt, per claim 51's overlap rule"),
    ("paid field inventory", "Engineering depth (agent_notes + failure_modes) is Pro"),
    ("research bibliography", "CNC Kitchen XT-CF20 destructive cross-section"),
    ("research bibliography", "grounded in filament datasheets (2024-2025)"),
    ("infrastructure internals", "On the hosted server (Supabase backend + a JWT tenant)"),
    ("infrastructure internals", "the permission check the cloud RLS will run"),
)

#: Wording that sells the upgrade or names the interface.  None of it may trip
#: a rule: a gate that flags the funnel gets switched off by the next person
#: who has to ship.
_ALLOWED_WORDING: tuple[str, ...] = (
    "Requires Kiln Business. Pricing: https://kiln3d.com/pricing",
    "Engineering depth (what is going wrong and how to fix it) is available in Kiln Pro.",
    "Kiln Pro adds per-printer firmware quirks and failure-mode playbooks.",
    "Decoration engine (patent pending).",
    "Needs ``KILN_CLOUD_SUPABASE_SECRET`` or ``SUPABASE_SERVICE_ROLE_KEY``.",
    "Grounded in ASTM D638 and ISO 527 tensile methodology.",
    "Jobs are executed in priority order, with FIFO tie-breaking.",
)


def _rules_hit(text: str) -> set[str]:
    return {rule for rule, _, _ in _GATE.judge("tool:example", text)}


# ── The rule table still catches what it was built to catch ────────────────

@pytest.mark.parametrize("rule, text", _LEAK_SHAPES)
def test_each_leak_shape_is_still_caught(rule: str, text: str) -> None:
    """Fails the moment a RULES literal is reworded into silence."""
    assert rule in _rules_hit(text), (
        f"the {rule!r} rule no longer matches {text!r} — if its pattern was "
        "reworded, the gate now runs green without looking"
    )


def test_every_rule_has_a_fixture() -> None:
    """Every rule in the table is exercised by a planted leak above.

    This is the part that stops the gap recurring rather than closing this
    one instance: a rule added later with no fixture fails here immediately,
    instead of sitting undetectable the way the self-label rule did from the
    day the gate landed.
    """
    declared = {rule for rule, _ in _GATE.RULES}
    covered = {rule for rule, _ in _LEAK_SHAPES}
    missing = sorted(declared - covered)
    assert not missing, (
        f"rules with no planted fixture: {missing}. Add a row to _LEAK_SHAPES "
        "so the rule is proven to fire, not merely present."
    )


@pytest.mark.parametrize("text", _ALLOWED_WORDING)
def test_funnel_and_interface_wording_is_not_flagged(text: str) -> None:
    assert not _rules_hit(text)


# ── The gate actually fails on a leak planted in a real served surface ─────

def _plant(data_dir: Path, note: str) -> None:
    (data_dir / "design_knowledge").mkdir(parents=True, exist_ok=True)
    (data_dir / "design_knowledge" / "planted.json").write_text(
        json.dumps({"_meta": {"description": note}}), encoding="utf-8"
    )


def test_planted_self_label_in_a_data_note_fails_the_gate(tmp_path, monkeypatch, capsys) -> None:
    """A genuine self-label on a served surface must exit non-zero and be named."""
    monkeypatch.setattr(_GATE, "_DATA", tmp_path)
    monkeypatch.setattr(_GATE, "_PKG", tmp_path)
    _plant(tmp_path, f"these curated values are the engineering {_LABEL}")

    assert _GATE.main(["--static-only"]) == 2
    out = capsys.readouterr().out
    assert "planted.json" in out
    assert "private-tier self-label" in out


def test_gate_is_clean_once_the_plant_is_removed(tmp_path, monkeypatch) -> None:
    """The same surface, benign wording: clean.  Pins that the failure above
    came from the planted text and not from the fixture itself."""
    monkeypatch.setattr(_GATE, "_DATA", tmp_path)
    monkeypatch.setattr(_GATE, "_PKG", tmp_path)
    _plant(tmp_path, "Engineering depth is available in Kiln Pro; see https://kiln3d.com/pricing.")

    assert _GATE.main(["--static-only"]) == 0


def test_planted_leak_is_caught_on_every_served_rule(tmp_path, monkeypatch) -> None:
    """Every rule, planted through the real data-note reader, not just judge()."""
    monkeypatch.setattr(_GATE, "_DATA", tmp_path)
    monkeypatch.setattr(_GATE, "_PKG", tmp_path)
    for rule, text in _LEAK_SHAPES:
        _plant(tmp_path, text)
        hits = {h[0] for s, t in _GATE.data_meta_texts() for h in _GATE.judge(s, t)}
        assert rule in hits, f"{rule!r} not raised for a planted {text!r}"


# ── The live tree stays green ──────────────────────────────────────────────

def test_live_served_surface_is_clean() -> None:
    """The real registry, CLI, manifest and data notes judge clean.

    Meaningful only because the plant tests above prove the gate can fail.
    """
    result = subprocess.run(
        [sys.executable, str(_SCRIPT)],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        timeout=900,
    )
    assert result.returncode == 0, result.stdout + result.stderr
