"""Tests for the public-language repository gate."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _load_gate():
    root = Path(__file__).resolve().parents[2]
    script = root / "scripts" / "check_public_language.py"
    spec = importlib.util.spec_from_file_location("check_public_language", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_GATE = _load_gate()


_INTERNAL_TIER = "".join(("found", "er"))


def test_catches_the_internal_tier_name_in_source() -> None:
    text = f"# measured on the {_INTERNAL_TIER}'s A1"
    findings = _GATE.find_violations(text, source="kiln/tests/test_example.py")
    assert "internal tier name" in [finding.rule for finding in findings]


def test_catches_the_internal_tier_surface_in_any_file() -> None:
    text = f"three printer models on the {_INTERNAL_TIER} dashboard's tile"
    findings = _GATE.find_violations(text, source="README.md")
    assert [finding.rule for finding in findings] == ["internal tier surface"]


def test_catches_the_internal_tier_name_in_a_commit_message() -> None:
    text = f"fix: the bit the {_INTERNAL_TIER} measured on an A1"
    findings = _GATE.find_violations(text, source="abcd123", commit_message=True)
    assert "internal tier name" in [finding.rule for finding in findings]


def test_allows_the_ordinary_word_in_prose() -> None:
    text = f"a small team; one {_INTERNAL_TIER} reads everything."
    assert _GATE.find_violations(text, source="policies/TERMS_OF_USE.md") == []


def test_catches_retired_provider_name() -> None:
    text = "provider=" + "".join(("sculp", "teo"))
    findings = _GATE.find_violations(text, source="example.py")
    assert [finding.rule for finding in findings] == ["retired public provider"]


def test_catches_unannounced_relationship_status() -> None:
    text = "Integration is " + "pending partner " + "credentials."
    findings = _GATE.find_violations(
        text,
        source="README.md",
    )
    assert [finding.rule for finding in findings] == [
        "unannounced relationship status"
    ]


def test_catches_internal_review_attribution() -> None:
    text = "This threshold was panel-" + "approved."
    findings = _GATE.find_violations(
        text,
        source="module.py",
    )
    assert [finding.rule for finding in findings] == ["internal review process"]


def test_catches_review_persona_phrases() -> None:
    # The forms that slipped past the older panel-only pattern.  Each fixture
    # is split so no single source line here matches the rule itself.
    for text in (
        "Judges" + "' verdict on placement: wire into the 4 canonical entry points",
        "the " + "judges asked for a smaller diff",
        "war-" + "room notes from the outage",
        "ship-" + "gate passed",
        "panel " + "verdict: ship it",
        "Judges" + ": keep the seam",
    ):
        findings = _GATE.find_violations(text, source="module.py")
        assert [finding.rule for finding in findings] == ["internal review process"], text


def test_allows_verb_judges_and_bare_panel() -> None:
    # "judges" as a verb and "panel" as the MCP Apps panel are ordinary
    # implementation language, not review attribution.
    for text in (
        "a new print judges its heaters afresh",
        "the composer re-centres a group it judges off ITS plate",
        "the MCP Apps panel renders the mesh inline",
        "``renders`` is what the panel declared, not the geometry verdict",
        "a judge of character",
        "shipping the gate",
    ):
        assert _GATE.find_violations(text, source="module.py") == [], text


def test_catches_commit_metadata() -> None:
    message = (
        "fix: neutral subject\n\nCo-"
        "Authored-By: Agent <agent@example.com>"
    )
    findings = _GATE.find_violations(
        message,
        source="COMMIT_EDITMSG",
        commit_message=True,
    )
    assert [finding.rule for finding in findings] == ["agent-work metadata"]


def test_allows_neutral_implementation_language() -> None:
    findings = _GATE.find_violations(
        "A hygroscopic material needs one corroborating moisture symptom.",
        source="module.py",
    )
    assert findings == []
