"""Tests for kiln.community_autofire — silent, geometry-keyed community
auto-contribution on monitored print completion (Slice A).

Pins: the signature comes from fingerprint_model (NOT a file hash), non-quality
outcomes are skipped, geometry-unavailable is a fail-safe skip (never a
file-hash stand-in), and both live wiring paths (watch_print_status'
_PrintWatcher._finish and await_print_completion) fire the contribution on a
real completion.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

import pytest

# --------------------------------------------------------------------------
# geometric_signature_for
# --------------------------------------------------------------------------


def test_signature_empty_for_missing_name():
    from kiln import community_autofire as ca

    assert ca.geometric_signature_for(None) == ""
    assert ca.geometric_signature_for("") == ""
    assert ca.geometric_signature_for("N/A") == ""


def test_signature_empty_when_source_unresolved():
    from kiln import community_autofire as ca

    with mock.patch("kiln.upload_manifest.resolve_source_path", return_value=None):
        assert ca.geometric_signature_for("plate.gcode") == ""


def test_signature_from_fingerprint(tmp_path):
    from kiln import community_autofire as ca

    src = tmp_path / "m.stl"
    src.write_text("solid x")  # existence is what matters; fingerprint is mocked
    fp = SimpleNamespace(geometric_signature="abc123def4567890")
    with mock.patch("kiln.upload_manifest.resolve_source_path", return_value=str(src)), \
         mock.patch("kiln.print_dna.fingerprint_model", return_value=fp):
        assert ca.geometric_signature_for("plate.gcode") == "abc123def4567890"


def test_signature_empty_on_fingerprint_error(tmp_path):
    """Non-STL / unparseable source → fail-safe empty, NOT a crash and NOT a
    file-hash substitute."""
    from kiln import community_autofire as ca

    src = tmp_path / "m.3mf"
    src.write_text("not an stl")
    with mock.patch("kiln.upload_manifest.resolve_source_path", return_value=str(src)), \
         mock.patch(
             "kiln.print_dna.fingerprint_model", side_effect=ValueError("no triangles")
         ):
        assert ca.geometric_signature_for("plate.gcode") == ""


# --------------------------------------------------------------------------
# auto_contribute_completion
# --------------------------------------------------------------------------


def test_skips_non_quality_outcomes():
    from kiln import community_autofire as ca

    with mock.patch("kiln.community_outbox.contribute") as contrib:
        for oc in ("cancelled", "timeout", "paused", "running", ""):
            r = ca.auto_contribute_completion(outcome=oc, printer_file_name="x.gcode")
            assert r == {"contributed": False, "reason": "non_quality_outcome"}
        contrib.assert_not_called()


def test_skips_when_no_geometry():
    from kiln import community_autofire as ca

    with mock.patch(
        "kiln.community_autofire.geometric_signature_for", return_value=""
    ), mock.patch("kiln.community_outbox.contribute") as contrib:
        r = ca.auto_contribute_completion(outcome="completed", printer_file_name="x.gcode")
    assert r == {"contributed": False, "reason": "no_geometry"}
    contrib.assert_not_called()


def test_completed_contributes_success_with_geo_signature():
    from kiln import community_autofire as ca

    with mock.patch(
        "kiln.community_autofire.geometric_signature_for",
        return_value="geo16char0000000",
    ), mock.patch(
        "kiln.community_outbox.contribute", return_value={"queued": True}
    ) as contrib:
        r = ca.auto_contribute_completion(
            outcome="completed",
            printer_file_name="plate.gcode",
            job_id="job-7",
            printer_model="Bambu A1",
            material="PLA",
            print_time_seconds=3600,
        )
    assert r["contributed"] is True
    key, record = contrib.call_args.args
    # The canonical per-print key, minted by community_outbox for BOTH
    # contribution paths — record_print_outcome mints this same key for this
    # job, which is what collapses the double-ship.
    assert key == "print:job-7"
    assert record["geometric_signature"] == "geo16char0000000"
    assert record["outcome"] == "success"  # completed -> success
    # The door canonicalizes: one noun per machine, everywhere the corpus
    # is touched ("Bambu A1" and "bambu_a1" were both live in prod).
    assert record["printer_model"] == "bambu_a1"
    assert record["material"] == "PLA"
    assert record["print_time_seconds"] == 3600
    assert "file_hash" not in record  # geometric signature only


def test_failed_contributes_failed_with_defaults():
    from kiln import community_autofire as ca

    with mock.patch(
        "kiln.community_autofire.geometric_signature_for",
        return_value="geo16char0000000",
    ), mock.patch(
        "kiln.community_outbox.contribute", return_value={"queued": True}
    ) as contrib:
        ca.auto_contribute_completion(outcome="failed", printer_file_name="p.gcode")
    _key, record = contrib.call_args.args
    assert record["outcome"] == "failed"
    assert record["printer_model"] == "unknown"
    assert record["material"] == "unknown"
    assert record["print_time_seconds"] == 0


def test_never_raises_on_contribute_error():
    from kiln import community_autofire as ca

    with mock.patch(
        "kiln.community_autofire.geometric_signature_for",
        return_value="geo16char0000000",
    ), mock.patch(
        "kiln.community_outbox.contribute", side_effect=RuntimeError("boom")
    ):
        r = ca.auto_contribute_completion(outcome="completed", printer_file_name="p.gcode")
    assert r == {"contributed": False, "reason": "error"}


# --------------------------------------------------------------------------
# Live wiring: watch_print_status (_PrintWatcher._finish)
# --------------------------------------------------------------------------


def test_watcher_finish_auto_contributes_on_completion():
    from kiln.plugins.monitoring_tools import _PrintWatcher

    job = SimpleNamespace(
        to_dict=lambda: {
            "file_name": "plate.gcode",
            "material": "PETG",
            "print_time_seconds": 1200,
            "job_id": None,
        }
    )
    adapter = SimpleNamespace(
        get_job=lambda: job,
        get_printer_info=lambda: SimpleNamespace(model="Prusa MK4"),
    )
    watcher = _PrintWatcher("watch-1", adapter, "my-printer")
    with mock.patch("kiln.community_autofire.auto_contribute_completion") as auto:
        watcher._finish({"outcome": "completed", "elapsed_seconds": 1200})
    auto.assert_called_once()
    kw = auto.call_args.kwargs
    assert kw["outcome"] == "completed"
    assert kw["printer_file_name"] == "plate.gcode"
    assert kw["printer_model"] == "Prusa MK4"
    assert kw["material"] == "PETG"


# --------------------------------------------------------------------------
# Live wiring: await_print_completion (direct-printer idle -> completed)
# --------------------------------------------------------------------------


def test_await_print_completion_auto_contributes_on_idle(monkeypatch):
    import kiln.server as srv
    from kiln.printers import PrinterStatus

    job = SimpleNamespace(
        completion=100.0,
        to_dict=lambda: {
            "file_name": "plate.gcode",
            "material": "PLA",
            "print_time_seconds": 900,
        },
    )
    adapter = SimpleNamespace(
        get_job=lambda: job,
        get_state=lambda: SimpleNamespace(state=PrinterStatus.IDLE),
        get_printer_info=lambda: SimpleNamespace(model="Bambu A1"),
    )
    monkeypatch.setattr(srv, "_check_auth", lambda *a, **k: None)
    monkeypatch.setattr(srv, "_get_adapter", lambda *a, **k: adapter)
    monkeypatch.setattr(srv, "_resolve_brief_context", lambda *a, **k: None)
    with mock.patch("kiln.community_autofire.auto_contribute_completion") as auto:
        result = srv.await_print_completion(
            job_id=None, timeout=30, poll_interval=0, brief_id="b"
        )
    assert result["outcome"] == "completed"
    auto.assert_called_once()
    kw = auto.call_args.kwargs
    assert kw["outcome"] == "completed"
    assert kw["printer_file_name"] == "plate.gcode"
    assert kw["printer_model"] == "Bambu A1"


# --------------------------------------------------------------------------
# 3MF source support — a non-STL source is round-tripped through the STL path
# so a 3MF and an STL of the same model hash IDENTICALLY (they must aggregate
# together).  trimesh is an optional dep; the consistency tests skip without
# it, but the fail-safe tests confirm graceful degradation when it is absent.
# --------------------------------------------------------------------------


def test_3mf_signature_matches_stl_twin(tmp_path):
    """The core A5 guarantee: a 3MF of a model hashes identically to its STL."""
    trimesh = pytest.importorskip("trimesh")
    from kiln import community_autofire as ca
    from kiln.print_dna import fingerprint_model

    mesh = trimesh.creation.box(extents=(20, 30, 12))
    stl = tmp_path / "m.stl"
    stl.write_bytes(mesh.export(file_type="stl"))
    tmf = tmp_path / "m.3mf"
    tmf.write_bytes(mesh.export(file_type="3mf"))

    sig_stl = fingerprint_model(str(stl)).geometric_signature
    sig_3mf = ca._signature_via_mesh_load(str(tmf))
    assert sig_stl and sig_3mf == sig_stl


def test_geometric_signature_for_routes_3mf_through_mesh_load(tmp_path):
    """End-to-end: geometric_signature_for on a .3mf source yields the same
    signature as the STL twin (STL via native path, 3MF via mesh-load)."""
    trimesh = pytest.importorskip("trimesh")
    from kiln import community_autofire as ca

    mesh = trimesh.creation.icosphere(subdivisions=2, radius=11)
    stl = tmp_path / "m.stl"
    stl.write_bytes(mesh.export(file_type="stl"))
    tmf = tmp_path / "m.3mf"
    tmf.write_bytes(mesh.export(file_type="3mf"))

    with mock.patch("kiln.upload_manifest.resolve_source_path", return_value=str(stl)):
        sig_stl = ca.geometric_signature_for("plate.gcode")
    with mock.patch("kiln.upload_manifest.resolve_source_path", return_value=str(tmf)):
        sig_3mf = ca.geometric_signature_for("plate.gcode")
    assert sig_stl and sig_3mf == sig_stl


def test_rigid_transformed_3mf_still_matches(tmp_path):
    """A plated (translated + rotated) 3MF still matches the as-designed STL —
    the signature is rigid-invariant, so build-plate placement doesn't fork it."""
    trimesh = pytest.importorskip("trimesh")
    from kiln import community_autofire as ca
    from kiln.print_dna import fingerprint_model

    mesh = trimesh.creation.box(extents=(18, 24, 9))
    stl = tmp_path / "m.stl"
    stl.write_bytes(mesh.export(file_type="stl"))
    sig_stl = fingerprint_model(str(stl)).geometric_signature

    placed = mesh.copy()
    rot = trimesh.transformations.rotation_matrix(0.7, [0.0, 0.0, 1.0])
    trans = trimesh.transformations.translation_matrix([120.0, -40.0, 6.0])
    placed.apply_transform(trans @ rot)
    tmf = tmp_path / "placed.3mf"
    tmf.write_bytes(placed.export(file_type="3mf"))

    assert ca._signature_via_mesh_load(str(tmf)) == sig_stl


def test_corrupt_3mf_skips(tmp_path):
    """A corrupt / non-mesh 3MF is a fail-safe skip ("") — never a garbage sig."""
    from kiln import community_autofire as ca

    bad = tmp_path / "bad.3mf"
    bad.write_bytes(b"PK\x03\x04 not a real 3mf payload")
    assert ca._signature_via_mesh_load(str(bad)) == ""


def test_mesh_load_skips_when_trimesh_absent(tmp_path, monkeypatch):
    """If trimesh isn't installed the 3MF path skips gracefully (no worse than
    pre-A5), rather than raising."""
    import builtins

    from kiln import community_autofire as ca

    real_import = builtins.__import__

    def _no_trimesh(name, *args, **kwargs):
        if name == "trimesh":
            raise ImportError("trimesh not installed (simulated)")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_trimesh)
    tmf = tmp_path / "x.3mf"
    tmf.write_bytes(b"PK\x03\x04 anything")
    assert ca._signature_via_mesh_load(str(tmf)) == ""
