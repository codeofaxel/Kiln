"""The printer's own copy of a file this computer holds no copy of.

Read back through a Bambu adapter whose FTPS is faked at the client the
adapter opens (``tests/_fake_bambu_ftps.py``): what is kept, what is read
again, and that a kept copy never stands for other bytes.
"""

from __future__ import annotations

import hashlib
import pathlib

import pytest

from kiln import printer_copy
from kiln.output_paths import REQUEST_OUTPUT_ROOT
from kiln.printers.base import PrinterAdapter
from tests._fake_bambu_ftps import FakeBambuFTPS, bambu_on

NAME = "cube.gcode.3mf"
STAMP = 1_764_100_000


@pytest.fixture(autouse=True)
def _private_folder(tmp_path):
    token = REQUEST_OUTPUT_ROOT.set(str(tmp_path / "out"))
    yield
    REQUEST_OUTPUT_ROOT.reset(token)


@pytest.fixture
def ftps() -> FakeBambuFTPS:
    return FakeBambuFTPS()


def _sha(path: str | None) -> str:
    return hashlib.sha256(pathlib.Path(str(path)).read_bytes()).hexdigest()


class TestReadBack:
    def test_the_printers_bytes_come_back_as_a_local_file(self, ftps, monkeypatch, tmp_path):
        ftps.hold(NAME, b"PK plate one", STAMP)
        got = printer_copy.copy_from_printer(bambu_on(ftps, monkeypatch, tmp_path), NAME)
        assert got.how == printer_copy.READ_BACK
        assert pathlib.Path(got.path).read_bytes() == b"PK plate one"
        assert got.path.endswith(".gcode.3mf")
        assert ftps.reads() == [f"RETR /sdcard/{NAME}"]

    def test_the_same_file_is_read_once(self, ftps, monkeypatch, tmp_path):
        ftps.hold(NAME, b"PK plate one", STAMP)
        adapter = bambu_on(ftps, monkeypatch, tmp_path)
        first = printer_copy.copy_from_printer(adapter, NAME)
        again = printer_copy.copy_from_printer(adapter, NAME)
        assert again.how == printer_copy.KEPT and again.path == first.path
        assert len(ftps.reads()) == 1

    def test_a_new_file_under_the_same_name_and_size_is_read_again(self, ftps, monkeypatch, tmp_path):
        """A changed temperature leaves a file the same size: only the
        printer's time stamp tells the two apart."""
        ftps.hold(NAME, b"M104 S210 plate", STAMP)
        adapter = bambu_on(ftps, monkeypatch, tmp_path)
        first = printer_copy.copy_from_printer(adapter, NAME)
        ftps.hold(NAME, b"M104 S215 plate", STAMP + 60)
        again = printer_copy.copy_from_printer(adapter, NAME)
        assert again.how == printer_copy.READ_BACK
        assert pathlib.Path(again.path).read_bytes() == b"M104 S215 plate"
        assert _sha(first.path) != _sha(again.path)

    def test_with_no_time_stamp_nothing_kept_stands_in(self, ftps, monkeypatch, tmp_path):
        ftps.answers_mdtm = False
        ftps.hold(NAME, b"M104 S210 plate")
        adapter = bambu_on(ftps, monkeypatch, tmp_path)
        printer_copy.copy_from_printer(adapter, NAME)
        ftps.hold(NAME, b"M104 S215 plate")
        again = printer_copy.copy_from_printer(adapter, NAME)
        assert again.how == printer_copy.READ_BACK
        assert pathlib.Path(again.path).read_bytes() == b"M104 S215 plate"
        assert len(ftps.reads()) == 2

    def test_the_same_name_on_another_printer_is_its_own_file(self, ftps, monkeypatch, tmp_path):
        ftps.hold(NAME, b"plate A", STAMP)
        printer_copy.copy_from_printer(bambu_on(ftps, monkeypatch, tmp_path), NAME)
        other = FakeBambuFTPS()
        other.hold(NAME, b"plate B", STAMP)
        monkeypatch.setattr("kiln.printers.bambu._ImplicitFTP_TLS", other)
        from kiln.printers.bambu import BambuAdapter

        second = BambuAdapter(host="192.0.2.11", access_code="12345678", serial="01S00A000000002", timeout=2)
        got = printer_copy.copy_from_printer(second, NAME)
        assert pathlib.Path(got.path).read_bytes() == b"plate B"
        assert len(other.reads()) == 1

    def test_a_missing_file_is_no_copy(self, ftps, monkeypatch, tmp_path):
        got = printer_copy.copy_from_printer(bambu_on(ftps, monkeypatch, tmp_path), NAME)
        assert got.path is None and got.how == printer_copy.READ_FAILED

    def test_a_printer_that_cannot_hand_a_file_back_says_so(self):
        class NoReadBack:
            read_print_file = PrinterAdapter.read_print_file

        got = printer_copy.copy_from_printer(NoReadBack(), NAME)
        assert got.path is None and got.how == printer_copy.CANNOT_READ_BACK


class TestTheBambuIdentity:
    def test_size_and_time_stamp_come_from_the_printer(self, ftps, monkeypatch, tmp_path):
        ftps.hold(NAME, b"12345", STAMP)
        assert bambu_on(ftps, monkeypatch, tmp_path).print_file_identity(NAME) == (5, STAMP)

    def test_firmware_without_mdtm_still_reports_the_size(self, ftps, monkeypatch, tmp_path):
        ftps.answers_mdtm = False
        ftps.hold(NAME, b"12345", STAMP)
        assert bambu_on(ftps, monkeypatch, tmp_path).print_file_identity(NAME) == (5, None)
