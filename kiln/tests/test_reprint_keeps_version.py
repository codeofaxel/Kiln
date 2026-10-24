"""A print started without naming its version still lands on the version it is.

Reprints, retries and quick prints slice a model file and start it without
saying which kept version the file is.  The start template now reads that off
this computer's own records, exactly: the slice ledger names the model the
printer-side file was sliced from, and the design history names the version
whose kept model is those very bytes.  Never a file name.

A/B: every test here fails before ``_version_of_sliced_model`` existed (the
row's version stayed blank).
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import time
from typing import Any

import pytest


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("KILN_DB_PATH", str(tmp_path / "kiln.db"))
    monkeypatch.setenv("HOME", str(tmp_path))
    import kiln.history_vault as hv
    import kiln.monitor_twin as mt
    import kiln.persistence as persistence

    monkeypatch.setattr(persistence, "_db", None, raising=False)
    monkeypatch.setattr(hv, "_kiln_home", lambda: tmp_path / ".kiln")
    twin = tmp_path / ".kiln" / "monitor_twin"
    twin.mkdir(parents=True)
    monkeypatch.setattr(mt, "_TWIN_DIR", twin)
    monkeypatch.setattr(mt, "_SLICES_FILE", twin / "slices.json")
    yield tmp_path
    monkeypatch.setattr(persistence, "_db", None, raising=False)


def _keep_version(home, version_id: str, design_id: str, data: bytes) -> None:
    """A version in this computer's design history whose model is *data*."""
    from kiln.history_vault import HistoryVault

    HistoryVault().close()  # creates the schema
    conn = sqlite3.connect(str(home / ".kiln" / "history_vault.db"))
    conn.execute(
        "INSERT INTO versions (version_id, design_id, parent_version_id, created_at, recipe, mesh_sha256) "
        "VALUES (?, ?, NULL, ?, '{}', ?)",
        (version_id, design_id, time.time(), hashlib.sha256(data).hexdigest()),
    )
    conn.commit()
    conn.close()


def _slice(home, model_bytes: bytes, name: str = "bracket") -> str:
    """Write a model and note it was sliced to <name>.gcode, as the slicer does."""
    from kiln.monitor_twin import note_sliced

    model = home / f"{name}.stl"
    model.write_bytes(model_bytes)
    gcode = home / f"{name}.gcode"
    gcode.write_text("; gcode")
    note_sliced(str(model), str(gcode))
    return str(gcode)


def _adapter():
    from kiln.printers.base import PrinterAdapter, PrintResult

    class _Fake(PrinterAdapter):
        name = "shop-a1"
        serial = "01P00A0000"

        def declared_printer_model(self) -> str:
            return ""

        def _start_print_impl(self, file_name: str, **kwargs: Any) -> PrintResult:
            return PrintResult(success=True, message="started")

        def connect(self): ...
        def disconnect(self): ...
        def get_status(self): ...
        def get_printer_info(self): ...
        def list_files(self): ...
        def upload_file(self, file_path): ...
        def cancel_print(self): ...
        def pause_print(self): ...
        def resume_print(self): ...

    _Fake.__abstractmethods__ = frozenset()
    return _Fake()


def _row():
    from kiln.persistence import get_db

    (row,) = get_db().list_unresolved_outcomes(printer_name="shop-a1")
    return row


MODEL = b"solid bracket v7 bytes"


def test_a_reprint_lands_on_the_version_its_model_is(home):
    _keep_version(home, "v-7", "bracket-design", MODEL)
    _slice(home, MODEL)
    assert _adapter().start_print("bracket.gcode").success
    row = _row()
    assert (row["version_id"], row["design_id"]) == ("v-7", "bracket-design")


def test_the_wrapped_file_a_bambu_receives_is_traced_too(home):
    from kiln.monitor_twin import note_wrapped

    _keep_version(home, "v-7", "bracket-design", MODEL)
    gcode = _slice(home, MODEL)
    note_wrapped(gcode, str(home / "bracket.gcode.3mf"))
    _adapter().start_print("bracket.gcode.3mf")
    assert _row()["version_id"] == "v-7"


def test_an_edited_model_with_the_same_name_is_no_version(home):
    _keep_version(home, "v-7", "bracket-design", MODEL)
    _slice(home, b"edited by hand after the keep")
    _adapter().start_print("bracket.gcode")
    row = _row()
    assert row["version_id"] is None and row["design_id"] is None


def test_two_versions_with_one_model_name_the_design_not_a_guess(home):
    _keep_version(home, "v-7", "bracket-design", MODEL)
    _keep_version(home, "v-8", "bracket-design", MODEL)
    _slice(home, MODEL)
    _adapter().start_print("bracket.gcode")
    row = _row()
    assert (row["version_id"], row["design_id"]) == (None, "bracket-design")


def test_a_named_version_wins_and_a_file_kiln_did_not_slice_is_nothing(home):
    _keep_version(home, "v-7", "bracket-design", MODEL)
    _slice(home, MODEL)
    _adapter().start_print("bracket.gcode", version_id="v-9", design_id="other")
    assert _row()["version_id"] == "v-9"
    from kiln.persistence import get_db

    get_db()._conn.execute("DELETE FROM print_outcomes")
    get_db()._conn.commit()
    _adapter().start_print("downloaded.gcode")
    assert _row()["version_id"] is None


def test_no_design_history_on_this_computer_is_nothing(home):
    _slice(home, MODEL)
    _adapter().start_print("bracket.gcode")
    assert _row()["version_id"] is None
    assert not os.path.exists(home / ".kiln" / "history_vault.db")
