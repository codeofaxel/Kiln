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


def test_shared_block_carries_the_line_once():
    assert TIERS_AND_TERMS.count(NO_REVERSE_ENGINEERING_LINE) == 1


def test_skill_md_carries_the_line_verbatim():
    text = (REPO / "SKILL.md").read_text()
    assert NO_REVERSE_ENGINEERING_LINE in text
    assert "https://kiln3d.com/terms" in text


def test_terms_of_use_bind_agents_and_tools_too():
    text = (REPO / "policies" / "TERMS_OF_USE.md").read_text()
    item = text[text.index("7. Reverse-engineer"): text.index("8. Use Kiln to build")]
    for needle in ("AI agent", "automated analysis", "AGPL-3.0", "own printers"):
        assert needle in item, needle
