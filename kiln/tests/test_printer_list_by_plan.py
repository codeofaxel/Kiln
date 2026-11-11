"""What each plan gets from the status tools on a bench of two running
printers: the list is everyone's; reading both at once is the fleet plan's.

Through the real tools and real adapter methods, so the plan gate and the
one-printer rule are the ones a caller meets.
"""

from __future__ import annotations

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
                else (False, f"This needs Kiln {_word(required).title()}.")
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

    fleet = server.fleet_status()
    assert fleet["success"] is False

    # Nothing above asked the second printer anything.
    assert b.state_calls == 0


@pytest.mark.parametrize("plan", ["business", "enterprise"])
def test_the_fleet_plan_reads_every_printer(bench, on_plan, plan):
    a, b = bench
    on_plan(plan)

    assert server.printer_status(detail="lite")["success"] is True
    assert server.printer_status(printer_name="garage", detail="lite")["success"] is True

    fleet = server.fleet_status()
    assert fleet["success"] is True
    assert {row["name"]: row["state"] for row in fleet["printers"]} == {"a1": "printing", "garage": "printing"}
