"""The live watch state reports what is RUNNING, never what could run.

Every block is read from this process's registries: a watchdog is attached
only when Kiln started the print, a health session only when one was
started, a background watch only while its thread is alive, a camera only
when the adapter can produce a frame.  One reader failing costs its own
block, in its "nothing" shape, and never the others.
"""

from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from kiln import watch_state
from kiln.print_watchdog import DEFAULT_STALL_SECONDS, DEFAULT_TOOL_DROP_C


class _Adapter:
    def __init__(self, *, own_camera: bool = False, registered: object | None = None):
        self.capabilities = SimpleNamespace(can_snapshot=own_camera)
        self._external_camera = registered

    @property
    def external_camera(self):
        return self._external_camera

    @property
    def has_camera(self) -> bool:
        return bool(self._external_camera) or self.capabilities.can_snapshot


def _live_thread() -> threading.Thread:
    stop = threading.Event()
    t = threading.Thread(target=stop.wait, daemon=True)
    t.start()
    t._kiln_stop = stop  # type: ignore[attr-defined]
    return t


def _quiet(monkeypatch) -> None:
    from kiln import server

    monkeypatch.setattr(server, "_print_watchdogs", {})
    monkeypatch.setattr(server, "_watchers", {})
    monkeypatch.setattr(server, "_resolve_effective_printer_name", lambda n=None: n or "default")
    monkeypatch.setattr(server, "_pro_bridge", lambda: None)
    from kiln import print_health_monitor as phm

    monkeypatch.setattr(phm, "get_print_health_monitor", lambda: SimpleNamespace(
        list_sessions=lambda **kw: [], _background_monitors={}
    ))


def test_nothing_running_reads_as_nothing_running(monkeypatch) -> None:
    _quiet(monkeypatch)
    state = watch_state.kiln_watch_state("default", adapter=_Adapter())
    assert state["kind"] == watch_state.WATCH_STATE_KIND
    assert state["camera"] == {"readable": False, "source": None}
    assert state["watchdog"] == {"attached": False, "running": False}
    assert state["health"] == {"active": False}
    assert state["watch"] == {"active": False, "count": 0}
    assert state["vision"] == {"armed": False}
    assert set(state["watchers"]) == {"watchdog", "health", "watch", "first_layer", "vision"}


def test_the_rule_words_are_read_from_the_code_not_retyped(monkeypatch) -> None:
    _quiet(monkeypatch)
    words = watch_state.kiln_watch_state("default", adapter=_Adapter())["watchers"]
    assert f"{DEFAULT_TOOL_DROP_C:.0f} °C" in words["watchdog"]["red"]["tool_drop"]
    assert f"{DEFAULT_STALL_SECONDS:.0f} s" in words["watchdog"]["red"]["stalled_layer"]
    # Every rule the watchdog can raise has words, and no words describe a rule it cannot.
    from kiln import print_watchdog

    src = Path(print_watchdog.__file__).read_text(encoding="utf-8")
    for rule in words["watchdog"]["red"]:
        bare = rule.removeprefix("tool_").removeprefix("bed_")
        assert f'"{bare}"' in src, rule
    for rule in words["watchdog"]["yellow"]:
        bare = rule.removeprefix("tool_").removeprefix("bed_")
        assert f'"{bare}"' in src, rule


def test_a_watchdog_is_reported_only_while_attached_and_alive(monkeypatch) -> None:
    _quiet(monkeypatch)
    from kiln import server

    class _Dog:
        _poll_interval = 2.5
        _stall_seconds = 90.0

        def __init__(self, running: bool):
            self._running = running

        def status(self):
            return {"running": self._running, "red_flags": [], "yellow_flags": [{"rule": "wifi_weak"}]}

    server._print_watchdogs["default"] = _Dog(True)
    live = watch_state.kiln_watch_state("default", adapter=_Adapter())["watchdog"]
    assert live["attached"] and live["running"] and live["yellow_flags"] == 1
    assert live["poll_seconds"] == 2.5 and live["stall_seconds"] == 90.0

    server._print_watchdogs["default"] = _Dog(False)
    dead = watch_state.kiln_watch_state("default", adapter=_Adapter())["watchdog"]
    assert dead["attached"] and not dead["running"]

    # Another printer's watchdog is not this printer's.
    server._print_watchdogs.clear()
    server._print_watchdogs["other"] = _Dog(True)
    assert watch_state.kiln_watch_state("default", adapter=_Adapter())["watchdog"] == {
        "attached": False, "running": False,
    }


def test_a_health_session_is_reported_with_its_live_settings(monkeypatch) -> None:
    _quiet(monkeypatch)
    from kiln import print_health_monitor as phm

    policy = phm.MonitorPolicy(stall_timeout=300, temp_drift_threshold=4.0, auto_cancel_on_emergency=True)
    session = SimpleNamespace(printer_name="default", policy=policy)
    monitor = SimpleNamespace(
        list_sessions=lambda **kw: [session] if kw.get("printer_name") == "default" else [],
        _background_monitors={"default": SimpleNamespace(interval_seconds=12)},
    )
    monkeypatch.setattr(phm, "get_print_health_monitor", lambda: monitor)
    health = watch_state.kiln_watch_state("default", adapter=_Adapter())["health"]
    assert health == {
        "active": True, "interval_seconds": 12, "stall_seconds": 300, "temp_drift_c": 4.0,
        "auto_pause": True, "auto_cancel_on_emergency": True,
    }


def test_a_background_watch_counts_only_while_its_thread_is_alive(monkeypatch) -> None:
    _quiet(monkeypatch)
    from kiln import server

    alive = _live_thread()
    try:
        server._watchers["w1"] = SimpleNamespace(
            _printer_name="default", _thread=alive, _snapshot_interval=45, _stall_timeout=600
        )
        server._watchers["w2"] = SimpleNamespace(
            _printer_name="default", _thread=None, _snapshot_interval=10, _stall_timeout=100
        )
        server._watchers["w3"] = SimpleNamespace(
            _printer_name="other", _thread=alive, _snapshot_interval=5, _stall_timeout=50
        )
        watch = watch_state.kiln_watch_state("default", adapter=_Adapter())["watch"]
        assert watch == {"active": True, "count": 1, "snapshot_interval": 45, "stall_seconds": 600}
    finally:
        alive._kiln_stop.set()  # type: ignore[attr-defined]


def test_a_camera_is_readable_from_the_printer_or_from_the_user(monkeypatch) -> None:
    _quiet(monkeypatch)
    assert watch_state.kiln_watch_state("default", adapter=_Adapter(own_camera=True))["camera"] == {
        "readable": True, "source": "printer",
    }
    assert watch_state.kiln_watch_state("default", adapter=_Adapter(registered=object()))["camera"] == {
        "readable": True, "source": "registered",
    }
    assert watch_state.kiln_watch_state("default", adapter=None)["camera"] == {
        "readable": False, "source": None,
    }


def test_the_vision_detector_is_armed_only_when_kiln_pro_says_so(monkeypatch) -> None:
    _quiet(monkeypatch)
    from kiln import server

    monkeypatch.setattr(server, "_pro_bridge", lambda: SimpleNamespace(is_available=lambda f: f == "vision"))
    assert watch_state.kiln_watch_state("default", adapter=_Adapter())["vision"] == {"armed": True}
    monkeypatch.setattr(server, "_pro_bridge", lambda: SimpleNamespace(is_available=lambda f: False))
    assert watch_state.kiln_watch_state("default", adapter=_Adapter())["vision"] == {"armed": False}


def test_one_failing_reader_costs_only_its_own_block(monkeypatch) -> None:
    _quiet(monkeypatch)
    from kiln import print_health_monitor as phm

    def _boom():
        raise RuntimeError("registry wedged")

    monkeypatch.setattr(phm, "get_print_health_monitor", _boom)
    state = watch_state.kiln_watch_state("default", adapter=_Adapter(own_camera=True))
    assert state["health"] == {"active": False}
    assert state["camera"]["readable"] is True
    assert state["watchers"]["watchdog"]["red"]


def test_the_status_read_carries_the_watch_state() -> None:
    """Both detail levels: the hosted door's agent-facing verb only ever
    polls lite, and the card must be live there too."""
    from unittest.mock import MagicMock

    from kiln import server

    adapter = MagicMock()
    state = MagicMock()
    state.to_dict.return_value = {"state": "printing"}
    job = MagicMock()
    job.to_dict.return_value = {}
    adapter.get_job.return_value = job
    adapter.capabilities.to_dict.return_value = {}
    with mock.patch.object(server, "_get_adapter", return_value=adapter), mock.patch.object(
        server, "read_status", return_value=(state, job)
    ), mock.patch.object(server, "_resolve_printer_model_live", return_value="bambu_x1c"), mock.patch(
        "kiln.watch_state.kiln_watch_state", return_value={"kind": watch_state.WATCH_STATE_KIND, "watchdog": {"attached": True, "running": True}}
    ) as reader:
        full = server.printer_status(detail="full")
        lite = server.printer_status(detail="lite")
    assert full["kiln_watch"]["watchdog"]["running"] is True
    assert lite["kiln_watch"]["watchdog"]["running"] is True
    assert reader.call_count == 2
