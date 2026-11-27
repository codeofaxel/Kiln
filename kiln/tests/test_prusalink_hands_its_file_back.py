"""A Prusa Link printer hands its print file back to Kiln.

The printer is faked at the HTTP session's transport, answering the way the
two published Prusa Link builds answer:

* the printer's own firmware (MK4, XL, MINI, CORE One): files live on the
  ``usb`` storage, a file's record at ``GET /api/v1/files/usb/<path>``
  carries ``size``, ``m_timestamp`` and ``refs.download`` (``/usb/<path>``),
  and ``GET /usb/<path>`` sends the file; the ``local`` storage is refused
  with 403 because nothing outside the USB drive is served;
* Prusa Link on a Raspberry Pi (MK3S, MK2.5S): files live on the ``local``
  storage, ``usb`` is answered 404, a file's record names
  ``/api/files/local/<storage name>/<path>/raw`` as its download, and a file
  it cannot serve (its SD card) carries ``refs.download: null``.

Every request is kept, so a test can see the path asked for, the headers
sent and the time limit used.
"""

from __future__ import annotations

import io
import logging
from typing import Any
from urllib.parse import unquote, urlsplit

import pytest
import requests
from requests.adapters import BaseAdapter
from requests.structures import CaseInsensitiveDict

from kiln.printers import read_back
from kiln.printers.base import PrinterError
from kiln.printers.prusalink import PrusaLinkAdapter

HOST = "http://prusa.local"
API_KEY = "test-key"
PI_STORAGE_NAME = "PrusaLink gcodes"
STAMP = 1_700_000_000


class _CountingReader(io.BytesIO):
    """The response body, counting how much of it was read off the wire."""

    def __init__(self, body: bytes) -> None:
        super().__init__(body)
        self.taken = 0

    def read(self, size: int | None = -1) -> bytes:
        chunk = super().read(size)
        self.taken += len(chunk)
        return chunk


class FakePrusaLink(BaseAdapter):
    """A Prusa Link printer's HTTP API at the session's transport."""

    def __init__(self, build: str = "firmware") -> None:
        super().__init__()
        assert build in ("firmware", "pi")
        self.build = build
        self.storage = "usb" if build == "firmware" else "local"
        #: storage path -> (bytes, m_timestamp or None, long name)
        self.files: dict[str, tuple[bytes, int | None, str]] = {}
        self.folders: set[str] = set()
        self.no_download: set[str] = set()
        self.no_size: set[str] = set()
        self.download_answer: tuple[int, dict[str, str]] | None = None
        self.down = False
        self.seen: list[requests.PreparedRequest] = []
        self.timeouts: list[Any] = []
        self.bodies: list[_CountingReader] = []

    # -- what a test sets -----------------------------------------------

    def holds(self, path: str, data: bytes, *, stamp: int | None = STAMP, long_name: str | None = None) -> None:
        self.files[path] = (data, stamp, long_name or path.rsplit("/", 1)[-1])
        parts = path.split("/")[:-1]
        for i in range(1, len(parts) + 1):
            self.folders.add("/".join(parts[:i]))

    # -- what the adapter's session meets ---------------------------------

    def send(self, request, stream=False, timeout=None, verify=True, cert=None, proxies=None):  # noqa: D401
        self.seen.append(request)
        self.timeouts.append(timeout)
        if self.down:
            raise requests.ConnectionError("no route to the printer")
        if request.headers.get("X-Api-Key") != API_KEY:
            if self.build == "pi":
                return self._answer(request, 403, b"Bad X-Api-Key.")
            return self._answer(request, 401, b"Unauthorized")
        path = unquote(urlsplit(request.url).path)
        method = request.method
        v1 = "/api/v1/files/"
        if path.startswith(v1) or path.rstrip("/") + "/" == v1:
            storage, _, rest = path[len(v1):].partition("/")
            return self._v1_files(request, method, storage, rest.strip("/"))
        if self.build == "firmware" and path.startswith("/usb/") and method == "GET":
            return self._file_bytes(request, path[len("/usb/"):])
        raw_prefix = f"/api/files/local/{PI_STORAGE_NAME}/"
        if self.build == "pi" and path.startswith(raw_prefix) and path.endswith("/raw") and method == "GET":
            return self._file_bytes(request, path[len(raw_prefix):-len("/raw")])
        return self._answer(request, 404, b"Not Found")

    def close(self) -> None:
        pass

    # -- the printer's answers ---------------------------------------------

    def _download_ref(self, path: str) -> str | None:
        if path in self.no_download:
            return None
        if self.build == "firmware":
            return f"/usb/{path}"
        return f"/api/files/local/{PI_STORAGE_NAME}/{path}/raw"

    def _record(self, path: str) -> dict[str, Any]:
        data, stamp, long_name = self.files[path]
        record: dict[str, Any] = {
            "name": path.rsplit("/", 1)[-1],
            "ro" if self.build == "firmware" else "read_only": False,
            "type": "PRINT_FILE",
            "display_name": long_name,
            "refs": {"download": self._download_ref(path), "icon": None, "thumbnail": None},
        }
        if stamp is not None:
            record["m_timestamp"] = stamp
        if path not in self.no_size:
            record["size"] = len(data)
        return record

    def _folder(self, path: str) -> dict[str, Any]:
        children: list[dict[str, Any]] = []
        for folder in sorted(self.folders):
            parent, _, leaf = folder.rpartition("/")
            if parent == path:
                child: dict[str, Any] = {"name": leaf, "type": "FOLDER", "m_timestamp": STAMP}
                if self.build == "pi":
                    child["children"] = self._folder(folder)["children"]
                children.append(child)
        for file_path in sorted(self.files):
            parent, _, _leaf = file_path.rpartition("/")
            if parent == path:
                child = self._record(file_path)
                if self.build == "firmware":
                    child.pop("size", None)  # the firmware's listing carries no size
                children.append(child)
        return {"name": path or self.storage, "type": "FOLDER", "children": children}

    def _v1_files(self, request, method: str, storage: str, path: str):
        if storage != self.storage:
            # The firmware serves nothing outside its USB drive (403); the Pi
            # build knows only ``local`` and ``sdcard`` (404).
            return self._answer(request, 403 if self.build == "firmware" else 404, b"Forbidden")
        if method == "GET":
            if path in self.files:
                return self._json(request, self._record(path))
            if path == "" or path in self.folders:
                return self._json(request, self._folder(path))
            return self._answer(request, 404, b"Not Found")
        if method == "POST":
            return self._answer(request, 204 if path in self.files else 404, b"")
        return self._answer(request, 405, b"Method Not Allowed")

    def _file_bytes(self, request, path: str):
        if self.download_answer is not None:
            status, headers = self.download_answer
            return self._answer(request, status, b"", headers)
        if path not in self.files:
            return self._answer(request, 404, b"Not Found")
        return self._answer(request, 200, self.files[path][0], {"Content-Type": "application/octet-stream"})

    def _json(self, request, payload: dict[str, Any]):
        import json

        return self._answer(request, 200, json.dumps(payload).encode(), {"Content-Type": "application/json"})

    def _answer(self, request, status: int, body: bytes, headers: dict[str, str] | None = None):
        response = requests.Response()
        response.status_code = status
        reader = _CountingReader(body)
        self.bodies.append(reader)
        response.raw = reader
        response.headers = CaseInsensitiveDict(headers or {})
        response.url = request.url
        response.request = request
        response.encoding = "utf-8"
        response.reason = "OK" if status < 400 else "Error"
        return response

    # -- what a test reads back ---------------------------------------------

    def asked(self, method: str | None = None) -> list[str]:
        return [
            unquote(urlsplit(r.url).path)
            for r in self.seen
            if method is None or r.method == method
        ]

    def raw_paths(self) -> list[str]:
        return [urlsplit(r.url).path for r in self.seen]


class _Elsewhere(BaseAdapter):
    """Any other host: records that it was reached, answers nothing."""

    def __init__(self) -> None:
        super().__init__()
        self.seen: list[requests.PreparedRequest] = []

    def send(self, request, **_kwargs):  # noqa: D401
        self.seen.append(request)
        raise requests.ConnectionError("Kiln must never come here")

    def close(self) -> None:
        pass


def _printer(build: str = "firmware", **kwargs: Any) -> tuple[PrusaLinkAdapter, FakePrusaLink, _Elsewhere]:
    adapter = PrusaLinkAdapter(host=HOST, api_key=API_KEY, retries=1, **kwargs)
    fake = FakePrusaLink(build)
    elsewhere = _Elsewhere()
    adapter._session.mount(HOST, fake)
    adapter._session.mount("http://", elsewhere)
    adapter._session.mount("https://", elsewhere)
    return adapter, fake, elsewhere


GCODE = b"; generated for a test\nG28\nG1 X10 Y10 Z0.2 F3000\nG1 X20 E1\n"


# ---------------------------------------------------------------------------
# The file comes back
# ---------------------------------------------------------------------------


class TestTheFileComesBack:
    def test_a_bare_long_name_reads_the_short_name_file_from_the_usb_drive(self):
        adapter, fake, _ = _printer("firmware")
        fake.holds("BENCHY~1.BGC", GCODE, long_name="benchy_0.4n_PLA.bgcode")

        assert adapter.read_print_file("benchy_0.4n_PLA.bgcode") == GCODE
        assert "/api/v1/files/usb/BENCHY~1.BGC" in fake.asked("GET")
        assert fake.asked("GET")[-1] == "/usb/BENCHY~1.BGC"

    def test_a_name_inside_a_folder_reads_that_file_through_its_named_download(self):
        adapter, fake, _ = _printer("pi")
        fake.holds("parts/left bracket.gcode", GCODE)

        assert adapter.read_print_file("left bracket.gcode") == GCODE
        assert "/api/v1/files/local/parts/left bracket.gcode" in fake.asked("GET")
        assert fake.asked("GET")[-1] == f"/api/files/local/{PI_STORAGE_NAME}/parts/left bracket.gcode/raw"
        # The space in the storage's own name is sent quoted, not raw.
        assert fake.raw_paths()[-1] == "/api/files/local/PrusaLink%20gcodes/parts/left%20bracket.gcode/raw"

    def test_a_folder_path_on_the_usb_drive_is_read_from_that_folder(self):
        adapter, fake, _ = _printer("firmware")
        fake.holds("PARTS/BRACKET.GCO", GCODE)

        assert adapter.read_print_file("PARTS/BRACKET.GCO") == GCODE
        assert fake.asked("GET")[-2:] == ["/api/v1/files/usb/PARTS/BRACKET.GCO", "/usb/PARTS/BRACKET.GCO"]

    def test_the_file_read_back_is_the_one_a_start_by_that_name_prints(self):
        for build, path, name in (
            ("firmware", "BENCHY~1.BGC", "benchy_0.4n_PLA.bgcode"),
            ("pi", "parts/benchy.gcode", "benchy.gcode"),
        ):
            adapter, fake, _ = _printer(build)
            fake.holds(path, GCODE, long_name=name)
            assert adapter.read_print_file(name) == GCODE
            read_from = [p for p in fake.asked("GET") if p.startswith("/api/v1/files/") and p.endswith(path)]
            adapter._start_print_impl(name)
            started = fake.asked("POST")
            assert started == read_from, build
            assert started[-1] == f"/api/v1/files/{fake.storage}/{path}", build

    def test_the_pi_build_is_read_from_local_after_usb_is_not_there(self):
        adapter, fake, _ = _printer("pi")
        fake.holds("benchy.gcode", GCODE)

        assert adapter.read_print_file("benchy.gcode") == GCODE
        records = [p for p in fake.asked("GET") if p.endswith("/benchy.gcode")]
        assert records == ["/api/v1/files/usb/benchy.gcode", "/api/v1/files/local/benchy.gcode"]

    def test_a_storage_named_in_the_file_name_is_asked_first(self):
        adapter, fake, _ = _printer("pi")
        fake.holds("benchy.gcode", GCODE)

        assert adapter.read_print_file("local/benchy.gcode") == GCODE
        records = [p for p in fake.asked("GET") if p.endswith("/benchy.gcode")]
        assert records == ["/api/v1/files/local/benchy.gcode"]


# ---------------------------------------------------------------------------
# What cannot come back, and what failed this time
# ---------------------------------------------------------------------------


class TestWhatCannotComeBack:
    def test_a_missing_file_is_a_failed_read_that_says_so(self):
        adapter, fake, _ = _printer("firmware")
        fake.holds("OTHER.GCO", GCODE)

        with pytest.raises(PrinterError) as caught:
            adapter.read_print_file("missing.gcode")
        assert caught.value.code == "PRINT_FILE_READ_FAILED"
        assert "missing.gcode" in str(caught.value)
        assert "/api/v1/files/usb/missing.gcode" in fake.asked("GET")
        assert not any(p.startswith("/usb/") for p in fake.asked())

    def test_a_file_the_printer_names_no_download_for_cannot_be_handed_back(self):
        adapter, fake, _ = _printer("pi")
        fake.holds("benchy.gcode", GCODE)
        fake.no_download.add("benchy.gcode")

        assert adapter.read_print_file("benchy.gcode") is None
        assert fake.asked("GET")[-1] == "/api/v1/files/local/benchy.gcode"
        assert not any(p.endswith("/raw") for p in fake.asked())

    def test_a_download_that_points_off_this_printer_is_not_followed(self):
        adapter, fake, elsewhere = _printer("firmware")
        fake.holds("BENCHY.GCO", GCODE)
        fake._download_ref = lambda path: f"http://elsewhere.example/{path}"  # type: ignore[method-assign]

        assert adapter.read_print_file("BENCHY.GCO") is None
        assert fake.asked("GET")[-1] == "/api/v1/files/usb/BENCHY.GCO"
        assert elsewhere.seen == []

    def test_a_redirect_is_refused_and_never_followed(self):
        adapter, fake, elsewhere = _printer("firmware")
        fake.holds("BENCHY.GCO", GCODE)
        fake.download_answer = (302, {"Location": "http://elsewhere.example/BENCHY.GCO"})

        with pytest.raises(PrinterError) as caught:
            adapter.read_print_file("BENCHY.GCO")
        assert caught.value.code == "PRINT_FILE_READ_FAILED"
        assert elsewhere.seen == []

    def test_a_folder_is_not_a_print_file(self):
        adapter, fake, _ = _printer("firmware")
        fake.holds("PARTS/BRACKET.GCO", GCODE)

        with pytest.raises(PrinterError) as caught:
            adapter.read_print_file("PARTS")
        assert caught.value.code == "PRINT_FILE_READ_FAILED"
        assert not any(p.startswith("/usb/") for p in fake.asked())

    def test_a_printer_that_does_not_answer_is_a_failed_read(self):
        adapter, fake, _ = _printer("firmware")
        fake.down = True

        with pytest.raises(PrinterError) as caught:
            adapter.read_print_file("BENCHY.GCO")
        assert caught.value.code == "PRINT_FILE_READ_FAILED"
        assert fake.seen


class TestTheCeiling:
    def test_a_file_the_record_says_is_past_the_ceiling_is_refused_before_it_is_sent(self, monkeypatch):
        monkeypatch.setattr(read_back, "READ_BACK_CAP_BYTES", 1000)
        adapter, fake, _ = _printer("firmware")
        fake.holds("BIG.GCO", b"G1 X1\n" * 400)

        with pytest.raises(PrinterError) as caught:
            adapter.read_print_file("BIG.GCO")
        assert caught.value.code == "PRINT_FILE_TOO_LARGE"
        assert "/api/v1/files/usb/BIG.GCO" in fake.asked("GET")
        assert not any(p.startswith("/usb/") for p in fake.asked())

    def test_a_download_past_the_ceiling_stops_without_reading_the_rest(self, monkeypatch):
        monkeypatch.setattr(read_back, "READ_BACK_CAP_BYTES", 1000)
        adapter, fake, _ = _printer("firmware")
        body = b"G1 X1\n" * 40_000  # 240 000 bytes, several reads' worth
        fake.holds("BIG.GCO", body)
        fake.no_size.add("BIG.GCO")

        with pytest.raises(PrinterError) as caught:
            adapter.read_print_file("BIG.GCO")
        assert caught.value.code == "PRINT_FILE_TOO_LARGE"
        download = fake.bodies[-1]
        assert fake.asked("GET")[-1] == "/usb/BIG.GCO"
        assert 0 < download.taken < len(body)


# ---------------------------------------------------------------------------
# What the printer says about a file without sending it
# ---------------------------------------------------------------------------


class TestIdentity:
    def test_size_and_time_stamp_come_from_the_file_record(self):
        adapter, fake, _ = _printer("firmware")
        fake.holds("BENCHY~1.BGC", GCODE, stamp=1_712_345_678, long_name="benchy.bgcode")

        assert adapter.print_file_identity("benchy.bgcode") == (len(GCODE), 1_712_345_678)
        assert fake.asked("GET")[-1] == "/api/v1/files/usb/BENCHY~1.BGC"
        assert not any(p.startswith("/usb/") for p in fake.asked())

    def test_a_record_with_only_a_size_gives_only_the_size(self):
        adapter, fake, _ = _printer("pi")
        fake.holds("benchy.gcode", GCODE, stamp=None)

        assert adapter.print_file_identity("benchy.gcode") == (len(GCODE), None)
        assert fake.asked("GET")[-1] == "/api/v1/files/local/benchy.gcode"

    def test_a_record_with_neither_is_no_identity(self):
        adapter, fake, _ = _printer("pi")
        fake.holds("benchy.gcode", GCODE, stamp=None)
        fake.no_size.add("benchy.gcode")

        assert adapter.print_file_identity("benchy.gcode") is None
        assert fake.asked("GET")[-1] == "/api/v1/files/local/benchy.gcode"

    def test_a_record_that_cannot_be_read_is_no_identity_and_never_raises(self):
        adapter, fake, _ = _printer("firmware")
        fake.down = True

        assert adapter.print_file_identity("BENCHY.GCO") is None
        assert fake.seen

    def test_a_missing_file_is_no_identity(self):
        adapter, fake, _ = _printer("firmware")

        assert adapter.print_file_identity("missing.gcode") is None
        assert "/api/v1/files/usb/missing.gcode" in fake.asked("GET")


# ---------------------------------------------------------------------------
# The key goes where the adapter's other calls send it, and nowhere else
# ---------------------------------------------------------------------------


class TestCredentials:
    def test_every_read_back_request_carries_the_key_the_other_calls_carry(self, caplog):
        caplog.set_level(logging.DEBUG)
        adapter, fake, _ = _printer("firmware", timeout=7)
        fake.holds("BENCHY.GCO", GCODE)

        adapter.list_files()
        listed_with = {r.headers.get("X-Api-Key") for r in fake.seen}
        fake.seen.clear()
        fake.timeouts.clear()
        assert adapter.read_print_file("BENCHY.GCO") == GCODE
        assert adapter.print_file_identity("BENCHY.GCO") == (len(GCODE), STAMP)

        assert listed_with == {API_KEY}
        assert fake.seen and {r.headers.get("X-Api-Key") for r in fake.seen} == {API_KEY}
        assert all("Authorization" not in r.headers for r in fake.seen)
        assert "/usb/BENCHY.GCO" in fake.asked("GET")
        assert set(fake.timeouts) == {7}
        assert API_KEY not in caplog.text

    def test_a_refused_key_is_a_failed_read_and_the_key_is_not_in_the_words(self):
        adapter, fake, _ = _printer("pi")
        fake.holds("benchy.gcode", GCODE)
        adapter._session.headers["X-Api-Key"] = "wrong-key"

        with pytest.raises(PrinterError) as caught:
            adapter.read_print_file("benchy.gcode")
        assert caught.value.code == "PRINT_FILE_READ_FAILED"
        assert "wrong-key" not in str(caught.value)
        assert not any(p.endswith("/raw") for p in fake.asked())
