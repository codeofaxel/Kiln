"""Tests for the B11 brief-passthrough wire on iterate_design.

The iteration tool now accepts an optional brief_id; when supplied AND
the winning iteration produced a mesh, a ``design_brief:<id>`` intent
sidecar is written next to the file via kiln-pro's explicit_attach
helper.  Best-effort: kiln-pro absent silently no-ops.

These tests focus on :func:`_attach_brief_to_iteration_result` — the
helper that does the sidecar write — since mocking the full generation
provider end-to-end is heavyweight.

To exercise the kiln-pro-installed branch without depending on
kiln-pro actually being importable in CI, we stub
``kiln_pro.design_brief.explicit_attach`` into ``sys.modules``.
"""
from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

import pytest

from kiln.plugins.design_reasoning_tools import _attach_brief_to_iteration_result


@pytest.fixture
def stubbed_explicit_attach():
    """Install a fake kiln_pro.design_brief.explicit_attach module."""
    # Ensure the parent packages exist as stub modules too.
    for parent in ("kiln_pro", "kiln_pro.design_brief"):
        if parent not in sys.modules:
            sys.modules[parent] = types.ModuleType(parent)

    fake_module = types.ModuleType("kiln_pro.design_brief.explicit_attach")
    fake_attach = MagicMock(name="attach_brief_id_to_result")
    fake_module.attach_brief_id_to_result = fake_attach
    # Make it discoverable via attribute access on the parent too
    sys.modules["kiln_pro.design_brief.explicit_attach"] = fake_module
    sys.modules["kiln_pro.design_brief"].explicit_attach = fake_module

    yield fake_attach

    # Cleanup: drop our stubs (don't touch real modules if they exist)
    sys.modules.pop("kiln_pro.design_brief.explicit_attach", None)
    parent_mod = sys.modules.get("kiln_pro.design_brief")
    if parent_mod is not None and hasattr(parent_mod, "explicit_attach"):
        delattr(parent_mod, "explicit_attach")


def test_attach_pulls_mesh_path_from_result_envelope(stubbed_explicit_attach):
    """Best-result with a `result.local_path` triggers the sidecar write."""
    _attach_brief_to_iteration_result(
        "abc123", {"result": {"local_path": "/tmp/best.stl"}},
    )
    stubbed_explicit_attach.assert_called_once_with(
        "abc123", {"local_path": "/tmp/best.stl"},
    )


def test_attach_falls_back_to_job_envelope(stubbed_explicit_attach):
    """Some providers return mesh path under `job.local_path` instead."""
    _attach_brief_to_iteration_result(
        "abc123", {"job": {"local_path": "/tmp/best.stl"}},
    )
    stubbed_explicit_attach.assert_called_once_with(
        "abc123", {"local_path": "/tmp/best.stl"},
    )


def test_attach_prefers_result_envelope_when_both_present(stubbed_explicit_attach):
    _attach_brief_to_iteration_result(
        "abc123",
        {
            "result": {"local_path": "/tmp/result.stl"},
            "job": {"local_path": "/tmp/job.stl"},
        },
    )
    stubbed_explicit_attach.assert_called_once_with(
        "abc123", {"local_path": "/tmp/result.stl"},
    )


def test_attach_skips_when_no_mesh_path(stubbed_explicit_attach):
    """No mesh path anywhere — silent no-op, helper isn't called."""
    _attach_brief_to_iteration_result(
        "abc123", {"result": {"some_other_key": "no_mesh_here"}},
    )
    stubbed_explicit_attach.assert_not_called()


def test_attach_swallows_attach_errors(stubbed_explicit_attach):
    """A failure inside explicit_attach must NOT raise — iteration
    result is still valid even if the goal didn't attach."""
    stubbed_explicit_attach.side_effect = Exception("simulated failure")
    # Must NOT raise
    _attach_brief_to_iteration_result(
        "abc123", {"result": {"local_path": "/tmp/best.stl"}},
    )


def test_attach_silent_when_kiln_pro_unavailable():
    """When kiln_pro.design_brief.explicit_attach can't be imported,
    helper silently no-ops (real-world: kiln-pro not installed at all)."""
    # Ensure no stub is installed — the actual import in the helper
    # will raise ImportError because explicit_attach doesn't exist on
    # any branch the running interpreter sees (cluster 2 branch only).
    sys.modules.pop("kiln_pro.design_brief.explicit_attach", None)
    # Must NOT raise
    _attach_brief_to_iteration_result(
        "abc123", {"result": {"local_path": "/tmp/best.stl"}},
    )
