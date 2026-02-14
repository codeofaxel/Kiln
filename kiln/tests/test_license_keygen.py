"""Tests for kiln.licensing -- license key generation."""

from __future__ import annotations

import base64
import json
import os
from unittest import mock

import pytest

from kiln.licensing import (
    LicenseManager,
    LicenseTier,
    generate_license_key,
)


class TestGenerateLicenseKey:
    """Key generation: format, tiers, validation, edge cases."""

    def test_generates_pro_key(self):
        key = generate_license_key(
            LicenseTier.PRO, "user@example.com", signing_key="secret"
        )
        assert key.startswith("kiln_pro_")
        parts = key.split("_")
        # kiln, pro, payload, signature
        assert len(parts) == 4

    def test_generates_business_key(self):
        key = generate_license_key(
            LicenseTier.BUSINESS, "biz@example.com", signing_key="secret"
        )
        assert key.startswith("kiln_biz_")
        parts = key.split("_")
        assert len(parts) == 4

    def test_free_tier_raises(self):
        with pytest.raises(ValueError, match="FREE"):
            generate_license_key(
                LicenseTier.FREE, "free@example.com", signing_key="secret"
            )

    def test_missing_signing_key_raises(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with pytest.raises(ValueError, match="[Ss]igning key"):
                generate_license_key(LicenseTier.PRO, "user@example.com")

    def test_roundtrip_pro(self, tmp_path):
        key = generate_license_key(
            LicenseTier.PRO, "pro@example.com", signing_key="test-secret"
        )
        with mock.patch.dict(
            os.environ, {"KILN_LICENSE_PUBLIC_KEY": "test-secret"}, clear=True
        ):
            mgr = LicenseManager(
                license_key=key,
                license_path=tmp_path / "license",
                cache_path=tmp_path / "cache.json",
            )
            assert mgr.get_tier() == LicenseTier.PRO

    def test_roundtrip_business(self, tmp_path):
        key = generate_license_key(
            LicenseTier.BUSINESS, "biz@example.com", signing_key="test-secret"
        )
        with mock.patch.dict(
            os.environ, {"KILN_LICENSE_PUBLIC_KEY": "test-secret"}, clear=True
        ):
            mgr = LicenseManager(
                license_key=key,
                license_path=tmp_path / "license",
                cache_path=tmp_path / "cache.json",
            )
            assert mgr.get_tier() == LicenseTier.BUSINESS

    def test_payload_contains_email(self):
        key = generate_license_key(
            LicenseTier.PRO, "check@example.com", signing_key="secret"
        )
        payload_b64 = key.split("_")[2]
        payload_bytes = base64.b64decode(payload_b64 + "==")
        payload = json.loads(payload_bytes)
        assert payload["email"] == "check@example.com"

    def test_custom_ttl(self):
        key = generate_license_key(
            LicenseTier.PRO, "ttl@example.com", signing_key="secret", ttl_seconds=3600
        )
        payload_b64 = key.split("_")[2]
        payload_bytes = base64.b64decode(payload_b64 + "==")
        payload = json.loads(payload_bytes)
        delta = payload["expires_at"] - payload["issued_at"]
        assert abs(delta - 3600) < 1

    def test_expired_key_resolves_to_free(self, tmp_path):
        key = generate_license_key(
            LicenseTier.PRO,
            "expired@example.com",
            signing_key="test-secret",
            ttl_seconds=-1,
        )
        with mock.patch.dict(
            os.environ, {"KILN_LICENSE_PUBLIC_KEY": "test-secret"}, clear=True
        ):
            mgr = LicenseManager(
                license_key=key,
                license_path=tmp_path / "license",
                cache_path=tmp_path / "cache.json",
            )
            assert mgr.get_tier() == LicenseTier.FREE
