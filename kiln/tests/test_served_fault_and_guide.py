"""A plain install gets a fault code's reading and the maker's repair guide
from Kiln's servers, and says why when it cannot.

Until 2026-10-17 the fault bridge and the guide bridge answered only where
kiln-pro was installed beside public Kiln, which is nowhere a customer is:
every ``pip install kiln3d`` showed a fault code's family line ("this is an
AMS lite filament load / unload fault ... free with a Kiln sign-in") to
every account, paying or not, and the sign-in it named changed nothing.
The repair guides, free at every tier, reached no plain install at all, and
the doctor line told those installs a plan would add them.

Now both bridges ask Kiln's servers when kiln-pro is not here
(``fault_reading`` for one code, ``maintenance_guide`` for one step).  Pinned
here, against a stand-in for the servers that answers in the wires' shape:

* signed in, the reading and the fix arrive at every door, once per code;
* a paid row below the plan answers with the servers' one line, said by
  the status door and never as a second nudge on the troubleshoot door;
* signed out or offline, the family line stands and the miss is said once,
  in the shared voice, with why beside it -- never "fine", never "the
  maker publishes no guide";
* the printer's own message thread never waits on the network;
* the AMS safety alert rides the same answer to every door at every tier;
* a guide step arrives one step at a time, as the door asked for it.

The real wires, at each tier and through the real hosted posture, are
pinned on the kiln-pro side (``tests/test_fault_and_guide_served.py``).
"""

from __future__ import annotations

import builtins
import importlib
import json
import threading
from typing import Any
from unittest import mock

import paho.mqtt.client as mqtt
import pytest

import kiln._pro_fault_bridge as fault_bridge
import kiln._pro_guide_bridge as guide_bridge
import kiln.server as srv
from kiln.plugins import repair_guide_tools as rg
from kiln.printers.bambu import BambuAdapter, read_bambu_fault

CUTTER_DECIMAL = 302022657
CUTTER = "1200-8001"
PAID_DECIMAL = 50338304  # 0300-1A00, a paid row in the stand-in below
PAID = "0300-1A00"
AMS_RUNAWAY = "0700-9600-0001-0001"

CUTTER_ROW = {
    "title": "Filament cutter",
    "cause": "The blade did not cross the filament path.",
    "fix": "Power off, reseat the blade in its slot.",
    "severity": "warning",
    "namespace": "print_error",
}
RUNAWAY = {
    "code": AMS_RUNAWAY,
    "means": "AMS dryer thermal runaway risk -- turn off the AMS power supply.",
    "action": "escalate_to_recovery",
    "severity": "critical",
}
UPGRADE = "Kiln Pro reads this code for you: what it means on your printer, the fix, and how serious it is."

GUIDE_SLUG = "bambu:a1-mini/maintenance/filament-cutter-replacement"
GUIDE = {
    "vendor": "bambu",
    "source_url": "https://wiki.bambulab.com/en/a1-mini/maintenance/filament-cutter-replacement",
    "source_title": "Filament Cutter Replacement Guide",
    "fetched_at": "2026-09-17",
    "machine": "A1 mini / A1",
    "applies_also_to": None,
    "topic": "cutter",
    "tools": ["H2.0 Allen key"],
    "interval": "every 10-15 spools",
    "parts": [],
    "power_off_warning": "Power off the printer first.",
    "warnings": [],
    "steps": [
        {"n": 1, "title": "Remove the cover", "do": "Undo the two screws.", "no_image": True, "verify": None},
        {"n": 2, "title": "Swap the blade", "do": "Lift the old blade out.", "no_image": True, "verify": None},
        {"n": 3, "title": "Refit", "do": "Refit the cover.", "no_image": True, "verify": "The lever springs back."},
    ],
}
COVERAGE = {
    "maker": "Bambu Lab", "count": 1, "topics": ["cutter"], "publishes": "photo_guides_per_model",
    "maker_guides_in_table": 110, "maker_index_url": "https://wiki.bambulab.com/en/a1-mini/maintenance",
}


def _hex(code: str) -> str:
    return "".join(c for c in code.split()[0].upper() if c in "0123456789ABCDEF")


class _Servers:
    """Kiln's servers as the two wires answer: one fault code, one guide step."""

    def __init__(self, tier: str = "free") -> None:
        self.tier = tier
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.offline = False
        self.signed_out = False

    def __call__(self, tool_name: str, _timeout: float | None = None, _asked_by_user: bool = True, **kw: Any):
        self.calls.append((tool_name, dict(kw)))
        if self.offline:
            return {"status": "error", "success": False, "code": "SERVER_UNREACHABLE", "error": "x",
                    "why": "offline", "tool": tool_name}
        if self.signed_out:
            return {"status": "error", "code": "KILN_ACCOUNT_NOT_PAIRED", "error": "sign in", "why": "signed_out"}
        if tool_name == "fault_reading":
            return self._fault(kw)
        if tool_name == "maintenance_guide":
            return self._guide(kw)
        raise AssertionError(f"unexpected wire {tool_name}")

    def _fault(self, kw: dict[str, Any]) -> dict[str, Any]:
        digits = _hex(kw["code"])
        out: dict[str, Any] = {"success": True, "reading": None, "guide": None}
        if digits == "12008001" and kw.get("kind") == "print_error":
            out["reading"] = dict(CUTTER_ROW)
            out["guide"] = {"slug": GUIDE_SLUG, "start_at_step": 1}
        elif digits == "03001A00" and kw.get("kind") == "print_error":
            if self.tier == "pro":
                out["reading"] = {**CUTTER_ROW, "cause": "The nozzle is clogged.", "fix": "Cold pull."}
            else:
                out.update(upgrade=UPGRADE, upgrade_url="https://kiln3d.com/pricing")
        if digits.startswith("07009600"):
            out["safety_alert"] = dict(RUNAWAY)
        return json.loads(json.dumps(out))

    def _guide(self, kw: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {"success": True, "printer_id": kw["printer_id"], "coverage": dict(COVERAGE), "found": None}
        if kw.get("topic") == "cutter" or _hex(kw.get("code") or "x") == "12008001" or kw.get("guide") == GUIDE_SLUG:
            wanted = None if kw.get("plan_only") else int(kw.get("step") or 0)
            steps = [
                {"n": s["n"], "title": s["title"], **({k: v for k, v in s.items() if k not in ("n", "title")}
                                                      if s["n"] == wanted else {})}
                for s in GUIDE["steps"]
            ]
            # The wire names the maker's page as the hosted repair_guide does.
            names = {"source_url": "maker_page_url", "source_title": "maker_page_title"}
            header = {names.get(k, k): v for k, v in GUIDE.items() if k != "steps"}
            out["found"] = {"slug": GUIDE_SLUG, "start_at_step": 1, "guide": {**header, "steps": steps}}
        return json.loads(json.dumps(out))


@pytest.fixture
def plain_install(monkeypatch: pytest.MonkeyPatch):
    """No kiln-pro on this computer: every ``kiln_pro`` import fails."""
    importlib.reload(fault_bridge)  # the real functions, past conftest's silence
    real_import = builtins.__import__

    def _no_pro(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "kiln_pro" or name.startswith("kiln_pro."):
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_pro)
    monkeypatch.setattr(guide_bridge, "_reader", lambda: None)
    monkeypatch.setattr(rg, "_probe_picture", lambda url: "live")
    monkeypatch.setattr(srv, "_check_auth", lambda *a, **k: None)
    yield
    fault_bridge.forget()


@pytest.fixture
def servers(plain_install, monkeypatch: pytest.MonkeyPatch) -> _Servers:
    fake = _Servers()
    monkeypatch.setattr(srv, "_pro_api_call", fake)
    return fake


def _status(adapter: BambuAdapter) -> dict[str, Any]:
    with mock.patch("kiln.server._get_adapter", return_value=adapter):
        return srv.printer_status()


@pytest.fixture
def adapter(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> BambuAdapter:
    monkeypatch.setenv("KILN_BAMBU_TLS_PIN_FILE", str(tmp_path / "pins.json"))
    a = BambuAdapter(host="192.0.2.10", access_code="12345678", serial="TEST1234567890", timeout=2)
    a._mqtt_connected.set()
    a._connected = True
    a._mqtt_client = mock.MagicMock()
    published = mock.MagicMock()
    published.rc = mqtt.MQTT_ERR_SUCCESS
    a._mqtt_client.publish.return_value = published
    return a


def _push(adapter: BambuAdapter, **fields: Any) -> None:
    msg = mock.MagicMock()
    msg.payload = json.dumps({"print": {"command": "push_status", **fields}}).encode()
    adapter._on_message(adapter._mqtt_client, None, msg)


def _fault_calls(fake: _Servers) -> list[dict[str, Any]]:
    return [kw for tool, kw in fake.calls if tool == "fault_reading"]


# ---------------------------------------------------------------------------
# The fault reading
# ---------------------------------------------------------------------------


class TestAReadingArrives:
    def test_signed_in_the_reader_gets_the_cause_and_the_fix(self, servers):
        fault = read_bambu_fault(CUTTER, kind="print_error")
        assert fault.reading == CUTTER_ROW["cause"] and fault.remedy == CUTTER_ROW["fix"]
        assert fault.private is True and fault.note is None
        assert _fault_calls(servers) == [{"code": CUTTER, "kind": "print_error"}]

    def test_one_code_is_asked_once_however_many_doors_read_it(self, servers, adapter):
        _push(adapter, gcode_state="idle", print_error=CUTTER_DECIMAL)
        for _ in range(3):
            adapter.get_state()
        read_bambu_fault(CUTTER, kind="print_error")
        assert len(_fault_calls(servers)) == 1

    def test_the_status_door_shows_the_reading_and_leads_the_remedy_with_the_fix(self, servers, adapter):
        _push(adapter, gcode_state="idle", print_error=CUTTER_DECIMAL)
        state = adapter.get_state()
        assert CUTTER_ROW["cause"] in state.fault_note
        assert state.fault_remedy.startswith(CUTTER_ROW["fix"].rstrip("."))

    def test_troubleshoot_carries_the_decoded_block_and_the_guide_sentence(self, servers):
        out = srv.troubleshoot_printer("bambu_a1_mini", hms_code=CUTTER)
        assert out["hms_decoded"] == CUTTER_ROW
        assert "repair_guide(" in out["repair_guide_next_step"] and "step=1" in out["repair_guide_next_step"]
        assert out["fault_readings"][0]["remedy"] == CUTTER_ROW["fix"]
        assert "fault_reading_note" not in out
        # The reading and the guide pointer came in one answer.
        assert len(_fault_calls(servers)) == 1

    def test_a_pro_account_gets_the_paid_row(self, servers):
        servers.tier = "pro"
        fault = read_bambu_fault(PAID, kind="print_error")
        assert fault.reading == "The nozzle is clogged." and fault.remedy == "Cold pull."


class TestAPaidRowBelowThePlan:
    def test_the_family_line_stands_and_the_note_is_the_servers_own_line(self, servers):
        fault = read_bambu_fault(PAID, kind="print_error")
        assert fault.decoded is None and fault.remedy is None
        assert fault.note == f"{UPGRADE} https://kiln3d.com/pricing"

    def test_the_status_door_says_it_once(self, servers, adapter):
        _push(adapter, gcode_state="idle", print_error=PAID_DECIMAL)
        first = _status(adapter)
        second = _status(adapter)
        assert first["fault_reading_note"].startswith(UPGRADE)
        assert "fault_reading_note" not in second

    def test_the_troubleshoot_door_leaves_the_nudge_to_its_own_hint(self, servers):
        out = srv.troubleshoot_printer("bambu_a1_mini", hms_code=PAID)
        assert "fault_reading_note" not in out


class TestAMissIsSaid:
    def test_signed_out_the_family_line_stands_and_why_is_said_once(self, servers, adapter):
        servers.signed_out = True
        _push(adapter, gcode_state="idle", print_error=CUTTER_DECIMAL)
        first = _status(adapter)
        second = _status(adapter)
        note = first["fault_reading_note"]
        assert "Kiln can't read 1200-8001 right now because Kiln is signed out" in note
        assert "shows only what kind of fault it is" in note and "sign in and ask again" in note
        assert first["fault_reading_why"]["why"] == "signed_out"
        assert "fault_reading_note" not in second
        assert "filament-cut fault" in first["printer"]["fault_note"]

    def test_offline_the_reader_backs_off_instead_of_asking_every_poll(self, servers):
        servers.offline = True
        first = read_bambu_fault(CUTTER, kind="print_error")
        read_bambu_fault(CUTTER, kind="print_error")
        read_bambu_fault("1200-8007", kind="print_error")
        assert "because this computer is offline" in first.note
        assert len(_fault_calls(servers)) == 1, "a dead link costs one ask, not one per read"

    def test_the_troubleshoot_door_says_why_the_reading_is_missing(self, servers):
        servers.signed_out = True
        out = srv.troubleshoot_printer("bambu_a1_mini", hms_code=CUTTER)
        assert "Kiln is signed out" in out["fault_reading_note"] and out["fault_reading_why"]["why"] == "signed_out"
        assert "hms_decoded" not in out and "repair_guide_next_step" not in out


class TestThePrintersOwnThreadNeverWaits:
    def test_a_first_sighting_reads_what_is_known_and_asks_beside_it(self, servers, monkeypatch):
        started = threading.Event()
        release = threading.Event()
        real = servers.__call__

        def slow(tool_name, _timeout=None, _asked_by_user=True, **kw):
            started.set()
            release.wait(5)
            return real(tool_name, _timeout, _asked_by_user, **kw)

        monkeypatch.setattr(srv, "_pro_api_call", slow)
        fault = read_bambu_fault(CUTTER, kind="print_error", wait=False)
        assert fault.decoded is None, "the message thread must not wait for the servers"
        assert started.wait(5)
        release.set()
        for t in threading.enumerate():
            if t.name == "kiln-fault-reading":
                t.join(5)
        assert read_bambu_fault(CUTTER, kind="print_error", wait=False).reading == CUTTER_ROW["cause"]


class TestTheSafetyAlertRides:
    def test_on_the_status_entry_and_the_troubleshoot_answer(self, servers):
        from kiln.printers.bambu import compose_bambu_faults

        faults = compose_bambu_faults({"hms": [{"attr": 0x07009600, "code": 0x00010001}]})
        assert faults and faults[0]["safety"] == RUNAWAY["means"]
        out = srv.troubleshoot_printer("bambu_a1_mini", hms_code=AMS_RUNAWAY)
        assert out["hms_safety_alert"] == RUNAWAY


# ---------------------------------------------------------------------------
# The repair guide
# ---------------------------------------------------------------------------


def _guide_calls(fake: _Servers) -> list[dict[str, Any]]:
    return [kw for tool, kw in fake.calls if tool == "maintenance_guide"]


class TestAGuideStepArrives:
    def test_a_step_is_the_makers_words_one_step_per_call(self, servers):
        out = rg.repair_guide("bambu_a1_mini", topic="cutter", step=2, power_off_confirmed=True)
        assert out["success"] is True and out["guide"] == GUIDE_SLUG
        assert out["do"] == "Lift the old blade out." and out["next_step"]["title"] == "Refit"
        assert out["step_count"] == 3 and out["maker"] == "Bambu Lab"
        assert out["maker_page_url"] == GUIDE["source_url"] and out["maker_page_title"] == GUIDE["source_title"]
        assert _guide_calls(servers) == [{"printer_id": "bambu_a1_mini", "topic": "cutter", "step": 2}]

    def test_the_plan_carries_the_titles(self, servers):
        out = rg.repair_guide("bambu_a1_mini", topic="cutter", plan_only=True)
        assert [s["title"] for s in out["steps"]] == ["Remove the cover", "Swap the blade", "Refit"]
        assert out["step_zero"]["text"] == "Power off the printer first."
        assert _guide_calls(servers)[0]["plan_only"] is True

    def test_the_power_off_gate_still_holds(self, servers):
        out = rg.repair_guide("bambu_a1_mini", topic="cutter", step=1)
        assert out["error"]["code"] == "POWER_OFF_REQUIRED"

    def test_a_code_finds_its_guide_through_the_servers(self, servers):
        out = rg.repair_guide("bambu_a1_mini", hms_code=CUTTER, plan_only=True)
        assert out["guide"] == GUIDE_SLUG and out["hms_code"] == "12008001"

    def test_no_guide_for_the_topic_names_what_the_maker_does_have(self, servers):
        out = rg.repair_guide("bambu_a1_mini", topic="belts")
        assert out["guide"] is None and out["coverage"]["count"] == 1
        assert "none for this topic" in out["kiln_note"]
        assert len(_guide_calls(servers)) == 1, "the coverage rode the same answer"

    def test_the_doctor_line_counts_the_guides(self, servers):
        detail, warn = rg.coverage_line("bambu_a1_mini")
        assert warn is False and detail.startswith("Bambu Lab: 1 guide(s) for this model")


class TestAGuideMissIsSaid:
    def test_offline_points_at_the_makers_pages_and_says_why(self, servers):
        servers.offline = True
        out = rg.repair_guide("bambu_a1_mini", topic="cutter")
        assert out["guide"] is None and out["why"] == "offline"
        assert "Kiln can't get the guide right now because this computer is offline" in out["kiln_note"]
        assert out["maker_index_url"] == "https://wiki.bambulab.com/en/home"
        assert rg.NO_GUIDE_SENTENCE not in out["kiln_note"]

    def test_the_doctor_line_makes_no_claim_it_could_not_check(self, servers):
        servers.signed_out = True
        detail, warn = rg.coverage_line("bambu_a1_mini")
        assert warn is True and rg.NO_GUIDE_SENTENCE not in detail and "Kiln Pro" not in detail
        assert "(Kiln is signed out); sign in and run kiln doctor again" in detail


class TestTheVoice:
    """Every miss sentence these two bridges produce, in the one shape."""

    @pytest.mark.parametrize("cause", ["offline", "signed_out", "unanswered", "refused"])
    def test_the_lines_read_in_the_shared_shape(self, plain_install, cause):
        from kiln.served_answer import Miss

        miss = Miss(cause, "X_CODE", "The servers said no." if cause == "refused" else "")
        fault_bridge._misses[(_hex(CUTTER), "print_error")] = miss
        guide_bridge._last_miss["bambu_a1_mini"] = miss
        for text in (fault_bridge.note_for(CUTTER, kind="print_error"),
                     guide_bridge.unavailable_line("bambu_a1_mini", maker="Bambu Lab")["line"]):
            assert "Kiln can't" in text and "right now because" in text and ", so it" in text
            assert "X_CODE" not in text and "kiln-pro" not in text and text.endswith(".")
