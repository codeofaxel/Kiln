"""An install with no printer says it can still order a print.

A plain install (no printer, no print-service key of its own) can get a price
from a print service and place and follow an order through Kiln's servers
once the person is signed in.  When nothing an agent reads says so, an agent
with no printer goes looking for another connector to order with.  Each
surface an agent reads first is driven here and must say the one fact, read
from ``kiln.tiers_and_terms``.
"""

from __future__ import annotations

import types
from pathlib import Path

import pytest

from kiln.tiers_and_terms import (
    AGENT_ACCOUNT_NUDGE,
    AGENT_ORDERING_LINE,
    ORDERING_LINE,
    ORDERING_STEPS,
)

REPO = Path(__file__).resolve().parents[2]

#: The tools an order takes, in the order it takes them.
ORDER_TOOLS = (
    "kiln_signin",
    "fulfillment_materials",
    "fulfillment_quote",
    "issue_preview_token",
    "issue_shipping_confirmation_token",
    "fulfillment_order",
    "fulfillment_order_status",
)


@pytest.fixture
def plain_install(tmp_path, monkeypatch):
    """No printer, no marketplace, no print-service key, signed out."""
    from kiln import server

    monkeypatch.setenv("KILN_AUTH_HOME", str(tmp_path))
    monkeypatch.delenv("KILN_LICENSE_KEY", raising=False)
    monkeypatch.setattr("kiln.terms.sync_to_account", lambda **k: None)
    monkeypatch.setattr(server, "_CRAFTCLOUD_API_KEY", "")
    monkeypatch.setattr(server, "_get_registry", lambda: types.SimpleNamespace(list_names=lambda: []))
    monkeypatch.setattr(server, "_marketplace_registry", types.SimpleNamespace(connected=[]))
    return server


def test_the_line_names_every_tool_in_order_and_the_persons_confirmation():
    at = [AGENT_ORDERING_LINE.index(f"`{t}`") for t in ORDER_TOOLS]
    assert at == sorted(at)
    assert "signed in to Kiln (a free account)" in ORDERING_LINE
    assert "No other Kiln connector is needed" in ORDERING_LINE
    assert "only after the person's own confirmation" in ORDERING_STEPS
    assert "—" not in AGENT_ORDERING_LINE, "no em dashes in public prose"


class TestConnectInstructions:
    def test_a_fresh_install_is_told_before_consent_and_given_the_tools_after(self, plain_install):
        text = plain_install._build_instructions()
        setup = text[text.index("FIRST-TIME SETUP:"): text.index("SAFETY:")]
        assert ORDERING_LINE in setup
        ordering = text.index("ORDERING: ")
        assert ordering > text.index("CONSENT:"), "the tools must never push consent down"
        assert ORDERING_STEPS in text[ordering:]

    def test_an_install_with_a_printer_still_hears_it(self, plain_install, monkeypatch):
        monkeypatch.setattr(
            plain_install, "_get_registry", lambda: types.SimpleNamespace(list_names=lambda: ["garage"])
        )
        text = plain_install._build_instructions()
        assert text.count(ORDERING_LINE) == 1
        assert text.index(AGENT_ORDERING_LINE) > text.index("CONSENT:")

    def test_its_own_key_keeps_the_direct_wording(self, plain_install, monkeypatch):
        monkeypatch.setattr(plain_install, "_CRAFTCLOUD_API_KEY", "key")
        text = plain_install._build_instructions()
        assert "FULFILLMENT: Craftcloud connected." in text
        assert ORDERING_LINE not in text and "ORDERING: " not in text

    def test_consent_stays_above_the_cut(self, plain_install):
        # Some clients cut the instructions after about five thousand
        # characters; the consent paragraph must start inside them.
        assert plain_install._build_instructions().index("CONSENT:") < 5000


def test_get_started_says_it(plain_install):
    gs = next(t.fn for t in plain_install.mcp._tool_manager.list_tools() if t.name == "get_started")
    steps = gs()["quick_start"]
    assert any(AGENT_ORDERING_LINE in s for s in steps)


def test_the_skill_manifest_has_the_ordering_workflow():
    from kiln.skill_manifest import SkillManifest

    steps = SkillManifest().workflows["order_a_print_without_a_printer"]
    assert ORDERING_LINE in steps[0]
    assert [s.split("(")[0] for s in steps] == list(ORDER_TOOLS)


def test_a_free_account_is_said_to_add_ordering():
    assert "a price from a print service and an order placed" in AGENT_ACCOUNT_NUDGE


def test_no_printer_onboarding_signs_in_before_the_quote():
    from kiln.consumer import get_onboarding

    ob = get_onboarding()
    tools = [s.tool for s in ob.steps]
    assert tools.index("kiln_signin") < tools.index("fulfillment_quote")
    assert ORDERING_LINE in ob.summary


@pytest.mark.parametrize("path", ["SKILL.md", "plugins/hermes/skills/kiln/SKILL.md"])
def test_skill_md_carries_the_line_verbatim(path):
    assert AGENT_ORDERING_LINE in (REPO / path).read_text(encoding="utf-8")
