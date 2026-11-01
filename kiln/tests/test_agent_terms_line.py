"""Every agent-facing surface public Kiln ships carries the one
no-reverse-engineering line, read from ``kiln.tiers_and_terms`` -- never a
paraphrase.  kiln-pro's ``audit_agent_terms_line.py`` checks its own surfaces
and this repo at the shipping revision; this is the half that runs where the
text is authored."""
from __future__ import annotations

import re
from pathlib import Path

from kiln.tiers_and_terms import NO_REVERSE_ENGINEERING_LINE, TIERS_AND_TERMS

REPO = Path(__file__).resolve().parents[2]


def test_line_has_its_three_properties():
    line = NO_REVERSE_ENGINEERING_LINE
    assert "Terms of Use" in line, "an agent weighs a terms constraint as binding; name it"
    assert "AGPL" in line and "own printers" in line, "the affirmative half keeps agents from refusing adjacent legitimate work"
    assert re.search(r"analysis, capture or decompilation tool", line), "name the tools a model would reach for"
    assert not re.search(r"§|\\b10\\.\\d", line), "no section number: Terms get renumbered"


def test_the_front_terms_block_does_not_carry_it():
    """Glued to TIERS_AND_TERMS (as first shipped, 46c574d8) the line sat in
    the first screen of the connect instructions and moved CONSENT 469
    characters later, past the cut some clients make: a rule about third
    parties must never cost the person the consent paragraph."""
    assert NO_REVERSE_ENGINEERING_LINE not in TIERS_AND_TERMS


def test_connect_instructions_say_it_once_and_after_safety_and_consent(monkeypatch):
    import types

    from kiln import server

    monkeypatch.setattr(server, "_get_registry", lambda: types.SimpleNamespace(list_names=lambda: ["garage"]))
    text = server._build_instructions()
    assert text.count(NO_REVERSE_ENGINEERING_LINE) == 1
    at = text.index(NO_REVERSE_ENGINEERING_LINE)
    assert at > text.index("SAFETY:") and at > text.index("CONSENT:")


def test_get_started_carries_it(monkeypatch, tmp_path):
    """get_started is the first call every agent is told to make, and a tool
    result no client truncates."""
    import kiln.server as srv

    monkeypatch.setenv("KILN_AUTH_HOME", str(tmp_path))
    monkeypatch.setattr("kiln.terms.sync_to_account", lambda **k: None)
    gs = next(t.fn for t in srv.mcp._tool_manager.list_tools() if t.name == "get_started")
    assert gs()["terms"] == NO_REVERSE_ENGINEERING_LINE


def test_kilns_own_agent_loop_carries_it():
    src = (REPO / "kiln" / "src" / "kiln" / "agent_loop.py").read_text()
    assert "+ NO_REVERSE_ENGINEERING_LINE" in src


def test_skill_md_carries_the_line_verbatim():
    text = (REPO / "SKILL.md").read_text()
    assert NO_REVERSE_ENGINEERING_LINE in text
    assert "https://kiln3d.com/terms" in text


def test_terms_of_use_bind_agents_and_tools_too():
    text = (REPO / "policies" / "TERMS_OF_USE.md").read_text()
    item = text[text.index("7. Reverse-engineer"): text.index("8. Use Kiln to build")]
    for needle in ("AI agent", "automated analysis", "AGPL-3.0", "own printers"):
        assert needle in item, needle
