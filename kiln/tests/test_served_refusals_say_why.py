"""Every door that leans on Kiln's servers says WHY when no answer comes.

Public Kiln asks Kiln's servers for a few things it cannot work out on its
own machine: a manifest tool, a printer's head-motion plan, whether a
filament-cutter blade is due.  Four different things can stop an answer --
this computer is offline, Kiln is signed out, the servers did not answer,
the servers said no -- and each has a different fix.  These tests pin that
every door names which one happened, in the one sentence shape the product
uses, with the code beside the sentence and never inside it; that a safety
floor (a head motion) stays shut whichever of the four it was; and that a
server's "try again shortly" is not mistaken for a ruling on the machine.

Written before the fix and run against it first: every test here failed on
the tree that worded each door its own way.
"""

from __future__ import annotations

import errno
import io
import json
import socket
import sys
import urllib.error

import pytest

# ruff: noqa: F811  -- `bambu` / `no_kiln_pro` are fixtures, re-used by name in every door test
from kiln.printers.base import (
    FilamentHandlingUnsupported,
    HomingUnsupported,
    PrinterState,
    PrinterStatus,
)

from .test_filament_handling import (  # noqa: F401
    _hot,
    _scripts,
    bambu,
    no_kiln_pro,
)


def _pro_api_call():
    from kiln.server import _pro_api_call as call

    return call


def _unpaired(tmp_path, monkeypatch):
    monkeypatch.setenv("KILN_AUTH_HOME", str(tmp_path))
    monkeypatch.delenv("KILN_API_URL", raising=False)
    monkeypatch.delenv("KILN_LICENSE_KEY", raising=False)


def _paired(tmp_path, monkeypatch):
    monkeypatch.setenv("KILN_AUTH_HOME", str(tmp_path))
    monkeypatch.delenv("KILN_API_URL", raising=False)
    monkeypatch.setenv("KILN_LICENSE_KEY", "kiln_test_key")


@pytest.fixture(autouse=True)
def _fresh(tmp_path, monkeypatch):
    """No cache, no backoff, no plate record, no served network from a test before."""
    monkeypatch.setenv("KILN_HOME", str(tmp_path / "kiln-home"))
    from kiln import _pro_cutter_bridge as cutter
    from kiln import _pro_motion_bridge as motion

    monkeypatch.setattr(motion, "_service_down_until", 0.0)
    monkeypatch.setattr(cutter, "_service_down_until", 0.0)
    for mod in (motion, cutter):
        for name in ("_misses", "_last_miss"):
            if isinstance(getattr(mod, name, None), dict):
                getattr(mod, name).clear()


def _idle(adapter, monkeypatch):
    monkeypatch.setattr(
        adapter, "get_state",
        lambda: PrinterState(connected=True, state=PrinterStatus.IDLE, tool_temp_actual=25.0),
    )


def _offline(monkeypatch):
    """The served door as it answers with no route to Kiln's servers."""
    import kiln.server as srv

    def _call(tool, _timeout=30.0, **kw):
        return {"status": "error", "success": False, "code": "SERVER_UNREACHABLE", "why": "offline",
                "error": "no route", "tool": tool}

    monkeypatch.setattr(srv, "_pro_api_call", _call)


def _answer(monkeypatch, body):
    import kiln.server as srv

    monkeypatch.setattr(srv, "_pro_api_call", lambda tool, _timeout=30.0, **kw: body)


# ---------------------------------------------------------------------------
# the manifest stubs: _pro_api_call words a transport failure for a person
# ---------------------------------------------------------------------------


class TestTheProxyNamesWhy:
    def test_no_route_reads_as_offline_in_plain_words(self, tmp_path, monkeypatch):
        _paired(tmp_path, monkeypatch)

        def _dns(_req, timeout):
            raise urllib.error.URLError(socket.gaierror(8, "nodename nor servname provided, or not known"))

        monkeypatch.setattr("urllib.request.urlopen", _dns)
        out = _pro_api_call()("generate_coaster", text="hi")
        assert out["code"] == "SERVER_UNREACHABLE" and out["why"] == "offline" and out["success"] is False
        assert "this computer is offline" in out["error"] and "Reconnect to the internet and try again" in out["error"]
        assert "generate_coaster" in out["error"] and "Kiln's servers" in out["error"]
        assert "Failed to reach" not in out["error"] and "nodename" not in out["error"]  # the transport detail rides beside
        assert "nodename" in out["transport"]

    def test_a_timeout_on_a_live_link_reads_as_unanswered(self, tmp_path, monkeypatch):
        from kiln import served_answer

        _paired(tmp_path, monkeypatch)
        monkeypatch.setattr(served_answer, "_route_to", lambda host: True)

        def _slow(_req, timeout):
            raise urllib.error.URLError(TimeoutError("timed out"))

        monkeypatch.setattr("urllib.request.urlopen", _slow)
        out = _pro_api_call()("generate_coaster")
        assert out["code"] == "SERVER_UNREACHABLE" and out["why"] == "unanswered"
        assert "Kiln's servers didn't answer" in out["error"] and "Wait a minute and try again" in out["error"]

    def test_a_timeout_with_no_route_reads_as_offline(self, tmp_path, monkeypatch):
        from kiln import served_answer

        _paired(tmp_path, monkeypatch)
        monkeypatch.setattr(served_answer, "_route_to", lambda host: False)

        def _slow(_req, timeout):
            raise TimeoutError("timed out")

        monkeypatch.setattr("urllib.request.urlopen", _slow)
        assert _pro_api_call()("generate_coaster")["why"] == "offline"

    def test_a_bare_detail_body_becomes_an_error_envelope(self, tmp_path, monkeypatch):
        """FastAPI's ``{"detail": ...}`` used to come back as-is: no status, no
        error, no code -- an answer nothing could tell from a success."""
        _paired(tmp_path, monkeypatch)
        detail = "Daily allowance for this lookup is used up (5/day). It resets at midnight UTC."

        def _quota(req, timeout):
            raise urllib.error.HTTPError(req.full_url, 429, "Too Many", {}, io.BytesIO(json.dumps({"detail": detail}).encode()))

        monkeypatch.setattr("urllib.request.urlopen", _quota)
        out = _pro_api_call()("answer_printer_question", printer_id="bambu_a1", question="q")
        assert out["status"] == "error" and out["success"] is False and out["why"] == "refused"
        assert out["http_status"] == 429 and detail in out["error"] and "Kiln's servers said no" in out["error"]

    def test_a_gateway_page_reads_as_unanswered(self, tmp_path, monkeypatch):
        _paired(tmp_path, monkeypatch)

        def _gateway(req, timeout):
            raise urllib.error.HTTPError(req.full_url, 502, "Bad Gateway", {}, io.BytesIO(b"<html>502</html>"))

        monkeypatch.setattr("urllib.request.urlopen", _gateway)
        out = _pro_api_call()("generate_coaster")
        assert out["code"] == "KILN_API_HTTP_ERROR" and out["why"] == "unanswered" and out["http_status"] == 502
        assert "didn't answer" in out["error"] and "502" not in out["error"]

    def test_the_account_wall_says_signed_out_beside_its_sentence(self, tmp_path, monkeypatch):
        _unpaired(tmp_path, monkeypatch)
        out = _pro_api_call()("generate_coaster")
        assert out["code"] == "KILN_ACCOUNT_NOT_PAIRED" and out["why"] == "signed_out"


# ---------------------------------------------------------------------------
# the motion bridge: which of the four, and a try-again is not a ruling
# ---------------------------------------------------------------------------


class _Machine:
    def __init__(self, model: str = "bambu_a1", serial: str = "01P00A000000001"):
        self._printer_model = model
        self.serial = serial


def _plan(verb: str = "home") -> dict:
    from kiln import _pro_motion_bridge as bridge

    return {"schema": bridge.SCHEMA, "printer_id": "bambu_a1", "verb": verb, "ok": True,
            "steps": [{"number": 1, "label": "raise", "you_will_see": "lift", "stops_when": "ends", "gcode": ["G91"]}],
            "homed_axes": ["X"], "summary": "ran", "raise_clearance_mm": 7.0}


def _signed_in(monkeypatch):
    monkeypatch.setattr("kiln.auth_session._read_tokens", lambda: {"email": "adam@example.com"})
    monkeypatch.setattr("kiln.api_device.device_fingerprint", lambda: "a" * 32)


class TestTheBridgeKnowsWhy:
    @pytest.mark.parametrize("code", ["MACHINE_UNVERIFIABLE", "CAP_UNAVAILABLE", "ACCOUNT_REQUIRED"])
    def test_a_try_again_from_the_service_keeps_the_cached_plan(self, no_kiln_pro, monkeypatch, code):
        """The service's own "try again shortly" (its heartbeat table or
        counter is down) is not a ruling on this machine.  It used to be read
        as one, and a paired printer lost its plan for a server blip."""
        from kiln import _pro_motion_bridge as bridge

        _signed_in(monkeypatch)
        _answer(monkeypatch, {"plan": _plan()})
        assert bridge.plan_for(_Machine(), "home")["summary"] == "ran"
        _answer(monkeypatch, {"status": "error", "code": code, "error": "try again shortly"})
        doc = bridge.plan_for(_Machine(), "home")
        assert doc is not None and doc["from_cache"] is True

    def test_a_ruling_still_drops_the_cache(self, no_kiln_pro, monkeypatch):
        from kiln import _pro_motion_bridge as bridge

        _signed_in(monkeypatch)
        _answer(monkeypatch, {"plan": _plan()})
        bridge.plan_for(_Machine(), "home")
        _answer(monkeypatch, {"status": "error", "code": "MACHINE_NOT_PAIRED", "error": "not this device"})
        assert bridge.plan_for(_Machine(), "home") is None
        _offline(monkeypatch)
        assert bridge.plan_for(_Machine(), "home") is None

    @pytest.mark.parametrize(
        "body, cause",
        [
            ({"status": "error", "code": "SERVER_UNREACHABLE", "why": "offline", "error": "no route"}, "offline"),
            ({"status": "error", "code": "SERVER_UNREACHABLE", "why": "unanswered", "error": "timed out"}, "unanswered"),
            ({"status": "error", "code": "KILN_ACCOUNT_NOT_PAIRED", "error": "sign in"}, "signed_out"),
            ({"status": "error", "code": "ACCOUNT_REQUIRED", "error": "sign in"}, "signed_out"),
            ({"status": "error", "code": "MACHINE_UNVERIFIABLE", "error": "try again shortly"}, "unanswered"),
            ({"status": "error", "code": "MODEL_CAP_REACHED", "error": "three models is the free cap"}, "refused"),
            ({"status": "error", "error": "no code at all"}, "unanswered"),
            ("G28", "unanswered"),
        ],
    )
    def test_the_miss_names_its_cause(self, no_kiln_pro, monkeypatch, body, cause):
        from kiln import _pro_motion_bridge as bridge

        _answer(monkeypatch, body)
        machine = _Machine()
        assert bridge.plan_for(machine, "home") is None
        miss = bridge.miss_for(machine, "home")
        assert miss is not None and miss.cause == cause
        if cause == "refused":
            assert miss.detail == "three models is the free cap" and miss.code == "MODEL_CAP_REACHED"

    def test_a_served_plan_clears_the_miss(self, no_kiln_pro, monkeypatch):
        from kiln import _pro_motion_bridge as bridge

        _offline(monkeypatch)
        machine = _Machine()
        bridge.plan_for(machine, "home")
        assert bridge.miss_for(machine, "home").cause == "offline"
        _answer(monkeypatch, {"plan": _plan()})
        monkeypatch.setattr(bridge, "_service_down_until", 0.0)  # the backoff has passed
        assert bridge.plan_for(machine, "home")["summary"] == "ran"
        assert bridge.miss_for(machine, "home") is None

    def test_the_backoff_remembers_why(self, no_kiln_pro, monkeypatch):
        """A door asked during the backoff hears the same cause, not a blank."""
        from kiln import _pro_motion_bridge as bridge

        _offline(monkeypatch)
        machine = _Machine()
        bridge.plan_for(machine, "home")
        _answer(monkeypatch, {"plan": _plan("park")})  # would answer, but the backoff is on
        assert bridge.plan_for(machine, "park", axes="XY") is None
        assert bridge.miss_for(machine, "park", axes="XY").cause == "offline"


# ---------------------------------------------------------------------------
# the Bambu doors: the floor stays shut, and the sentence says which of the four
# ---------------------------------------------------------------------------


_JOG = ("jog controls", "Z UP first", "Home button descends")
_OLD = ("check the network", "kiln-pro", "hosted service", "SERVER_UNREACHABLE")


class TestTheMotionDoorsSayWhy:
    @pytest.mark.parametrize("verb", ["home", "park"])
    def test_offline_refuses_and_says_so(self, no_kiln_pro, bambu, monkeypatch, verb):
        _offline(monkeypatch)
        bambu._printer_model = "bambu_a1"
        _idle(bambu, monkeypatch)
        with pytest.raises(HomingUnsupported) as info:
            bambu.home_axes() if verb == "home" else bambu.park_head()
        text = str(info.value)
        assert "this computer is offline" in text and f"won't {verb} bambu_a1" in text
        assert "reconnect to the internet and try again" in text
        assert all(word in text for word in _JOG) and not any(word in text for word in _OLD)
        assert _scripts(bambu) == []

    def test_signed_out_refuses_and_says_sign_in(self, no_kiln_pro, bambu, monkeypatch):
        _answer(monkeypatch, {"status": "error", "code": "KILN_ACCOUNT_NOT_PAIRED", "error": "wall"})
        bambu._printer_model = "bambu_a1"
        _idle(bambu, monkeypatch)
        with pytest.raises(HomingUnsupported) as info:
            bambu.home_axes()
        text = str(info.value)
        assert "Kiln is signed out" in text and "sign in and try again" in text
        assert "offline" not in text and _scripts(bambu) == []

    def test_unanswered_refuses_and_says_wait(self, no_kiln_pro, bambu, monkeypatch):
        _answer(monkeypatch, {"status": "error", "code": "MACHINE_UNVERIFIABLE", "error": "try again shortly"})
        bambu._printer_model = "bambu_a1"
        _idle(bambu, monkeypatch)
        with pytest.raises(HomingUnsupported) as info:
            bambu.park_head()
        text = str(info.value)
        assert "Kiln's servers didn't answer" in text and "wait a minute and try again" in text
        assert _scripts(bambu) == []

    def test_a_refusal_carries_the_services_own_words(self, no_kiln_pro, bambu, monkeypatch):
        said = ("This device's Kiln has not reported a bambu_a1 in the last 30 days, so its motion plan is "
                "not served here. Register the printer in this install's config.yaml (printer_model) and "
                "let its daily heartbeat run once, then ask again.")
        _answer(monkeypatch, {"status": "error", "code": "MACHINE_NOT_PAIRED", "error": said})
        bambu._printer_model = "bambu_a1"
        _idle(bambu, monkeypatch)
        with pytest.raises(HomingUnsupported) as info:
            bambu.home_axes()
        text = str(info.value)
        assert "Kiln's servers said no" in text and said in text
        assert "wait a minute" not in text  # the service named the fix; waiting is not it
        assert all(word in text for word in _JOG) and _scripts(bambu) == []

    def test_the_wipe_says_why_and_points_at_the_screen(self, no_kiln_pro, bambu, monkeypatch):
        _offline(monkeypatch)
        bambu._printer_model = "bambu_a1"
        bambu._last_status["ams"]["tray_now"] = "0"
        _hot(bambu, monkeypatch)
        with pytest.raises(FilamentHandlingUnsupported) as info:
            bambu.wipe_nozzle()
        text = str(info.value)
        assert "this computer is offline" in text and "won't wipe bambu_a1" in text
        assert "printer's own screen" in text and "Home button" not in text
        assert not any(word in text for word in _OLD) and _scripts(bambu) == []

    def test_the_purge_runs_in_place_and_says_why(self, no_kiln_pro, bambu, monkeypatch):
        _offline(monkeypatch)
        bambu._printer_model = "bambu_a1"
        bambu._last_status["ams"]["tray_now"] = "0"
        _hot(bambu, monkeypatch)
        result = bambu.purge_filament(length_mm=10)
        assert result.success and result.details["purge_station"]["status"] == "in_place"
        assert "this computer is offline" in result.message and "own park sequence" in result.message
        assert not any(word in result.message for word in _OLD)
        assert not any("G28 X" in s for s in _scripts(bambu))

    def test_the_gate_the_doctor_reads_says_why(self, no_kiln_pro, bambu, monkeypatch):
        _answer(monkeypatch, {"status": "error", "code": "KILN_ACCOUNT_NOT_PAIRED", "error": "wall"})
        bambu._printer_model = "bambu_a1"
        for capability in ("purge", "wipe", "park", "home_z"):
            ok, why = bambu._station_supports(None, capability)
            assert ok is False and "Kiln is signed out" in why and not any(word in why for word in _OLD)

    def test_the_mcp_door_carries_the_why_beside_the_sentence(self, no_kiln_pro, bambu, monkeypatch):
        import kiln.server as srv
        from kiln.plugins.homing_tools import home_axes

        _offline(monkeypatch)
        bambu._printer_model = "bambu_a1"
        _idle(bambu, monkeypatch)
        monkeypatch.setattr(srv, "_resolve_control_target", lambda name: (bambu, "default"))
        for gate in ("_emergency_latch_error", "_check_auth", "_check_rate_limit", "_check_confirmation"):
            monkeypatch.setattr(srv, gate, lambda *a, **k: None)
        out = home_axes()
        assert out["success"] is False and out["error"]["code"] == "UNSUPPORTED"
        assert "this computer is offline" in out["error"]["message"] and out["why"] == "offline"


# ---------------------------------------------------------------------------
# the blade line: a pre-flight names what it could not check
# ---------------------------------------------------------------------------


def _hosted_only(monkeypatch):
    from kiln import _pro_cutter_bridge as bridge

    for name in ("kiln_pro", "kiln_pro.cutter_intelligence", "kiln_pro.cutter_intelligence.catalogue"):
        monkeypatch.setitem(sys.modules, name, None)
    monkeypatch.setattr(bridge, "_declared_model", lambda name: "bambu_a1")
    monkeypatch.setattr(bridge, "recent_faults_for", lambda name, days=30: [])


def _preflight(monkeypatch):
    from unittest.mock import MagicMock, patch

    state = MagicMock()
    state.connected = True
    state.state = PrinterStatus.IDLE
    state.tool_temp_actual = 25.0
    state.tool_temp_target = 0.0
    state.bed_temp_actual = 25.0
    state.bed_temp_target = 0.0
    with patch("kiln.server._get_adapter") as adapter, patch("kiln.server._get_temp_limits", return_value=(280.0, 120.0)), \
            patch("kiln.server.get_db") as db, patch("kiln.server._registry") as registry:
        adapter.return_value.get_state.return_value = state
        registry.count = 1
        registry.list_names.return_value = ["a1"]
        db.return_value.get_printer_learning_insights.return_value = {"total_outcomes": 0}
        monkeypatch.setattr("kiln.server._resolve_control_target", lambda name: (adapter.return_value, "a1"))
        from kiln.server import preflight_check

        return preflight_check()


class TestTheBladeLineSaysWhatItCouldNotCheck:
    def test_the_consult_reports_why_it_has_no_line(self, monkeypatch):
        from kiln import _pro_cutter_bridge as bridge

        _hosted_only(monkeypatch)
        _offline(monkeypatch)
        assert bridge.consult_blade("a1") is None
        gap = bridge.blade_unchecked("a1")
        assert gap["word"] == "unchecked" and gap["why"] == "offline"
        assert "this computer is offline" in gap["line"] and "Print as usual" in gap["line"]

    def test_a_healthy_answer_leaves_no_gap(self, monkeypatch):
        from kiln import _pro_cutter_bridge as bridge

        _hosted_only(monkeypatch)
        _answer(monkeypatch, {"success": True, "word": "ok", "why": "fine", "confidence": "verified", "next_step": ""})
        assert bridge.consult_blade("a1") is None and bridge.blade_unchecked("a1") is None

    def test_the_preflight_lists_the_blade_as_not_checked(self, monkeypatch):
        _hosted_only(monkeypatch)
        _offline(monkeypatch)
        result = _preflight(monkeypatch)
        blade = [c for c in result["checks"] if c["name"] == "cutter_blade"]
        assert len(blade) == 1
        assert blade[0]["passed"] is True and blade[0]["advisory"] is True and blade[0]["checked"] is False
        assert "this computer is offline" in blade[0]["message"] and blade[0]["why"] == "offline"
        assert result["ready"] is True

    def test_the_start_stays_quiet(self, monkeypatch):
        """A start is not a checklist: no blade line when nothing wants attention."""
        from kiln import _pro_cutter_bridge as bridge

        _hosted_only(monkeypatch)
        _offline(monkeypatch)
        assert bridge.consult_blade("a1") is None


# ---------------------------------------------------------------------------
# one voice: the sentence itself
# ---------------------------------------------------------------------------


class TestTheSentence:
    def test_the_four_causes_and_their_fixes(self):
        from kiln.served_answer import Miss, sentence

        words = dict(feature="servers", on_the_line="The head is over the plate", cannot="plan the move",
                     wont="won't move it", safe_remedy="Use the screen's jog controls")
        assert sentence(Miss("offline"), **words) == (
            "The head is over the plate. Kiln can't plan the move right now because this computer is offline, "
            "so it won't move it. Use the screen's jog controls, or reconnect to the internet and try again."
        )
        assert sentence(Miss("signed_out"), **words).endswith("Use the screen's jog controls, or sign in and try again.")
        assert "because Kiln is signed out, so" in sentence(Miss("signed_out"), **words)
        assert sentence(Miss("unanswered"), **words).endswith(", or wait a minute and try again.")
        assert "because Kiln's servers didn't answer, so" in sentence(Miss("unanswered"), **words)
        refused = sentence(Miss("refused", code="MODEL_CAP_REACHED", detail="Three models is the free cap."), **words)
        assert refused == (
            "The head is over the plate. Kiln can't plan the move right now because Kiln's servers said no, "
            "so it won't move it. Three models is the free cap. Use the screen's jog controls."
        )
        assert "MODEL_CAP_REACHED" not in refused

    def test_no_safe_remedy_and_a_custom_then(self):
        from kiln.served_answer import Miss, sentence

        out = sentence(Miss("offline"), feature="servers", on_the_line="X", cannot="do it", wont="did nothing",
                       then="run the pre-flight again")
        assert out == "X. Kiln can't do it right now because this computer is offline, so it did nothing. Reconnect to the internet and run the pre-flight again."

    def test_a_refusal_with_no_words_still_offers_a_next_step(self):
        from kiln.served_answer import Miss, sentence

        out = sentence(Miss("refused", code="WEIRD"), feature="servers", on_the_line="X", cannot="do it", wont="did nothing")
        assert "said no and gave no reason" in out and out.endswith("try again.")

    def test_fields_ride_beside(self):
        from kiln.served_answer import Miss, fields

        assert fields(Miss("refused", code="C", detail="d")) == {"why": "refused", "why_code": "C", "why_detail": "d"}
        assert fields(None) == {}

    def test_classifying_an_answer(self):
        from kiln.served_answer import classify_answer

        assert classify_answer({"plan": {}}) is None
        assert classify_answer({"success": True}) is None
        assert classify_answer({"success": False, "error": {"code": "TIER_REQUIRED", "message": "needs Pro"}}).detail == "needs Pro"
        assert classify_answer({"status": "error", "code": "TIER_REQUIRED", "error": "x", "retryable": True}).cause == "unanswered"
        assert classify_answer({"detail": "quota"}).cause == "refused"
        assert classify_answer(None).cause == "unanswered"

    def test_classifying_a_transport_error(self, monkeypatch):
        from kiln import served_answer as sa

        assert sa.classify_transport_error(urllib.error.URLError(socket.gaierror(8, "no dns"))).cause == "offline"
        assert sa.classify_transport_error(OSError(errno.ENETUNREACH, "Network is unreachable")).cause == "offline"
        assert sa.classify_transport_error(ConnectionRefusedError(61, "refused")).cause == "unanswered"
        assert sa.classify_transport_error(urllib.error.URLError("unknown url type")).cause == "unanswered"
        monkeypatch.setattr(sa, "_route_to", lambda host: True)
        assert sa.classify_transport_error(TimeoutError("slow")).cause == "unanswered"
        monkeypatch.setattr(sa, "_route_to", lambda host: False)
        assert sa.classify_transport_error(urllib.error.URLError(TimeoutError("slow"))).cause == "offline"
