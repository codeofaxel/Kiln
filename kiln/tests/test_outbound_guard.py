"""What this computer reads off its own printers never reaches Kiln's servers raw.

2026-10-17: a served power-loss check was sent a Klipper printer's whole
configuration (the controller's serial port, CAN ids, macro bodies carrying
tokens), and a nozzle reading carried the printer's raw serial.  The facts
reader promised never to send an address, a serial or a credential; nothing
enforced it, and its tests used stand-in printers too clean to leak.  These
pin the guard (:mod:`kiln.outbound_guard`) at every way out:

* the rule itself, on a realistic leak;
* the served door: the bytes it actually sends, for an ask the person never
  made and for the install's own reads on one they did;
* a printer that leaks everything, read through every fact a served tool may
  ask for, with the bytes checked on their way out;
* every module that sends to Kiln's servers, found in the source and named
  in :data:`kiln.outbound_guard.SENDERS` -- a new way out fails here until
  someone decides what it may send.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pytest

from kiln import outbound_guard as guard

SECRETS = (
    "01P00A000000001", "01p00a000000001", "s3cr3t-access", "10.0.0.5", "192.168.1.40", "voron.local",
    "/dev/serial/by-id/usb-Klipper", "61755fe321ac", "aa:bb:cc:dd:ee:ff", "hooks.example.com",
    "adamarreola", "jane@example.com", "tok_live_9f8e7d",
)


def _leaks(text: str) -> list[str]:
    return [s for s in SECRETS if s in text]


class TestTheRule:
    def test_secrets_and_places_go_identities_become_codes_words_stay(self):
        out = guard.scrub({
            "serial": "01P00A000000001", "access_code": "s3cr3t-access", "host": "10.0.0.5",
            "url": "https://hooks.example.com/x", "api_key": "tok_live_9f8e7d",
            "reading": {"fingerprint": "bambu:serial:01p00a000000001", "material": "brass", "nozzle_diameter_mm": 0.4},
            "error": "HTTPConnectionPool(host='192.168.1.40', port=7125) at voron.local on /dev/serial/by-id/usb-Klipper-if00",
            "note": "saved to /Users/adamarreola/prints/x.gcode by jane@example.com",
            "mac": "aa:bb:cc:dd:ee:ff", "colour": "#C12E1F", "layer": 12,
            "source_artifact_token": "a1b2c3", "printer_name": "voron",
        })
        text = json.dumps(out)
        assert _leaks(text) == [], text
        assert out["colour"] == "#C12E1F" and out["layer"] == 12 and out["printer_name"] == "voron"
        assert out["source_artifact_token"] == "a1b2c3", "a handle the servers minted is not a credential"
        assert out["reading"]["material"] == "brass" and out["reading"]["nozzle_diameter_mm"] == 0.4
        assert out["serial"].startswith("id:") and out["reading"]["fingerprint"].startswith("id:")
        assert "host" not in out and "access_code" not in out and "url" not in out and "api_key" not in out

    def test_a_machine_keeps_one_code_and_another_machine_another(self):
        a = guard.machine_code("01P00A000000001")
        assert a == guard.machine_code("01p00a000000001 ") and a != guard.machine_code("01P00A000000002")
        assert "01P00A" not in a.upper()


@pytest.fixture
def wire(monkeypatch):
    """The served door with a sign-in, and the bytes it would send."""
    import urllib.request

    import kiln.server as server
    from kiln import auth_session

    sent: list[bytes] = []

    class _Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"success": true}'

    def urlopen(req, timeout=None):
        sent.append(req.data or b"")
        return _Resp()

    class _Bearer:
        token = "test-bearer"
        state = "ok"
        detail = ""

    monkeypatch.setattr(auth_session, "resolve_api_bearer", lambda: _Bearer())
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(server, "_heartbeat_device_header", lambda: {})
    return server, sent


class TestTheServedDoor:
    def test_an_ask_the_person_never_made_is_scrubbed_whole(self, wire):
        server, sent = wire
        server._pro_api_call("motion_plan", _asked_by_user=False, printer_id="bambu_a1",
                             serial="01P00A000000001", host="10.0.0.5", note="at voron.local")
        body = sent[-1].decode()
        assert _leaks(body) == [] and "bambu_a1" in body

    def test_the_persons_own_words_go_as_given_and_the_installs_reads_do_not(self, wire):
        server, sent = wire
        server._pro_api_call(
            "design_session", prompt="a bracket for the printer at 10.0.0.5",
            step_results={"printer_facts": {"data": {"printers": [{"name": "a1", "host": "10.0.0.5",
                                                                   "serial": "01P00A000000001"}]}}},
            printer_reading={"fingerprint": "bambu:serial:01p00a000000001", "material": "brass"},
        )
        body = json.loads(sent[-1])
        assert body["prompt"] == "a bracket for the printer at 10.0.0.5", "what the person typed is theirs"
        assert _leaks(json.dumps(body["step_results"])) == [] and _leaks(json.dumps(body["printer_reading"])) == []

    def test_a_bug_reports_log_tail_carries_no_address(self, wire, monkeypatch):
        server, sent = wire
        import kiln.log_config as log_config

        monkeypatch.setattr(log_config, "read_log_tail", lambda *a, **k: (
            "connecting to voron.local (192.168.1.40) via /dev/serial/by-id/usb-Klipper-if00 "
            "reading /Users/adamarreola/prints/x.gcode"
        ))
        monkeypatch.setattr(server, "_with_local_log_tail", server._with_local_log_tail)
        server._pro_api_call("report_issue", summary="it broke", context={})
        assert _leaks(sent[-1].decode()) == []


class _Honeypot:
    """A printer that answers every question with everything that identifies it."""

    name = "bambu"
    host = "10.0.0.5"
    serial = "01P00A000000001"
    access_code = "s3cr3t-access"

    def get_printer_config(self):
        return {
            "mcu": {"serial": "/dev/serial/by-id/usb-Klipper_stm32-if00"},
            "mcu toolhead": {"canbus_uuid": "61755fe321ac"},
            "gcode_macro NOTIFY": {"gcode": "RUN_SHELL_COMMAND CMD=curl https://hooks.example.com/?t=tok_live_9f8e7d"},
            "probe": {"z_offset": "1.8", "pin": "^PG15"},
            "stepper_z": {"endstop_pin": "probe:z_virtual_endstop", "position_max": "250"},
        }

    def get_multi_material_status(self):
        from kiln.multi_material import unknown_status

        return unknown_status("bambu:mqtt", "probe failed: HTTPConnectionPool(host='192.168.1.40') at voron.local")

    def get_state(self):
        raise OSError("no route to 10.0.0.5 (voron.local)")

    def get_job(self):
        raise OSError("no route to 10.0.0.5")


def test_every_fact_a_served_tool_may_ask_for_leaves_clean(wire, monkeypatch):
    """Every fact in served_steps' allow-list, read off a printer that leaks
    everything, through the door that sends it.  A fact added to the list
    is read here the day it is added."""
    import kiln.printer_nozzle_reading as reading
    from kiln import served_steps

    server, sent = wire
    honeypot = _Honeypot()
    monkeypatch.setattr(server, "_read_config_printers", lambda: {
        "a1": {"type": "bambu", "printer_model": "bambu_a1", "host": "10.0.0.5", "serial": "01P00A000000001",
               "access_code": "s3cr3t-access"},
        "voron": {"type": "moonraker", "printer_model": "voron_2", "host": "voron.local"},
    })
    monkeypatch.setattr(server, "_resolve_effective_printer_name", lambda name=None: "a1")
    monkeypatch.setattr(server, "_resolve_adapter", lambda name=None: honeypot)

    class _Registry:
        def get(self, _name):
            return honeypot

    monkeypatch.setattr(server, "_get_registry", lambda: _Registry())
    monkeypatch.setattr(reading, "observe_printer_nozzle", lambda name: {
        "machine": name, "fingerprint": "bambu:serial:01p00a000000001", "family": "bambu", "material": "brass",
        "nozzle_diameter_mm": 0.4, "read_from": "mqtt at 10.0.0.5", "read_at": "2026-10-17T10:00:00+00:00",
    })
    for extra in sorted(served_steps._FACTS_EXTRAS):
        done, refused = served_steps.read_first(
            "t", [{"kind": "printer_facts", "want": [extra], "colours_from": "colours"}],
            printer_name="a1", call={"colours": ["#C12E1F"]},
        )
        assert refused is None, extra
        server._pro_api_call("fleet_tool", step_results=done)
        assert _leaks(sent[-1].decode()) == [], (extra, sent[-1].decode())


#: The calls that put bytes on the network, by their dotted names.
_SENDS = {
    ("urllib", "request", "urlopen"), ("request", "urlopen"), ("urlopen",),
    *{("requests", m) for m in ("get", "post", "put", "patch", "delete", "request", "Session")},
    *{("httpx", m) for m in ("get", "post", "put", "patch", "delete", "request", "stream", "Client", "AsyncClient")},
    ("websockets", "connect"), ("websocket", "create_connection"),
}
#: How a module names Kiln's servers: the address, its constant, the
#: override, or one of the modules' own ``*api_base()`` helpers.
_KILN_API = ("api.kiln3d.com", "_HOSTED_KILN_API_URL", "KILN_API_URL", "api_base(")


def _dotted(node: ast.AST) -> tuple[str, ...]:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return tuple(reversed(parts))


def _sends_to_kiln(path: Path) -> bool:
    text = path.read_text(encoding="utf-8")
    if not any(marker in text for marker in _KILN_API):
        return False
    return any(
        isinstance(node, ast.Call) and _dotted(node.func) in _SENDS
        for node in ast.walk(ast.parse(text))
    )


def test_every_way_out_to_kilns_servers_is_named():
    package = Path(guard.__file__).resolve().parent
    found = {
        str(p.relative_to(package.parent)).replace("\\\\", "/")
        for p in package.rglob("*.py") if _sends_to_kiln(p)
    }
    unnamed = sorted(found - set(guard.SENDERS))
    stale = sorted(set(guard.SENDERS) - found)
    assert not unnamed, f"a new way out to Kiln's servers, not named in outbound_guard.SENDERS: {unnamed}"
    assert not stale, f"named in outbound_guard.SENDERS but no longer sends: {stale}"
    for path, (kind, why) in guard.SENDERS.items():
        assert kind in {"scrubbed", "own_schema", "person's_own"} and why.strip(), path
        if kind == "scrubbed":
            source = (package.parent / path).read_text(encoding="utf-8")
            assert "outbound_guard" in source and "scrub" in source, f"{path} is named scrubbed and never scrubs"
