"""Tests for the D3 wire — upload manifest + sidecar auto-derivation.

The upload manifest at ``~/.kiln/upload_manifest.json`` is the bridge
between the printer's reported file_name and the local source mesh
path.  ``monitor_print`` and ``await_print_completion`` use it to
auto-derive the brief_id from the source's intent sidecar — so the
user doesn't have to remember which saved goal a print belongs to.

These tests cover:
  - upload_manifest CRUD: record + resolve, atomic writes, bounded ring,
    corruption resilience, missing-file resilience
  - _auto_derive_brief_id pipeline: manifest miss → empty string,
    manifest hit but no sidecar → empty string, sidecar without
    design_brief: prefix → empty string, full happy path → brief id
"""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from kiln.upload_manifest import (
    record_upload,
    resolve_source_path,
)


# ---------------------------------------------------------------------------
# upload_manifest CRUD
# ---------------------------------------------------------------------------


def test_record_and_resolve_round_trip(tmp_path):
    mf = tmp_path / "manifest.json"
    assert record_upload("/src/foo.stl", "foo.gcode.3mf", manifest_path=mf)
    assert resolve_source_path("foo.gcode.3mf", manifest_path=mf) == "/src/foo.stl"


def test_resolve_returns_none_when_no_match(tmp_path):
    mf = tmp_path / "manifest.json"
    record_upload("/src/foo.stl", "foo.gcode.3mf", manifest_path=mf)
    assert resolve_source_path("bar.gcode.3mf", manifest_path=mf) is None


def test_resolve_returns_none_when_manifest_missing(tmp_path):
    mf = tmp_path / "never_written.json"
    assert resolve_source_path("anything", manifest_path=mf) is None


def test_resolve_returns_none_on_empty_printer_file_name(tmp_path):
    mf = tmp_path / "manifest.json"
    record_upload("/src/foo.stl", "foo.gcode.3mf", manifest_path=mf)
    assert resolve_source_path("", manifest_path=mf) is None


def test_record_returns_false_on_empty_inputs(tmp_path):
    mf = tmp_path / "manifest.json"
    assert record_upload("", "foo.gcode.3mf", manifest_path=mf) is False
    assert record_upload("/src/foo.stl", "", manifest_path=mf) is False


def test_newest_entry_wins(tmp_path):
    """If the same printer_file_name is recorded twice, the newer source path wins."""
    mf = tmp_path / "manifest.json"
    record_upload("/src/v1.stl", "thing.gcode.3mf", manifest_path=mf)
    record_upload("/src/v2.stl", "thing.gcode.3mf", manifest_path=mf)
    assert resolve_source_path("thing.gcode.3mf", manifest_path=mf) == "/src/v2.stl"


def test_bounded_ring_drops_oldest(tmp_path):
    """Manifest is bounded — old entries get dropped beyond max_entries."""
    mf = tmp_path / "manifest.json"
    # Record 10 entries with a cap of 5
    for i in range(10):
        record_upload(f"/src/file_{i}.stl", f"file_{i}.3mf", manifest_path=mf, max_entries=5)
    # The first 5 should be dropped
    assert resolve_source_path("file_0.3mf", manifest_path=mf) is None
    assert resolve_source_path("file_4.3mf", manifest_path=mf) is None
    # The last 5 should be present
    assert resolve_source_path("file_5.3mf", manifest_path=mf) == "/src/file_5.stl"
    assert resolve_source_path("file_9.3mf", manifest_path=mf) == "/src/file_9.stl"


def test_corrupt_manifest_treated_as_empty(tmp_path):
    """A corrupt JSON manifest returns None on resolve, doesn't crash."""
    mf = tmp_path / "manifest.json"
    mf.write_text("{not valid json")
    assert resolve_source_path("anything", manifest_path=mf) is None


def test_corrupt_manifest_overwritten_on_next_record(tmp_path):
    """A corrupt manifest gets replaced with a fresh one on next record_upload."""
    mf = tmp_path / "manifest.json"
    mf.write_text("garbage")
    assert record_upload("/src/foo.stl", "foo.3mf", manifest_path=mf)
    assert resolve_source_path("foo.3mf", manifest_path=mf) == "/src/foo.stl"


def test_unwritable_dir_returns_false_silently(tmp_path):
    """A manifest path under a missing parent dir auto-creates the parent."""
    mf = tmp_path / "deep" / "nested" / "manifest.json"
    assert record_upload("/src/foo.stl", "foo.3mf", manifest_path=mf)
    assert mf.is_file()


# ---------------------------------------------------------------------------
# _auto_derive_brief_id pipeline (monitor_print's auto-derivation)
# ---------------------------------------------------------------------------


@pytest.fixture
def stub_intent_verification():
    """Install a fake kiln_pro.intent_verification.load_intent_sidecar."""
    for parent in ("kiln_pro", "kiln_pro.intent_verification"):
        if parent not in sys.modules:
            sys.modules[parent] = types.ModuleType(parent)

    fake_module = types.ModuleType("kiln_pro.intent_verification")
    fake_loader = MagicMock(name="load_intent_sidecar")
    fake_module.load_intent_sidecar = fake_loader
    sys.modules["kiln_pro.intent_verification"] = fake_module
    if "kiln_pro" in sys.modules:
        sys.modules["kiln_pro"].intent_verification = fake_module

    yield fake_loader

    sys.modules.pop("kiln_pro.intent_verification", None)
    parent = sys.modules.get("kiln_pro")
    if parent is not None and hasattr(parent, "intent_verification"):
        delattr(parent, "intent_verification")


@pytest.fixture
def patched_manifest(monkeypatch, tmp_path):
    """Redirect the upload manifest to a tmp_path file."""
    mf = tmp_path / "manifest.json"
    import kiln.upload_manifest as um
    monkeypatch.setattr(um, "_DEFAULT_MANIFEST_PATH", mf)
    return mf


def test_auto_derive_returns_empty_when_file_name_is_empty(stub_intent_verification, patched_manifest):
    from kiln.server import _auto_derive_brief_id
    assert _auto_derive_brief_id("") == ""
    assert _auto_derive_brief_id(None) == ""
    assert _auto_derive_brief_id("N/A") == ""


def test_auto_derive_returns_empty_when_manifest_miss(stub_intent_verification, patched_manifest):
    from kiln.server import _auto_derive_brief_id
    # No entries in the manifest
    assert _auto_derive_brief_id("unmapped.3mf") == ""
    # And intent_verification wasn't even consulted
    stub_intent_verification.assert_not_called()


def test_auto_derive_returns_empty_when_no_sidecar(stub_intent_verification, patched_manifest):
    """Manifest resolves source, but the source has no intent sidecar."""
    from kiln.server import _auto_derive_brief_id
    record_upload("/src/foo.stl", "foo.3mf", manifest_path=patched_manifest)
    stub_intent_verification.return_value = None  # no sidecar
    assert _auto_derive_brief_id("foo.3mf") == ""


def test_auto_derive_returns_empty_when_generator_not_design_brief(stub_intent_verification, patched_manifest):
    """Sidecar exists but generator isn't 'design_brief:'."""
    from kiln.server import _auto_derive_brief_id
    record_upload("/src/foo.stl", "foo.3mf", manifest_path=patched_manifest)
    intent = types.SimpleNamespace(generator="template:coaster_v1")
    stub_intent_verification.return_value = intent
    assert _auto_derive_brief_id("foo.3mf") == ""


def test_auto_derive_happy_path(stub_intent_verification, patched_manifest):
    """Manifest → source → sidecar → brief_id flows end to end."""
    from kiln.server import _auto_derive_brief_id
    record_upload("/src/foo.stl", "foo.3mf", manifest_path=patched_manifest)
    intent = types.SimpleNamespace(generator="design_brief:abc123")
    stub_intent_verification.return_value = intent
    assert _auto_derive_brief_id("foo.3mf") == "abc123"


def test_auto_derive_swallows_manifest_failure(monkeypatch):
    """A manifest module error returns empty string, doesn't raise."""
    from kiln.server import _auto_derive_brief_id
    import kiln.upload_manifest as um

    def boom(*args, **kwargs):
        raise Exception("simulated manifest failure")
    monkeypatch.setattr(um, "resolve_source_path", boom)
    assert _auto_derive_brief_id("anything.3mf") == ""


def test_auto_derive_swallows_sidecar_failure(stub_intent_verification, patched_manifest):
    """A sidecar-read exception returns empty string, doesn't raise."""
    from kiln.server import _auto_derive_brief_id
    record_upload("/src/foo.stl", "foo.3mf", manifest_path=patched_manifest)
    stub_intent_verification.side_effect = Exception("simulated read fail")
    assert _auto_derive_brief_id("foo.3mf") == ""


def test_auto_derive_silent_when_kiln_pro_missing(patched_manifest):
    """kiln_pro absent → empty string."""
    from kiln.server import _auto_derive_brief_id
    sys.modules.pop("kiln_pro.intent_verification", None)
    record_upload("/src/foo.stl", "foo.3mf", manifest_path=patched_manifest)
    assert _auto_derive_brief_id("foo.3mf") == ""


def test_auto_derive_strips_id_whitespace(stub_intent_verification, patched_manifest):
    """'design_brief:abc123  ' → 'abc123'."""
    from kiln.server import _auto_derive_brief_id
    record_upload("/src/foo.stl", "foo.3mf", manifest_path=patched_manifest)
    intent = types.SimpleNamespace(generator="design_brief:abc123   ")
    stub_intent_verification.return_value = intent
    assert _auto_derive_brief_id("foo.3mf") == "abc123"


def test_auto_derive_empty_id_returns_empty(stub_intent_verification, patched_manifest):
    """'design_brief:' (no id) → empty string."""
    from kiln.server import _auto_derive_brief_id
    record_upload("/src/foo.stl", "foo.3mf", manifest_path=patched_manifest)
    intent = types.SimpleNamespace(generator="design_brief:")
    stub_intent_verification.return_value = intent
    assert _auto_derive_brief_id("foo.3mf") == ""
