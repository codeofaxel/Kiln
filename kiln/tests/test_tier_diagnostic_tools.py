"""Unit tests for the check_my_tier MCP tool.

Coverage matrix:
  - Free-tier WITHOUT kiln-pro installed (ImportError path)
  - Free-tier WITH kiln-pro installed but no license / no OAuth session
  - Pro tier (LicenseManager returns PRO)
  - Business tier
  - Enterprise tier
  - Per-request override short-circuits early
  - License key env var detected in resolution chain
  - License key file detected in resolution chain
  - OAuth session presence detected in resolution chain
  - Diagnostic catches its own crashes (try/except wrapper)
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

# Import the real kiln_pro LicenseTier so test enums match what the
# tool produces.  Skip the whole module if kiln-pro isn't installed —
# the only test that doesn't need it is test_free_tier_without_kiln_pro,
# which we keep importable separately.
try:
    from kiln_pro.enterprise.licensing import LicenseTier as _RealLicenseTier
    _KILN_PRO_AVAILABLE = True
except ImportError:
    _KILN_PRO_AVAILABLE = False
    _RealLicenseTier = None  # type: ignore[assignment]

from kiln.plugins.tier_diagnostic_tools import _walk_resolution_chain, plugin


requires_kiln_pro = pytest.mark.skipif(
    not _KILN_PRO_AVAILABLE,
    reason="kiln-pro not installed; only the no-kiln-pro test path is meaningful",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _capture_check_my_tier():
    """Register the plugin against a mock MCP and return the tool fn."""
    captured = {}

    class _MockMcp:
        def tool(self):
            def deco(fn):
                captured[fn.__name__] = fn
                return fn
            return deco

    plugin.register(_MockMcp())
    return captured["check_my_tier"]


def _patch_license_manager_returning(monkeypatch, tier_name: str):
    """Patch the live kiln_pro LicenseManager so get_tier() returns the
    requested tier.  Real LicenseTier enum, real module — just the
    manager's tier-resolution result is mocked.  The contextvar override
    is left at its default (None) unless a specific test sets it via
    set_caller_tier() — see test_request_override_short_circuits."""
    if not _KILN_PRO_AVAILABLE:
        pytest.skip("kiln-pro not installed")
    from kiln_pro.enterprise import licensing as lic_mod

    mock_mgr = MagicMock()
    mock_mgr.get_tier.return_value = _RealLicenseTier(tier_name)
    monkeypatch.setattr(lic_mod, "get_license_manager", lambda: mock_mgr)
    return mock_mgr


# ---------------------------------------------------------------------------
# Free-tier paths
# ---------------------------------------------------------------------------


def test_free_tier_without_kiln_pro(monkeypatch):
    """When kiln-pro is not importable, tool reports free + clear reason."""
    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __builtins__.__import__

    def blocking_import(name, *args, **kwargs):
        if name == "kiln_pro.enterprise.licensing" or name.startswith("kiln_pro.enterprise.licensing"):
            raise ImportError(f"simulated: {name} not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", blocking_import)

    out = _walk_resolution_chain()
    assert out["success"] is True
    assert out["effective_tier"] == "free"
    assert out["matched_source"] == "kiln_pro_install"
    assert any("kiln-pro is not installed" in step["detail"] for step in out["resolution_chain"])
    assert "Free tier" in out["agent_summary"]
    assert "kiln3d.com/pricing" in out["agent_summary"]


@requires_kiln_pro
def test_free_tier_with_kiln_pro_no_credentials(monkeypatch):
    """kiln-pro is installed but the user has no license/OAuth — tool
    reports free with chain showing every source missed."""
    _patch_license_manager_returning(monkeypatch, "free")
    monkeypatch.delenv("KILN_LICENSE_KEY", raising=False)
    # Redirect HOME so license file + auth tokens lookups miss
    monkeypatch.setenv("HOME", "/tmp/nonexistent_home_for_tier_test")

    out = _walk_resolution_chain()
    assert out["success"] is True
    assert out["effective_tier"] == "free"
    sources = {step["source"] for step in out["resolution_chain"]}
    assert "license_key_env" in sources
    assert "license_key_file" in sources
    assert "oauth_session" in sources


# ---------------------------------------------------------------------------
# Paid-tier paths (mock LicenseManager)
# ---------------------------------------------------------------------------


@requires_kiln_pro
@pytest.mark.parametrize("tier_name,expected_summary_phrase", [
    ("pro", "Pro tier"),
    ("business", "Business tier"),
    ("enterprise", "Enterprise tier"),
])
def test_paid_tier_summary(monkeypatch, tier_name, expected_summary_phrase):
    """Each paid-tier path returns the right effective_tier + a summary
    phrase that names the tier."""
    _patch_license_manager_returning(monkeypatch, tier_name)
    monkeypatch.delenv("KILN_LICENSE_KEY", raising=False)

    out = _walk_resolution_chain()
    assert out["success"] is True
    assert out["effective_tier"] == tier_name
    assert out["tier_label"] == tier_name.title()
    assert expected_summary_phrase in out["agent_summary"]
    assert out["matched_source"] == "license_manager_resolve"


@requires_kiln_pro
def test_tier_rank_ordering(monkeypatch):
    """tier_rank reflects the canonical order free<pro<business<enterprise."""
    ranks = {}
    for t in ("free", "pro", "business", "enterprise"):
        _patch_license_manager_returning(monkeypatch, t)
        monkeypatch.delenv("KILN_LICENSE_KEY", raising=False)
        out = _walk_resolution_chain()
        ranks[t] = out["tier_rank"]
    assert ranks["free"] < ranks["pro"] < ranks["business"] < ranks["enterprise"]


# ---------------------------------------------------------------------------
# Resolution chain — request override short-circuits
# ---------------------------------------------------------------------------


@requires_kiln_pro
def test_request_override_short_circuits():
    """When _caller_tier_override is set, chain stops at step 1."""
    from kiln_pro.enterprise.licensing import (
        reset_caller_tier,
        set_caller_tier,
    )
    token = set_caller_tier(_RealLicenseTier("business"))
    try:
        out = _walk_resolution_chain()
    finally:
        reset_caller_tier(token)

    assert out["success"] is True
    assert out["effective_tier"] == "business"
    assert out["matched_source"] == "request_override"
    assert len(out["resolution_chain"]) == 1
    assert out["resolution_chain"][0]["matched"] is True


# ---------------------------------------------------------------------------
# Resolution chain detection of credentials
# ---------------------------------------------------------------------------


@requires_kiln_pro
def test_chain_detects_license_key_env(monkeypatch):
    """When KILN_LICENSE_KEY is set, the chain reports license_key_env matched."""
    _patch_license_manager_returning(monkeypatch, "pro")
    monkeypatch.setenv("KILN_LICENSE_KEY", "test-key-abc123")

    out = _walk_resolution_chain()
    env_step = next(s for s in out["resolution_chain"] if s["source"] == "license_key_env")
    assert env_step["matched"] is True
    assert "15 chars" in env_step["detail"]  # len("test-key-abc123")


@requires_kiln_pro
def test_chain_detects_no_license_key_env(monkeypatch):
    """When KILN_LICENSE_KEY is unset, the chain reports it missed."""
    _patch_license_manager_returning(monkeypatch, "free")
    monkeypatch.delenv("KILN_LICENSE_KEY", raising=False)

    out = _walk_resolution_chain()
    env_step = next(s for s in out["resolution_chain"] if s["source"] == "license_key_env")
    assert env_step["matched"] is False


# ---------------------------------------------------------------------------
# Tool registration + crash-resilience
# ---------------------------------------------------------------------------


def test_check_my_tier_registers():
    """plugin.register() exposes a check_my_tier callable."""
    fn = _capture_check_my_tier()
    assert callable(fn)


@requires_kiln_pro
def test_check_my_tier_returns_proper_shape(monkeypatch):
    """Top-level call returns the documented response shape."""
    _patch_license_manager_returning(monkeypatch, "pro")
    monkeypatch.delenv("KILN_LICENSE_KEY", raising=False)

    fn = _capture_check_my_tier()
    out = fn()
    expected_keys = {
        "success", "effective_tier", "tier_label", "tier_rank",
        "resolution_chain", "matched_source", "agent_summary", "pricing_url"
    }
    assert expected_keys <= set(out.keys()), f"missing: {expected_keys - set(out.keys())}"
    assert out["pricing_url"] == "https://kiln3d.com/pricing"


def test_check_my_tier_catches_internal_crash(monkeypatch):
    """If _walk_resolution_chain raises, the tool returns a structured
    error instead of propagating."""
    monkeypatch.setattr(
        "kiln.plugins.tier_diagnostic_tools._walk_resolution_chain",
        lambda: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    fn = _capture_check_my_tier()
    out = fn()
    assert out["success"] is False
    assert out["effective_tier"] == "unknown"
    assert "boom" in out["error"]
    assert "report at" in out["agent_summary"]


# ---------------------------------------------------------------------------
# Agent-discoverability sanity (docstring keywords)
# ---------------------------------------------------------------------------


def test_check_my_tier_docstring_has_discovery_keywords():
    """The tool's docstring needs the keywords agents search for when
    users ask tier/plan/subscription/paywall questions."""
    fn = _capture_check_my_tier()
    doc = (fn.__doc__ or "").lower()
    for keyword in (
        "tier", "subscription", "plan", "pro", "business", "enterprise",
        "free", "paywall", "upgrade",
    ):
        assert keyword in doc, f"missing discovery keyword: {keyword!r}"
