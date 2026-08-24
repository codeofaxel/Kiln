"""Kiln keeps a live watch on as many machines as your plan runs.

The print watchdog has always obeyed this: it spawns inside
``start_print`` and nowhere else, so a plan that runs one printer at a
time has only ever had one machine watched automatically.  The EXPLICIT
watchers — ``watch_print`` and ``start_printer_health_monitoring`` —
escaped the rule, so a free user could hand-start four machines on their
touchscreens and have Kiln keep continuous eyes, with alerting, on the
whole farm.  Kiln is not a free fleet monitor.

The line, and it is the load-bearing part of this file: **looking is not
watching.**

  * LOOKING — ``printer_status``, ``monitor_print``, ``printer_snapshot``,
    ``emergency_status`` — is one call and one answer.  Free, unlimited,
    every machine, every tier.  It is how a user finds out a machine is
    in trouble, and charging for that would be charging for sight of a
    hot printer.
  * STOPPING — pause, cancel, emergency stop — is never limited by
    anything here, at any tier.  A watch limit that could strand a
    running machine would be the paywall reaching a hazard, which
    ``print_gate``'s contract forbids outright.
  * WATCHING — a background thread Kiln runs on the user's behalf until
    told to stop — is the thing a plan sizes.

So a free user with five hot machines can still see all five and stop
all five; what they don't get is Kiln standing over four of them.
"""

from __future__ import annotations

import pytest

from kiln import server
from kiln.registry import PrinterRegistry


class _FakePrinter:
    name = "fake"

    def __init__(self, host: str) -> None:
        self.host = host
        self.serial = ""
        self.cancelled = 0

    def get_state(self):
        from kiln.printers.base import PrinterState, PrinterStatus

        return PrinterState(
            state=PrinterStatus.PRINTING,
            connected=True,
            tool_temp_actual=220.0,
            tool_temp_target=220.0,
            bed_temp_actual=60.0,
            bed_temp_target=60.0,
        )

    def cancel_print(self):
        from kiln.printers.base import PrintResult

        self.cancelled += 1
        return PrintResult(success=True, message="cancelled")

    def pause_print(self):
        from kiln.printers.base import PrintResult

        return PrintResult(success=True, message="paused")

    def set_tool_temp(self, value: float) -> None:
        pass

    def set_bed_temp(self, value: float) -> None:
        pass


class _LiveWatcher:
    """A watcher whose thread is alive — i.e. really watching."""

    class _T:
        @staticmethod
        def is_alive() -> bool:
            return True

    def __init__(self, adapter) -> None:
        self.adapter = adapter
        self._thread = self._T()


class _DeadWatcher(_LiveWatcher):
    """A watcher whose thread has exited."""

    class _T:
        @staticmethod
        def is_alive() -> bool:
            return False


@pytest.fixture(autouse=True)
def _bench(monkeypatch):
    monkeypatch.setattr(server, "_registry", PrinterRegistry())
    monkeypatch.setattr(server, "_watchers", {})
    monkeypatch.setattr(server, "_TOOL_RATE_LIMITS", {})
    monkeypatch.setattr(server, "_tool_limiter", server._ToolRateLimiter())
    # No health-monitor sessions unless a test adds them.
    import kiln.print_health_monitor as phm

    monkeypatch.setattr(
        phm, "get_print_health_monitor",
        lambda: type("_M", (), {"_background_monitors": {}})(),
    )
    garage = _FakePrinter("192.0.2.10")
    workshop = _FakePrinter("192.0.2.11")
    shed = _FakePrinter("192.0.2.12")
    reg = server._get_registry()
    reg.register("garage", garage)
    reg.register("workshop", workshop)
    reg.register("shed", shed)
    monkeypatch.setattr(server, "_get_adapter", lambda: garage)
    return garage, workshop, shed


def _free_tier(monkeypatch):
    """Force the one-machine cap the gate reads."""
    import kiln.printers.print_gate  # noqa: F401 — ensure module import path exists

    monkeypatch.setattr(
        server, "_watch_capacity_error",
        server._watch_capacity_error,  # identity: we patch the cap, not the gate
    )


# ---------------------------------------------------------------------------
# The limit itself
# ---------------------------------------------------------------------------


def test_a_second_machine_cannot_be_watched_on_a_one_machine_plan(_bench):
    """The gap this closes: four hand-started machines, all watched free."""
    garage, workshop, _shed = _bench
    server._watchers["w1"] = _LiveWatcher(garage)

    block = server._watch_capacity_error(workshop, "workshop")

    assert block is not None
    assert block["error"]["code"] == "TIER_CONCURRENT_WATCH_LIMIT"


def test_the_refusal_names_what_is_still_free(_bench):
    """A refusal that hides the free path reads as "Kiln can't see your
    printer" — which is false, and is the shape that turns a bounded free
    tier into a broken-feeling one."""
    garage, workshop, _shed = _bench
    server._watchers["w1"] = _LiveWatcher(garage)

    msg = server._watch_capacity_error(workshop, "workshop")["error"]["message"]

    assert "printer_status" in msg          # looking stays free
    assert "stopping any machine" in msg    # and so does stopping
    assert "pricing" in msg                 # and the way to get more


def test_watching_the_machine_already_watched_is_not_a_new_machine(_bench):
    """Re-watching is idempotent, not a second machine."""
    garage, _workshop, _shed = _bench
    server._watchers["w1"] = _LiveWatcher(garage)

    assert server._watch_capacity_error(garage, "garage") is None


def test_one_machine_under_two_names_is_one_watch(_bench, monkeypatch):
    """The server registers the active printer as "default" AND its config
    name.  Counted by name, a single machine would fill a single-machine
    plan's only slot and lock the user out of watching it."""
    garage, _workshop, _shed = _bench
    server._get_registry().register("default", garage)  # same machine, 2nd name
    server._watchers["w1"] = _LiveWatcher(garage)

    assert server._watch_capacity_error(garage, "default") is None


def test_a_dead_watcher_does_not_hold_the_slot(_bench):
    """A crashed watcher permanently consuming a free user's only slot
    would be a bug that reads as a paywall."""
    garage, workshop, _shed = _bench
    server._watchers["w1"] = _DeadWatcher(garage)

    assert server._watch_capacity_error(workshop, "workshop") is None


def test_health_monitor_sessions_count_toward_the_same_limit(_bench, monkeypatch):
    """Two watcher surfaces, one limit — or a user opens one of each and
    watches two machines on a one-machine plan."""
    _garage, workshop, _shed = _bench
    import kiln.print_health_monitor as phm

    monkeypatch.setattr(
        phm, "get_print_health_monitor",
        lambda: type("_M", (), {"_background_monitors": {"garage": object()}})(),
    )

    block = server._watch_capacity_error(workshop, "workshop")

    assert block is not None
    assert block["error"]["code"] == "TIER_CONCURRENT_WATCH_LIMIT"


def test_the_health_monitor_tool_enforces_it(_bench):
    """Through the tool, not just the helper."""
    garage, _workshop, _shed = _bench
    server._watchers["w1"] = _LiveWatcher(garage)

    out = server.start_printer_health_monitoring(printer_name="workshop")

    assert out["success"] is False
    assert out["error"]["code"] == "TIER_CONCURRENT_WATCH_LIMIT"


def test_an_unknown_printer_is_a_not_found_not_a_paywall(_bench):
    """A typo must not be reported as a tier problem — that sends the user
    to the pricing page to fix a spelling mistake."""
    out = server.start_printer_health_monitoring(printer_name="grage")

    assert out["success"] is False
    assert out["error"]["code"] == "PRINTER_NOT_FOUND"


# ---------------------------------------------------------------------------
# The floor: looking and stopping are never limited
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tool", ["printer_status", "monitor_print", "printer_snapshot", "emergency_status"],
)
def test_looking_at_any_machine_is_never_watch_limited(_bench, tool, monkeypatch):
    """One call, one answer — on every machine, however many are watched.

    Called with every slot full: the answer must never be a watch-limit
    refusal, because this is how a user finds out a machine is in trouble.
    """
    garage, workshop, shed = _bench
    server._watchers["w1"] = _LiveWatcher(garage)
    server._watchers["w2"] = _LiveWatcher(shed)

    fn = server.mcp._tool_manager._tools.get(tool) or getattr(server, tool)
    result = getattr(fn, "fn", fn)(printer_name="workshop")

    if isinstance(result, dict):
        err = result.get("error")
        code = err.get("code") if isinstance(err, dict) else result.get("code")
        assert code != "TIER_CONCURRENT_WATCH_LIMIT", (
            f"{tool} was refused on a watch limit — looking is not watching, "
            "and sight of a hot machine is never for sale."
        )


@pytest.mark.parametrize("verb", ["cancel_print", "pause_print"])
def test_stopping_any_machine_is_never_watch_limited(_bench, verb):
    """The floor. A watch limit that could strand a running machine would
    be the paywall reaching a hazard."""
    garage, workshop, shed = _bench
    server._watchers["w1"] = _LiveWatcher(garage)
    server._watchers["w2"] = _LiveWatcher(shed)

    out = getattr(server, verb)(printer_name="workshop")

    assert out["success"] is True
    assert out["printer_name"] == "workshop"


def test_stopping_a_watch_frees_the_slot(_bench):
    """Moving the watch to another machine must be possible without an
    upgrade — otherwise the first machine ever watched wins forever."""
    garage, workshop, _shed = _bench
    server._watchers["w1"] = _LiveWatcher(garage)
    assert server._watch_capacity_error(workshop, "workshop") is not None

    server._watchers.pop("w1")

    assert server._watch_capacity_error(workshop, "workshop") is None
