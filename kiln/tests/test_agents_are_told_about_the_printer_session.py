"""An agent is told Kiln can get to know a printer, and when to offer it.

The guided session (``printer_bench``) only helps if an agent reaches for it
at the right moment.  Each surface an agent reads first is driven here and
must carry the one fact and the one rule, read from ``kiln.bench``.
"""

from __future__ import annotations

import types
from pathlib import Path

import pytest

from kiln.bench import AGENT_LEARN_LINE, LEARN_LINE, LEARN_WHEN

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def install(tmp_path, monkeypatch):
    """A signed-out install with one printer registered."""
    from kiln import server

    monkeypatch.setenv("KILN_AUTH_HOME", str(tmp_path))
    monkeypatch.delenv("KILN_LICENSE_KEY", raising=False)
    monkeypatch.setattr("kiln.terms.sync_to_account", lambda **k: None)
    monkeypatch.setattr(server, "_get_registry", lambda: types.SimpleNamespace(list_names=lambda: ["garage"]))
    monkeypatch.setattr(server, "_marketplace_registry", types.SimpleNamespace(connected=[]))
    return server


def test_the_line_names_the_tool_and_both_kinds_of_printer():
    assert "`printer_bench`" in LEARN_LINE
    assert "new to Kiln" in LEARN_LINE and "only in part" in LEARN_LINE
    assert "'later'" in LEARN_WHEN, "a person who says later is not pressed"
    assert AGENT_LEARN_LINE == f"{LEARN_LINE} {LEARN_WHEN}"
    assert "—" not in AGENT_LEARN_LINE, "no em dashes in public prose"


class TestConnectInstructions:
    def test_an_install_with_a_printer_hears_it_once_after_consent(self, install):
        text = install._build_instructions()
        assert text.count(LEARN_LINE) == 1
        assert text.index(AGENT_LEARN_LINE) > text.index("CONSENT:"), "it must never push consent down"

    def test_a_fresh_install_hears_it_too(self, install, monkeypatch):
        monkeypatch.setattr(install, "_get_registry", lambda: types.SimpleNamespace(list_names=lambda: []))
        assert AGENT_LEARN_LINE in install._build_instructions()

    def test_consent_stays_above_the_cut(self, install):
        assert install._build_instructions().index("CONSENT:") < 5000


def test_get_started_says_it(install):
    gs = next(t.fn for t in install.mcp._tool_manager.list_tools() if t.name == "get_started")
    assert any(AGENT_LEARN_LINE in step for step in gs()["quick_start"])


def test_the_skill_manifest_has_the_workflow():
    from kiln.skill_manifest import SkillManifest

    steps = SkillManifest().workflows["get_to_know_a_printer"]
    assert AGENT_LEARN_LINE in steps[0]
    assert all(step.startswith("printer_bench(") for step in steps)


def test_the_tool_says_when_to_reach_for_it(install):
    tool = next(t for t in install.mcp._tool_manager.list_tools() if t.name == "printer_bench")
    assert LEARN_WHEN in " ".join((tool.description or "").split())


@pytest.mark.parametrize("path", ["SKILL.md", "plugins/hermes/skills/kiln/SKILL.md"])
def test_skill_md_carries_the_line_verbatim(path):
    assert AGENT_LEARN_LINE in (REPO / path).read_text(encoding="utf-8")
