"""What the Bridge sends back never says where a printer is or how to
unlock it, whichever tool answered and whatever it put in its reply.

Driven through ``handle_relay_request``: its return value is the frame the
Bridge writes to the socket.
"""
import json
import textwrap
from unittest.mock import MagicMock, PropertyMock, patch

import pytest

from kiln import printer_model_resolver as pmr
from kiln.bridge_client import handle_relay_request
from kiln.printers.base import PrinterError
from kiln.printers.octoprint import OctoPrintAdapter
from kiln.server import printer_status

ADDRESS, HOSTNAME = "192.168.77.41", "octopi.workshop.example.com"
ACCESS_CODE, API_KEY = "83527164", "KEY-9f3c1e77a2b84d06"
SERIAL, MAINBOARD = "03919C4612345678", "MB-7f41c0de99"
SECRETS = (ADDRESS, HOSTNAME, ACCESS_CODE, API_KEY, SERIAL, MAINBOARD)


@pytest.fixture(autouse=True)
def saved_printers(tmp_path, monkeypatch):
    from kiln.cli import config as cli_config

    cfg = tmp_path / "config.yaml"
    cfg.write_text(textwrap.dedent(f"""
        active_printer: garage-a1
        printers:
          garage-a1:
            type: bambu
            host: {ADDRESS}
            access_code: "{ACCESS_CODE}"
            serial: {SERIAL}
            printer_model: bambu_a1
          office-mk4:
            type: octoprint
            host: https://{HOSTNAME}:8443
            api_key: {API_KEY}
            mainboard_id: {MAINBOARD}
            printer_model: prusa_mk4
    """))
    cfg.chmod(0o600)
    monkeypatch.setattr(pmr, "_CONFIG_PATH", cfg)
    monkeypatch.setattr(cli_config, "get_config_path", lambda: cfg)
    pmr.invalidate_cache()


def _relay(tool: str, answer, args: dict | None = None) -> dict:
    def call_tool(name: str, a: dict):
        if isinstance(answer, Exception):
            raise answer
        return answer(**a) if callable(answer) else answer

    return handle_relay_request(
        {"request_id": "r1", "tool_name": tool, "args": args or {}},
        call_tool=call_tool, fetch_artifact=lambda token: "",
    )


def _clean(frame: dict) -> str:
    wire = json.dumps(frame)
    for secret in SECRETS:
        assert secret not in wire, secret
    return wire


class TestAPrinterThatDoesNotAnswer:
    def test_the_adapters_own_error_text_leaves_without_the_address(
        self, mock_job_progress, mock_capabilities
    ):
        adapter = MagicMock(spec=OctoPrintAdapter)
        adapter.get_state.side_effect = PrinterError(
            f"Request error for GET /api/printer: timed out. Cannot reach OctoPrint at http://{ADDRESS}. "
            "Check network and that OctoPrint is running."
        )
        adapter.get_job.return_value = mock_job_progress
        type(adapter).capabilities = PropertyMock(return_value=mock_capabilities)
        with patch("kiln.server._get_adapter", return_value=adapter):
            frame = _relay("printer_status", printer_status, {"detail": "lite"})

        assert frame["ok"] is True and frame["result"]["printer"]["connected"] is False
        wire = _clean(frame)
        # The sentence is still there to read; only the place is gone.
        assert "Cannot reach OctoPrint at" in wire and "Check network" in wire


class TestWhateverAToolPutsInItsReply:
    def test_this_computers_own_printer_values_are_taken_out_at_any_depth(self):
        answer = {
            "success": True,
            "trusted_printers": [ADDRESS, HOSTNAME],
            "printer": {"host": f"http://{ADDRESS}:7125", "serial": SERIAL, "access_code": ACCESS_CODE},
            "log": [f"subscribed device/{SERIAL}/report", f"GET https://{HOSTNAME}:8443/api/job X-Api-Key: {API_KEY}"],
            "note": f"mainboard {MAINBOARD} answered",
        }

        _clean(_relay("list_trusted_printers", answer))

    def test_an_address_this_computer_never_saved_is_taken_out_of_text_too(self):
        frame = _relay("printer_status", {
            "success": True,
            "remedy": "Could not reach 10.9.8.7:7125, tried ender.local and fe80:0:0:0:1c2d:3eff:fe4f:5a6b, "
                      "adapter aa:bb:cc:dd:ee:ff on /dev/ttyUSB0",
        })

        wire = json.dumps(frame)
        for place in ("10.9.8.7", "ender.local", "fe80:0:0:0:1c2d:3eff:fe4f:5a6b", "aa:bb:cc:dd:ee:ff", "/dev/ttyUSB0"):
            assert place not in wire, place

    def test_a_tool_that_raises_does_not_leak_through_the_error(self):
        frame = _relay("printer_status", RuntimeError(f"401 from https://{HOSTNAME}:8443 with key {API_KEY}"))

        assert frame["ok"] is False
        assert "401 from" in _clean(frame)


class TestWhatAReplyIsForStillArrives:
    def test_names_numbers_kilns_links_and_handles_are_untouched(self):
        answer = {
            "success": True,
            "printer_name": "garage-a1",
            "printer": {"state": "printing", "tool_temp_actual": 215.2, "connected": True},
            "known_printers": [{"name": "garage-a1", "printer_model": "bambu_a1"}],
            "upgrade_url": "https://kiln3d.com/pricing?from=fleet_status",
            "monitor_note": "Watch it at https://app.kiln3d.com/monitor",
            "artifact_token": "art_5f2c9d8e7b6a41f0a3c2",
            "fingerprint": "9b1d6f0c4e2a8b7d5c3f1e0a9b8c7d6e5f4a3b2c1d0e9f8a7b6c5d4e3f2a1b0c",
            "model_url": "https://makerworld.com/en/models/12345",
            "file": "/Users/someone/.kiln/prints/benchy.gcode",
            "count": 2,
            "ok": None,
        }

        assert _relay("printer_status", answer)["result"] == answer

    def test_a_camera_frame_is_passed_through_byte_for_byte(self):
        # A frame is megabytes of base64 with no spaces; short values such as
        # an access code can occur in it by chance and must not be cut out.
        frame_b64 = ("QUJD" * 600) + ACCESS_CODE + ("REVG" * 600)
        bare = _relay("printer_snapshot", frame_b64)
        keyed = _relay("printer_snapshot", {"success": True, "image_base64": frame_b64})

        assert bare["result"] == frame_b64
        assert keyed["result"]["image_base64"] == frame_b64

    def test_a_reply_that_cannot_be_read_is_still_sent(self):
        class Odd:
            pass

        odd = Odd()
        assert _relay("printer_status", {"success": True, "thing": odd})["result"]["thing"] is odd


class TestTheWaysRoundTheScrub:
    def test_a_long_run_with_no_spaces_is_only_exempt_when_it_is_file_bytes(self):
        # Long and whitespace-free, but plainly not a camera frame.
        link = f"https://{HOSTNAME}:8443/api/files?apikey={API_KEY}&path=" + "segment/" * 400
        chain = ";".join([f"device/{SERIAL}/report"] * 200)

        _clean(_relay("printer_status", {"success": True, "link": link, "topics": chain}))

    def test_a_serial_inside_file_bytes_is_still_taken_out(self):
        blob = ("QUJD" * 600) + SERIAL + ("REVG" * 600)

        assert SERIAL not in json.dumps(_relay("printer_snapshot", blob))

    def test_a_key_that_is_an_address_does_not_leave(self):
        answer = {"success": True, "by_host": {ADDRESS: {"trusted": True}, HOSTNAME: {"trusted": True}}}

        frame = _relay("printer_status", answer)

        _clean(frame)
        assert len(frame["result"]["by_host"]) == 2

    def test_a_code_sent_as_a_number_does_not_leave(self):
        frame = _relay("printer_status", {"success": True, "code": int(ACCESS_CODE), "layer": 42, "ok": True})

        _clean(frame)
        assert frame["result"]["layer"] == 42 and frame["result"]["ok"] is True

    def test_a_camera_address_held_in_the_environment_does_not_leave(self, monkeypatch):
        monkeypatch.setenv("KILN_PRINTER_CAMERA_SNAPSHOT_URL", "http://cam.workshop.example.org:8080/snap")
        monkeypatch.setenv("KILN_PRINTER_CAMERA_STREAM_URL", "rtsp://203.0.113.77:554/live")

        wire = json.dumps(_relay("printer_status", {
            "success": True,
            "note": "snapshot from cam.workshop.example.org failed; stream at 203.0.113.77 answered",
        }))

        assert "cam.workshop.example.org" not in wire and "203.0.113.77" not in wire
