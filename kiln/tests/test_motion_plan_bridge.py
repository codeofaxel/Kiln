"""Where a motion plan comes from, and how it is kept.

Three sources, tried in order -- local kiln-pro, the hosted service, the
on-disk cache -- and the floor when none answers.  Pinned with fakes for
all three, so the order, the fallbacks, and the cache's honesty (only for
the same machine, only for the account that was served, only while fresh)
cannot drift.  The executor is pinned separately on a fake document.
"""

from __future__ import annotations

import sys
import time
import types

import pytest

from kiln import _pro_motion_bridge as bridge
from kiln.printers import motion_plan_cache as cache


def _doc(verb: str = "home", printer_id: str = "bambu_a1") -> dict:
    return {"schema": bridge.SCHEMA, "printer_id": printer_id, "verb": verb, "ok": True,
            "steps": [{"number": 1, "label": "raise", "you_will_see": "lift", "stops_when": "ends", "gcode": ["G91"]}],
            "homed_axes": ["X"], "summary": "ran", "raise_clearance_mm": 7.0}


class _Machine:
    def __init__(self, model: str = "bambu_a1", serial: str = "01P00A000000001"):
        self._printer_model = model
        self.serial = serial


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("KILN_HOME", str(tmp_path / "kiln-home"))
    for name in list(sys.modules):
        if name == "kiln_pro" or name.startswith("kiln_pro."):
            monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setitem(sys.modules, "kiln_pro", None)
    monkeypatch.setattr(bridge, "_service_down_until", 0.0)  # no backoff leaks between tests
    # the served door is never the real network in a test
    import kiln.server as srv

    monkeypatch.setattr(srv, "_pro_api_call", lambda tool, **kw: {"status": "error", "error": "no network in tests"})


def _install_local_pro(monkeypatch, build_plan):
    pkg = types.ModuleType("kiln_pro")
    motion = types.ModuleType("kiln_pro.motion")
    motion.build_plan = build_plan
    motion.station_supports = lambda station, capability: (True, "")
    pkg.motion = motion
    monkeypatch.setitem(sys.modules, "kiln_pro", pkg)
    monkeypatch.setitem(sys.modules, "kiln_pro.motion", motion)


def _signed_in(monkeypatch, who: str = "adam@example.com"):
    monkeypatch.setattr("kiln.auth_session._read_tokens", lambda: {"email": who})
    monkeypatch.setattr("kiln.api_device.device_fingerprint", lambda: "a" * 32)


class TestTheOrder:
    def test_no_source_means_no_plan(self):
        assert bridge.plan_for(_Machine(), "home") is None
        assert bridge.available() is False

    def test_an_undeclared_model_asks_nobody(self, monkeypatch):
        asked = []
        monkeypatch.setattr(bridge, "_served_plan", lambda request: asked.append(request) or None)
        assert bridge.plan_for(_Machine(model=""), "home") is None and asked == []

    def test_local_kiln_pro_builds_it_and_the_service_is_not_asked(self, monkeypatch):
        seen = {}

        def build_plan(**request):
            seen.update(request)
            return _doc()

        _install_local_pro(monkeypatch, build_plan)
        asked = []
        monkeypatch.setattr(bridge, "_served_plan", lambda request: asked.append(request) or None)
        doc = bridge.plan_for(_Machine(), "home", axes="XY", on_plate_ok=True)
        assert doc["summary"] == "ran" and asked == []
        assert seen == {"printer_id": "bambu_a1", "serial": "01P00A000000001", "verb": "home", "axes": "XY", "on_plate_ok": True}
        assert bridge.available() is True

    def test_a_local_builder_that_raises_falls_through_to_the_service(self, monkeypatch):
        def build_plan(**request):
            raise RuntimeError("overlay missing")

        _install_local_pro(monkeypatch, build_plan)
        monkeypatch.setattr(bridge, "_served_plan", lambda request: _doc())
        assert bridge.plan_for(_Machine(), "home")["summary"] == "ran"

    def test_the_service_answers_with_a_plan_envelope(self, monkeypatch):
        import kiln.server as srv

        calls = []
        monkeypatch.setattr(srv, "_pro_api_call", lambda tool, **kw: calls.append((tool, kw)) or {"plan": _doc("park")})
        doc = bridge.plan_for(_Machine(), "park", axes="XY")
        assert doc["verb"] == "park" and "from_cache" not in doc
        assert calls[0][0] == "motion_plan" and calls[0][1]["serial"] == "01P00A000000001" and calls[0][1]["verb"] == "park"

    def test_a_refusal_from_the_service_is_no_plan(self, monkeypatch):
        import kiln.server as srv

        monkeypatch.setattr(srv, "_pro_api_call", lambda tool, **kw: {"status": "error", "code": "MACHINE_NOT_PAIRED", "error": "no heartbeat"})
        assert bridge.plan_for(_Machine(), "home") is None

    def test_a_malformed_answer_is_no_plan(self, monkeypatch):
        import kiln.server as srv

        for bad in ({"plan": {"schema": "other", "verb": "home"}}, {"plan": "G28"}, {"schema": bridge.SCHEMA, "verb": "fly"}, "G28"):
            monkeypatch.setattr(srv, "_pro_api_call", lambda tool, _bad=bad, **kw: _bad)
            assert bridge.plan_for(_Machine(), "home") is None

    def test_the_network_raising_is_no_plan_never_a_motion(self, monkeypatch):
        import kiln.server as srv

        def _boom(tool, **kw):
            raise OSError("dns")

        monkeypatch.setattr(srv, "_pro_api_call", _boom)
        assert bridge.plan_for(_Machine(), "home") is None


class TestTheCache:
    def test_a_served_plan_is_kept_and_answers_offline_for_the_same_machine(self, monkeypatch):
        import kiln.server as srv

        _signed_in(monkeypatch)
        monkeypatch.setattr(srv, "_pro_api_call", lambda tool, **kw: {"plan": _doc()})
        first = bridge.plan_for(_Machine(), "home")
        assert "from_cache" not in first
        monkeypatch.setattr(srv, "_pro_api_call", lambda tool, **kw: {"status": "error", "error": "offline"})
        again = bridge.plan_for(_Machine(), "home")
        assert again["from_cache"] is True and again["summary"] == "ran"
        # a different machine, verb, axes or consent is a different plan
        assert bridge.plan_for(_Machine(serial="OTHER"), "home") is None
        assert bridge.plan_for(_Machine(), "park") is None
        assert bridge.plan_for(_Machine(), "home", axes="XY") is None
        assert bridge.plan_for(_Machine(), "home", on_plate_ok=True) is None

    def test_nothing_is_cached_without_a_sign_in_and_nothing_is_read_after_a_sign_out(self, monkeypatch):
        import kiln.server as srv

        monkeypatch.setattr("kiln.auth_session._read_tokens", lambda: {})
        monkeypatch.setattr(srv, "_pro_api_call", lambda tool, **kw: {"plan": _doc()})
        assert bridge.plan_for(_Machine(), "home")["summary"] == "ran"
        monkeypatch.setattr(srv, "_pro_api_call", lambda tool, **kw: {"status": "error"})
        assert bridge.plan_for(_Machine(), "home") is None
        _signed_in(monkeypatch, "adam@example.com")
        monkeypatch.setattr(srv, "_pro_api_call", lambda tool, **kw: {"plan": _doc()})
        bridge.plan_for(_Machine(), "home")
        monkeypatch.setattr(srv, "_pro_api_call", lambda tool, **kw: {"status": "error"})
        _signed_in(monkeypatch, "someone@else.example")  # another account on the same device cannot read it
        assert bridge.plan_for(_Machine(), "home") is None

    def test_the_file_is_encrypted_and_private(self, monkeypatch, tmp_path):
        _signed_in(monkeypatch)
        request = {"printer_id": "bambu_a1", "serial": "S", "verb": "home", "axes": "XYZ", "on_plate_ok": False}
        assert cache.store(request, _doc()) is True
        files = list((tmp_path / "kiln-home" / "motion_plans").glob("*.plan"))
        assert len(files) == 1
        raw = files[0].read_bytes()
        # The file is a base64 token, so a bare word CAN appear in it by
        # chance; the plaintext's quoted JSON strings never can.
        assert b'"G91"' not in raw and b'"bambu_a1"' not in raw and b'"ran"' not in raw
        assert b'"' not in raw and b"{" not in raw
        assert oct(files[0].stat().st_mode & 0o777) == "0o600"
        assert cache.load(request)["summary"] == "ran"

    def test_a_stale_or_torn_file_is_not_a_plan(self, monkeypatch, tmp_path):
        _signed_in(monkeypatch)
        request = {"printer_id": "bambu_a1", "serial": "S", "verb": "home", "axes": "XYZ", "on_plate_ok": False}
        cache.store(request, _doc())
        later = time.time() + 10**9
        monkeypatch.setattr(time, "time", lambda: later)
        assert cache.load(request) is None
        monkeypatch.undo()
        _signed_in(monkeypatch)
        monkeypatch.setenv("KILN_HOME", str(tmp_path / "kiln-home"))
        cache.store(request, _doc())
        path = next((tmp_path / "kiln-home" / "motion_plans").glob("*.plan"))
        path.write_bytes(path.read_bytes()[:20])
        assert cache.load(request) is None

    def test_forget_all_empties_the_cache(self, monkeypatch, tmp_path):
        _signed_in(monkeypatch)
        request = {"printer_id": "bambu_a1", "serial": "S", "verb": "home", "axes": "XYZ", "on_plate_ok": False}
        cache.store(request, _doc())
        cache.store({**request, "verb": "park"}, _doc("park"))
        assert cache.forget_all() == 2 and cache.load(request) is None


class TestTheBackoff:
    def test_an_unreachable_service_is_not_asked_again_for_a_while(self, monkeypatch):
        import kiln.server as srv

        calls = []
        monkeypatch.setattr(srv, "_pro_api_call", lambda tool, **kw: calls.append(tool) or {"status": "error", "code": "SERVER_UNREACHABLE", "error": "dns"})
        monkeypatch.setattr(bridge, "_service_down_until", 0.0)
        for _ in range(5):
            assert bridge.plan_for(_Machine(), "home") is None
        assert len(calls) == 1  # doctor's five asks cost one timeout, not five
        monkeypatch.setattr(bridge, "_service_down_until", 0.0)
        bridge.plan_for(_Machine(), "park")
        assert len(calls) == 2

    def test_a_refusal_is_not_a_backoff(self, monkeypatch):
        import kiln.server as srv

        calls = []
        monkeypatch.setattr(srv, "_pro_api_call", lambda tool, **kw: calls.append(tool) or {"status": "error", "code": "MACHINE_NOT_PAIRED", "error": "no"})
        monkeypatch.setattr(bridge, "_service_down_until", 0.0)
        bridge.plan_for(_Machine(), "home")
        bridge.plan_for(_Machine(), "park")
        assert len(calls) == 2


class TestTheServedRequest:
    def test_the_heartbeat_device_rides_on_the_served_call(self, monkeypatch):
        import kiln.server as srv

        monkeypatch.setattr("kiln.device.get_device_fingerprint", lambda: "f" * 32)
        assert srv._heartbeat_device_header() == {"X-Kiln-Heartbeat-Device": "f" * 32}
        monkeypatch.setattr("kiln.device.get_device_fingerprint", lambda: "")
        assert srv._heartbeat_device_header() == {}

        def _boom():
            raise RuntimeError("no telemetry")

        monkeypatch.setattr("kiln.device.get_device_fingerprint", _boom)
        assert srv._heartbeat_device_header() == {}


class TestARefusalIsARevocation:
    def test_a_refusal_drops_the_cached_plan_instead_of_serving_it(self, monkeypatch):
        import kiln.server as srv

        _signed_in(monkeypatch)
        monkeypatch.setattr(srv, "_pro_api_call", lambda tool, **kw: {"plan": _doc()})
        assert bridge.plan_for(_Machine(), "home")["summary"] == "ran"
        monkeypatch.setattr(srv, "_pro_api_call", lambda tool, **kw: {"status": "error", "code": "MACHINE_NOT_PAIRED", "error": "no"})
        assert bridge.plan_for(_Machine(), "home") is None  # not the cached copy
        monkeypatch.setattr(srv, "_pro_api_call", lambda tool, **kw: {"status": "error", "code": "SERVER_UNREACHABLE", "error": "dns"})
        monkeypatch.setattr(bridge, "_service_down_until", 0.0)
        assert bridge.plan_for(_Machine(), "home") is None  # and it is gone for good, not merely skipped once

    def test_no_sign_in_is_not_a_ruling_and_keeps_the_cache(self, monkeypatch):
        import kiln.server as srv

        _signed_in(monkeypatch)
        monkeypatch.setattr(srv, "_pro_api_call", lambda tool, **kw: {"plan": _doc()})
        bridge.plan_for(_Machine(), "home")
        monkeypatch.setattr(srv, "_pro_api_call", lambda tool, **kw: {"status": "error", "code": "KILN_ACCOUNT_NOT_PAIRED", "error": "sign in"})
        assert bridge.plan_for(_Machine(), "home")["from_cache"] is True
