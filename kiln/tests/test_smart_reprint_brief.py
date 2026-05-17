"""Tests for the D2 wire — smart_reprint auto-derives brief_id from the
source model's intent sidecar so a reprint of a brief-attached design
keeps the saved-goal link without the user re-specifying it.

The full smart_reprint flow involves AMS detection, slicer dispatch,
and printer comms — too heavy to mock end-to-end.  These tests cover
the BRIEF DERIVATION LOGIC in isolation: given an intent sidecar
shaped ``design_brief:<id>`` next to a model file, the helper extracts
the id; given anything else (no sidecar, non-brief generator, kiln-pro
missing) the helper returns None.

The extraction logic lives inline in smart_reprint (server.py) — these
tests replicate the same parsing rules so a regression in the inline
code would fail an equivalent unit test.
"""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest


def _extract_brief_id_from_sidecar(found_path: str) -> str | None:
    """Mirror of the inline derivation in smart_reprint.

    Kept as a module-level helper so unit tests can pin the contract
    without spinning up the full smart_reprint dispatch (slicer +
    printer comms).  Any change to this logic in server.py should
    update this mirror in lockstep.
    """
    try:
        from kiln_pro.intent_verification import load_intent_sidecar
        intent = load_intent_sidecar(found_path)
        if (
            intent is not None
            and isinstance(intent.generator, str)
            and intent.generator.startswith("design_brief:")
        ):
            candidate = intent.generator.split(":", 1)[1].strip()
            if candidate:
                return candidate
    except Exception:
        pass
    return None


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
    # Also expose at the parent for the import path to resolve cleanly
    if "kiln_pro" in sys.modules:
        sys.modules["kiln_pro"].intent_verification = fake_module

    yield fake_loader

    sys.modules.pop("kiln_pro.intent_verification", None)
    parent = sys.modules.get("kiln_pro")
    if parent is not None and hasattr(parent, "intent_verification"):
        delattr(parent, "intent_verification")


def _fake_intent(generator: str):
    """Tiny stand-in for a DeclaredIntent with just the .generator field."""
    intent = types.SimpleNamespace()
    intent.generator = generator
    return intent


def test_derives_brief_id_from_design_brief_sidecar(stub_intent_verification):
    """A sidecar with generator='design_brief:abc123' yields 'abc123'."""
    stub_intent_verification.return_value = _fake_intent("design_brief:abc123")
    assert _extract_brief_id_from_sidecar("/any/path.stl") == "abc123"


def test_returns_none_when_generator_is_other(stub_intent_verification):
    """An intent from a non-brief generator (template, manual, ...) yields None."""
    stub_intent_verification.return_value = _fake_intent("template:coaster_v1")
    assert _extract_brief_id_from_sidecar("/any/path.stl") is None


def test_returns_none_when_no_sidecar(stub_intent_verification):
    """load_intent_sidecar returns None when the file has no sidecar."""
    stub_intent_verification.return_value = None
    assert _extract_brief_id_from_sidecar("/any/path.stl") is None


def test_returns_none_when_generator_prefix_lacks_id(stub_intent_verification):
    """Malformed 'design_brief:' (empty id) → None, not empty string."""
    stub_intent_verification.return_value = _fake_intent("design_brief:")
    assert _extract_brief_id_from_sidecar("/any/path.stl") is None


def test_returns_none_when_generator_prefix_whitespace_id(stub_intent_verification):
    """'design_brief:   ' (whitespace only) → None."""
    stub_intent_verification.return_value = _fake_intent("design_brief:   ")
    assert _extract_brief_id_from_sidecar("/any/path.stl") is None


def test_returns_none_when_generator_is_not_string(stub_intent_verification):
    """A non-string generator (defensive — substrate types it str, but
    a third-party verifier could violate the contract) → None, no crash."""
    stub_intent_verification.return_value = _fake_intent(None)
    assert _extract_brief_id_from_sidecar("/any/path.stl") is None


def test_returns_none_when_load_raises(stub_intent_verification):
    """A sidecar-load exception (corrupt JSON, IO error) → None silently."""
    stub_intent_verification.side_effect = Exception("simulated read failure")
    assert _extract_brief_id_from_sidecar("/any/path.stl") is None


def test_returns_none_when_kiln_pro_unavailable():
    """No kiln-pro installed at all → silent None.  Real-world: free-tier
    user who never had design_session ergonomics in the first place."""
    # Drop any stub so the inline import fails
    sys.modules.pop("kiln_pro.intent_verification", None)
    assert _extract_brief_id_from_sidecar("/any/path.stl") is None


def test_brief_id_strips_trailing_whitespace(stub_intent_verification):
    """'design_brief:abc123\\n' → 'abc123' (strip the tail whitespace)."""
    stub_intent_verification.return_value = _fake_intent("design_brief:abc123  ")
    assert _extract_brief_id_from_sidecar("/any/path.stl") == "abc123"
