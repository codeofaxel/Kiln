"""A Duet board hands its own copy of a print file back to Kiln.

The fake below is a RepRapFirmware board's HTTP interface, served to the
adapter's own :class:`requests.Session` through a mounted transport, so
every request is the real prepared one: its query string, its session-key
header, its timeout, and a streamed body.  It answers the way the board's
documented requests do:

* ``rr_connect`` checks the password and hands out a session key; any other
  request without that key is answered 401.
* ``rr_download?name=`` opens the name from the volume root and sends the
  file (``text/plain`` for G-code), or 404 when there is none.
* ``rr_fileinfo?name=`` opens the name inside the G-code folder and answers
  ``err``, ``size`` and ``lastModified`` (left out when the board's clock
  had no date), or ``{"err":1}``.
* ``M32 "name"`` through ``rr_gcode`` opens the name inside the G-code
  folder.

A name is absolute when it starts with ``/`` or a single-digit volume
(``0:``); any other name is taken inside the folder the request names.
"""

from __future__ import annotations

import io
import json
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import pytest
import requests
from requests.adapters import BaseAdapter
from requests.structures import CaseInsensitiveDict

from kiln.printers import read_back
from kiln.printers.base import PrinterError
from kiln.printers.duet import DuetAdapter

HOST = "http://duet.local"
PASSWORD = "s3cret-pw"
GCODE_DIR = "0:/gcodes"
VOLUME_ROOT = "0:"


def _absolute(name: str) -> bool:
    return name.startswith("/") or (len(name) > 1 and name[0].isdigit() and name[1] == ":")


def _resolve(directory: str, name: str) -> str:
    """The file a board opens for *name* asked inside *directory*."""
    path = name if _absolute(name) else f"{directory.rstrip('/')}/{name}"
    return f"0:{path}" if path.startswith("/") else path


class _CountingBody(io.BytesIO):
    """A response body that remembers how many bytes were read from it."""

    def __init__(self, data: bytes) -> None:
        super().__init__(data)
        self.taken = 0

    def read(self, size: int | None = -1) -> bytes:  # type: ignore[override]
        chunk = super().read(size)
        self.taken += len(chunk)
        return chunk


class _FakeBoard(BaseAdapter):
    """A RepRapFirmware board's HTTP answers (see the module docstring)."""

    def __init__(self, files: dict[str, bytes], dates: dict[str, str] | None = None) -> None:
        super().__init__()
        self.files = dict(files)
        self.dates = dict(dates or {})
        self.session_key = 4242
        self.sent: list[dict[str, Any]] = []
        self.printed: list[str] = []
        self.bodies: list[_CountingBody] = []
        self.status = "idle"
        self.down = False
        self.announce_length = True
        self.expire_next_session = False
        self.emulated_error_on_download = False

    # -- what the requests were ---------------------------------------
    def endpoints(self) -> list[str]:
        return [s["endpoint"] for s in self.sent]

    def asked(self, endpoint: str) -> list[dict[str, Any]]:
        return [s for s in self.sent if s["endpoint"] == endpoint]

    # -- the transport ------------------------------------------------
    def send(self, request: requests.PreparedRequest, stream: bool = False, timeout: Any = None, **_: Any) -> requests.Response:
        url = urlsplit(request.url or "")
        endpoint = url.path.lstrip("/")
        query = dict(parse_qsl(url.query, keep_blank_values=True))
        self.sent.append(
            {"endpoint": endpoint, "query": query, "headers": dict(request.headers), "stream": stream, "timeout": timeout}
        )
        if self.down:
            raise requests.exceptions.ConnectionError("no route to host")
        if endpoint == "rr_connect":
            if query.get("password") != PASSWORD:
                return self._json(request, {"err": 1})
            self.session_key += 1
            return self._json(request, {"err": 0, "sessionTimeout": 8000, "boardType": "duet3mb6hc101", "sessionKey": self.session_key})
        if request.headers.get("X-Session-Key") != str(self.session_key) or self.expire_next_session:
            self.expire_next_session = False
            return self._answer(request, 401, b"", "text/plain")
        if endpoint == "rr_download":
            return self._download(request, query.get("name", ""))
        if endpoint == "rr_fileinfo":
            return self._fileinfo(request, query.get("name", ""))
        if endpoint == "rr_gcode":
            gcode = query.get("gcode", "")
            if gcode.startswith("M32 "):
                self.printed.append(_resolve(GCODE_DIR, gcode[4:].strip().strip('"')))
            return self._json(request, {"buff": 255})
        if endpoint == "rr_reply":
            return self._answer(request, 200, b"", "text/plain")
        if endpoint == "rr_model":
            result: Any = {"state.status": self.status}.get(query.get("key", ""))
            return self._json(request, {"key": query.get("key"), "flags": query.get("flags"), "result": result})
        return self._answer(request, 404, b"page not found", "text/html")

    def close(self) -> None:
        pass

    def _download(self, request: requests.PreparedRequest, name: str) -> requests.Response:
        if self.emulated_error_on_download:
            return self._json(request, {"err": 1})
        path = _resolve(VOLUME_ROOT, name)
        if path not in self.files:
            return self._answer(request, 404, b"file not found", "text/html")
        kind = "text/plain" if path.lower().endswith((".g", ".gc", ".gcode")) else "application/octet-stream"
        return self._answer(request, 200, self.files[path], kind, length=self.announce_length)

    def _fileinfo(self, request: requests.PreparedRequest, name: str) -> requests.Response:
        path = _resolve(GCODE_DIR, name)
        if path not in self.files:
            return self._json(request, {"err": 1})
        info: dict[str, Any] = {"err": 0, "fileName": name, "size": len(self.files[path])}
        if path in self.dates:
            info["lastModified"] = self.dates[path]
        info.update({"height": 5.0, "layerHeight": 0.2, "numLayers": 25, "filament": [123.4], "thumbnails": []})
        return self._json(request, info)

    def _json(self, request: requests.PreparedRequest, body: Any) -> requests.Response:
        return self._answer(request, 200, json.dumps(body).encode(), "application/json")

    def _answer(
        self, request: requests.PreparedRequest, status: int, body: bytes, kind: str, *, length: bool = True
    ) -> requests.Response:
        response = requests.Response()
        response.status_code = status
        response.reason = {200: "OK", 401: "Unauthorized", 404: "Not Found"}.get(status, "")
        headers = {"Content-Type": kind}
        if length:
            headers["Content-Length"] = str(len(body))
        response.headers = CaseInsensitiveDict(headers)
        raw = _CountingBody(body)
        self.bodies.append(raw)
        response.raw = raw
        response.url = request.url or ""
        response.request = request
        return response


def _duet(board: _FakeBoard, *, timeout: int = 7) -> DuetAdapter:
    adapter = DuetAdapter(HOST, password=PASSWORD, timeout=timeout, retries=1)
    adapter._session.mount(HOST, board)
    return adapter


_HOMED = b"; generated by a slicer\nG28\nG90\nM104 S210\nG1 X10 Y10 Z0.2 F3000\nG1 X20 E1\n"
_NOT_HOMED = b"; generated by a slicer\nM190 S60\nM104 S210\nM109 S210\nG90\nG1 Z0.2 F3000\nG1 X20 E1\n"


# ---------------------------------------------------------------------------
# read_print_file: the bytes M32 would open
# ---------------------------------------------------------------------------


def test_a_bare_name_comes_back_from_the_gcode_folder() -> None:
    board = _FakeBoard({"0:/gcodes/bracket.gcode": _HOMED, "0:/bracket.gcode": b"; a different file at the root\n"})
    duet = _duet(board)

    assert duet.read_print_file("bracket.gcode") == _HOMED
    (download,) = board.asked("rr_download")
    assert download["query"] == {"name": "0:/gcodes/bracket.gcode"}
    assert download["stream"] is True


def test_a_name_inside_a_folder_is_read_where_m32_opens_it() -> None:
    board = _FakeBoard({"0:/gcodes/parts/arm.gcode": _HOMED})
    duet = _duet(board)

    assert duet.read_print_file("parts/arm.gcode") == _HOMED
    duet._start_print_impl("parts/arm.gcode")
    assert board.printed == ["0:/gcodes/parts/arm.gcode"]
    assert [d["query"]["name"] for d in board.asked("rr_download")] == ["0:/gcodes/parts/arm.gcode"]


@pytest.mark.parametrize(
    ("name", "stored"),
    [
        ("/gcodes/plate.gcode", "0:/gcodes/plate.gcode"),
        ("0:/gcodes/plate.gcode", "0:/gcodes/plate.gcode"),
        ("1:/jobs/plate.gcode", "1:/jobs/plate.gcode"),
    ],
)
def test_an_absolute_name_reads_the_volume_start_print_uses(name: str, stored: str) -> None:
    board = _FakeBoard({stored: _HOMED})
    duet = _duet(board)

    assert duet.read_print_file(name) == _HOMED
    duet._start_print_impl(name)
    assert board.printed == [stored]


def test_a_name_the_firmware_takes_inside_the_gcode_folder_is_read_from_there() -> None:
    """A colon in the first part of a name (legal on a board run from a
    single-board computer) is not a volume: M32 opens it inside the G-code
    folder, and the read must not land on a same-named file at the root."""
    board = _FakeBoard({"0:/gcodes/my:part.gcode": _HOMED, "0:/my:part.gcode": b"; not the print\n"})
    duet = _duet(board)

    duet._start_print_impl("my:part.gcode")
    assert board.printed == ["0:/gcodes/my:part.gcode"]
    assert duet.read_print_file("my:part.gcode") == _HOMED


def test_a_missing_file_raises_the_read_failure() -> None:
    board = _FakeBoard({})
    duet = _duet(board)

    with pytest.raises(PrinterError) as caught:
        duet.read_print_file("gone.gcode")
    assert caught.value.code == "PRINT_FILE_READ_FAILED"
    assert "gone.gcode" in str(caught.value)
    assert "0:/gcodes/gone.gcode" in str(caught.value)


def test_an_unreachable_board_raises_the_read_failure() -> None:
    board = _FakeBoard({"0:/gcodes/a.gcode": _HOMED})
    duet = _duet(board)
    board.down = True

    with pytest.raises(PrinterError) as caught:
        duet.read_print_file("a.gcode")
    assert caught.value.code == "PRINT_FILE_READ_FAILED"


def test_an_error_reply_is_never_taken_for_the_file() -> None:
    """A board run from a single-board computer answers a download it could
    not serve with ``{"err":1}`` as JSON and status 200."""
    board = _FakeBoard({"0:/gcodes/a.gcode": _HOMED})
    board.emulated_error_on_download = True
    duet = _duet(board)

    with pytest.raises(PrinterError) as caught:
        duet.read_print_file("a.gcode")
    assert caught.value.code == "PRINT_FILE_READ_FAILED"


def test_a_file_past_the_ceiling_is_refused_while_it_streams(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(read_back, "READ_BACK_CAP_BYTES", 1000)
    board = _FakeBoard({"0:/gcodes/big.gcode": b"G1 X1\n" * 1000})
    board.announce_length = False
    duet = _duet(board)

    with pytest.raises(PrinterError) as caught:
        duet.read_print_file("big.gcode")
    assert caught.value.code == "PRINT_FILE_TOO_LARGE"


def test_a_file_announced_past_the_ceiling_is_refused_before_it_is_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(read_back, "READ_BACK_CAP_BYTES", 1000)
    board = _FakeBoard({"0:/gcodes/big.gcode": b"G1 X1\n" * 1000})
    duet = _duet(board)

    with pytest.raises(PrinterError) as caught:
        duet.read_print_file("big.gcode")
    assert caught.value.code == "PRINT_FILE_TOO_LARGE"
    assert board.bodies[-1].taken == 0


def test_a_file_at_the_ceiling_comes_back_whole(monkeypatch: pytest.MonkeyPatch) -> None:
    data = b"G1 X1\n" * 1000
    monkeypatch.setattr(read_back, "READ_BACK_CAP_BYTES", len(data))
    board = _FakeBoard({"0:/gcodes/big.gcode": data})
    board.announce_length = False
    duet = _duet(board)

    assert duet.read_print_file("big.gcode") == data


def test_reading_goes_through_the_session_every_other_call_uses() -> None:
    board = _FakeBoard({"0:/gcodes/a.gcode": _HOMED})
    duet = _duet(board, timeout=7)

    duet.read_print_file("a.gcode")

    connect, download = board.sent
    assert connect["endpoint"] == "rr_connect" and connect["query"]["password"] == PASSWORD
    assert download["headers"]["X-Session-Key"] == str(board.session_key)
    assert "password" not in download["query"]
    assert download["timeout"] == 7
    # A plain call afterwards carries the same session key, and no new sign-in.
    duet.get_job()
    assert board.endpoints().count("rr_connect") == 1
    assert board.sent[-1]["headers"]["X-Session-Key"] == download["headers"]["X-Session-Key"]


def test_an_expired_session_is_renewed_and_the_read_replayed() -> None:
    board = _FakeBoard({"0:/gcodes/a.gcode": _HOMED})
    duet = _duet(board)
    duet._ensure_session()
    board.expire_next_session = True

    assert duet.read_print_file("a.gcode") == _HOMED
    assert board.endpoints() == ["rr_connect", "rr_download", "rr_connect", "rr_download"]


def test_reading_changes_nothing_on_the_board() -> None:
    board = _FakeBoard({"0:/gcodes/a.gcode": _HOMED}, dates={"0:/gcodes/a.gcode": "2026-11-20T10:30:00"})
    duet = _duet(board)

    duet.read_print_file("a.gcode")
    duet.print_file_identity("a.gcode")
    assert set(board.endpoints()) == {"rr_connect", "rr_download", "rr_fileinfo"}
    assert board.files == {"0:/gcodes/a.gcode": _HOMED}


# ---------------------------------------------------------------------------
# print_file_identity: size and time stamp, without the file
# ---------------------------------------------------------------------------


def test_identity_has_both_parts() -> None:
    board = _FakeBoard({"0:/gcodes/a.gcode": _HOMED}, dates={"0:/gcodes/a.gcode": "2026-11-20T10:30:00"})
    duet = _duet(board)

    expected = int(datetime(2026, 11, 20, 10, 30, tzinfo=timezone.utc).timestamp())
    assert duet.print_file_identity("a.gcode") == (len(_HOMED), expected)
    (info,) = board.asked("rr_fileinfo")
    assert info["query"] == {"name": "0:/gcodes/a.gcode"}
    assert "rr_download" not in board.endpoints()


def test_identity_with_only_the_size() -> None:
    board = _FakeBoard({"0:/gcodes/parts/a.gcode": _HOMED})
    duet = _duet(board)

    assert duet.print_file_identity("parts/a.gcode") == (len(_HOMED), None)


def test_identity_of_a_missing_file_is_none() -> None:
    board = _FakeBoard({})
    duet = _duet(board)

    assert duet.print_file_identity("gone.gcode") is None
    assert [i["query"] for i in board.asked("rr_fileinfo")] == [{"name": "0:/gcodes/gone.gcode"}]


def test_identity_when_the_board_stops_answering_is_none() -> None:
    board = _FakeBoard({"0:/gcodes/a.gcode": _HOMED})
    duet = _duet(board)
    duet._ensure_session()
    board.down = True

    assert duet.print_file_identity("a.gcode") is None
    assert [i["query"] for i in board.asked("rr_fileinfo")] == [{"name": "0:/gcodes/a.gcode"}]


# ---------------------------------------------------------------------------
# The print gate judges the board's own copy
# ---------------------------------------------------------------------------


def test_a_start_by_name_is_judged_on_the_boards_copy() -> None:
    board = _FakeBoard({"0:/gcodes/raw.gcode": _NOT_HOMED})
    duet = _duet(board)

    result = duet.start_print("raw.gcode")

    assert result.success is False
    assert "G28" in result.message
    assert board.printed == []
    assert [d["query"]["name"] for d in board.asked("rr_download")][:1] == ["0:/gcodes/raw.gcode"]


def test_a_complete_copy_on_the_board_starts_the_file_that_was_read(tmp_path: Any) -> None:
    from kiln.printers.gcode_complete import complete_gcode_for_printer

    source = tmp_path / "ready.gcode"
    source.write_bytes(_HOMED + b"; filament used [mm] = 1.00\n")
    complete_gcode_for_printer(str(source), "duet")
    board = _FakeBoard({"0:/gcodes/jobs/ready.gcode": source.read_bytes()})
    duet = _duet(board)
    duet.printer_id = "kilntest_open"  # a catalogue row with a known bed, so the fit is judged

    result = duet.start_print("jobs/ready.gcode")

    assert result.success is True, result.message
    assert board.printed == ["0:/gcodes/jobs/ready.gcode"]
    assert [d["query"]["name"] for d in board.asked("rr_download")] == ["0:/gcodes/jobs/ready.gcode"]
