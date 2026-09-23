"""Tests for the public-language repository gate."""

from __future__ import annotations

import importlib.util
import io
import os
import re
import subprocess
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
# The private-rules socket stays OFF for these tests whatever this machine's
# git config says; the tests that exercise it plug rules in by hand.
if _GATE._leak_gate_module() is not None:
    _GATE._leak_gate_module()._PRIVATE_RULES[:] = [None]


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


class TestBundledDataProvenanceDoor:
    """The commit-time half of kiln.data_note_contract: a catalogue string that
    carries research provenance is refused at the commit, not at the PR."""

    def test_a_leaky_data_string_is_a_finding_and_a_clean_one_is_not(self):
        gate = _load_gate()
        leaky = '{"ender3": {"notes": "per the vendor wiki (wiki.example.com/x, read 2026-09-16)"}}'
        found = gate.data_note_findings("kiln/src/kiln/data/safety_profiles.json", leaky)
        assert [f.text for f in found] == ["safety_profiles.json:ender3.notes"]
        assert "a link" in found[0].rule
        clean = '{"ender3": {"notes": "the vendor states the bed moves in Z"}}'
        assert gate.data_note_findings("kiln/src/kiln/data/safety_profiles.json", clean) == []

    def test_only_bundled_data_json_is_judged_and_a_material_may_link_its_maker(self):
        gate = _load_gate()
        assert gate.data_note_findings("kiln/src/kiln/server.py", "https://example.com") == []
        assert gate.data_note_findings("kiln/src/kiln/data/scad_libraries/x.json", '{"a": "https://x.com"}') == []
        bought = '{"pla": {"sources": {"manufacturer": "https://www.example.com"}}}'
        assert gate.data_note_findings("kiln/src/kiln/data/material_catalog.json", bought) == []

    def test_a_motion_note_is_also_held_to_its_length(self):
        gate = _load_gate()
        long_note = '{"k1": {"motion": {"_sources": {"z_carrier": {"class": "vendor_config", "note": "%s"}}}}}' % ("x" * 421)
        found = gate.data_note_findings("kiln/src/kiln/data/printer_intelligence.json", long_note)
        assert found and "421 chars" in found[0].rule


def test_a_commit_message_carrying_research_provenance_is_refused() -> None:
    """The repository's history is as public as its tree: a source file:line
    pin, a wiki page, a fetch date or a research project's name in a commit
    message is the trail a comment is refused for."""
    for text in (
        "Pinned from DevMapping.cpp:127",
        "per gcode/host/M115.cpp:63-75 @ 2.1.2.4",
        "as wiki.bambulab.com/en/hms/home says",
        "read 2026-09-20",
        "cross-checked against pybambu",
    ):
        findings = _GATE.find_violations(text, source="abcd123", commit_message=True)
        assert [f.rule for f in findings] == ["research provenance"], text


def test_a_commit_message_naming_how_a_sequence_was_captured_is_refused() -> None:
    """The history is as public as the tree: which slicer build a sequence
    was read from, the file inside its profile bundle, the capture method
    and the date the work was done are the same trail in a message."""
    for text in (
        "Captured from PrusaSlicer 9.9.9's own command line",
        'Source: "Acme 0.4 nozzle template widget_gcode.json"',
        "harvested from the vendor index on 2030-01-02",
        "the reader matches it in widget.cpp L12",
    ):
        findings = _GATE.find_violations(text, source="abcd123", commit_message=True)
        assert [f.rule for f in findings] == ["research provenance"], text
    # A body wraps at 72 columns; a build split over two lines is one
    # sentence, reported once, on the line it starts on.
    wrapped = "Pin the end.\n\nThe values are read from the PrusaSlicer\n9.9.9 slice the start came from.\n"
    findings = _GATE.find_violations(wrapped, source="abcd123", commit_message=True)
    assert [(f.line, f.rule) for f in findings] == [(3, "research provenance")]


def test_a_commit_may_say_whose_a_sequence_is() -> None:
    for text in (
        "Every Bambu printer now starts and finishes each print the way Bambu designed it",
        "OrcaSlicer 2.3.2 and BambuStudio 02.06.00.51 both reject --export-gcode",
        "The end sequence is the maker's own, expanded at the part's height",
    ):
        assert _GATE.find_violations(text, source="abcd123", commit_message=True) == [], text


def test_a_dependency_bump_and_kilns_own_links_pass_the_commit_rule() -> None:
    for text in (
        "Bumps stripe from 15.6.0 to 15.6.1 (https://github.com/stripe/stripe-python)",
        "See https://kiln3d.com/pricing and github.com/codeofaxel/Kiln/issues/12",
        "Read from the maker's own client and its published guides.",
    ):
        assert _GATE.find_violations(text, source="abcd123", commit_message=True) == [], text


def _plug(rules):
    gate = _GATE._leak_gate_module()
    saved = list(gate._PRIVATE_RULES)
    gate._PRIVATE_RULES[:] = [rules]
    return gate, saved


def test_a_commit_message_is_held_to_the_private_rules_when_plugged() -> None:
    """The words a public rule must not spell out reach the commit-message
    door through the same socket as the tree, across a 72-column wrap."""
    gate = _GATE._leak_gate_module()
    rules = gate.PrivateRules(((
        "a widget method", re.compile(r"(?i:\bfrobnicate the (?:sprocket|gizmo)s?\b)")),),
        frozenset(), gate.FINGERPRINT_WINDOW)
    gate, saved = _plug(rules)
    try:
        wrapped = "Park the head.\n\nFirst we frobnicate the\nsprockets, then park.\n"
        found = _GATE.find_violations(wrapped, source="abcd123", commit_message=True)
        assert [(f.line, f.rule) for f in found] == [(3, "private research")]
    finally:
        gate._PRIVATE_RULES[:] = saved
    assert _GATE.find_violations(wrapped, source="abcd123", commit_message=True) == []


def _git(repo: Path, *args: str) -> str:
    """git in a scratch repo, never under an inherited hook's GIT_DIR."""
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@x", *args],
        check=True, capture_output=True, text=True,
        env={k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
    ).stdout.strip()


def test_the_outgoing_door_reads_every_commit_no_remote_has(tmp_path, monkeypatch) -> None:
    """A rebased or --no-verify commit never met the commit-message check;
    the pre-push door reads every commit a push would publish."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "a.txt").write_text("a\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-qm", "cross-checked against pybambu")  # already public
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    (repo / "b.txt").write_text("b\n")
    _git(repo, "add", "b.txt")
    _git(repo, "commit", "-qm", "Pinned from DevMapping.cpp:127")
    (repo / "c.txt").write_text("c\n")
    _git(repo, "add", "c.txt")
    _git(repo, "commit", "-qm", "A clean message")
    head = _git(repo, "rev-parse", "HEAD")
    monkeypatch.setattr(_GATE, "_ROOT", repo)
    lines = f"refs/heads/x {head} refs/heads/main {'0' * 40}\n"
    messages = [m.strip() for _s, m in _GATE._outgoing_messages(lines)]
    assert messages == ["A clean message", "Pinned from DevMapping.cpp:127"]
    assert _GATE._outgoing_messages(f"(delete) {'0' * 40} refs/heads/old {head}\n") == []
    monkeypatch.setattr(sys, "stdin", io.StringIO(lines))
    assert _GATE.main(["--outgoing"]) == 2
    monkeypatch.setattr(sys, "stdin", io.StringIO(f"refs/heads/x {_git(repo, 'rev-parse', 'HEAD~2')} refs/heads/main {'0' * 40}\n"))
    assert _GATE.main(["--outgoing"]) == 0
