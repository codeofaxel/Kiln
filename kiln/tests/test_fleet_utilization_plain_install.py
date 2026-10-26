"""fleet_utilization on an install without kiln-pro answers from its own
registry and its own print queue, and refuses nothing.

The Business tool imported kiln-pro's orchestrator unguarded, so a plain
install got "Failed to get fleet utilization: No module named
'kiln.fleet_orchestrator'" at every tier (2026-10-06).
"""

from __future__ import annotations

import sys

import pytest


class _FakeMCP:
    def __init__(self):
        self.tools = {}

    def tool(self, **_kw):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


class _Registry:
    def __init__(self, statuses):
        self._statuses = statuses

    def get_fleet_status(self):
        return [{"name": f"p{i}", "status": s} for i, s in enumerate(self._statuses)]


@pytest.fixture
def fleet_utilization(monkeypatch):
    import kiln.server as srv
    from kiln.plugins.fleet_tools import _FleetToolsPlugin

    monkeypatch.setattr(srv, "requires_tier", lambda tier: (lambda fn: fn), raising=False)
    monkeypatch.setitem(sys.modules, "kiln.fleet_orchestrator", None)  # a plain install
    monkeypatch.setattr(srv, "_get_registry", lambda: _Registry(["printing", "idle", "offline", "paused", "error"]))
    from kiln.queue import PrintQueue

    queue = PrintQueue()
    queue.submit("a.gcode", printer_name="p0")
    monkeypatch.setattr(srv, "_get_queue", lambda: queue)
    mcp = _FakeMCP()
    _FleetToolsPlugin().register(mcp)
    return mcp.tools["fleet_utilization"]


def test_a_plain_install_gets_its_printer_counts(fleet_utilization):
    answer = fleet_utilization()
    assert answer["success"] is True
    util = answer["utilization"]
    assert util["total_printers"] == 5 and util["busy_printers"] == 2 and util["idle_printers"] == 1
    assert util["offline_printers"] == 1 and util["error_printers"] == 1
    assert util["utilization_pct"] == 50.0  # busy over the reachable four


def test_the_job_figures_come_from_this_installs_own_queue(fleet_utilization):
    """Since 2026-10-26 the figures are the print queue's own (the queue the
    scheduler runs); kiln-pro's orchestrator kept a table nothing filled."""
    util = fleet_utilization()["utilization"]
    assert util["job_metrics"] == {"queued": 1, "running": 0, "completed": 0, "failed": 0}
