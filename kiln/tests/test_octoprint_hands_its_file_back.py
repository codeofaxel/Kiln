"""An OctoPrint printer hands the file it would print back to Kiln.

OctoPrint is faked at the HTTP answers it gives (``responses``), shaped like
its REST API: ``GET /api/files/local/<path>`` answers a file's record (its
``size``, its ``date`` and a ``refs.download`` link, left out for a folder
and for a storage that cannot read files), and ``GET
/downloads/files/local/<path>`` streams the bytes.  Both need the API key;
OctoPrint decodes an escaped path segment back into folders, and its
download route serves nothing outside the uploads folder.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import re
from typing import Any
from urllib.parse import unquote, urlsplit

import pytest
import requests
import responses

from kiln import printer_copy
from kiln.output_paths import REQUEST_OUTPUT_ROOT
from kiln.printers import read_back
from kiln.printers.base import PrinterError
from kiln.printers.octoprint import OctoPrintAdapter

HOST = "http://octopi.local"
KEY = "TESTAPIKEY123"
STAMP = 1_764_100_000
GCODE = b"G28\nG1 Z0.3 F3000\nG1 X10 Y10 E1 F1200\nM104 S0\n"


class FakeOctoPrint:
    """OctoPrint's file answers for the files it holds in its uploads folder."""

    def __init__(self, mock: responses.RequestsMock) -> None:
        self.files: dict[str, bytes] = {}
        self.dates: dict[str, int | None] = {}
        self.folders: set[str] = set()
        #: OctoPrint leaves ``refs.download`` out for a storage that cannot read files.
        self.serves_downloads = True
        self.sends_length = True
        self.selected: list[str] = []
        self.requests: list[requests.PreparedRequest] = []
        files = re.compile(re.escape(HOST) + r"/api/files/local/([^?]+)")
        downloads = re.compile(re.escape(HOST) + r"/downloads/files/local/([^?]+)")
        mock.add_callback(responses.GET, files, callback=self._record)
        mock.add_callback(responses.POST, files, callback=self._select)
        mock.add_callback(responses.GET, downloads, callback=self._download)

    # -- what a test sets -----------------------------------------------
    def hold(self, path: str, data: bytes, date: int | None = STAMP) -> None:
        self.files[path] = data
        self.dates[path] = date

    def downloads(self) -> list[str]:
        return [urlsplit(r.url).path for r in self.requests if "/downloads/" in str(r.url)]

    def asked(self) -> list[str]:
        return [urlsplit(r.url).path for r in self.requests]

    # -- OctoPrint's side -------------------------------------------------
    def _refused(self, request: requests.PreparedRequest) -> tuple[int, dict[str, str], str] | None:
        self.requests.append(request)
        if request.headers.get("X-Api-Key") != KEY:
            return 403, {}, '{"error": "You don\'t have the permission to access the requested resource."}'
        return None

    @staticmethod
    def _storage_path(request: requests.PreparedRequest, prefix: str) -> str:
        return unquote(urlsplit(request.url).path[len(prefix):])

    def _record(self, request: requests.PreparedRequest) -> tuple[int, dict[str, str], str]:
        if refused := self._refused(request):
            return refused
        path = self._storage_path(request, "/api/files/local/").lstrip("/")
        name = path.rsplit("/", 1)[-1]
        if path in self.folders:
            entry: dict[str, Any] = {
                "name": name, "display": name, "path": path, "origin": "local",
                "type": "folder", "typePath": ["folder"], "date": STAMP, "children": [],
                "refs": {"resource": f"{HOST}/api/files/local/{path}"},
            }
            return 200, {"Content-Type": "application/json"}, json.dumps(entry)
        if path not in self.files:
            return 404, {"Content-Type": "text/html"}, "<h1>Not Found</h1>"
        entry = {
            "name": name, "display": name, "path": path, "origin": "local",
            "type": "machinecode", "typePath": ["machinecode", "gcode"],
            "size": len(self.files[path]),
            "refs": {"resource": f"{HOST}/api/files/local/{path}"},
        }
        if self.dates[path] is not None:
            entry["date"] = self.dates[path]
        if self.serves_downloads:
            # The link names a host the adapter never sends its key to.
            entry["refs"]["download"] = f"http://elsewhere.example/downloads/files/local/{path}"
        return 200, {"Content-Type": "application/json"}, json.dumps(entry)

    def _download(self, request: requests.PreparedRequest) -> tuple[int, dict[str, str], bytes | str]:
        if refused := self._refused(request):
            return refused
        path = self._storage_path(request, "/downloads/files/local/")
        if path.startswith("/"):
            return 403, {}, "not in root static directory"
        if not self.serves_downloads or path not in self.files:
            return 404, {}, "Not Found"
        data = self.files[path]
        headers = {"Content-Type": "text/x.gcode"}
        if self.sends_length:
            headers["Content-Length"] = str(len(data))
        return 200, headers, data

    def _select(self, request: requests.PreparedRequest) -> tuple[int, dict[str, str], str]:
        if refused := self._refused(request):
            return refused
        self.selected.append(urlsplit(request.url).path)
        return 204, {}, ""


def _complete(tmp_path: pathlib.Path) -> bytes:
    """:data:`GCODE` as Kiln leaves a file for an OctoPrint printer."""
    from kiln.printers.gcode_complete import complete_gcode_for_printer

    path = tmp_path / "complete.gcode"
    path.write_bytes(GCODE)
    return pathlib.Path(complete_gcode_for_printer(path, "octoprint")).read_bytes()


@pytest.fixture
def octoprint():
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mock:
        yield FakeOctoPrint(mock)


@pytest.fixture
def adapter() -> OctoPrintAdapter:
    return OctoPrintAdapter(host=HOST, api_key=KEY, timeout=5, retries=1)


@pytest.fixture(autouse=True)
def _private_folder(tmp_path):
    token = REQUEST_OUTPUT_ROOT.set(str(tmp_path / "out"))
    yield
    REQUEST_OUTPUT_ROOT.reset(token)


class TestTheBytesComeBack:
    def test_a_bare_name(self, octoprint, adapter):
        octoprint.hold("benchy.gcode", GCODE)
        assert adapter.read_print_file("benchy.gcode") == GCODE
        assert octoprint.asked() == ["/api/files/local/benchy.gcode", "/downloads/files/local/benchy.gcode"]

    def test_a_name_inside_a_folder(self, octoprint, adapter):
        octoprint.hold("parts/brackets/left.gcode", b"G28 ; left")
        octoprint.hold("parts/left.gcode", b"G28 ; the other left")
        assert adapter.read_print_file("parts/brackets/left.gcode") == b"G28 ; left"
        assert octoprint.downloads() == ["/downloads/files/local/parts%2Fbrackets%2Fleft.gcode"]

    def test_a_leading_slash_names_the_same_upload(self, octoprint, adapter):
        octoprint.hold("benchy.gcode", GCODE)
        assert adapter.read_print_file("/benchy.gcode") == GCODE

    @pytest.mark.parametrize("name", ["benchy.gcode", "parts/left.gcode", "/benchy.gcode", "my file.gcode"])
    def test_the_file_read_is_the_one_start_print_selects(self, octoprint, adapter, tmp_path, name):
        adapter.printer_model = "ender3"
        octoprint.hold(name.lstrip("/"), _complete(tmp_path))
        result = adapter.start_print(name)
        assert result.success, result.message
        assert octoprint.downloads(), "the start was not checked against the printer's copy"
        record = next(p for p in octoprint.asked() if p.startswith("/api/files/"))
        assert octoprint.selected == [record]

    def test_a_start_by_name_is_judged_on_the_printers_bytes(self, octoprint, adapter):
        """A file the printer holds that Kiln would not send is not started."""
        octoprint.hold("benchy.gcode", GCODE)
        result = adapter.start_print("benchy.gcode")
        assert not result.success and result.refused_before_send
        assert "printer's own copy" in result.message
        assert octoprint.selected == []

    def test_the_printers_copy_is_kept_and_read_once(self, octoprint, adapter):
        octoprint.hold("benchy.gcode", GCODE)
        first = printer_copy.copy_from_printer(adapter, "benchy.gcode")
        again = printer_copy.copy_from_printer(adapter, "benchy.gcode")
        assert first.how == printer_copy.READ_BACK and again.how == printer_copy.KEPT
        assert hashlib.sha256(pathlib.Path(again.path).read_bytes()).hexdigest() == hashlib.sha256(GCODE).hexdigest()
        assert len(octoprint.downloads()) == 1


class TestWhatCannotComeBack:
    def test_a_missing_file_fails_this_time(self, octoprint, adapter):
        with pytest.raises(PrinterError) as caught:
            adapter.read_print_file("missing.gcode")
        assert caught.value.code == "PRINT_FILE_READ_FAILED"
        assert "missing.gcode" in str(caught.value) and "no file by that name" in str(caught.value)
        assert octoprint.downloads() == []

    def test_a_folder_is_not_a_print_file(self, octoprint, adapter):
        octoprint.folders.add("parts")
        with pytest.raises(PrinterError) as caught:
            adapter.read_print_file("parts")
        assert caught.value.code == "PRINT_FILE_READ_FAILED"
        assert octoprint.downloads() == []

    def test_a_storage_that_serves_no_downloads_says_none(self, octoprint, adapter):
        octoprint.serves_downloads = False
        octoprint.hold("benchy.gcode", GCODE)
        assert adapter.read_print_file("benchy.gcode") is None
        # The answer is OctoPrint's own record, which carries no download link.
        assert octoprint.asked() == ["/api/files/local/benchy.gcode"]

    def test_an_unreachable_printer_fails_this_time(self, adapter):
        with responses.RequestsMock() as mock:
            mock.add(responses.GET, f"{HOST}/api/files/local/benchy.gcode",
                     body=requests.exceptions.ConnectionError("refused"))
            with pytest.raises(PrinterError) as caught:
                adapter.read_print_file("benchy.gcode")
        assert caught.value.code == "PRINT_FILE_READ_FAILED"
        assert "refused" not in str(caught.value)

    @pytest.mark.parametrize("sends_length", [True, False])
    def test_a_file_past_the_ceiling_is_refused(self, octoprint, adapter, monkeypatch, sends_length):
        monkeypatch.setattr(read_back, "READ_BACK_CAP_BYTES", 16)
        octoprint.sends_length = sends_length
        octoprint.hold("big.gcode", b"G1 X1\n" * 10)
        with pytest.raises(PrinterError) as caught:
            adapter.read_print_file("big.gcode")
        assert caught.value.code == "PRINT_FILE_TOO_LARGE"

    def test_a_file_at_the_ceiling_still_comes_back(self, octoprint, adapter, monkeypatch):
        monkeypatch.setattr(read_back, "READ_BACK_CAP_BYTES", 16)
        octoprint.hold("small.gcode", b"G28\nG1 X1 Y1 Z1\n"[:16])
        assert adapter.read_print_file("small.gcode") == b"G28\nG1 X1 Y1 Z1\n"[:16]


class TestTheKey:
    def test_every_request_carries_the_key_as_the_other_calls_do(self, octoprint, adapter):
        octoprint.hold("benchy.gcode", GCODE)
        adapter.read_print_file("benchy.gcode")
        adapter.print_file_identity("benchy.gcode")
        assert octoprint.requests
        for request in octoprint.requests:
            assert request.headers["X-Api-Key"] == KEY
            assert KEY not in str(request.url)
            assert urlsplit(request.url).netloc == "octopi.local"

    def test_a_refused_key_fails_without_saying_the_key(self, octoprint):
        octoprint.hold("benchy.gcode", GCODE)
        wrong = OctoPrintAdapter(host=HOST, api_key="WRONGKEY999", timeout=5, retries=1)
        with pytest.raises(PrinterError) as caught:
            wrong.read_print_file("benchy.gcode")
        assert caught.value.code == "PRINT_FILE_READ_FAILED"
        assert "API key" in str(caught.value) and "WRONGKEY999" not in str(caught.value)


class TestTheIdentity:
    def test_size_and_time_stamp(self, octoprint, adapter):
        octoprint.hold("parts/left.gcode", GCODE)
        assert adapter.print_file_identity("parts/left.gcode") == (len(GCODE), STAMP)
        assert octoprint.downloads() == []

    def test_only_the_size(self, octoprint, adapter):
        octoprint.hold("benchy.gcode", GCODE, date=None)
        assert adapter.print_file_identity("benchy.gcode") == (len(GCODE), None)

    def test_a_missing_file_has_none(self, octoprint, adapter):
        assert adapter.print_file_identity("missing.gcode") is None
        assert octoprint.asked() == ["/api/files/local/missing.gcode"]

    def test_a_folder_has_none(self, octoprint, adapter):
        octoprint.folders.add("parts")
        assert adapter.print_file_identity("parts") is None
        assert octoprint.asked() == ["/api/files/local/parts"]

    @pytest.mark.parametrize("answer", [
        requests.exceptions.ConnectionError("refused"),
        requests.exceptions.ReadTimeout("slow"),
    ])
    def test_a_call_that_fails_is_none(self, adapter, answer):
        with responses.RequestsMock() as mock:
            mock.add(responses.GET, f"{HOST}/api/files/local/benchy.gcode", body=answer)
            assert adapter.print_file_identity("benchy.gcode") is None

    def test_an_answer_that_is_not_a_record_is_none(self, adapter):
        with responses.RequestsMock() as mock:
            mock.add(responses.GET, f"{HOST}/api/files/local/benchy.gcode", status=500, body="oops")
            assert adapter.print_file_identity("benchy.gcode") is None
        with responses.RequestsMock() as mock:
            mock.add(responses.GET, f"{HOST}/api/files/local/benchy.gcode", body="<html>")
            assert adapter.print_file_identity("benchy.gcode") is None
