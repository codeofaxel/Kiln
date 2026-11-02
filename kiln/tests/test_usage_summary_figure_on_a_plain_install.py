"""``printer_usage_summary`` on a plain install carries the person's own extra-printer figure.

On an install without kiln-pro (every customer's) the tool used to answer the
count of printers registered here and a link to the pricing page, and nothing
about what those printers add to the person's own bill.  Kiln's servers price
any number of printers on the caller's plan, free on every plan
(``preview_overage_charge``), so the tool now asks them with the count and
answers their figure.  When no figure comes back the count still does, with
why there is no figure, said once in the shared voice, and the pricing link;
a figure that did not come back is never a number.

Kiln's servers are stood in at the one served door
(``kiln.server._pro_api_call``) and kiln-pro is removed from the import
system.
"""

from __future__ import annotations

import re
import sys
from types import SimpleNamespace
from typing import Any

import pytest

import kiln.server as srv
from kiln.served_answer import SESSION_EXPIRED_CODE

_PRICING = "https://kiln3d.com/pricing"


class _FakeMCP:
    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}

    def tool(self, *a, **k):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn

        return deco


class _Servers:
    """Kiln's servers at the served door: records each ask, answers with *answer* at *status*."""

    def __init__(self, answer: Any, status: int | None):
        self.answer, self.status = answer, status
        self.asks: list[tuple[str, dict]] = []

    def call(self, tool, /, _timeout=None, _asked_by_user=True, _http=None, **kwargs):
        self.asks.append((tool, dict(kwargs)))
        if _http is not None and self.status is not None:
            _http["status"] = self.status
        return dict(self.answer)


@pytest.fixture
def plain_install(monkeypatch):
    for name in [n for n in sys.modules if n == "kiln_pro" or n.startswith("kiln_pro.")]:
        monkeypatch.setitem(sys.modules, name, None)
    monkeypatch.setitem(sys.modules, "kiln_pro", None)


@pytest.fixture
def summary(monkeypatch, plain_install):
    """The registered tool with five printers here, the local plan read as Free."""
    import kiln.licensing as licensing
    from kiln.plugins.enterprise_tools import _EnterpriseToolsPlugin

    monkeypatch.setattr(srv, "_check_auth", lambda scope: None)
    monkeypatch.setattr(srv, "_get_registry", lambda: SimpleNamespace(count=5))
    monkeypatch.setattr(licensing, "get_tier", lambda: licensing.LicenseTier("free"))
    mcp = _FakeMCP()
    _EnterpriseToolsPlugin().register(mcp)
    return mcp.tools["printer_usage_summary"]


@pytest.fixture
def servers(monkeypatch):
    def install(answer: Any, *, status: int | None = 200, bearer: tuple = ("bearer", None)) -> _Servers:
        s = _Servers(answer, status)
        monkeypatch.setattr(srv, "_served_bearer", lambda: bearer)
        monkeypatch.setattr(srv, "_pro_api_call", s.call)
        return s

    return install


#: What the servers answer for five printers on a Business plan (the shape
#: ``preview_overage_charge`` serves; the numbers here are this fixture's).
_BUSINESS_FIVE = {
    "success": True,
    "your_plan": "business",
    "plan": "business",
    "for_your_plan": True,
    "printers": 5,
    "included_printers": 3,
    "printer_limit": 50,
    "extra_printers": 2,
    "extra_printer_rate_usd": 15.0,
    "extra_printers_monthly_usd": 30.0,
    "printers_past_limit": 0,
    "summary": "Your Business plan includes 3 printers. 5 printers is 3 included and 2 extra at $15/mo each: $30/mo on top of your plan.",
    "pricing_url": _PRICING,
}

_FIGURE_KEYS = ("extra_printers", "extra_printer_rate_usd", "extra_printers_monthly_usd", "included_printers")


def _a_sentence_for_a_person(text: str) -> None:
    assert not re.search(r"\b[A-Z][A-Z0-9]*_[A-Z0-9_]+\b", text), f"a wire code in the words: {text!r}"
    for word in ("kiln-pro", "kiln_pro", "this server", "API"):
        assert word not in text, f"{word!r} in the words: {text!r}"


def test_signed_in_the_count_is_priced_on_the_servers_for_the_accounts_plan(summary, servers):
    s = servers(_BUSINESS_FIVE)
    out = summary()

    assert s.asks == [("preview_overage_charge", {"active_printer_count": 5})]
    assert out["success"] is True
    # The figure is the servers', for the plan the ACCOUNT holds, not this
    # machine's local reading (stood in as Free above).
    assert out["your_plan"] == "business"
    assert out["extra_printers_monthly_usd"] == 30.0
    assert out["summary"] == _BUSINESS_FIVE["summary"]
    assert out["printers"] == 5


def test_signed_out_the_count_comes_back_with_why_once_and_no_figure(summary, servers):
    s = servers(_BUSINESS_FIVE, bearer=("", None))
    out = summary()

    assert s.asks == [], "nothing is asked without a sign-in"
    assert out["success"] is True
    assert out["printers"] == 5 and out["pricing_url"] == _PRICING
    assert out["why"] == "signed_out"
    assert not any(key in out for key in _FIGURE_KEYS), "a figure that did not come back is never a number"
    text = out["summary"]
    assert text.startswith("5 printers are registered with this Kiln. ")
    assert "Kiln is signed out" in text and "Sign in and ask again" in text
    assert text.endswith(f"What extra printers cost on each plan: {_PRICING}")
    assert text.count("signed out") == 1
    _a_sentence_for_a_person(text)


def test_a_session_that_ended_is_told_to_sign_back_in(summary, servers):
    """Lapsed: the install had a sign-in and can no longer renew it."""
    servers(_BUSINESS_FIVE, bearer=("", "Your Kiln session has ended."))
    out = summary()
    assert out["why"] == "signed_out" and out["why_code"] == SESSION_EXPIRED_CODE
    assert "your Kiln session has expired" in out["summary"] and "Sign back in" in out["summary"]
    assert not any(key in out for key in _FIGURE_KEYS)


def test_offline_says_reconnect(summary, servers):
    offline = {
        "status": "error", "success": False, "error": "The tool asks Kiln's servers.",
        "code": "SERVER_UNREACHABLE", "why": "offline", "why_code": "SERVER_UNREACHABLE",
    }
    servers(offline, status=None)
    out = summary()
    assert out["success"] is True and out["printers"] == 5
    assert out["why"] == "offline"
    assert "this computer is offline" in out["summary"] and "Reconnect to the internet" in out["summary"]
    assert not any(key in out for key in _FIGURE_KEYS)
    _a_sentence_for_a_person(out["summary"])


def test_the_servers_own_refusal_rides_in_their_words(summary, servers):
    refusal = {
        "status": "error", "code": "CLIENT_TOO_OLD",
        "error": "This Kiln is older than the servers accept; update it with upgrade_kiln.",
    }
    servers(refusal, status=426)
    out = summary()
    assert out["why"] == "refused" and out["why_code"] == "CLIENT_TOO_OLD"
    assert "update it with upgrade_kiln" in out["summary"]
    assert out["summary"].endswith(_PRICING)
    assert not any(key in out for key in _FIGURE_KEYS)


def test_an_answer_that_is_not_a_figure_is_not_relayed_as_one(summary, servers):
    servers({"success": False, "error": "active_printer_count must be a non-negative integer"})
    out = summary()
    assert out["success"] is True and out["printers"] == 5
    assert out["why"] == "unanswered"
    assert not any(key in out for key in _FIGURE_KEYS)
