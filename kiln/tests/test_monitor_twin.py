"""Tests for the monitor-twin ledger — the local retention that feeds the
web Monitor's live layer viewer.

The contract under test: the engine remembers exactly what it sliced and
what it printed, joins the two by the EXACT names it wrote itself (never a
fuzzy match), retains per-printer copies at print start, and publishes them
only on explicit request with honest refusals for every gap.
"""

from __future__ import annotations

import gzip
import io
import json
from pathlib import Path

import pytest

from kiln import monitor_twin


@pytest.fixture()
def twin_dir(tmp_path, monkeypatch):
    """Point the ledger at a private directory for each test."""
    d = tmp_path / "monitor_twin"
    monkeypatch.setattr(monitor_twin, "_TWIN_DIR", d)
    monkeypatch.setattr(monitor_twin, "_SLICES_FILE", d / "slices.json")
    monkeypatch.setattr(monitor_twin, "_ACTIVE_FILE", d / "active.json")
    return d


def _make_files(tmp_path: Path, gcode_name: str = "part.gcode") -> tuple[Path, Path]:
    mesh = tmp_path / "part.stl"
    mesh.write_bytes(b"solid part\nendsolid part\n")
    gcode = tmp_path / gcode_name
    gcode.write_text("; sliced by test\nG28\nG1 X10 Y10 E1\n")
    return mesh, gcode


class TestLedgerJoin:
    def test_exact_join_retains_gcode_and_mesh(self, twin_dir, tmp_path):
        mesh, gcode = _make_files(tmp_path)
        monitor_twin.note_sliced(str(mesh), str(gcode))
        monitor_twin.note_print_started("bambu_a1", "part.gcode")

        rec = monitor_twin.active_twin("bambu_a1")
        assert rec is not None
        assert rec["file_name"] == "part.gcode"
        assert Path(rec["gcode"]).read_text() == gcode.read_text()
        assert Path(rec["mesh"]).read_bytes() == mesh.read_bytes()
        # Retained copies live in the twin dir, not the original locations —
        # the slicer's temp file may be cleaned long before the print ends.
        assert Path(rec["gcode"]).parent == twin_dir

    def test_wrapped_name_joins_back_to_raw_gcode(self, twin_dir, tmp_path):
        """A Bambu printer knows the job by the WRAP's name; the retained
        toolpath must be the raw G-code inside it."""
        mesh, gcode = _make_files(tmp_path)
        wrapped = tmp_path / "part.3mf"
        wrapped.write_bytes(b"PK\x03\x04not-really-a-zip")
        monitor_twin.note_sliced(str(mesh), str(gcode))
        monitor_twin.note_wrapped(str(gcode), str(wrapped))
        monitor_twin.note_print_started("bambu_a1", "part.3mf")

        rec = monitor_twin.active_twin("bambu_a1")
        assert rec is not None
        assert Path(rec["gcode"]).read_text() == gcode.read_text()

    def test_no_match_is_honestly_empty(self, twin_dir, tmp_path):
        """A print of a file Kiln never sliced retains nothing — and clears
        any previous job's twin so it can't decorate the new print."""
        mesh, gcode = _make_files(tmp_path)
        monitor_twin.note_sliced(str(mesh), str(gcode))
        monitor_twin.note_print_started("bambu_a1", "part.gcode")
        monitor_twin.note_print_started("bambu_a1", "mystery_from_sd_card.gcode")

        rec = monitor_twin.active_twin("bambu_a1")
        assert rec is not None
        assert rec["file_name"] == "mystery_from_sd_card.gcode"
        assert rec["gcode"] is None

    def test_newest_slice_wins_for_duplicate_names(self, twin_dir, tmp_path):
        mesh, old = _make_files(tmp_path)
        newer_dir = tmp_path / "again"
        newer_dir.mkdir()
        newer = newer_dir / "part.gcode"
        newer.write_text("; resliced\nG28\nG1 X99 Y99 E2\n")
        monitor_twin.note_sliced(str(mesh), str(old))
        monitor_twin.note_sliced(str(mesh), str(newer))
        monitor_twin.note_print_started("p1", "part.gcode")

        rec = monitor_twin.active_twin("p1")
        assert Path(rec["gcode"]).read_text() == newer.read_text()

    def test_single_printer_resolves_without_a_name(self, twin_dir, tmp_path):
        mesh, gcode = _make_files(tmp_path)
        monitor_twin.note_sliced(str(mesh), str(gcode))
        monitor_twin.note_print_started("only_printer", "part.gcode")
        assert monitor_twin.active_twin(None)["file_name"] == "part.gcode"

    def test_ledger_is_ring_buffered(self, twin_dir, tmp_path):
        mesh, gcode = _make_files(tmp_path)
        for _ in range(monitor_twin._MAX_SLICE_ENTRIES + 5):
            monitor_twin.note_sliced(str(mesh), str(gcode))
        entries = json.loads((twin_dir / "slices.json").read_text())
        assert len(entries) == monitor_twin._MAX_SLICE_ENTRIES

    def test_note_functions_never_raise(self, twin_dir):
        # Paths that don't exist, names that are empty — bookkeeping is
        # best-effort by contract and must never blow up a print path.
        monitor_twin.note_sliced("", "")
        monitor_twin.note_wrapped("/nope.gcode", "/nope.3mf")
        monitor_twin.note_print_started("", "")


class TestPublish:
    def test_no_active_twin(self, twin_dir):
        out = monitor_twin.publish()
        assert out["success"] is False
        assert out["code"] == "NO_ACTIVE_TWIN"

    def test_unretained_print_refuses_with_reason(self, twin_dir, tmp_path):
        monitor_twin.note_print_started("p1", "sd_card_job.gcode")
        out = monitor_twin.publish("p1")
        assert out["success"] is False
        assert out["code"] == "TWIN_NOT_RETAINED"
        assert out["file_name"] == "sd_card_job.gcode"

    def test_sign_in_required(self, twin_dir, tmp_path, monkeypatch):
        mesh, gcode = _make_files(tmp_path)
        monitor_twin.note_sliced(str(mesh), str(gcode))
        monitor_twin.note_print_started("p1", "part.gcode")

        from kiln import auth_session

        monkeypatch.setattr(
            auth_session,
            "resolve_api_bearer",
            lambda *a, **k: auth_session.ApiBearer(token="", state="signed_out"),
        )
        out = monitor_twin.publish("p1")
        assert out["success"] is False
        assert out["code"] == "SIGN_IN_REQUIRED"

    def test_publish_uploads_gzipped_toolpath(self, twin_dir, tmp_path, monkeypatch):
        mesh, gcode = _make_files(tmp_path)
        monitor_twin.note_sliced(str(mesh), str(gcode))
        monitor_twin.note_print_started("p1", "part.gcode")

        from kiln import auth_session

        monkeypatch.setattr(
            auth_session,
            "resolve_api_bearer",
            lambda *a, **k: auth_session.ApiBearer(token="tok123", state="license"),
        )

        captured: dict = {}

        class _Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(request, timeout=0):
            captured["url"] = request.full_url
            captured["auth"] = request.headers.get("Authorization")
            captured["body"] = request.data
            return _Resp(
                json.dumps(
                    {
                        "status": "success",
                        "artifact_token": "tw_abc",
                        "stl_url": "/api/artifact/tw_abc",
                        "gcode_url": "/api/artifact/tw_abc?asset=gcode",
                        "format": "stl",
                        "expires_in": 21600,
                    }
                ).encode()
            )

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

        out = monitor_twin.publish("p1")
        assert out["success"] is True
        assert out["artifact_token"] == "tw_abc"
        assert out["gcode_url"] == "/api/artifact/tw_abc?asset=gcode"
        assert out["file_name"] == "part.gcode"
        assert captured["url"].endswith("/api/print-twin")
        assert captured["auth"] == "Bearer tok123"
        # The toolpath crosses the wire as a gzip container whose content is
        # the exact retained bytes — the layer viewer's input, verbatim.
        body = captured["body"]
        start = body.find(b"\x1f\x8b")
        assert start != -1
        end = body.find(b"\r\n--kiln-twin-", start)
        assert gzip.decompress(body[start:end]) == gcode.read_bytes()
        # The mesh rides along when retained.
        assert b'name="mesh"' in body

    def test_server_refusal_passes_through(self, twin_dir, tmp_path, monkeypatch):
        mesh, gcode = _make_files(tmp_path)
        monitor_twin.note_sliced(str(mesh), str(gcode))
        monitor_twin.note_print_started("p1", "part.gcode")

        from kiln import auth_session

        monkeypatch.setattr(
            auth_session,
            "resolve_api_bearer",
            lambda *a, **k: auth_session.ApiBearer(token="tok", state="license"),
        )

        class _Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        monkeypatch.setattr(
            "urllib.request.urlopen",
            lambda *a, **k: _Resp(
                json.dumps(
                    {"status": "error", "code": "ACCOUNT_REQUIRED", "error": "no"}
                ).encode()
            ),
        )
        out = monitor_twin.publish("p1")
        assert out["success"] is False
        assert out["code"] == "ACCOUNT_REQUIRED"


class TestPluginSurface:
    def test_plugin_registers_the_tool(self):
        from kiln.plugin_loader import ToolPlugin
        from kiln.plugins import monitor_twin_tools

        assert isinstance(monitor_twin_tools.plugin, ToolPlugin)

        registered: list[str] = []

        class _FakeMcp:
            def tool(self, *a, **k):
                def deco(fn):
                    registered.append(fn.__name__)
                    return fn

                return deco

        monitor_twin_tools.plugin.register(_FakeMcp())
        assert registered == ["publish_print_twin"]


class TestMultiPrinterAndCaps:
    def test_unnamed_resolves_the_most_recent_start(self, twin_dir, tmp_path):
        """Owning two machines must not kill the twin: the unnamed ask
        answers with the print that started last — a fact from our own
        start_print stamps, not a guess."""
        mesh, gcode = _make_files(tmp_path)
        monitor_twin.note_sliced(str(mesh), str(gcode))
        monitor_twin.note_print_started("older_printer", "part.gcode")
        # Force distinct timestamps even on a coarse clock.
        active = json.loads((twin_dir / "active.json").read_text())
        active["older_printer"]["started_at"] = "2020-01-01T00:00:00+00:00"
        (twin_dir / "active.json").write_text(json.dumps(active))
        monitor_twin.note_sliced(str(mesh), str(gcode))
        monitor_twin.note_print_started("newer_printer", "part.gcode")

        rec = monitor_twin.active_twin(None)
        assert rec is not None
        assert rec["printer_name"] == "newer_printer"
        # A named ask still answers only for that name.
        assert monitor_twin.active_twin("older_printer")["printer_name"] == "older_printer"

    def test_oversized_mesh_is_not_retained_but_gcode_is(
        self, twin_dir, tmp_path, monkeypatch
    ):
        """A mesh past the server's own accept-cap is dead weight: it can
        only be uploaded and refused, so it is not retained at all."""
        monkeypatch.setattr(monitor_twin, "_MAX_MESH_BYTES", 4)
        mesh, gcode = _make_files(tmp_path)
        monitor_twin.note_sliced(str(mesh), str(gcode))
        monitor_twin.note_print_started("p1", "part.gcode")
        rec = monitor_twin.active_twin("p1")
        assert rec["gcode"] is not None
        assert rec["mesh"] is None

    def test_oversized_toolpath_refuses_before_any_upload(
        self, twin_dir, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(monitor_twin, "_MAX_GCODE_GZ_UPLOAD", 8)
        mesh, gcode = _make_files(tmp_path)
        monitor_twin.note_sliced(str(mesh), str(gcode))
        monitor_twin.note_print_started("p1", "part.gcode")

        from kiln import auth_session

        monkeypatch.setattr(
            auth_session,
            "resolve_api_bearer",
            lambda *a, **k: auth_session.ApiBearer(token="tok", state="license"),
        )

        def _no_network(*a, **k):  # pragma: no cover - the assertion IS no call
            raise AssertionError("an oversized twin must never reach the wire")

        monkeypatch.setattr("urllib.request.urlopen", _no_network)
        out = monitor_twin.publish("p1")
        assert out["success"] is False
        assert out["code"] == "TWIN_TOO_LARGE"
