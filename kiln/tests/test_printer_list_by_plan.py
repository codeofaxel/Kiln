"""What each plan gets from the status tools on a bench of two running
printers: the list is everyone's; reading both at once is the fleet plan's.

Through the real tools and real adapter methods, so the plan gate and the
one-printer rule are the ones a caller meets.
"""

from __future__ import annotations

import asyncio

import pytest

from kiln import licensing, server
from kiln.registry import PrinterRegistry
from tests.test_single_printer_engagement import _real_adapter_class


@pytest.fixture
def bench(monkeypatch):
    cls = _real_adapter_class()
    a, b = cls("AAA111"), cls("BBB222", job="gasket.gcode")
    registry = PrinterRegistry()
    registry.register("a1", a)
    registry.register("garage", b)
    monkeypatch.setattr("kiln.registry.get_registry", lambda: registry)
    monkeypatch.setattr(server, "_get_registry", lambda: registry)
    monkeypatch.setattr(server, "_get_adapter", lambda: a)
    return a, b


@pytest.fixture
def one_printer(monkeypatch):
    a = _real_adapter_class()("AAA111")
    registry = PrinterRegistry()
    registry.register("a1", a)
    monkeypatch.setattr("kiln.registry.get_registry", lambda: registry)
    monkeypatch.setattr(server, "_get_registry", lambda: registry)
    monkeypatch.setattr(server, "_get_adapter", lambda: a)
    return a


@pytest.fixture
def no_printer(monkeypatch):
    registry = PrinterRegistry()

    def _none_configured():
        raise RuntimeError("no printer configured")

    monkeypatch.setattr("kiln.registry.get_registry", lambda: registry)
    monkeypatch.setattr(server, "_get_registry", lambda: registry)
    monkeypatch.setattr(server, "_get_adapter", _none_configured)


def _ask(tool: str, **arguments) -> dict:
    """Call *tool* the way a client does: by name, through the tool manager."""
    return asyncio.run(server.mcp._tool_manager.call_tool(tool, arguments))


_LADDER = ["free", "pro", "business", "enterprise"]


def _word(tier) -> str:
    return str(getattr(tier, "value", tier)).lower()


@pytest.fixture
def on_plan(monkeypatch):
    """Make the caller's plan *plan* for every gate: the per-request plan
    where the plan module has one, and the module's own answer."""
    tokens = []

    def _on(plan: str) -> None:
        tier = licensing.LicenseTier(plan)
        monkeypatch.setattr(licensing, "get_tier", lambda: tier)
        if hasattr(licensing, "set_caller_tier"):
            tokens.append(licensing.set_caller_tier(tier))
        # The suite answers every plan check "yes" by default; here the
        # check compares the plan asked for with the caller's, as it does
        # outside the suite.
        monkeypatch.setattr(
            licensing, "check_tier",
            lambda required: (
                (True, None) if _LADDER.index(_word(required)) <= _LADDER.index(plan)
                else (
                    False,
                    f"This feature needs Kiln {_word(required).title()}. "
                    f"See what {_word(required).title()} includes at kiln3d.com/pricing",
                )
            ),
        )

    yield _on
    for token in reversed(tokens):
        licensing.reset_caller_tier(token)


@pytest.mark.parametrize("plan", ["free", "pro"])
def test_below_the_fleet_plan_the_list_is_whole_and_one_printer_is_read(bench, on_plan, plan):
    a, b = bench
    on_plan(plan)

    first = server.printer_status(detail="lite")
    assert first["success"] is True
    assert sorted(row["name"] for row in first["known_printers"]) == ["a1", "garage"]
    # The list says nothing about what the other printer is doing.
    assert all(set(row) <= {"name", "printer_model", "kiln_is_working_with"} for row in first["known_printers"])

    other = server.printer_status(printer_name="garage", detail="lite")
    assert other["success"] is False
    assert "TIER_SINGLE_PRINTER_LIMIT" in str(other)

    fleet = _ask("fleet_status")
    assert fleet["success"] is True and fleet["count"] == 2
    assert sorted(row["name"] for row in fleet["known_printers"]) == ["a1", "garage"]
    # The list says nothing about what either printer is doing, here too.
    assert all(set(row) <= {"name", "printer_model", "kiln_is_working_with"} for row in fleet["known_printers"])
    assert "printers" not in fleet and "state_counts" not in fleet and "idle_printers" not in fleet
    assert "printing" not in str(fleet)
    assert fleet["note"].count("Kiln Business") == 1
    assert fleet["upgrade_url"] == "https://kiln3d.com/pricing?src=agent&tool=fleet_status"

    # Nothing above asked the second printer anything.
    assert b.state_calls == 0


@pytest.mark.parametrize("plan", ["free", "pro"])
def test_asking_for_the_fleet_grouped_by_site_reads_no_second_printer_either(bench, on_plan, plan):
    a, b = bench
    on_plan(plan)

    fleet = _ask("fleet_status", by_site=True)

    assert fleet["success"] is True and "sites" not in fleet and "printers" not in fleet
    assert fleet["note"].count("Kiln Business") == 1 and "Enterprise" not in str(fleet)
    assert a.state_calls == 0 and b.state_calls == 0


@pytest.mark.parametrize("plan", _LADDER)
def test_one_printer_is_read_on_every_plan_with_no_plan_named(one_printer, on_plan, plan):
    on_plan(plan)

    fleet = _ask("fleet_status")

    assert fleet["success"] is True and fleet["count"] == 1
    assert [(row["name"], row["state"]) for row in fleet["printers"]] == [("a1", "printing")]
    assert "note" not in fleet and "upgrade_url" not in fleet and "upgrade_nudge" not in fleet
    assert "Business" not in str(fleet) and "pricing" not in str(fleet)


@pytest.mark.parametrize("plan", _LADDER)
def test_no_printer_set_up_is_said_on_every_plan_with_no_plan_named(no_printer, on_plan, plan):
    on_plan(plan)

    fleet = _ask("fleet_status")

    assert fleet == {"success": True, "printers": [], "count": 0, "message": "No printers registered."}


@pytest.mark.parametrize("plan", ["business", "enterprise"])
def test_the_fleet_plan_reads_every_printer(bench, on_plan, plan):
    a, b = bench
    on_plan(plan)

    assert server.printer_status(detail="lite")["success"] is True
    assert server.printer_status(printer_name="garage", detail="lite")["success"] is True

    fleet = _ask("fleet_status")
    assert fleet["success"] is True
    assert "note" not in fleet and "upgrade_url" not in fleet and "known_printers" not in fleet
    assert {row["name"]: row["state"] for row in fleet["printers"]} == {"a1": "printing", "garage": "printing"}


class TestThePlanIsNamedInThePlanChecksOwnWords:
    """Two printers, and the plan check of an install with no kiln-pro
    (``kiln.account_plan``), in each state a sign-in can be in."""

    @pytest.fixture(autouse=True)
    def _account_plan_decides(self, bench, monkeypatch, tmp_path):
        from kiln import account_plan

        monkeypatch.setenv("KILN_AUTH_HOME", str(tmp_path))
        monkeypatch.delenv("KILN_LICENSE_KEY", raising=False)
        monkeypatch.setattr(account_plan, "_last_ask_at", 0.0)
        monkeypatch.setattr(account_plan, "_last_ask_answered", True)
        monkeypatch.setattr(licensing, "check_tier", account_plan.check_tier)
        self.home = tmp_path

    def _sign_in(self, *, tier: str, checked_ago_s: float = 0.0, **extra) -> None:
        from tests.test_account_plan import _sign_in

        _sign_in(self.home, tier=tier, checked_ago_s=checked_ago_s, **extra)

    def test_signed_out_is_told_how_a_subscriber_connects_and_where_the_plan_is(self):
        fleet = _ask("fleet_status")

        assert fleet["success"] is True and len(fleet["known_printers"]) == 2
        assert "needs Kiln Business" in fleet["note"] and "Already subscribed?" in fleet["note"]
        assert fleet["upgrade_url"].endswith("tool=fleet_status")

    def test_signed_in_on_a_smaller_plan_is_told_which_plan_kiln_sees(self):
        from tests.test_account_plan import _account_says

        self._sign_in(tier="pro")
        with _account_says("pro"):
            fleet = _ask("fleet_status")

        assert len(fleet["known_printers"]) == 2 and "printers" not in fleet
        assert "on the Pro plan" in fleet["note"] and "Kiln Pro" not in fleet["note"]
        assert fleet["upgrade_url"].endswith("tool=fleet_status")

    def test_a_refused_sign_in_is_told_to_sign_back_in_and_never_to_buy(self):
        import time

        self._sign_in(tier="business", refresh_token="", refresh_rejected_at=int(time.time()))

        fleet = _ask("fleet_status")

        assert fleet["success"] is True and len(fleet["known_printers"]) == 2
        assert "upgrade_url" not in fleet and "pricing" not in str(fleet)

    def test_an_unconfirmed_fleet_plan_is_told_to_get_online_and_never_to_buy(self):
        from tests.test_account_plan import _account_unreachable

        self._sign_in(tier="business", checked_ago_s=8 * 24 * 3600)
        with _account_unreachable():
            fleet = _ask("fleet_status")

        assert len(fleet["known_printers"]) == 2 and "printers" not in fleet
        assert "your Business plan" in fleet["note"]
        assert "upgrade_url" not in fleet and "pricing" not in str(fleet)

    def test_a_signed_in_fleet_plan_reads_every_printer(self):
        from tests.test_account_plan import _account_says

        self._sign_in(tier="business")
        with _account_says("business"):
            fleet = _ask("fleet_status")

        assert {row["name"]: row["state"] for row in fleet["printers"]} == {"a1": "printing", "garage": "printing"}
        assert "note" not in fleet and "upgrade_url" not in fleet
