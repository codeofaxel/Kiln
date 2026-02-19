"""Tests for kiln.licensing -- license tier management module."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from unittest import mock

import pytest

from kiln.licensing import (
    FEATURE_TIERS,
    FREE_TIER_MAX_PRINTERS,
    FREE_TIER_MAX_QUEUED_JOBS,
    LicenseError,
    LicenseInfo,
    LicenseManager,
    LicenseTier,
    TierRequiredError,
    _KEY_PREFIX_BUSINESS,
    _KEY_PREFIX_PRO,
    check_tier,
    generate_license_key,
    get_license_manager,
    get_tier,
    requires_tier,
)

# ---------------------------------------------------------------------------
# Test signing secret — used to generate valid signed keys for tests
# ---------------------------------------------------------------------------

_TEST_SIGNING_SECRET = "test-signing-secret-for-unit-tests"


def _signed_key(tier: LicenseTier, email: str = "test@example.com") -> str:
    """Generate a properly signed license key for testing."""
    return generate_license_key(tier, email, signing_key=_TEST_SIGNING_SECRET)


def _env_with_signing(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Return a minimal env dict with the test signing secret set."""
    env = {"KILN_LICENSE_SIGNING_SECRET": _TEST_SIGNING_SECRET}
    if extra:
        env.update(extra)
    return env


# ---------------------------------------------------------------------------
# 1. LicenseTier enum
# ---------------------------------------------------------------------------


class TestLicenseTier:
    def test_values(self):
        assert LicenseTier.FREE.value == "free"
        assert LicenseTier.PRO.value == "pro"
        assert LicenseTier.BUSINESS.value == "business"

    def test_ordering_ge(self):
        assert LicenseTier.BUSINESS >= LicenseTier.PRO
        assert LicenseTier.PRO >= LicenseTier.PRO
        assert LicenseTier.PRO >= LicenseTier.FREE
        assert not (LicenseTier.FREE >= LicenseTier.PRO)

    def test_ordering_gt(self):
        assert LicenseTier.BUSINESS > LicenseTier.PRO
        assert LicenseTier.PRO > LicenseTier.FREE
        assert not (LicenseTier.FREE > LicenseTier.FREE)

    def test_ordering_le(self):
        assert LicenseTier.FREE <= LicenseTier.PRO
        assert LicenseTier.PRO <= LicenseTier.PRO
        assert not (LicenseTier.BUSINESS <= LicenseTier.PRO)

    def test_ordering_lt(self):
        assert LicenseTier.FREE < LicenseTier.PRO
        assert LicenseTier.PRO < LicenseTier.BUSINESS
        assert not (LicenseTier.PRO < LicenseTier.FREE)


# ---------------------------------------------------------------------------
# 2. LicenseInfo dataclass
# ---------------------------------------------------------------------------


class TestLicenseInfo:
    def test_not_expired_when_no_expiry(self):
        info = LicenseInfo(tier=LicenseTier.PRO)
        assert info.is_expired is False
        assert info.is_valid is True

    def test_expired_when_past_expiry(self):
        info = LicenseInfo(
            tier=LicenseTier.PRO,
            expires_at=time.time() - 3600,
        )
        assert info.is_expired is True
        assert info.is_valid is False

    def test_not_expired_when_future_expiry(self):
        info = LicenseInfo(
            tier=LicenseTier.PRO,
            expires_at=time.time() + 3600,
        )
        assert info.is_expired is False
        assert info.is_valid is True

    def test_to_dict(self):
        info = LicenseInfo(
            tier=LicenseTier.PRO,
            license_key_hint="abc123",
            source="env",
        )
        data = info.to_dict()
        assert data["tier"] == "pro"
        assert data["license_key_hint"] == "abc123"
        assert data["source"] == "env"
        assert data["is_valid"] is True
        assert data["is_expired"] is False


# ---------------------------------------------------------------------------
# 3. LicenseManager — tier resolution
# ---------------------------------------------------------------------------


class TestLicenseManagerDefaults:
    def test_defaults_to_free_no_env(self, tmp_path):
        """With no env var and no license file, tier is FREE."""
        with mock.patch.dict(os.environ, {}, clear=True):
            mgr = LicenseManager(
                license_path=tmp_path / "license",
                cache_path=tmp_path / "cache.json",
            )
            assert mgr.get_tier() == LicenseTier.FREE

    def test_pro_from_env_var(self, tmp_path):
        """KILN_LICENSE_KEY with signed pro key resolves to PRO."""
        key = _signed_key(LicenseTier.PRO)
        with mock.patch.dict(os.environ, _env_with_signing({"KILN_LICENSE_KEY": key}), clear=True):
            mgr = LicenseManager(
                license_path=tmp_path / "license",
                cache_path=tmp_path / "cache.json",
            )
            assert mgr.get_tier() == LicenseTier.PRO

    def test_business_from_env_var(self, tmp_path):
        """KILN_LICENSE_KEY with signed business key resolves to BUSINESS."""
        key = _signed_key(LicenseTier.BUSINESS)
        with mock.patch.dict(os.environ, _env_with_signing({"KILN_LICENSE_KEY": key}), clear=True):
            mgr = LicenseManager(
                license_path=tmp_path / "license",
                cache_path=tmp_path / "cache.json",
            )
            assert mgr.get_tier() == LicenseTier.BUSINESS

    def test_pro_from_constructor(self, tmp_path):
        """Explicit license_key arg takes priority."""
        key = _signed_key(LicenseTier.PRO)
        with mock.patch.dict(os.environ, _env_with_signing(), clear=True):
            mgr = LicenseManager(
                license_key=key,
                license_path=tmp_path / "license",
                cache_path=tmp_path / "cache.json",
            )
            assert mgr.get_tier() == LicenseTier.PRO

    def test_unknown_prefix_defaults_to_free(self, tmp_path):
        """A key with an unrecognised prefix defaults to FREE."""
        mgr = LicenseManager(
            license_key="unknown_prefix_key_12345",
            license_path=tmp_path / "license",
            cache_path=tmp_path / "cache.json",
        )
        assert mgr.get_tier() == LicenseTier.FREE


class TestLicenseManagerFromFile:
    def test_reads_license_from_file(self, tmp_path):
        """License key is loaded from the license file."""
        license_file = tmp_path / "license"
        key = _signed_key(LicenseTier.PRO)
        license_file.write_text(key, encoding="utf-8")

        with mock.patch.dict(os.environ, _env_with_signing(), clear=True):
            mgr = LicenseManager(
                license_path=license_file,
                cache_path=tmp_path / "cache.json",
            )
            assert mgr.get_tier() == LicenseTier.PRO

    def test_env_var_overrides_file(self, tmp_path):
        """Env var takes priority over license file."""
        license_file = tmp_path / "license"
        pro_key = _signed_key(LicenseTier.PRO)
        license_file.write_text(pro_key, encoding="utf-8")

        biz_key = _signed_key(LicenseTier.BUSINESS)
        with mock.patch.dict(os.environ, _env_with_signing({"KILN_LICENSE_KEY": biz_key}), clear=True):
            mgr = LicenseManager(
                license_path=license_file,
                cache_path=tmp_path / "cache.json",
            )
            assert mgr.get_tier() == LicenseTier.BUSINESS

    def test_missing_file_defaults_to_free(self, tmp_path):
        """Missing license file defaults to FREE."""
        with mock.patch.dict(os.environ, {}, clear=True):
            mgr = LicenseManager(
                license_path=tmp_path / "nonexistent",
                cache_path=tmp_path / "cache.json",
            )
            assert mgr.get_tier() == LicenseTier.FREE

    def test_empty_file_defaults_to_free(self, tmp_path):
        """Empty license file defaults to FREE."""
        license_file = tmp_path / "license"
        license_file.write_text("", encoding="utf-8")

        with mock.patch.dict(os.environ, {}, clear=True):
            mgr = LicenseManager(
                license_path=license_file,
                cache_path=tmp_path / "cache.json",
            )
            assert mgr.get_tier() == LicenseTier.FREE


# ---------------------------------------------------------------------------
# 4. check_tier
# ---------------------------------------------------------------------------


class TestCheckTier:
    def test_free_meets_free(self, tmp_path):
        with mock.patch.dict(os.environ, {}, clear=True):
            mgr = LicenseManager(
                license_path=tmp_path / "license",
                cache_path=tmp_path / "cache.json",
            )
            ok, msg = mgr.check_tier(LicenseTier.FREE)
            assert ok is True
            assert msg is None

    def test_free_fails_pro(self, tmp_path):
        with mock.patch.dict(os.environ, {}, clear=True):
            mgr = LicenseManager(
                license_path=tmp_path / "license",
                cache_path=tmp_path / "cache.json",
            )
            ok, msg = mgr.check_tier(LicenseTier.PRO)
            assert ok is False
            assert "Pro" in msg
            assert "kiln upgrade" in msg

    def test_pro_meets_pro(self, tmp_path):
        key = _signed_key(LicenseTier.PRO)
        with mock.patch.dict(os.environ, _env_with_signing(), clear=True):
            mgr = LicenseManager(
                license_key=key,
                license_path=tmp_path / "license",
                cache_path=tmp_path / "cache.json",
            )
            ok, msg = mgr.check_tier(LicenseTier.PRO)
            assert ok is True
            assert msg is None

    def test_business_meets_pro(self, tmp_path):
        key = _signed_key(LicenseTier.BUSINESS)
        with mock.patch.dict(os.environ, _env_with_signing(), clear=True):
            mgr = LicenseManager(
                license_key=key,
                license_path=tmp_path / "license",
                cache_path=tmp_path / "cache.json",
            )
            ok, msg = mgr.check_tier(LicenseTier.PRO)
            assert ok is True

    def test_pro_fails_business(self, tmp_path):
        key = _signed_key(LicenseTier.PRO)
        with mock.patch.dict(os.environ, _env_with_signing(), clear=True):
            mgr = LicenseManager(
                license_key=key,
                license_path=tmp_path / "license",
                cache_path=tmp_path / "cache.json",
            )
            ok, msg = mgr.check_tier(LicenseTier.BUSINESS)
            assert ok is False
            assert "Business" in msg


# ---------------------------------------------------------------------------
# 5. activate / deactivate
# ---------------------------------------------------------------------------


class TestActivateDeactivate:
    def test_activate_license(self, tmp_path):
        """Activating a key writes it to file and updates tier."""
        with mock.patch.dict(os.environ, _env_with_signing(), clear=True):
            mgr = LicenseManager(
                license_path=tmp_path / "license",
                cache_path=tmp_path / "cache.json",
            )
            assert mgr.get_tier() == LicenseTier.FREE

            key = _signed_key(LicenseTier.PRO)
            info = mgr.activate_license(key)
            assert info.tier == LicenseTier.PRO
            assert mgr.get_tier() == LicenseTier.PRO

            # Verify file was written
            content = (tmp_path / "license").read_text(encoding="utf-8")
            assert content == key

    def test_deactivate_license(self, tmp_path):
        """Deactivating removes the key file and resets to FREE."""
        license_file = tmp_path / "license"
        key = _signed_key(LicenseTier.PRO)
        license_file.write_text(key, encoding="utf-8")

        with mock.patch.dict(os.environ, _env_with_signing(), clear=True):
            mgr = LicenseManager(
                license_path=license_file,
                cache_path=tmp_path / "cache.json",
            )
            assert mgr.get_tier() == LicenseTier.PRO

            mgr.deactivate_license()
            assert mgr.get_tier() == LicenseTier.FREE
            assert not license_file.exists()

    def test_activate_overwrites_previous(self, tmp_path):
        """Activating a new key replaces the old one."""
        license_file = tmp_path / "license"
        pro_key = _signed_key(LicenseTier.PRO)
        license_file.write_text(pro_key, encoding="utf-8")

        with mock.patch.dict(os.environ, _env_with_signing(), clear=True):
            mgr = LicenseManager(
                license_path=license_file,
                cache_path=tmp_path / "cache.json",
            )
            assert mgr.get_tier() == LicenseTier.PRO

            biz_key = _signed_key(LicenseTier.BUSINESS)
            info = mgr.activate_license(biz_key)
            assert info.tier == LicenseTier.BUSINESS
            assert mgr.get_tier() == LicenseTier.BUSINESS


# ---------------------------------------------------------------------------
# 6. Cache (offline fallback)
# ---------------------------------------------------------------------------


class TestLicenseCache:
    def test_writes_cache_on_resolve(self, tmp_path):
        """Resolving a tier writes a cache file."""
        cache_file = tmp_path / "cache.json"
        key = _signed_key(LicenseTier.PRO)
        with mock.patch.dict(os.environ, _env_with_signing(), clear=True):
            mgr = LicenseManager(
                license_key=key,
                license_path=tmp_path / "license",
                cache_path=cache_file,
            )
            mgr.get_tier()
            assert cache_file.exists()

            data = json.loads(cache_file.read_text(encoding="utf-8"))
            assert data["tier"] == "pro"
            assert data["key_hint"] == key[-6:]

    def test_no_cache_for_free_tier(self, tmp_path):
        """No cache is written when there's no license key."""
        cache_file = tmp_path / "cache.json"
        with mock.patch.dict(os.environ, {}, clear=True):
            mgr = LicenseManager(
                license_path=tmp_path / "license",
                cache_path=cache_file,
            )
            mgr.get_tier()
        assert not cache_file.exists()

    def test_cache_used_for_unknown_prefix(self, tmp_path):
        """Unknown prefix without signing secret or offline mode defaults to FREE."""
        cache_file = tmp_path / "cache.json"
        key = "custom_format_key_xyzabc"
        cache_data = {
            "tier": "pro",
            "key_hint": key[-6:],
            "validated_at": time.time(),
        }
        cache_file.write_text(json.dumps(cache_data), encoding="utf-8")

        with mock.patch.dict(os.environ, {}, clear=True):
            mgr = LicenseManager(
                license_key=key,
                license_path=tmp_path / "license",
                cache_path=cache_file,
            )
            # No signing secret, no offline mode, non-kiln prefix → FREE
            assert mgr.get_tier() == LicenseTier.FREE

    def test_expired_cache_ignored(self, tmp_path):
        """Cache past TTL is not used."""
        cache_file = tmp_path / "cache.json"
        key = "custom_format_key_xyzabc"
        cache_data = {
            "tier": "pro",
            "key_hint": key[-6:],
            "validated_at": time.time() - (8 * 24 * 3600),  # 8 days ago
        }
        cache_file.write_text(json.dumps(cache_data), encoding="utf-8")

        mgr = LicenseManager(
            license_key=key,
            license_path=tmp_path / "license",
            cache_path=cache_file,
        )
        # Expired cache → unknown prefix → FREE
        assert mgr.get_tier() == LicenseTier.FREE


# ---------------------------------------------------------------------------
# 7. get_info
# ---------------------------------------------------------------------------


class TestGetInfo:
    def test_returns_license_info(self, tmp_path):
        key = _signed_key(LicenseTier.PRO)
        with mock.patch.dict(os.environ, _env_with_signing(), clear=True):
            mgr = LicenseManager(
                license_key=key,
                license_path=tmp_path / "license",
                cache_path=tmp_path / "cache.json",
            )
            info = mgr.get_info()
            assert isinstance(info, LicenseInfo)
            assert info.tier == LicenseTier.PRO
            assert info.license_key_hint == key[-6:]

    def test_source_env(self, tmp_path):
        key = _signed_key(LicenseTier.PRO)
        with mock.patch.dict(os.environ, _env_with_signing({"KILN_LICENSE_KEY": key}), clear=True):
            mgr = LicenseManager(
                license_path=tmp_path / "license",
                cache_path=tmp_path / "cache.json",
            )
            info = mgr.get_info()
            assert info.source == "env"

    def test_source_file(self, tmp_path):
        license_file = tmp_path / "license"
        key = _signed_key(LicenseTier.PRO)
        license_file.write_text(key, encoding="utf-8")

        with mock.patch.dict(os.environ, _env_with_signing(), clear=True):
            mgr = LicenseManager(
                license_path=license_file,
                cache_path=tmp_path / "cache.json",
            )
            info = mgr.get_info()
            assert info.source == "file"

    def test_source_default(self, tmp_path):
        with mock.patch.dict(os.environ, {}, clear=True):
            mgr = LicenseManager(
                license_path=tmp_path / "license",
                cache_path=tmp_path / "cache.json",
            )
            info = mgr.get_info()
            assert info.source == "default"


# ---------------------------------------------------------------------------
# 8. @requires_tier decorator
# ---------------------------------------------------------------------------


class TestRequiresTierDecorator:
    def test_allows_when_tier_met(self, tmp_path):
        """Decorator passes through when tier is sufficient."""
        key = _signed_key(LicenseTier.PRO)

        with mock.patch.dict(os.environ, _env_with_signing(), clear=True):
            # Patch the module-level singleton
            with mock.patch("kiln.licensing._manager", LicenseManager(
                license_key=key,
                license_path=tmp_path / "license",
                cache_path=tmp_path / "cache.json",
            )):
                @requires_tier(LicenseTier.PRO)
                def my_tool():
                    return {"success": True, "data": "hello"}

                result = my_tool()
                assert result["success"] is True
                assert result["data"] == "hello"

    def test_blocks_when_tier_insufficient(self, tmp_path):
        """Decorator returns error dict when tier is insufficient."""
        with mock.patch("kiln.licensing._manager", LicenseManager(
            license_path=tmp_path / "license",
            cache_path=tmp_path / "cache.json",
        )), mock.patch.dict(os.environ, {}, clear=True):
            @requires_tier(LicenseTier.PRO)
            def my_tool():
                return {"success": True}

            result = my_tool()
            assert result["success"] is False
            assert result["code"] == "LICENSE_REQUIRED"
            assert result["required_tier"] == "pro"
            assert "upgrade_url" in result

    def test_preserves_function_name(self):
        """Decorator preserves the wrapped function's name."""
        @requires_tier(LicenseTier.PRO)
        def fleet_status():
            """Fleet status docstring."""
            return {}

        assert fleet_status.__name__ == "fleet_status"
        assert "Fleet status" in fleet_status.__doc__

    def test_passes_args_through(self, tmp_path):
        """Decorator passes args and kwargs to the wrapped function."""
        key = _signed_key(LicenseTier.PRO)
        with mock.patch.dict(os.environ, _env_with_signing(), clear=True):
            with mock.patch("kiln.licensing._manager", LicenseManager(
                license_key=key,
                license_path=tmp_path / "license",
                cache_path=tmp_path / "cache.json",
            )):
                @requires_tier(LicenseTier.FREE)
                def my_tool(job_id: str, limit: int = 10):
                    return {"job_id": job_id, "limit": limit}

                result = my_tool("abc", limit=5)
                assert result["job_id"] == "abc"
                assert result["limit"] == 5

    def test_business_gate(self, tmp_path):
        """Business tier gate blocks PRO users."""
        key = _signed_key(LicenseTier.PRO)
        with mock.patch.dict(os.environ, _env_with_signing(), clear=True):
            with mock.patch("kiln.licensing._manager", LicenseManager(
                license_key=key,
                license_path=tmp_path / "license",
                cache_path=tmp_path / "cache.json",
            )):
                @requires_tier(LicenseTier.BUSINESS)
                def business_tool():
                    return {"success": True}

                result = business_tool()
                assert result["success"] is False
                assert result["required_tier"] == "business"


# ---------------------------------------------------------------------------
# 9. TierRequiredError
# ---------------------------------------------------------------------------


class TestTierRequiredError:
    def test_message_includes_feature_and_tier(self):
        err = TierRequiredError("fleet_status", LicenseTier.PRO)
        assert "fleet_status" in str(err)
        assert "Pro" in str(err)
        assert "kiln upgrade" in str(err)

    def test_attributes(self):
        err = TierRequiredError("submit_job", LicenseTier.BUSINESS)
        assert err.feature == "submit_job"
        assert err.required_tier == LicenseTier.BUSINESS

    def test_is_license_error(self):
        err = TierRequiredError("test", LicenseTier.PRO)
        assert isinstance(err, LicenseError)


# ---------------------------------------------------------------------------
# 10. Module-level convenience functions
# ---------------------------------------------------------------------------


class TestModuleLevelConvenience:
    def test_get_tier_returns_free_by_default(self, tmp_path):
        with mock.patch("kiln.licensing._manager", LicenseManager(
            license_path=tmp_path / "license",
            cache_path=tmp_path / "cache.json",
        )), mock.patch.dict(os.environ, {}, clear=True):
            assert get_tier() == LicenseTier.FREE

    def test_check_tier_convenience(self, tmp_path):
        with mock.patch("kiln.licensing._manager", LicenseManager(
            license_path=tmp_path / "license",
            cache_path=tmp_path / "cache.json",
        )), mock.patch.dict(os.environ, {}, clear=True):
            ok, msg = check_tier(LicenseTier.FREE)
            assert ok is True

            ok, msg = check_tier(LicenseTier.PRO)
            assert ok is False

    def test_get_license_manager_creates_singleton(self):
        """get_license_manager() returns the same instance on repeat calls."""
        with mock.patch("kiln.licensing._manager", None):
            mgr1 = get_license_manager()
            mgr2 = get_license_manager()
            assert mgr1 is mgr2


# ---------------------------------------------------------------------------
# 11. FEATURE_TIERS mapping
# ---------------------------------------------------------------------------


class TestFeatureTiers:
    def test_fleet_orchestration_features_are_pro(self):
        assert FEATURE_TIERS["fleet_status"] == LicenseTier.PRO
        assert FEATURE_TIERS["fleet_analytics"] == LicenseTier.PRO

    def test_free_tier_features_not_gated(self):
        """Queue, register, and billing features are free (with caps) — not in FEATURE_TIERS."""
        for feature in [
            "register_printer", "submit_job", "job_status",
            "queue_summary", "cancel_job", "job_history",
            "billing_summary", "billing_history",
        ]:
            assert feature not in FEATURE_TIERS, f"{feature} should be free"

    def test_fulfillment_features_are_business(self):
        assert FEATURE_TIERS["fulfillment_order"] == LicenseTier.BUSINESS
        assert FEATURE_TIERS["fulfillment_cancel"] == LicenseTier.BUSINESS

    def test_all_values_are_valid_tiers(self):
        for feature, tier in FEATURE_TIERS.items():
            assert isinstance(tier, LicenseTier), f"{feature} has invalid tier: {tier}"


# ---------------------------------------------------------------------------
# 12. Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_whitespace_in_key_stripped(self, tmp_path):
        """Keys with leading/trailing whitespace are handled."""
        license_file = tmp_path / "license"
        key = _signed_key(LicenseTier.PRO)
        license_file.write_text(f"  {key}  \n", encoding="utf-8")

        with mock.patch.dict(os.environ, _env_with_signing(), clear=True):
            mgr = LicenseManager(
                license_path=license_file,
                cache_path=tmp_path / "cache.json",
            )
            assert mgr.get_tier() == LicenseTier.PRO

    def test_repeated_get_tier_returns_cached(self, tmp_path):
        """get_tier() doesn't re-resolve on every call."""
        key = _signed_key(LicenseTier.PRO)
        with mock.patch.dict(os.environ, _env_with_signing(), clear=True):
            mgr = LicenseManager(
                license_key=key,
                license_path=tmp_path / "license",
                cache_path=tmp_path / "cache.json",
            )
            tier1 = mgr.get_tier()
            tier2 = mgr.get_tier()
            assert tier1 == tier2 == LicenseTier.PRO

    def test_corrupted_cache_file_handled(self, tmp_path):
        """Corrupted cache file doesn't crash, falls back gracefully."""
        cache_file = tmp_path / "cache.json"
        cache_file.write_text("not valid json {{{{", encoding="utf-8")

        key = _signed_key(LicenseTier.PRO)
        with mock.patch.dict(os.environ, _env_with_signing(), clear=True):
            mgr = LicenseManager(
                license_key=key,
                license_path=tmp_path / "license",
                cache_path=cache_file,
            )
            assert mgr.get_tier() == LicenseTier.PRO

    def test_readonly_filesystem_cache_write_handled(self, tmp_path):
        """Cache write failure doesn't crash."""
        key = _signed_key(LicenseTier.PRO)
        with mock.patch.dict(os.environ, _env_with_signing(), clear=True):
            mgr = LicenseManager(
                license_key=key,
                license_path=tmp_path / "license",
                cache_path=Path("/dev/null/impossible/cache.json"),
            )
            assert mgr.get_tier() == LicenseTier.PRO

    def test_expired_license_falls_back_to_free(self, tmp_path):
        """An expired resolved license returns FREE on subsequent checks."""
        key = _signed_key(LicenseTier.PRO)
        with mock.patch.dict(os.environ, _env_with_signing(), clear=True):
            mgr = LicenseManager(
                license_key=key,
                license_path=tmp_path / "license",
                cache_path=tmp_path / "cache.json",
            )
            assert mgr.get_tier() == LicenseTier.PRO

            # Manually expire the resolved license
            mgr._resolved.expires_at = time.time() - 1
            assert mgr.get_tier() == LicenseTier.FREE

    def test_unsigned_key_with_pro_prefix_defaults_to_free(self, tmp_path):
        """An unsigned key with a pro prefix (no signing secret) defaults to FREE."""
        key = f"{_KEY_PREFIX_PRO}unsigned_test_key"
        with mock.patch.dict(os.environ, {}, clear=True):
            mgr = LicenseManager(
                license_key=key,
                license_path=tmp_path / "license",
                cache_path=tmp_path / "cache.json",
            )
            assert mgr.get_tier() == LicenseTier.FREE


# ---------------------------------------------------------------------------
# 13. Free-tier resource limits
# ---------------------------------------------------------------------------


class TestFreeTierLimits:
    def test_max_printers_constant(self):
        assert FREE_TIER_MAX_PRINTERS == 2

    def test_max_queued_jobs_constant(self):
        assert FREE_TIER_MAX_QUEUED_JOBS == 10
