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
        """The service's own "try again shortly" is not a ruling on this
        machine.  It used to be read as one, and a paired printer lost its
        plan for a server blip."""
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


# ---------------------------------------------------------------------------
# the perfectionist lap: kinds, every door's why, the probe, the loader
# ---------------------------------------------------------------------------


class TestWhatIsOnTheLineForAManifestTool:
    def test_a_verdict_not_given_never_reads_as_a_yes(self, tmp_path, monkeypatch):
        _paired(tmp_path, monkeypatch)

        def _dns(_req, timeout):
            raise urllib.error.URLError(socket.gaierror(8, "no dns"))

        monkeypatch.setattr("urllib.request.urlopen", _dns)
        out = _pro_api_call()("check_skin_contact_suitability", material="PLA")
        assert "asks Kiln's servers for a verdict" in out["error"] and "never as a yes" in out["error"]

    def test_the_manifests_kind_wins_over_the_name_rule(self, tmp_path, monkeypatch):
        import kiln.server as srv

        _paired(tmp_path, monkeypatch)
        monkeypatch.setitem(srv._PRO_TOOL_OFFLINE_KIND, "generate_coaster", "record")

        def _dns(_req, timeout):
            raise urllib.error.URLError(socket.gaierror(8, "no dns"))

        monkeypatch.setattr("urllib.request.urlopen", _dns)
        out = _pro_api_call()("generate_coaster")
        assert "recorded nothing" in out["error"] and "made nothing" not in out["error"]

    def test_the_stub_loader_reads_only_a_known_kind_of_the_known_version(self, tmp_path, monkeypatch):
        import kiln.server as srv

        manifest = {"tools": [
            {"name": "k_verdict", "description": "d", "tier": "free", "parameters": {"properties": {}},
             "offline": {"schema_version": 1, "kind": "verdict"}},
            {"name": "k_unknown", "description": "d", "tier": "free", "parameters": {"properties": {}},
             "offline": {"schema_version": 1, "kind": "wormhole"}},
            {"name": "k_future", "description": "d", "tier": "free", "parameters": {"properties": {}},
             "offline": {"schema_version": 2, "kind": "verdict"}},
            {"name": "k_none", "description": "d", "tier": "free", "parameters": {"properties": {}}},
        ]}
        (tmp_path / "pro_tool_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        monkeypatch.setattr(srv, "Path", lambda _p: tmp_path / "kiln")
        for name in ("_PRO_TOOL_NUDGES", "_PRO_TOOL_TIERS", "_PRO_TOOL_QUOTA", "_PRO_TOOL_OFFLINE_KIND"):
            monkeypatch.setattr(srv, name, {})

        class _FakeMCP:
            def tool(self, **_kwargs):
                return lambda fn: fn

        srv._register_pro_tool_stubs(_FakeMCP())
        assert srv._PRO_TOOL_OFFLINE_KIND == {"k_verdict": "verdict"}

    def test_a_401_detail_carries_the_sign_in_hints(self, tmp_path, monkeypatch):
        _paired(tmp_path, monkeypatch)

        def _rejected(req, timeout):
            raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {},
                                         io.BytesIO(json.dumps({"detail": "Invalid or missing auth token"}).encode()))

        monkeypatch.setattr("urllib.request.urlopen", _rejected)
        out = _pro_api_call()("generate_coaster")
        assert out["why"] == "signed_out" and out["code"] == "KILN_AUTH_REJECTED"
        assert "Kiln is signed out" in out["error"] and "Sign in and try again" in out["error"]
        assert out["agent_hint"] and out["setup_hint"]


class TestEveryDoorCarriesTheWhy:
    def test_the_filament_door(self, no_kiln_pro, bambu, monkeypatch):
        import kiln.server as srv
        from kiln.plugins.filament_handling_tools import wipe_nozzle

        _offline(monkeypatch)
        bambu._printer_model = "bambu_a1"
        bambu._last_status["ams"]["tray_now"] = "0"
        _hot(bambu, monkeypatch)
        monkeypatch.setattr(srv, "_resolve_control_target", lambda name: (bambu, "default"))
        for gate in ("_emergency_latch_error", "_check_auth", "_check_rate_limit", "_check_confirmation"):
            monkeypatch.setattr(srv, gate, lambda *a, **k: None)
        out = wipe_nozzle()
        assert out["success"] is False and out["error"]["code"] == "UNSUPPORTED"
        assert "this computer is offline" in out["error"]["message"] and out["why"] == "offline"

    def test_the_purge_placement(self, no_kiln_pro, bambu, monkeypatch):
        _answer(monkeypatch, {"status": "error", "code": "KILN_ACCOUNT_NOT_PAIRED", "error": "wall"})
        bambu._printer_model = "bambu_a1"
        bambu._last_status["ams"]["tray_now"] = "0"
        _hot(bambu, monkeypatch)
        result = bambu.purge_filament(length_mm=10)
        station = result.details["purge_station"]
        assert station["status"] == "in_place" and station["why"] == "signed_out"
        assert "Kiln is signed out" in station["reason"] and "sign in and the next purge parks first" in station["reason"]

    def test_a_cached_plan_says_where_it_came_from_and_why(self, no_kiln_pro, monkeypatch):
        from kiln import _pro_motion_bridge as bridge

        _signed_in(monkeypatch)
        _answer(monkeypatch, {"plan": _plan("park")})
        bridge.plan_for(_Machine(), "park", axes="XY")
        _offline(monkeypatch)
        doc = bridge.plan_for(_Machine(), "park", axes="XY")
        assert doc["from_cache"] is True and doc["cache_because"] == "offline"

    def test_a_failed_cut_report_tells_the_next_preflight_why(self, monkeypatch):
        from kiln import _pro_cutter_bridge as bridge

        _hosted_only(monkeypatch)
        _offline(monkeypatch)
        bridge._served_report("a1", {"command": "load"})  # the report itself is dropped, quietly
        assert bridge._service_down_miss.cause == "offline"
        assert bridge.consult_blade("a1") is None  # the backoff is on, so the status is not asked
        assert bridge.blade_unchecked("a1")["why"] == "offline"


class TestTheProbeAndTheCap:
    def test_the_probe_dials_the_urls_own_host_and_port(self, monkeypatch):
        from kiln import served_answer as sa

        dialed = []

        class _Sock:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        monkeypatch.setattr(socket, "create_connection", lambda addr, timeout: dialed.append(addr) or _Sock())
        assert sa._route_to("http://localhost:8000") is True
        assert sa._route_to("https://api.kiln3d.com") is True
        assert sa._route_to("api.kiln3d.com") is True
        assert dialed == [("localhost", 8000), ("api.kiln3d.com", 443), ("api.kiln3d.com", 443)]
        assert sa._route_to("") is True and dialed[-1] == ("api.kiln3d.com", 443)  # no host: nothing dialed

    def test_no_route_reads_as_no_route(self, monkeypatch):
        from kiln import served_answer as sa

        def _refuse(addr, timeout):
            raise OSError("no route")

        monkeypatch.setattr(socket, "create_connection", _refuse)
        assert sa._route_to("https://api.kiln3d.com") is False

    def test_an_identifier_keeps_its_spelling(self):
        from kiln.served_answer import Miss, sentence

        out = sentence(Miss("refused", "INVALID_ARGUMENT", "printer_id must name the printer's catalogue model."),
                       feature="servers", on_the_line="X", cannot="do it", wont="did nothing")
        assert "printer_id must name" in out and "Printer_id" not in out


# ---------------------------------------------------------------------------
# the browser stage link speaks the same voice
# ---------------------------------------------------------------------------


class TestTheStageLinkSpeaksTheSameVoice:
    """The preview link used to explain itself in a second vocabulary, one of
    whose sentences handed a person a command to type.  Every served cause
    it can hit now reads as the clause the other doors use; the local
    conditions (opted out, too large, empty, no httpx) keep their own plain
    words."""

    @pytest.mark.parametrize(
        "reason, cause_words, fix_words",
        [
            ("offline", "this computer is offline", "reconnect to the internet and try again"),
            ("signed_out", "Kiln is signed out", "sign in and try again"),
            ("session_refused", "Kiln is signed out", "sign in and try again"),
            ("unanswered", "Kiln's servers didn't answer", "wait a minute and try again"),
            ("transport", "Kiln's servers didn't answer", "wait a minute and try again"),  # older records
            ("bad_response", "Kiln's servers didn't answer", "wait a minute and try again"),
            ("http_503", "Kiln's servers didn't answer", "wait a minute and try again"),
            ("http_401", "Kiln is signed out", "sign in and try again"),
            ("http_403", "Kiln's servers said no and gave no reason", "wait a minute and try again"),
        ],
    )
    def test_each_served_cause_reads_as_the_shared_clause(self, reason, cause_words, fix_words):
        from kiln.stage_link import refusal_sentence

        s = refusal_sentence(reason)
        assert s.startswith("Kiln can't issue a browser link right now (") and cause_words in s and fix_words in s
        assert not any(bad in s for bad in ("kiln_signin", "link service", "link door", "HTTP", "http_", "_out", "_refused", "_response"))
        assert s == s.strip() and not s.endswith(".")  # a clause, for the sentence that carries it

    def test_the_local_conditions_keep_plain_words(self):
        from kiln.stage_link import refusal_sentence

        for reason in ("opted_out", "too_large", "empty", "no_httpx"):
            s = refusal_sentence(reason)
            assert "link door" not in s and "link service" not in s and "Kiln can't" not in s
        assert refusal_sentence(None) == "no browser link was asked for"

    def test_the_result_that_carries_the_clause_still_reads(self):
        """The still-image fallback embeds the clause after a colon."""
        from kiln.stage_link import refusal_sentence

        line = f"no browser link could be issued: {refusal_sentence('offline')}. The still image is the floor."
        assert line == (
            "no browser link could be issued: Kiln can't issue a browser link right now (this computer is "
            "offline); reconnect to the internet and try again. The still image is the floor."
        )


# ---------------------------------------------------------------------------
# the nozzle-life verdict is served, and a miss is named at every door
# ---------------------------------------------------------------------------


def _hosted_only_nozzle(monkeypatch):
    from kiln import _pro_nozzle_bridge as bridge

    for name in ("kiln_pro", "kiln_pro.data_overlays", "kiln_pro.nozzle_intelligence",
                 "kiln_pro.nozzle_intelligence.capacity", "kiln_pro.nozzle_intelligence.store_resolver"):
        monkeypatch.setitem(sys.modules, name, None)
    monkeypatch.setattr(bridge, "_service_down_until", 0.0)
    monkeypatch.setattr(bridge, "_service_down_miss", None)
    monkeypatch.setattr(bridge, "_last_miss", {})
    monkeypatch.setattr(bridge, "_declared_model", lambda pid: "bambu_a1")
    return bridge


_VERDICT = {"success": True, "status": "exceeded_p90", "narrative": "93% of the brass budget on this CF filament.",
            "percent_used": 93.0, "tool": "check_nozzle_capacity_for_print"}


class TestTheNozzleVerdictIsServed:
    def test_without_kiln_pro_the_hosted_door_is_asked_with_a_short_timeout(self, monkeypatch):
        bridge = _hosted_only_nozzle(monkeypatch)
        import kiln.server as srv

        seen = {}

        def _call(tool, _timeout=30.0, **kw):
            seen.update({"tool": tool, "timeout": _timeout, **kw})
            return dict(_VERDICT)

        monkeypatch.setattr(srv, "_pro_api_call", _call)
        out = bridge.consult_capacity(printer_id="a1", planned_grams=340.0, filament_material="PLA-CF")
        assert out["status"] == "exceeded_p90" and out["percent_used"] == 93.0
        assert seen["tool"] == "check_nozzle_capacity_for_print" and seen["timeout"] == bridge._CONSULT_TIMEOUT_S
        assert seen["printer_id"] == "a1" and seen["planned_grams"] == 340.0
        assert seen["filament_material"] == "PLA-CF" and seen["printer_model"] == "bambu_a1"
        assert bridge.nozzle_unchecked("a1") is None

    def test_a_miss_is_named_and_never_a_verdict(self, monkeypatch):
        bridge = _hosted_only_nozzle(monkeypatch)
        _offline(monkeypatch)
        assert bridge.consult_capacity(printer_id="a1", planned_grams=340.0) is None
        gap = bridge.nozzle_unchecked("a1")
        assert gap["why"] == "offline" and "this computer is offline" in gap["line"] and "Print as usual" in gap["line"]
        start = bridge.nozzle_unchecked("a1", at="start")
        assert "started this one without that check" in start["line"] and "before the next print" in start["line"]

    def test_a_signed_out_install_is_told_to_sign_in(self, monkeypatch):
        bridge = _hosted_only_nozzle(monkeypatch)
        _answer(monkeypatch, {"status": "error", "code": "KILN_ACCOUNT_NOT_PAIRED", "error": "wall"})
        assert bridge.consult_capacity(printer_id="a1", planned_grams=10.0) is None
        assert bridge.nozzle_unchecked("a1")["why"] == "signed_out"

    def test_a_served_answer_with_no_record_is_an_answer_not_a_miss(self, monkeypatch):
        bridge = _hosted_only_nozzle(monkeypatch)
        _answer(monkeypatch, {"success": True, "status": "unknown_nozzle", "narrative": "no record"})
        out = bridge.consult_capacity(printer_id="a1", planned_grams=10.0)
        assert out["status"] == "unknown_nozzle" and bridge.nozzle_unchecked("a1") is None

    def test_an_unreachable_service_backs_off_and_remembers_why(self, monkeypatch):
        bridge = _hosted_only_nozzle(monkeypatch)
        import kiln.server as srv

        calls = []
        monkeypatch.setattr(srv, "_pro_api_call", lambda tool, _timeout=30.0, **kw: calls.append(tool) or {
            "status": "error", "code": "SERVER_UNREACHABLE", "why": "offline", "error": "no route"})
        for _ in range(3):
            assert bridge.consult_capacity(printer_id="a1", planned_grams=10.0) is None
        assert len(calls) == 1 and bridge.nozzle_unchecked("a1")["why"] == "offline"

    def test_with_kiln_pro_installed_the_servers_are_never_asked(self, monkeypatch):
        import types

        import kiln.server as srv
        from kiln import _pro_nozzle_bridge as bridge

        pkg = types.ModuleType("kiln_pro")
        ov = types.ModuleType("kiln_pro.data_overlays")
        ni = types.ModuleType("kiln_pro.nozzle_intelligence")
        cap = types.ModuleType("kiln_pro.nozzle_intelligence.capacity")
        res = types.ModuleType("kiln_pro.nozzle_intelligence.store_resolver")
        ov.load_overlay = lambda name: None
        cap.resolve_capacity_baseline = lambda **k: {"p50_grams": 100.0, "p90_grams": 200.0}
        cap.compute_print_capacity_for_nozzle = lambda **k: {"status": "safe", "narrative": "local"}

        class _State:
            material = types.SimpleNamespace(value="brass")
            grams_through = 0.0

        res.resolve_backend = lambda tool_name: (object(), None)
        res.resolve_state_or_factory_default = lambda backend, printer_id, printer_model=None: _State()
        for name, mod in (("kiln_pro", pkg), ("kiln_pro.data_overlays", ov), ("kiln_pro.nozzle_intelligence", ni),
                          ("kiln_pro.nozzle_intelligence.capacity", cap), ("kiln_pro.nozzle_intelligence.store_resolver", res)):
            monkeypatch.setitem(sys.modules, name, mod)
        monkeypatch.setattr(bridge, "_last_miss", {})
        asked = []
        monkeypatch.setattr(srv, "_pro_api_call", lambda tool, **kw: asked.append(tool) or {})
        assert bridge.consult_capacity(printer_id="a1", planned_grams=10.0)["status"] == "safe"
        assert asked == [] and bridge.nozzle_unchecked("a1") is None


class TestTheNozzleDoors:
    def _gcode(self, tmp_path):
        path = tmp_path / "part.gcode"
        path.write_text("G28\nG1 X10 Y10 Z0.2 E1\n; filament used [g] = 340.0\n")
        return path

    def test_the_preflight_reads_the_grams_from_the_file_and_carries_the_verdict(self, tmp_path, monkeypatch):
        """The pre-flight's nozzle check could never fire: nothing produced the
        grams field it read.  Now the file's own grams line feeds it."""
        _hosted_only_nozzle(monkeypatch)
        seen = {}

        def _call(tool, _timeout=30.0, **kw):
            seen.update(kw)
            return dict(_VERDICT)

        import kiln.server as srv

        monkeypatch.setattr(srv, "_pro_api_call", _call)
        monkeypatch.setattr("kiln._pro_cutter_bridge.consult_blade", lambda name, printer_model=None: None)
        result = _preflight_with_file(monkeypatch, str(self._gcode(tmp_path)))
        nozzle = [c for c in result["checks"] if c["name"] == "nozzle_capacity"]
        assert len(nozzle) == 1 and nozzle[0]["status"] == "exceeded_p90" and nozzle[0]["advisory"] is True
        assert nozzle[0]["passed"] is False and "brass budget" in nozzle[0]["message"]
        assert seen["planned_grams"] == 340.0 and seen["printer_id"] == "a1"

    def test_the_preflight_lists_the_nozzle_as_not_checked_when_offline(self, tmp_path, monkeypatch):
        _hosted_only_nozzle(monkeypatch)
        _offline(monkeypatch)
        monkeypatch.setattr("kiln._pro_cutter_bridge.consult_blade", lambda name, printer_model=None: None)
        result = _preflight_with_file(monkeypatch, str(self._gcode(tmp_path)))
        nozzle = [c for c in result["checks"] if c["name"] == "nozzle_capacity"]
        assert len(nozzle) == 1 and nozzle[0]["checked"] is False and nozzle[0]["why"] == "offline"
        assert "this computer is offline" in nozzle[0]["message"] and nozzle[0]["passed"] is True

    def test_the_start_refuses_a_served_exceeded_nozzle_the_same_as_a_local_one(self, monkeypatch):
        import os
        from unittest.mock import patch

        import kiln.server as srv
        from kiln.printers.base import PrinterFile

        from .test_every_start_says_so import _two_printers

        _hosted_only_nozzle(monkeypatch)
        garage, _ = _two_printers(monkeypatch)
        monkeypatch.setattr(srv, "_check_rate_limit", lambda *a, **k: None)  # two starts in one process
        monkeypatch.setattr(garage, "list_files", lambda: [PrinterFile(name="part.gcode", path="part.gcode", filament_used_mm=5000.0)])
        _answer(monkeypatch, dict(_VERDICT))
        monkeypatch.setattr("kiln._pro_cutter_bridge.consult_blade", lambda name, printer_model=None: None)
        with patch.dict(os.environ, {"KILN_SKIP_PREFLIGHT": "1", "KILN_SKIP_PREVIEW_GATE": "1", "KILN_SKIP_NOZZLE_CHECK": ""}):
            out = srv.start_print(file_name="part.gcode", printer_name="garage")
        assert out["success"] is False and out["error"]["code"] == "NOZZLE_CAPACITY_EXCEEDED"
        assert garage.started == []

    def test_the_start_goes_ahead_quietly_for_a_nozzle_never_flagged(self, monkeypatch):
        """A miss never blocks.  It is named at a start only for a nozzle
        already flagged as wearing (pinned in test_nozzle_milestones); for
        one never flagged, a per-print "not checked" would be noise."""
        import os
        from unittest.mock import patch

        import kiln.server as srv
        from kiln.printers.base import PrinterFile

        from .test_every_start_says_so import _two_printers

        _hosted_only_nozzle(monkeypatch)
        garage, _ = _two_printers(monkeypatch)
        monkeypatch.setattr(srv, "_check_rate_limit", lambda *a, **k: None)  # two starts in one process
        monkeypatch.setattr(garage, "list_files", lambda: [PrinterFile(name="part.gcode", path="part.gcode", filament_used_mm=5000.0)])
        _offline(monkeypatch)
        monkeypatch.setattr("kiln._pro_cutter_bridge.consult_blade", lambda name, printer_model=None: None)
        with patch.dict(os.environ, {"KILN_SKIP_PREFLIGHT": "1", "KILN_SKIP_PREVIEW_GATE": "1"}):
            out = srv.start_print(file_name="part.gcode", printer_name="garage")
        assert out.get("success") is not False, out
        assert garage.started == ["part.gcode"] and "nozzle_check" not in out


def _preflight_with_file(monkeypatch, file_path: str):
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

        return preflight_check(file_path=file_path)
