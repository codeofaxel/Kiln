"""A Bambu printer declared by the name on its box reads as that model.

The camera transport, the storage folder and the AMS readings compare
against catalogue ids, so a ``printer_model`` written "Bambu Lab P2S" has
to read as ``bambu_p2s``, and the config resolver has to hand every reader
the id, not the words.
"""

from __future__ import annotations

import ftplib
import logging
from pathlib import Path
from unittest import mock

import pytest

from kiln.printer_model_resolver import invalidate_cache, resolve_printer_model
from kiln.printers.bambu import BambuAdapter

#: A serial whose prefix is the P2S's.
P2S_SERIAL = "22E0000000000"

_UNKNOWN_PRINTER = "doesn't match any known printer"


def _adapter(printer_model: str | None, serial: str = P2S_SERIAL) -> BambuAdapter:
    return BambuAdapter(host="192.0.2.10", access_code="12345678", serial=serial, printer_model=printer_model)


def _ftp_accepting(*allowed: str) -> mock.MagicMock:
    """A fake FTP whose ``cwd`` succeeds only for ``allowed`` paths."""
    ftp = mock.MagicMock()

    def _cwd(path: str) -> None:
        if path not in allowed:
            raise ftplib.error_perm("550 No such directory")

    ftp.cwd = mock.MagicMock(side_effect=_cwd)
    return ftp


@pytest.mark.parametrize("written", ["Bambu Lab P2S", "Bambu P2S", "P2S", "bambu_p2s"])
def test_every_spelling_of_the_p2s_takes_its_camera_and_its_folder(written: str) -> None:
    adapter = _adapter(written)
    assert adapter._printer_model == "bambu_p2s"
    assert adapter._camera_transport() == "rtsps"
    assert adapter.camera_needs_addon() is True
    assert adapter._detect_storage_path(_ftp_accepting("/cache", "/model", "/sdcard")) == "/cache"


def test_a_placed_declaration_outranks_the_serial() -> None:
    """The A1 mini's box name on a P2S serial: the declaration decides."""
    adapter = _adapter("Bambu Lab A1 mini")
    assert adapter._printer_model == "bambu_a1_mini"
    assert adapter._camera_transport() == "port6000"


@pytest.mark.parametrize("written", ["Bambu Lab Q9", "Bambu Lab P2X"])
def test_a_name_kiln_cannot_place_is_kept_and_gives_way_to_the_printer(written: str) -> None:
    """Never a guess (P2X is one letter off a row): kept as written, and the
    camera follows the printer's own serial instead."""
    adapter = _adapter(written)
    assert adapter._printer_model == written.lower()
    assert adapter._camera_transport() == "rtsps"


def _config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, model: str) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "active_printer: default\n"
        "printers:\n"
        "  default:\n"
        "    type: bambu\n"
        "    host: 192.0.2.10\n"
        f"    serial: {P2S_SERIAL}\n"
        f"    printer_model: {model}\n"
    )
    monkeypatch.setattr("kiln.printer_model_resolver._CONFIG_PATH", cfg)
    invalidate_cache()


def test_the_box_name_in_config_reads_as_the_catalogue_id_without_a_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    _config(tmp_path, monkeypatch, "Bambu Lab P2S")
    try:
        with caplog.at_level(logging.WARNING, logger="kiln.printer_model_resolver"):
            assert resolve_printer_model() == "bambu_p2s"
        assert not [r for r in caplog.records if _UNKNOWN_PRINTER in r.getMessage()]
    finally:
        invalidate_cache()


def test_a_name_kiln_does_not_know_is_still_warned_about(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    _config(tmp_path, monkeypatch, "Bambu Lab Q9")
    try:
        with caplog.at_level(logging.WARNING, logger="kiln.printer_model_resolver"):
            assert resolve_printer_model() == "Bambu Lab Q9"
        assert [r for r in caplog.records if _UNKNOWN_PRINTER in r.getMessage()]
    finally:
        invalidate_cache()
