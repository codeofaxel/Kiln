"""One fleet view: ``fleet_status`` flat, and grouped by site with ``by_site=True``.

Until 2026-10-26 the grouping was a second tool, ``fleet_status_by_site``
(Enterprise), beside ``fleet_status`` (Business).  Merged on Adam's
decision: one door, one answer shape, the grouping a parameter.  Pinned
here, through the registered tools:

* Business, ``by_site=False``: the flat answer, exactly as before;
* Business, ``by_site=True``: the same flat answer, never a refusal, plus
  ONE line naming the plan that groups it, with the pricing link;
* Enterprise, ``by_site=True``: the flat answer plus ``sites`` -- per site
  its printers, count, idle, busy and needs-repair -- carrying the repair
  record on every row;
* Free: refused at the Business gate, ``by_site`` either way;
* the old name: still registered, refuses, and names the new call.

A/B: every test here fails at public f0f987ab (``fleet_status`` took no
``by_site``; ``fleet_status_by_site`` answered).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import kiln.server as srv
from kiln import machine_repairs as pr
from kiln import persistence as persistence_mod
from kiln.persistence import KilnDB
from kiln.printers.base import PrinterState, PrinterStatus
from kiln.printers.octoprint import OctoPrintAdapter
from kiln.registry import PrinterRegistry

_ORDER = ("free", "pro", "business", "enterprise")


def _plan(monkeypatch, tier: str) -> None:
    """Put the caller on *tier*, whichever licensing module answers."""

    def covers(required) -> bool:
        needed = getattr(required, "value", str(required)).lower()
        return _ORDER.index(tier) >= _ORDER.index(needed)

    monkeypatch.setattr(
        "kiln.licensing.check_tier",
        lambda required, *_a, **_k: (True, None) if covers(required) else (False, f"needs {required}"),
    )
    monkeypatch.setattr("kiln.account_plan._covers", covers)


@pytest.fixture
def db(tmp_path):
    return KilnDB(str(tmp_path / "kiln.db"))


@pytest.fixture
def fleet(monkeypatch, db):
    """Two idle printers at two sites, one marked needs repair."""
    monkeypatch.delenv("KILN_HOSTED_MULTITENANT", raising=False)
    monkeypatch.setattr(persistence_mod, "get_db", lambda: db)
    registry = PrinterRegistry()
    for name, host in (("a", "10.0.0.5"), ("b", "10.0.0.6")):
        adapter = MagicMock(spec=OctoPrintAdapter)
        adapter.name, adapter.host, adapter.serial = "OctoPrint", host, None
        adapter.get_state.return_value = PrinterState(
            connected=True, state=PrinterStatus.IDLE, tool_temp_actual=24.0, tool_temp_target=0.0,
            bed_temp_actual=22.0, bed_temp_target=0.0,
        )
        registry.register(name, adapter)
    registry.update_printer_metadata("a", site="shop")
    monkeypatch.setattr(srv, "_registry", registry)
    pr.mark("a", "needed", db=db)
    return srv.fleet_status


def _old_name():
    return srv.mcp._tool_manager._tools["fleet_status_by_site"].fn


FLAT_KEYS = {
    "success", "printers", "count", "idle_printers", "connected_count", "disconnected_count",
    "state_counts", "offline_printers", "busy_printers", "needs_repair_printers",
}


class TestBusiness:
    def test_flat_answer_is_unchanged(self, fleet, monkeypatch):
        _plan(monkeypatch, "business")
        out = fleet(by_site=False)
        assert out["success"] is True
        assert set(out) == FLAT_KEYS
        assert out["count"] == 2 and out["needs_repair_printers"] == ["a"]

    def test_by_site_is_the_flat_answer_plus_one_line(self, fleet, monkeypatch):
        _plan(monkeypatch, "business")
        flat = fleet()
        out = fleet(by_site=True)
        assert out["success"] is True
        assert "sites" not in out and "site_count" not in out
        assert {k: out[k] for k in FLAT_KEYS} == flat
        assert set(out) == FLAT_KEYS | {"note", "upgrade_url", "upgrade_nudge"}
        # The plan named once, as a fact, with the link.
        assert out["note"].count("Enterprise") == 1
        assert out["upgrade_url"].startswith("https://kiln3d.com/pricing")
        assert out["upgrade_nudge"]["display_text"] == out["note"]
        assert out["upgrade_nudge"]["cta"]["tier"] == "enterprise"

    def test_an_empty_fleet_asked_by_site_still_gets_the_line(self, monkeypatch):
        _plan(monkeypatch, "business")
        monkeypatch.setattr(srv, "_registry", PrinterRegistry())
        monkeypatch.setattr(srv, "_adapter", None)
        monkeypatch.setattr(srv, "_PRINTER_HOST", "")
        out = srv.fleet_status(by_site=True)
        assert out["success"] is True and out["count"] == 0
        assert out["note"].count("Enterprise") == 1


class TestEnterprise:
    def test_by_site_groups_the_same_rows_with_their_repair(self, fleet, monkeypatch):
        _plan(monkeypatch, "enterprise")
        out = fleet(by_site=True)
        assert out["success"] is True
        assert "note" not in out and "upgrade_nudge" not in out
        assert set(out) >= FLAT_KEYS
        assert out["site_count"] == 2 and set(out["sites"]) == {"shop", "unassigned"}
        shop, loose = out["sites"]["shop"], out["sites"]["unassigned"]
        assert {k for k in shop} == {"needs_repair", "printers", "count", "idle", "busy"}
        assert shop["count"] == 1 and shop["printers"][0]["name"] == "a"
        assert shop["needs_repair"] == ["a"] and loose["needs_repair"] == []
        assert shop["idle"] == ["a"] and shop["busy"] == []
        assert loose["idle"] == ["b"]
        assert shop["printers"][0]["repair"]["status"] == "needed"
        assert out["needs_repair_printers"] == ["a"]
        # The grouped rows ARE the flat rows, not a second reading.
        assert shop["printers"][0] is out["printers"][0]

    def test_without_by_site_enterprise_gets_the_flat_view(self, fleet, monkeypatch):
        _plan(monkeypatch, "enterprise")
        assert set(fleet()) == FLAT_KEYS


class TestFree:
    @pytest.mark.parametrize("by_site", [False, True])
    def test_refused_at_the_business_gate(self, fleet, monkeypatch, by_site):
        _plan(monkeypatch, "free")
        out = fleet(by_site=by_site)
        assert out["success"] is False
        assert out["code"] == "TIER_REQUIRED" and out["required_tier"] == "business"
        assert "sites" not in out and "note" not in out


class TestTheOldName:
    @pytest.mark.parametrize("tier", ["free", "business", "enterprise"])
    def test_refuses_and_names_the_new_call(self, fleet, monkeypatch, tier):
        _plan(monkeypatch, tier)
        out = _old_name()()
        assert out["success"] is False
        assert out["error"]["code"] == "RETIRED"
        assert "fleet_status(by_site=True)" in out["error"]["message"]
        assert "sites" not in out
