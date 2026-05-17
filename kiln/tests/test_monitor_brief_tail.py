"""Tests for the B10 brief-tail wire on monitor_print + await_print_completion.

Both tools now accept an optional brief_id; when the brief resolves
via kiln-pro's brief_context_dict, the resulting report carries the
saved-goal context — a single ``Goal: ...`` line for monitor_print's
text output, a ``design_goal`` dict in await_print_completion's
terminal-state response.

Tests focus on the two pure helpers — :func:`_resolve_brief_context`
and :func:`_format_goal_line_for_monitor` — since mocking the
printer-state machinery is heavyweight.  Integration coverage of the
loop attaching design_goal across all 7 terminal-state returns is
implicit via the helper test plus visual inspection of the wrap
pattern in server.py.

The kiln-pro module path is stubbed into sys.modules so tests work
regardless of whether kiln-pro is installed in the test environment.
"""
from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

import pytest

from kiln.server import (
    _format_goal_line_for_monitor,
    _resolve_brief_context,
)


@pytest.fixture
def stubbed_brief_context():
    """Install a fake kiln_pro.design_brief.context module."""
    for parent in ("kiln_pro", "kiln_pro.design_brief"):
        if parent not in sys.modules:
            sys.modules[parent] = types.ModuleType(parent)

    fake_module = types.ModuleType("kiln_pro.design_brief.context")
    fake_fn = MagicMock(name="brief_context_dict")
    fake_module.brief_context_dict = fake_fn
    sys.modules["kiln_pro.design_brief.context"] = fake_module
    sys.modules["kiln_pro.design_brief"].context = fake_module

    yield fake_fn

    sys.modules.pop("kiln_pro.design_brief.context", None)
    parent_mod = sys.modules.get("kiln_pro.design_brief")
    if parent_mod is not None and hasattr(parent_mod, "context"):
        delattr(parent_mod, "context")


# ---------------------------------------------------------------------------
# _resolve_brief_context
# ---------------------------------------------------------------------------


def test_resolve_empty_brief_returns_none():
    assert _resolve_brief_context("") is None


def test_resolve_known_brief_returns_context(stubbed_brief_context):
    stubbed_brief_context.return_value = {
        "brief_id": "abc",
        "duty": "decorative",
        "duty_label": "Decorative",
        "environment": ["indoor_ambient"],
    }
    ctx = _resolve_brief_context("abc")
    assert ctx is not None
    assert ctx["duty"] == "decorative"


def test_resolve_unknown_brief_returns_none(stubbed_brief_context):
    """brief_context_dict returns None for unknown id → helper returns None too."""
    stubbed_brief_context.return_value = None
    assert _resolve_brief_context("nonexistent") is None


def test_resolve_swallows_lookup_errors(stubbed_brief_context):
    """A failure inside brief_context_dict must NOT raise."""
    stubbed_brief_context.side_effect = Exception("simulated failure")
    # Must NOT raise
    assert _resolve_brief_context("abc") is None


def test_resolve_silent_when_kiln_pro_unavailable():
    """When kiln_pro isn't importable, helper silently returns None."""
    sys.modules.pop("kiln_pro.design_brief.context", None)
    # Must NOT raise
    assert _resolve_brief_context("abc") is None


# ---------------------------------------------------------------------------
# _format_goal_line_for_monitor — text-report tail
# ---------------------------------------------------------------------------


def test_format_goal_line_with_duty_and_env(stubbed_brief_context):
    stubbed_brief_context.return_value = {
        "duty_label": "Decorative",
        "environment": ["indoor_ambient", "wet"],
    }
    line = _format_goal_line_for_monitor("abc")
    assert line == "Goal: Decorative design for indoor_ambient, wet"


def test_format_goal_line_with_duty_only(stubbed_brief_context):
    """When environment is empty, still render the duty."""
    stubbed_brief_context.return_value = {
        "duty_label": "Load-bearing structural",
        "environment": [],
    }
    line = _format_goal_line_for_monitor("abc")
    assert line == "Goal: Load-bearing structural design"


def test_format_goal_line_empty_for_no_brief():
    """No brief_id → empty string, monitor_print omits the line entirely."""
    assert _format_goal_line_for_monitor("") == ""


def test_format_goal_line_empty_when_lookup_fails(stubbed_brief_context):
    stubbed_brief_context.return_value = None
    assert _format_goal_line_for_monitor("abc") == ""


def test_format_goal_line_falls_back_to_raw_duty(stubbed_brief_context):
    """When duty_label is missing (older brief snapshot), use raw duty."""
    stubbed_brief_context.return_value = {
        "duty": "decorative",
        "environment": ["indoor_ambient"],
    }
    line = _format_goal_line_for_monitor("abc")
    assert "decorative" in line
    assert "indoor_ambient" in line
