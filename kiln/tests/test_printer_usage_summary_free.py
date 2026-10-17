"""``printer_usage_summary`` is free on every plan, and states no price itself.

It used to be Enterprise-only and described an Enterprise price the pricing
page does not publish.  Now any plan may ask how many printers are registered
with this Kiln and what they add to the bill.  On an install without kiln-pro
it answers the count and where the prices are; with kiln-pro the numbers come
from the plan's own terms, which kiln-pro's tests pin to the pricing page.
"""

from __future__ import annotations

import re
import sys
from types import SimpleNamespace
from typing import Any

import pytest


class _FakeMCP:
    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}

    def tool(self, *a, **k):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn

        return deco


@pytest.fixture
def summary(monkeypatch):
    """The tool as registered, with two printers registered here."""
    import kiln.server as srv
    from kiln.plugins.enterprise_tools import _EnterpriseToolsPlugin

    monkeypatch.setattr(srv, "_check_auth", lambda scope: None)
    monkeypatch.setattr(srv, "_get_registry", lambda: SimpleNamespace(count=2))
    mcp = _FakeMCP()
    _EnterpriseToolsPlugin().register(mcp)
    return mcp.tools["printer_usage_summary"]


def _without_the_paid_package(monkeypatch) -> None:
    """Every module of the paid package unimportable, as on a plain install."""
    for name in [m for m in sys.modules if m == "kiln_pro" or m.startswith("kiln_pro.")]:
        monkeypatch.setitem(sys.modules, name, None)
    monkeypatch.setitem(sys.modules, "kiln_pro", None)


def test_a_free_plan_is_answered(summary, monkeypatch):
    import kiln.licensing as licensing

    monkeypatch.setattr(licensing, "get_tier", lambda: licensing.LicenseTier("free"))
    _without_the_paid_package(monkeypatch)

    out = summary()

    assert out["success"] is True, out
    assert out["printers"] == 2
    assert out["your_plan"] == "free"
    assert out["pricing_url"] == "https://kiln3d.com/pricing"
    assert "2 printers are registered" in out["summary"]


def test_the_description_states_no_price(summary):
    """Prices live on the pricing page; a copy here would drift from it."""
    doc = " ".join((summary.__doc__ or "").split())
    assert "kiln3d.com/pricing" in doc
    assert not re.search(r"\$\d|\b\d+ printers?\b", doc), doc
