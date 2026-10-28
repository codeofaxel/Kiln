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
        def tool(self, **_kwargs):
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


def _block_kiln_pro(monkeypatch):
    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __builtins__.__import__

    def blocking_import(name, *args, **kwargs):
        if name == "kiln_pro.enterprise.licensing" or name.startswith("kiln_pro.enterprise.licensing"):
            raise ImportError(f"simulated: {name} not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", blocking_import)


def test_plain_install_signed_out_is_free(monkeypatch, tmp_path):
    """Without kiln-pro and without a sign-in, the answer is Free and says
    how a subscriber connects this machine."""
    _block_kiln_pro(monkeypatch)
    monkeypatch.setenv("KILN_AUTH_HOME", str(tmp_path))

    out = _walk_resolution_chain()
    assert out["success"] is True
    assert out["effective_tier"] == "free"
    assert out["matched_source"] == "default"
    assert any("isn't connected to a Kiln account" in step["detail"] for step in out["resolution_chain"])
    assert "Free tier" in out["agent_summary"]
    assert "kiln3d.com/pricing" in out["agent_summary"]
    assert out["setup_hint"] == "kiln signin"


def test_plain_install_reports_the_signed_in_accounts_plan(monkeypatch, tmp_path):
    """Without kiln-pro, a signed-in subscriber is on their plan, and the
    account is asked NOW: this is the tool for "I just paid and it still
    says Free"."""
    import base64
    import json
    import time
    from unittest import mock

    from kiln import account_plan

    _block_kiln_pro(monkeypatch)
    monkeypatch.setenv("KILN_AUTH_HOME", str(tmp_path))
    monkeypatch.setattr(account_plan, "_last_ask_at", 0.0)

    def b64(obj) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    (tmp_path / ".kiln").mkdir()
    (tmp_path / ".kiln" / "auth_tokens.json").write_text(json.dumps({
        "access_token": f"{b64({'alg': 'none'})}.{b64({'exp': int(time.time()) + 86400})}.s",
        "refresh_token": "r",
        "email": "maker@example.com",
        "tier": "free",  # what the sign-in saved, before the upgrade
        "signed_in_at": int(time.time()),
    }))
    resp = mock.MagicMock(status_code=200)
    resp.json.return_value = {"success": True, "tier": "pro", "has_entitlement": True}
    with mock.patch("requests.get", return_value=resp) as get:
        out = _walk_resolution_chain()

    assert get.call_count == 1
    assert out["effective_tier"] == "pro"
    assert out["matched_source"] == "account_plan"
    assert "setup_hint" not in out
    assert json.loads((tmp_path / ".kiln" / "auth_tokens.json").read_text())["tier"] == "pro"


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
# Hosted caller: the plan comes from the account making the request
# ---------------------------------------------------------------------------
#
# On Kiln's servers one process answers every customer, and the server
# holds no sign-in of its own.  A request reaches this tool only after the
# server has verified the caller's bearer and set their plan for that one
# request, so the caller IS signed in.  The answer must report their plan
# and must never send them to sign in on a machine they do not control.
# The licensing module is a stand-in so this runs without kiln-pro too.


def _hosted_call(monkeypatch, tmp_path, tier_name: str) -> dict:
    """Call the registered check_my_tier as a hosted caller on *tier_name*."""
    import contextvars
    import enum
    import sys
    import types

    class _Tier(enum.Enum):
        FREE = "free"
        PRO = "pro"
        BUSINESS = "business"
        ENTERPRISE = "enterprise"

    def _no_process_licence():
        raise AssertionError("a hosted caller's plan must not come from the server's own licence")

    override: contextvars.ContextVar = contextvars.ContextVar("hosted_tier_override", default=None)
    licensing = types.ModuleType("kiln_pro.enterprise.licensing")
    licensing._caller_tier_override = override
    licensing.get_license_manager = _no_process_licence
    monkeypatch.setitem(sys.modules, "kiln_pro.enterprise.licensing", licensing)

    # The server's own home holds no sign-in: the shape that would leak the
    # server's state into the caller's answer if anything read it.
    monkeypatch.setenv("KILN_HOSTED_MULTITENANT", "1")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("KILN_AUTH_HOME", str(tmp_path))
    monkeypatch.delenv("KILN_LICENSE_KEY", raising=False)

    tool = _capture_check_my_tier()
    token = override.set(_Tier(tier_name))
    try:
        return tool()
    finally:
        override.reset(token)


_SIGN_IN_PROSE = (
    "isn't connected",
    "isn't bound",
    "already subscribed",
    "once you're signed in",
    "sign in",
    "sign back in",
)


def _assert_speaks_for_the_signed_in_caller(out: dict, tier_name: str) -> None:
    assert out["success"] is True
    assert out["effective_tier"] == tier_name
    assert out["matched_source"] == "request_override"
    summary = out["agent_summary"]
    assert f"{tier_name.title()} tier" in summary
    assert "Kiln account" in summary
    words = " ".join(
        [summary.lower()] + [str(step.get("detail", "")).lower() for step in out["resolution_chain"]]
    )
    for phrase in _SIGN_IN_PROSE:
        assert phrase not in words, f"hosted {tier_name} answer says {phrase!r}: {summary}"
    # No internal plumbing in what the person reads.
    assert "rest" not in words.replace("interest", "").split() and "dispatcher" not in words
    assert "override" not in words
    # No sign-in instruction for the agent either: the caller is signed in.
    assert "agent_hint" not in out
    assert "setup_hint" not in out


def test_hosted_free_caller_is_told_their_plan_not_to_sign_in(monkeypatch, tmp_path):
    out = _hosted_call(monkeypatch, tmp_path, "free")
    _assert_speaks_for_the_signed_in_caller(out, "free")
    assert "kiln3d.com/pricing" in out["agent_summary"]


@pytest.mark.parametrize("tier_name", ["pro", "business", "enterprise"])
def test_hosted_paid_caller_is_told_their_plan_in_plain_words(monkeypatch, tmp_path, tier_name):
    out = _hosted_call(monkeypatch, tmp_path, tier_name)
    _assert_speaks_for_the_signed_in_caller(out, tier_name)


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
