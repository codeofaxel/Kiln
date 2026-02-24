"""Tests for pilot_access entitlement enforcement."""

from __future__ import annotations

import base64
import json
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from kiln.licensing import LicenseTier, generate_license_key_v2, parse_license_claims
from kiln.pilot_access import EntitlementEnforcer, hash_identifier


class _FakeStore:
    def __init__(self) -> None:
        self.grants: dict[str, dict[str, Any]] = {}
        self.events: list[dict[str, Any]] = []
        self.activation_hashes: dict[str, set[str]] = {}

    def get_grant_detailed(self, jti: str) -> tuple[dict[str, Any] | None, str | None]:
        return self.grants.get(jti), None

    def activation_device_hashes(self, jti: str) -> set[str]:
        return set(self.activation_hashes.get(jti, set()))

    def record_security_event(self, **kwargs: Any) -> tuple[bool, str | None]:
        self.events.append(kwargs)
        if kwargs.get("event_type") == "activation":
            jti = str(kwargs.get("jti", ""))
            device = str(kwargs.get("device_fingerprint", ""))
            if jti and device:
                self.activation_hashes.setdefault(jti, set()).add(hash_identifier(device))
        return True, None

    def list_revocations(self, *, limit: int = 500, since_iso: str | None = None) -> list[dict[str, Any]]:
        rows = [v for v in self.grants.values() if str(v.get("status", "")).lower() == "revoked"]
        return rows[:limit]


def _make_v2_key_for_test(monkeypatch, tier: LicenseTier = LicenseTier.PRO) -> tuple[str, dict[str, Any]]:
    private_key = Ed25519PrivateKey.generate()
    private_raw = private_key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_raw = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    private_hex = private_raw.hex()
    public_b64u = base64.urlsafe_b64encode(public_raw).decode("ascii").rstrip("=")
    monkeypatch.setenv("KILN_LICENSE_VERIFY_KEYS_JSON", json.dumps({"k1": public_b64u}))
    key = generate_license_key_v2(tier=tier, email="pilot@example.com", signing_private_key=private_hex)
    claims = parse_license_claims(key) or {}
    return key, claims


class TestEntitlementEnforcer:
    def test_revoked_grant_denies_license(self, monkeypatch):
        key, claims = _make_v2_key_for_test(monkeypatch, LicenseTier.PRO)
        jti = str(claims["jti"])
        store = _FakeStore()
        store.grants[jti] = {
            "jti": jti,
            "status": "revoked",
            "expires_at": "2099-01-01T00:00:00+00:00",
            "max_activations": 3,
        }
        enforcer = EntitlementEnforcer(store=store, require_ledger_for_v2=True)

        decision = enforcer.evaluate_license(
            license_key=key,
            enforce_activation_cap=True,
            device_fingerprint="device-a",
        )
        assert decision.valid is False
        assert "revoked" in decision.reason.lower()

    def test_activation_limit_enforced(self, monkeypatch):
        key, claims = _make_v2_key_for_test(monkeypatch, LicenseTier.BUSINESS)
        jti = str(claims["jti"])
        store = _FakeStore()
        store.grants[jti] = {
            "jti": jti,
            "status": "active",
            "expires_at": "2099-01-01T00:00:00+00:00",
            "max_activations": 1,
        }
        store.activation_hashes[jti] = {hash_identifier("existing-device")}
        enforcer = EntitlementEnforcer(store=store, require_ledger_for_v2=True)

        decision = enforcer.evaluate_license(
            license_key=key,
            device_fingerprint="new-device",
            enforce_activation_cap=True,
        )
        assert decision.valid is False
        assert "activation limit" in decision.reason.lower()

    def test_missing_device_fingerprint_is_rejected_when_activation_enforced(self, monkeypatch):
        key, claims = _make_v2_key_for_test(monkeypatch, LicenseTier.PRO)
        jti = str(claims["jti"])
        store = _FakeStore()
        store.grants[jti] = {
            "jti": jti,
            "status": "active",
            "expires_at": "2099-01-01T00:00:00+00:00",
            "max_activations": 2,
        }
        enforcer = EntitlementEnforcer(store=store, require_ledger_for_v2=True)
        decision = enforcer.evaluate_license(license_key=key, enforce_activation_cap=True)
        assert decision.valid is False
        assert "fingerprint" in decision.reason.lower()

    def test_auto_activation_records_event(self, monkeypatch):
        key, claims = _make_v2_key_for_test(monkeypatch, LicenseTier.PRO)
        jti = str(claims["jti"])
        store = _FakeStore()
        store.grants[jti] = {
            "jti": jti,
            "status": "active",
            "expires_at": "2099-01-01T00:00:00+00:00",
            "max_activations": 2,
        }
        enforcer = EntitlementEnforcer(store=store, require_ledger_for_v2=True)

        decision = enforcer.evaluate_license(
            license_key=key,
            device_fingerprint="device-a",
            ip_address_raw="127.0.0.1",
            client_version="test",
            enforce_activation_cap=True,
            auto_activate_if_needed=True,
            record_event=True,
        )
        assert decision.valid is True
        assert decision.activation_count == 1
        assert any(evt.get("event_type") == "activation" for evt in store.events)
        assert any(evt.get("event_type") == "validation" for evt in store.events)

    def test_expired_grant_denies_license(self, monkeypatch):
        key, claims = _make_v2_key_for_test(monkeypatch, LicenseTier.PRO)
        jti = str(claims["jti"])
        store = _FakeStore()
        store.grants[jti] = {
            "jti": jti,
            "status": "active",
            "expires_at": "2000-01-01T00:00:00+00:00",
            "max_activations": 2,
        }
        enforcer = EntitlementEnforcer(store=store, require_ledger_for_v2=True)

        decision = enforcer.evaluate_license(license_key=key)
        assert decision.valid is False
        assert "expired" in decision.reason.lower()
