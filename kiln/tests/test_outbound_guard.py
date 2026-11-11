"""What this computer reads off its own printers never reaches Kiln's servers raw.

Unguarded, a served power-loss check sends a Klipper printer's whole
configuration (the controller's serial port, CAN ids, macro bodies carrying
tokens), and a nozzle reading carries the printer's raw serial.  A reader's
promise never to send an address, a serial or a credential is nothing
without enforcement, and stand-in printers too clean to leak prove nothing.
These pin the guard (:mod:`kiln.outbound_guard`) at every way out:

* the rule itself, on a realistic leak;
* the served door: the bytes it actually sends, for an ask the person never
  made and for the install's own reads on one they did;
* a printer that leaks everything, read through every fact a served tool may
  ask for, with the bytes checked on their way out;
* a make that names no goal, told to Kiln's servers in words on an install
  without kiln-pro: the bytes carry the named words and nothing that locates
  or identifies anyone, and a signed-out install sends nothing;
* every module that sends to Kiln's servers, found in the source and named
  in :data:`kiln.outbound_guard.SENDERS` -- a new way out fails here until
  someone decides what it may send.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from kiln import outbound_guard as guard

SECRETS = (
    "01P00A000000001", "01p00a000000001", "s3cr3t-access", "10.0.0.5", "192.168.1.40", "voron.local",
    "/dev/serial/by-id/usb-Klipper", "61755fe321ac", "aa:bb:cc:dd:ee:ff", "hooks.example.com",
    "adamarreola", "jane@example.com", "tok_live_9f8e7d", "555-010-4477", "7946 0958",
)


def _leaks(text: str) -> list[str]:
    return [s for s in SECRETS if s in text]


class TestTheRule:
    def test_a_web_address_read_off_a_printer_is_dropped(self):
        out = guard.redact_text("webhook https://shop.duckdns.org/hook?id=7 failed at voron.local")
        assert "duckdns" not in out and "voron.local" not in out, out

    def test_secrets_and_places_go_identities_become_codes_words_stay(self):
        out = guard.scrub({
            "serial": "01P00A000000001", "access_code": "s3cr3t-access", "host": "10.0.0.5",
            "url": "https://hooks.example.com/x", "api_key": "tok_live_9f8e7d",
            "reading": {"fingerprint": "bambu:serial:01p00a000000001", "material": "brass", "nozzle_diameter_mm": 0.4},
            "error": "HTTPConnectionPool(host='192.168.1.40', port=7125) at voron.local on /dev/serial/by-id/usb-Klipper-if00",
            "note": "saved to /Users/adamarreola/prints/x.gcode by jane@example.com, "
                    "reach her on 555-010-4477 or +44 20 7946 0958",
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


class TestAMakeToldInWords:
    """An install without kiln-pro tells Kiln's servers about a make that
    names no goal (:mod:`kiln._pro_make_session_bridge`).  Nobody asked for
    that by name, so what leaves is checked here to the bytes."""

    #: Everything one such ask may carry: the door, the id the make is kept
    #: under, the call's naming arguments, and the made file's own name.
    ALLOWED = {
        "door", "version_id", "made_file",
        "name", "title", "description", "prompt", "idea", "notes",
        "design_id", "template_id", "product_type", "file_name",
    }

    @pytest.fixture
    def make(self, wire, monkeypatch, tmp_path):
        import sys

        from kiln import _pro_make_session_bridge as bridge
        from kiln import served_public, tool_context
        from kiln.front_door import join_goal

        for name in [n for n in sys.modules if n == "kiln_pro" or n.startswith("kiln_pro.")]:
            monkeypatch.setitem(sys.modules, name, None)
        monkeypatch.setitem(sys.modules, "kiln_pro", None)
        served_public._reset_for_tests()
        bridge._reset_for_tests()
        folder = tmp_path / "adamarreola" / "secret client work"
        folder.mkdir(parents=True)
        made = folder / "pendant_lamp_shade.stl"
        made.write_text("solid FILE-CONTENTS-MARKER\nendsolid\n")
        server, sent = wire

        def run(door: str, arguments: dict, envelope: dict | None = None) -> dict:
            envelope = envelope if envelope is not None else {"success": True, "local_path": str(made)}
            token = tool_context.enter_tool(door, arguments)
            try:
                join_goal("", envelope, str(made))
            finally:
                tool_context.leave_tool(token)
            return envelope

        yield run, sent, made
        served_public._reset_for_tests()
        bridge._reset_for_tests()

    def test_what_locates_or_identifies_anyone_never_leaves(self, make):
        run, sent, made = make
        run("generate_and_print", {
            "name": "/Users/adamarreola/Desktop/secret client work/lamp shade.stl",
            "description": (
                "a pendant lamp shade for the hall; the printer is voron.local at 192.168.1.40 on "
                "/dev/serial/by-id/usb-Klipper_rp2040, source in /Users/adamarreola/Documents/lamps/shade.scad"
            ),
            "prompt": (
                "lamp shade, 200 mm wide; questions to jane@example.com, 555-010-4477 or +44 20 7946 0958; "
                r"old copy at C:\Users\adamarreola\lamps\old.stl, token=tok_live_9f8e7d"
            ),
        })
        assert len(sent) == 1
        raw = sent[0].decode()
        assert _leaks(raw) == [], raw
        for folder in ("Desktop", "Documents", "secret client work", "lamps", str(made.parent)):
            assert folder not in raw, folder
        words = json.loads(raw)["made_with"]
        # The words themselves arrive: the object is still named.
        assert words["name"] == "lamp shade.stl"
        assert "a pendant lamp shade for the hall" in words["description"]
        assert "lamp shade, 200 mm wide" in words["prompt"]

    _PLANTED = {
        "name": "lamp for adamarreola at voron.local",
        "description": "printer 192.168.1.40 on /dev/serial/by-id/usb-Klipper_rp2040, mail jane@example.com",
        "prompt": "a pendant lamp shade; kept as /Users/adamarreola/lamps/shade.scad, ring 555-010-4477",
    }

    def test_the_served_door_scrubs_it_whatever_the_sender_did(self, make, monkeypatch):
        """Two layers, each proven alone.  With the sender's own cleaning
        off, the door still scrubs every value of an ask nobody made."""
        from kiln import _pro_make_session_bridge as bridge

        run, sent, _made = make
        monkeypatch.setattr(bridge, "_word", lambda value, limit=bridge.MAX_WORD_CHARS: value[:limit])
        run("generate_and_print", dict(self._PLANTED, name="a lamp at voron.local"))
        raw = sent[0].decode()
        assert _leaks(raw) == [], raw
        assert "a pendant lamp shade" in raw

    def test_the_sender_cleans_it_before_the_door_does(self, make, monkeypatch):
        """With the door's scrub off, what the sender hands over is already
        clean."""
        run, sent, _made = make
        server = __import__("kiln.server", fromlist=["x"])
        monkeypatch.setattr(server, "_guarded_for_the_wire", lambda kwargs, asked_by_user: kwargs)
        run("generate_and_print", dict(self._PLANTED, name="a lamp at voron.local"))
        raw = sent[0].decode()
        assert _leaks(raw) == [] and "lamps" not in raw, raw
        assert "a pendant lamp shade" in raw

    def test_a_network_share_and_a_country_coded_number_never_leave(self, make):
        run, sent, _made = make
        run("generate_and_print", {
            "description": r"a pendant lamp shade kept on \\fileserver01\designs\lamps\shade.stl, 200 x 300 x 1500 mm",
            "prompt": "lamp shade 120-80-4000; ring 1-555-010-4477, 1 555 010 9988 or 0044 20 7946 0958, "
                      "write jane+kiln@mail.example.co.uk, see file:///Users/adamarreola/lamps/shade.stl",
        })
        raw = sent[0].decode()
        for never in ("fileserver01", "designs", "lamps", "555", "7946", "example", "adamarreola", "file:"):
            assert never not in raw, (never, raw)
        words = json.loads(raw)["made_with"]
        # The object's own words and sizes arrive.
        assert "a pendant lamp shade kept on shade.stl, 200 x 300 x 1500 mm" == words["description"]
        assert words["prompt"].startswith("lamp shade 120-80-4000; ring [a phone number]")

    def test_only_the_made_files_own_name_is_sent(self, make):
        run, sent, made = make
        run("download_model", {"file_id": "f-1", "dest_dir": str(made.parent), "file_name": str(made)})
        body = json.loads(sent[0])
        assert body == {"verb": "join", "made_with": {
            "door": "download_model", "version_id": "pendant_lamp_shade", "file_name": "pendant_lamp_shade.stl",
        }}
        assert str(made.parent) not in sent[0].decode() and "/" not in json.dumps(body["made_with"])

    def test_only_the_named_arguments_go_and_each_is_capped(self, make):
        from kiln import _pro_make_session_bridge as bridge

        run, sent, made = make
        long_text = "a pendant lamp shade " * 400
        arguments = {key: f"{key} {long_text}" for key in bridge.WORD_ARGUMENTS}
        arguments.update({
            "scad_code": "module shade() { cube([10, 10, 10]); } shade();",
            "scad_path": str(made.parent / "shade.scad"),
            "file_path": str(made), "output_path": str(made), "dest_dir": str(made.parent),
            "api_key": "tok_live_9f8e7d", "access_code": "s3cr3t-access", "token": "tok_live_9f8e7d",
            "printer_name": "garage-voron", "printer_id": "garage-voron", "serial": "01P00A000000001",
            "material": "PETG-CF", "parameters": {"name": "hidden in a dict"}, "operations": [{"name": "hidden in a list"}],
            "requirements": "unlisted free text", "brief_id": "",
        })
        arguments["title"] = {"not": "a string"}
        arguments["notes"] = "module shade() { cylinder(h = 30, d = 60); } shade();"
        run("generate_and_print", arguments)
        raw = sent[0].decode()
        body = json.loads(raw)
        assert set(body) == {"verb", "made_with"}
        words = body["made_with"]
        assert set(words) <= self.ALLOWED and len(words) <= bridge.MAX_WORDS + 2
        assert "title" not in words and "notes" not in words
        assert all(isinstance(v, str) and len(v) <= bridge.MAX_WORD_CHARS for v in words.values())
        assert len(raw) < 8000
        for never in (
            "cube(", "cylinder(", "module ", "FILE-CONTENTS-MARKER", "tok_live_9f8e7d", "s3cr3t-access",
            "garage-voron", "01P00A000000001", "PETG-CF", "hidden in a", "unlisted free text", str(made.parent),
        ):
            assert never not in raw, never

    def test_the_number_of_texts_is_capped_however_long_the_list_grows(self, make, monkeypatch):
        from kiln import _pro_make_session_bridge as bridge

        run, sent, _made = make
        many = tuple(f"word_{n}" for n in range(40))
        monkeypatch.setattr(bridge, "WORD_ARGUMENTS", many)
        run("generate_and_print", {key: f"a lamp shade {key}" for key in many})
        words = json.loads(sent[0])["made_with"]
        texts = {k: v for k, v in words.items() if k not in ("door", "version_id")}
        assert len(texts) == bridge.MAX_WORDS, sorted(texts)

    def test_the_named_arguments_are_the_ones_the_sender_reads(self):
        from kiln import _pro_make_session_bridge as bridge

        assert set(bridge.WORD_ARGUMENTS) | {"door", "version_id", "made_file"} == self.ALLOWED

    def test_a_signed_out_install_sends_nothing_at_all(self, make, monkeypatch):
        from kiln import auth_session

        run, sent, _made = make

        class _NoBearer:
            token = ""
            state = "signed_out"
            detail = ""

        monkeypatch.setattr(auth_session, "resolve_api_bearer", lambda: _NoBearer())
        first = run("generate_and_print", {"prompt": "a pendant lamp shade", "name": "lamp"})
        second = run("generate_and_print", {"prompt": "a pendant lamp shade", "name": "lamp"})
        assert sent == [], "a signed-out make reached the network"
        assert first["saved_goal"]["why"] == "signed_out" and "saved_goal" not in second


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
            "extruder": {"heater_pin": "PA1", "serial": "/dev/ttyACM0"},
            "verify_heater extruder": {"max_error": "120", "note": "token tok_live_9f8e7d at 10.0.0.5"},
            "stepper_z": {"endstop_pin": "probe:z_virtual_endstop", "position_max": "250"},
        }

    def get_multi_material_status(self):
        from kiln.multi_material import unknown_status

        return unknown_status("bambu:mqtt", "probe failed: HTTPConnectionPool(host='192.168.1.40') at voron.local")

    def reported_firmware_version(self):
        return "v0.12.0 on voron.local (10.0.0.5)"

    def get_ams_status(self):
        return {
            "tray_now": "254",
            "units": [{
                "unit_id": 0, "name": "A at voron.local", "module_name": "n3f/0 sn 01P00A000000001",
                "sn": "01P00A000000001", "humidity_raw": 41, "host": "10.0.0.5",
                "dry_setting": {"dry_filament": "PLA via 10.0.0.5", "dry_temperature": 55},
                "trays": [{"slot": 0, "tray_id": 0, "name": "A1", "tray_type": "PLA",
                           "tag_uid": "01P00A000000001", "tray_uuid": "s3cr3t-access"}],
            }],
        }

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
