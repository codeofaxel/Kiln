"""Tests for kiln.retention -- RetentionPolicy, RetentionManager, RetentionResult.

Covers:
- Default policy values
- Env var overrides (valid, invalid, negative)
- Dry run reporting
- Policy retrieval
- RetentionResult total_expired and to_dict
- Custom policy injection
"""

from __future__ import annotations

from unittest.mock import patch

from kiln.retention import (
    RetentionManager,
    RetentionPolicy,
    RetentionResult,
)

# ---------------------------------------------------------------------------
# RetentionPolicy defaults
# ---------------------------------------------------------------------------


class TestRetentionPolicy:
    """Tests for RetentionPolicy dataclass defaults and serialization."""

    def test_default_values(self):
        p = RetentionPolicy()
        assert p.audit_log_days == 365
        assert p.print_history_days == 365
        assert p.traceability_days == 2555
        assert p.agent_memory_days == 90
        assert p.event_log_days == 180

    def test_custom_values(self):
        p = RetentionPolicy(audit_log_days=30, traceability_days=3650)
        assert p.audit_log_days == 30
        assert p.traceability_days == 3650

    def test_to_dict(self):
        p = RetentionPolicy()
        d = p.to_dict()
        assert d == {
            "audit_log_days": 365,
            "print_history_days": 365,
            "traceability_days": 2555,
            "agent_memory_days": 90,
            "event_log_days": 180,
        }


# ---------------------------------------------------------------------------
# RetentionResult
# ---------------------------------------------------------------------------


class TestRetentionResult:
    """Tests for RetentionResult dataclass."""

    def test_total_expired_zero(self):
        r = RetentionResult(dry_run=True)
        assert r.total_expired == 0

    def test_total_expired_sums_all_categories(self):
        r = RetentionResult(
            dry_run=False,
            audit_log_expired=10,
            print_history_expired=20,
            traceability_expired=5,
            agent_memory_expired=3,
            event_log_expired=2,
        )
        assert r.total_expired == 40

    def test_to_dict(self):
        r = RetentionResult(dry_run=True, audit_log_expired=7)
        d = r.to_dict()
        assert d["dry_run"] is True
        assert d["audit_log_expired"] == 7
        assert d["total_expired"] == 7
        assert "print_history_expired" in d


# ---------------------------------------------------------------------------
# RetentionManager with default policy
# ---------------------------------------------------------------------------


class TestRetentionManagerDefaults:
    """Tests for RetentionManager with no env vars set."""

    def test_default_policy(self):
        mgr = RetentionManager()
        p = mgr.get_policy()
        assert p.audit_log_days == 365
        assert p.print_history_days == 365
        assert p.traceability_days == 2555

    def test_dry_run_returns_result(self):
        mgr = RetentionManager()
        result = mgr.apply(dry_run=True)
        assert result.dry_run is True
        assert result.total_expired == 0

    def test_non_dry_run_returns_result(self):
        mgr = RetentionManager()
        result = mgr.apply(dry_run=False)
        assert result.dry_run is False
        assert result.total_expired == 0


# ---------------------------------------------------------------------------
# Env var overrides
# ---------------------------------------------------------------------------


class TestRetentionManagerEnvVars:
    """Tests for environment variable overrides on RetentionManager."""

    @patch.dict(
        "os.environ",
        {
            "KILN_RETENTION_AUDIT_DAYS": "30",
            "KILN_RETENTION_PRINT_DAYS": "60",
            "KILN_RETENTION_TRACE_DAYS": "3650",
        },
    )
    def test_env_vars_override_defaults(self):
        mgr = RetentionManager()
        p = mgr.get_policy()
        assert p.audit_log_days == 30
        assert p.print_history_days == 60
        assert p.traceability_days == 3650

    @patch.dict("os.environ", {"KILN_RETENTION_AUDIT_DAYS": "not_a_number"})
    def test_invalid_env_var_uses_default(self):
        mgr = RetentionManager()
        p = mgr.get_policy()
        assert p.audit_log_days == 365

    @patch.dict("os.environ", {"KILN_RETENTION_AUDIT_DAYS": "0"})
    def test_zero_env_var_clamps_to_one(self):
        mgr = RetentionManager()
        p = mgr.get_policy()
        assert p.audit_log_days == 1

    @patch.dict("os.environ", {"KILN_RETENTION_AUDIT_DAYS": "-5"})
    def test_negative_env_var_clamps_to_one(self):
        mgr = RetentionManager()
        p = mgr.get_policy()
        assert p.audit_log_days == 1

    @patch.dict("os.environ", {"KILN_RETENTION_AUDIT_DAYS": "1"})
    def test_minimum_one_day(self):
        mgr = RetentionManager()
        p = mgr.get_policy()
        assert p.audit_log_days == 1


# ---------------------------------------------------------------------------
# Custom policy injection
# ---------------------------------------------------------------------------


class TestRetentionManagerCustomPolicy:
    """Tests for injecting a custom RetentionPolicy."""

    def test_custom_policy_is_used(self):
        custom = RetentionPolicy(
            audit_log_days=7,
            print_history_days=14,
            traceability_days=30,
            agent_memory_days=3,
            event_log_days=7,
        )
        mgr = RetentionManager(policy=custom)
        p = mgr.get_policy()
        assert p.audit_log_days == 7
        assert p.print_history_days == 14
        assert p.traceability_days == 30
        assert p.agent_memory_days == 3
        assert p.event_log_days == 7

    @patch.dict("os.environ", {"KILN_RETENTION_AUDIT_DAYS": "999"})
    def test_custom_policy_ignores_env_vars(self):
        custom = RetentionPolicy(audit_log_days=7)
        mgr = RetentionManager(policy=custom)
        p = mgr.get_policy()
        # Custom policy takes precedence over env vars
        assert p.audit_log_days == 7
