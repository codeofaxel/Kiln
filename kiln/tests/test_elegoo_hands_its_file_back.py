"""An Elegoo SDCP printer hands a print file back to Kiln.

The printer is faked at its two real transports: the SDCP WebSocket (the
adapter's socket object answers each command the way a Centauri Carbon
does, attributes arriving as their own message beside the answer) and its
HTTP file server (a real server on this machine, serving
``GET /downloadFile/local/<name>`` from the files it holds and taking
``POST /uploadFile/upload``).  Files are listed in the Centauri Carbon's
shape: a full path, ``FileSize``, ``TotalLayers``, ``CreateTime``.
"""

from __future__ import annotations

import contextlib
import hashlib
import http.server
import json
import os
import socket
import tempfile
import threading
import time
from collections.abc import Iterator
from typing import Any
from unittest import mock
from urllib.parse import unquote

import pytest

from kiln.printers import read_back
from kiln.printers.base import PrinterError
from kiln.printers.elegoo import _CMD_GET_ATTRIBUTES, _CMD_LIST_FILES, _CMD_START_PRINT, ElegooAdapter

MAINBOARD_ID = "000000000001d354"
STAMP = 1_752_335_626

#: A complete, homed, small G-code file the print gate can vouch for.
GCODE = (
    b"; generated for a test\n"
    b"M140 S60\nM104 S210\nM190 S60\nM109 S210\nG28\nG90\nG92 E0\n"
    b"G1 Z0.3 F600\nG1 X50 Y50 F3000\nG1 X60 Y50 E1 F1200\nG1 X60 Y60 E2\n"
    b"G1 X50 Y60 E3\nG1 X50 Y50 E4\nG1 Z10 F600\nM104 S0\nM140 S0\nM84\n"
)


class _FakeFiles:
    """What the printer's HTTP file server holds and was asked."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.requests: list[tuple[str, str, dict[str, str]]] = []
        self.web_page_for_unknown = False
        self.chunked = False


def _handler(store: _FakeFiles) -> type[http.server.BaseHTTPRequestHandler]:
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def handle(self) -> None:
            # A client that stops reading part-way (a refused download) is
            # how a printer's connection ends too, not a server fault.
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                super().handle()

        def do_GET(self) -> None:  # noqa: N802
            store.requests.append(("GET", self.path, dict(self.headers)))
            prefix = "/downloadFile"
            path = unquote(self.path[len(prefix):]) if self.path.startswith(prefix) else ""
            data = store.files.get(path)
            if data is None:
                if store.web_page_for_unknown:
                    page = b"<!DOCTYPE html><html><body>printer</body></html>"
                    self._send(200, page, "text/html")
                    return
                self._send(404, b"Not Found", "text/plain")
                return
            if store.chunked:
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                for i in range(0, len(data), 1000):
                    piece = data[i:i + 1000]
                    self.wfile.write(f"{len(piece):x}\r\n".encode() + piece + b"\r\n")
                self.wfile.write(b"0\r\n\r\n")
                return
            self._send(200, data, "application/octet-stream")

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(length)
            store.requests.append(("POST", self.path, dict(self.headers)))
            body = json.dumps({"code": "000000", "messages": None, "data": {}, "success": True}).encode()
            self._send(200, body, "application/json")

        def _send(self, status: int, body: bytes, kind: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


class FakeCentauriSocket:
    """The SDCP WebSocket of a Centauri Carbon, at the socket object the
    adapter sends on.  Each command is answered at once; Cmd 1's attributes
    arrive as their own message before the answer, as the printer sends
    them."""

    def __init__(self, adapter: ElegooAdapter, machine_name: str = "Centauri Carbon") -> None:
        self.adapter = adapter
        self.machine_name = machine_name
        self.listing: list[dict[str, Any]] = []
        self.list_ack = 0
        self.answers_list = True
        self.sent: list[dict[str, Any]] = []

    def hold(self, name: str, data: bytes, **fields: Any) -> None:
        entry: dict[str, Any] = {"name": f"/local/{name}", "type": 1}
        entry.update({"FileSize": len(data), "TotalLayers": 12, "CreateTime": STAMP})
        entry.update(fields)
        self.listing = [e for e in self.listing if e["name"] != entry["name"]] + [entry]

    def commands(self, cmd: int) -> list[dict[str, Any]]:
        return [m["Data"] for m in self.sent if m["Data"]["Cmd"] == cmd]

    def _answer(self, cmd: int, request_id: str, data: dict[str, Any]) -> None:
        self.adapter._handle_message({
            "Id": "",
            "Data": {
                "Cmd": cmd, "Data": data, "RequestID": request_id,
                "MainboardID": MAINBOARD_ID, "TimeStamp": STAMP,
            },
            "Topic": f"sdcp/response/{MAINBOARD_ID}",
        })

    def send(self, raw: str) -> None:
        msg = json.loads(raw)
        self.sent.append(msg)
        cmd = msg["Data"]["Cmd"]
        rid = msg["Data"]["RequestID"]
        if cmd == _CMD_GET_ATTRIBUTES:
            self.adapter._handle_message({
                "Attributes": {"Name": "Workshop", "MachineName": self.machine_name},
                "MainboardID": MAINBOARD_ID,
                "TimeStamp": STAMP,
                "Topic": f"sdcp/attributes/{MAINBOARD_ID}",
            })
            self._answer(cmd, rid, {"Ack": 0})
        elif cmd == _CMD_LIST_FILES:
            if self.answers_list:
                self._answer(cmd, rid, {"Ack": self.list_ack, "FileList": list(self.listing)})
        else:
            self._answer(cmd, rid, {"Ack": 0})

    def recv(self) -> str:
        return ""

    def settimeout(self, value: float) -> None:
        return

    def close(self) -> None:
        return


@pytest.fixture
def files() -> Iterator[tuple[_FakeFiles, int]]:
    store = _FakeFiles()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _handler(store))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield store, server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


def _printer(port: int, machine_name: str = "Centauri Carbon") -> tuple[ElegooAdapter, FakeCentauriSocket]:
    adapter = ElegooAdapter(host="127.0.0.1", mainboard_id=MAINBOARD_ID, timeout=2)
    adapter._SDCP_DOWNLOAD_PORT = port
    adapter._SDCP_V3_UPLOAD_PORT = port
    ws = FakeCentauriSocket(adapter, machine_name)
    adapter._ws = ws
    adapter._connected = True
    return adapter, ws


def _gets(store: _FakeFiles) -> list[str]:
    return [path for method, path, _ in store.requests if method == "GET"]


# ---------------------------------------------------------------------------
# read_print_file
# ---------------------------------------------------------------------------


class TestTheFileComesBack:
    def test_a_bare_name_comes_back_as_the_printers_bytes(self, files: tuple[_FakeFiles, int]) -> None:
        store, port = files
        store.files["/local/cube.gcode"] = GCODE
        adapter, _ = _printer(port)

        assert adapter.read_print_file("cube.gcode") == GCODE
        assert _gets(store) == ["/downloadFile/local/cube.gcode"]

    def test_a_name_inside_a_folder_reads_the_file_start_print_prints(
        self, files: tuple[_FakeFiles, int],
    ) -> None:
        store, port = files
        store.files["/local/cube.gcode"] = GCODE
        adapter, ws = _printer(port)

        with mock.patch("kiln.printers.print_gate.run_adapter_gate", return_value=None):
            adapter.start_print("parts/brackets/cube.gcode")
        started = ws.commands(_CMD_START_PRINT)[0]["Data"]["Filename"]

        assert adapter.read_print_file("parts/brackets/cube.gcode") == GCODE
        assert _gets(store) == [f"/downloadFile/local/{started}"]

    def test_a_leading_slash_and_another_storage_name_read_onboard_storage(
        self, files: tuple[_FakeFiles, int],
    ) -> None:
        store, port = files
        store.files["/local/cube.gcode"] = GCODE
        store.files["/usb/cube.gcode"] = b"; a different file on the USB stick\nG28\n"
        adapter, _ = _printer(port)

        # start_print sends the bare name, which the printer prints from
        # its onboard storage, so that is the copy read back.
        assert adapter.read_print_file("/local/cube.gcode") == GCODE
        assert adapter.read_print_file("/usb/cube.gcode") == GCODE
        assert _gets(store) == ["/downloadFile/local/cube.gcode"] * 2

    def test_a_name_with_a_space_is_sent_encoded(self, files: tuple[_FakeFiles, int]) -> None:
        store, port = files
        store.files["/local/my part.gcode"] = GCODE
        adapter, _ = _printer(port)

        assert adapter.read_print_file("my part.gcode") == GCODE
        assert _gets(store) == ["/downloadFile/local/my%20part.gcode"]

    def test_a_download_sent_in_pieces_comes_back_whole(self, files: tuple[_FakeFiles, int]) -> None:
        store, port = files
        store.chunked = True
        big = GCODE * 200
        store.files["/local/cube.gcode"] = big
        adapter, _ = _printer(port)

        assert adapter.read_print_file("cube.gcode") == big

    def test_reading_changes_nothing_on_the_printer(self, files: tuple[_FakeFiles, int]) -> None:
        store, port = files
        store.files["/local/cube.gcode"] = GCODE
        adapter, ws = _printer(port)

        adapter.read_print_file("cube.gcode")

        assert [method for method, _, _ in store.requests] == ["GET"]
        assert {m["Data"]["Cmd"] for m in ws.sent} <= {_CMD_GET_ATTRIBUTES}


class TestTheFileDoesNotComeBack:
    def test_a_missing_file_raises_read_failed(self, files: tuple[_FakeFiles, int]) -> None:
        store, port = files
        adapter, _ = _printer(port)

        with pytest.raises(PrinterError) as caught:
            adapter.read_print_file("gone.gcode")

        assert caught.value.code == "PRINT_FILE_READ_FAILED"
        assert "no file by that name" in str(caught.value)
        assert "404" not in str(caught.value)
        assert _gets(store) == ["/downloadFile/local/gone.gcode"]

    def test_a_web_page_in_place_of_the_file_is_not_the_file(self, files: tuple[_FakeFiles, int]) -> None:
        store, port = files
        store.web_page_for_unknown = True
        adapter, _ = _printer(port)

        with pytest.raises(PrinterError) as caught:
            adapter.read_print_file("gone.gcode")

        assert caught.value.code == "PRINT_FILE_READ_FAILED"

    def test_a_printer_that_does_not_answer_raises_read_failed(self) -> None:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            closed_port = probe.getsockname()[1]
        adapter, _ = _printer(closed_port)

        with pytest.raises(PrinterError) as caught:
            adapter.read_print_file("cube.gcode")

        assert caught.value.code == "PRINT_FILE_READ_FAILED"

    def test_a_listed_file_the_download_does_not_serve_is_not_handed_back(
        self, files: tuple[_FakeFiles, int],
    ) -> None:
        # The file list holds the file, but the download answers it has none:
        # the printer serves no download of it, which is no failed read.
        store, port = files
        adapter, ws = _printer(port)
        ws.listing = [{"name": "/local/kept.gcode", "FileSize": 12, "CreateTime": 1_764_000_000, "type": 1}]

        assert adapter.read_print_file("kept.gcode") is None
        assert _gets(store) == ["/downloadFile/local/kept.gcode"]

    def test_a_printer_whose_model_is_unknown_is_not_asked_for_a_download(
        self, files: tuple[_FakeFiles, int],
    ) -> None:
        store, port = files
        store.files["/local/cube.gcode"] = GCODE
        adapter, _ = _printer(port, machine_name="")

        assert adapter.read_print_file("cube.gcode") is None
        assert _gets(store) == []

    def test_another_sdcp_model_cannot_hand_files_back(self, files: tuple[_FakeFiles, int]) -> None:
        store, port = files
        store.files["/local/model.ctb"] = b"\x12\xfd\x00\x01resin"
        adapter, ws = _printer(port, machine_name="Saturn 4 Ultra")

        assert adapter.read_print_file("model.ctb") is None
        assert store.requests == []
        assert ws.commands(_CMD_GET_ATTRIBUTES)

    def test_the_declared_model_decides_without_asking_the_printer(
        self, files: tuple[_FakeFiles, int],
    ) -> None:
        store, port = files
        store.files["/local/model.ctb"] = b"resin"
        adapter, ws = _printer(port)
        adapter._printer_model = "elegoo_saturn_4_ultra"

        assert adapter.read_print_file("model.ctb") is None
        assert store.requests == []
        assert ws.sent == []

        # The same printer declared as a Centauri Carbon hands it back.
        adapter._printer_model = "elegoo_centauri_carbon"
        assert adapter.read_print_file("model.ctb") == b"resin"

    def test_a_declared_centauri_carbon_reads_its_file(self, files: tuple[_FakeFiles, int]) -> None:
        store, port = files
        store.files["/local/cube.gcode"] = GCODE
        adapter, ws = _printer(port, machine_name="")
        adapter._printer_model = "elegoo_centauri_carbon"

        assert adapter.read_print_file("cube.gcode") == GCODE
        assert ws.sent == []

    def test_a_file_past_the_ceiling_is_refused(
        self, files: tuple[_FakeFiles, int], monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        store, port = files
        store.files["/local/cube.gcode"] = GCODE * 20
        monkeypatch.setattr(read_back, "READ_BACK_CAP_BYTES", 1024)
        adapter, _ = _printer(port)

        with pytest.raises(PrinterError) as caught:
            adapter.read_print_file("cube.gcode")

        assert caught.value.code == "PRINT_FILE_TOO_LARGE"

    def test_a_file_past_the_ceiling_sent_in_pieces_is_refused(
        self, files: tuple[_FakeFiles, int], monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        store, port = files
        store.chunked = True
        store.files["/local/cube.gcode"] = GCODE * 20
        monkeypatch.setattr(read_back, "READ_BACK_CAP_BYTES", 1024)
        adapter, _ = _printer(port)

        with pytest.raises(PrinterError) as caught:
            adapter.read_print_file("cube.gcode")

        assert caught.value.code == "PRINT_FILE_TOO_LARGE"


class TestCredentials:
    def test_the_download_sends_what_the_upload_sends_and_no_credential(
        self, files: tuple[_FakeFiles, int],
    ) -> None:
        store, port = files
        adapter, _ = _printer(port)
        with tempfile.NamedTemporaryFile(suffix=".gcode", delete=False) as fh:
            fh.write(GCODE)
            local = fh.name
        try:
            name = os.path.basename(local)
            result = adapter._upload_file_v3(local, name, len(GCODE), hashlib.md5(GCODE).hexdigest())  # noqa: S324
            assert result is not None and result.success
        finally:
            os.unlink(local)
        store.files[f"/local/{os.path.basename(local)}"] = GCODE

        assert adapter.read_print_file(os.path.basename(local)) == GCODE

        sent = {method: {k.lower() for k in headers} for method, _, headers in store.requests}
        assert set(sent) == {"POST", "GET"}
        for method in ("POST", "GET"):
            assert not sent[method] & {"authorization", "cookie", "x-token", "proxy-authorization"}
            assert "host" in sent[method]


# ---------------------------------------------------------------------------
# print_file_identity
# ---------------------------------------------------------------------------


class TestIdentity:
    def test_both_parts_from_the_file_list(self, files: tuple[_FakeFiles, int]) -> None:
        _, port = files
        adapter, ws = _printer(port)
        ws.hold("cube.gcode", GCODE)

        assert adapter.print_file_identity("cube.gcode") == (len(GCODE), STAMP)
        assert ws.commands(_CMD_LIST_FILES)[0]["Data"] == {"Url": "/local"}

    def test_the_file_start_print_would_print_is_the_one_reported(
        self, files: tuple[_FakeFiles, int],
    ) -> None:
        _, port = files
        adapter, ws = _printer(port)
        ws.hold("cube.gcode", GCODE)
        ws.hold("other.gcode", b"G28\n" * 3, CreateTime=STAMP + 99)

        assert adapter.print_file_identity("/usb/parts/cube.gcode") == (len(GCODE), STAMP)

    def test_a_time_in_milliseconds_reads_as_seconds(self, files: tuple[_FakeFiles, int]) -> None:
        _, port = files
        adapter, ws = _printer(port)
        ws.hold("cube.gcode", GCODE, CreateTime=STAMP * 1000)

        assert adapter.print_file_identity("cube.gcode") == (len(GCODE), STAMP)

    def test_only_the_size(self, files: tuple[_FakeFiles, int]) -> None:
        _, port = files
        adapter, ws = _printer(port)
        ws.listing = [{"name": "/local/cube.gcode", "FileSize": 4321, "type": 1}]

        assert adapter.print_file_identity("cube.gcode") == (4321, None)

    def test_a_list_with_neither_reports_nothing(self, files: tuple[_FakeFiles, int]) -> None:
        _, port = files
        adapter, ws = _printer(port, machine_name="Saturn 4 Ultra")
        # A resin SDCP printer lists a file's path and type only; the
        # storage's own sizes are not the file's.
        ws.listing = [{"name": "/local/model.ctb", "usedSize": 1, "totalSize": 9, "type": 1}]
        ws.hold("cube.gcode", GCODE)

        assert adapter.print_file_identity("model.ctb") is None
        assert adapter.print_file_identity("cube.gcode") == (len(GCODE), STAMP)

    def test_a_file_not_listed_reports_nothing(self, files: tuple[_FakeFiles, int]) -> None:
        _, port = files
        adapter, ws = _printer(port)
        ws.hold("other.gcode", GCODE)
        ws.listing.append({"name": "/local/parts", "type": 0, "FileSize": 9, "CreateTime": STAMP})

        assert adapter.print_file_identity("cube.gcode") is None
        assert adapter.print_file_identity("parts") is None
        assert adapter.print_file_identity("other.gcode") == (len(GCODE), STAMP)

    def test_a_refused_list_reports_nothing_and_does_not_raise(
        self, files: tuple[_FakeFiles, int],
    ) -> None:
        _, port = files
        adapter, ws = _printer(port)
        ws.hold("cube.gcode", GCODE)
        ws.list_ack = 1

        assert adapter.print_file_identity("cube.gcode") is None
        ws.list_ack = 0
        assert adapter.print_file_identity("cube.gcode") == (len(GCODE), STAMP)

    def test_an_unanswered_list_reports_nothing_and_does_not_raise(
        self, files: tuple[_FakeFiles, int],
    ) -> None:
        _, port = files
        adapter, ws = _printer(port)
        adapter._timeout = 0.2
        ws.hold("cube.gcode", GCODE)
        ws.answers_list = False

        started = time.monotonic()
        assert adapter.print_file_identity("cube.gcode") is None
        assert time.monotonic() - started < 2
        assert ws.commands(_CMD_LIST_FILES)
        ws.answers_list = True
        assert adapter.print_file_identity("cube.gcode") == (len(GCODE), STAMP)

    def test_an_unreachable_printer_reports_nothing(self, files: tuple[_FakeFiles, int]) -> None:
        _, port = files
        adapter, ws = _printer(port)
        ws.hold("cube.gcode", GCODE)
        with mock.patch.object(adapter, "_ensure_ws", side_effect=PrinterError("unreachable")):
            assert adapter.print_file_identity("cube.gcode") is None
        assert adapter.print_file_identity("cube.gcode") == (len(GCODE), STAMP)


# ---------------------------------------------------------------------------
# A start by name is checked against the printer's own bytes
# ---------------------------------------------------------------------------


class TestAStartByName:
    def test_the_printers_copy_is_read_before_the_start(self, files: tuple[_FakeFiles, int]) -> None:
        store, port = files
        store.files["/local/cube.gcode"] = GCODE
        adapter, ws = _printer(port)

        result = adapter.start_print("cube.gcode")

        assert result.success, result.message
        assert "/downloadFile/local/cube.gcode" in _gets(store)
        assert ws.commands(_CMD_START_PRINT)[0]["Data"]["Filename"] == "cube.gcode"

    def test_a_file_the_printer_does_not_hold_is_not_started(self, files: tuple[_FakeFiles, int]) -> None:
        store, port = files
        adapter, ws = _printer(port)

        result = adapter.start_print("gone.gcode")

        assert not result.success
        assert "/downloadFile/local/gone.gcode" in _gets(store)
        assert ws.commands(_CMD_START_PRINT) == []
