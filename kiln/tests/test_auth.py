"""Tests for kiln.auth -- API key authentication module."""

from __future__ import annotations

import hashlib
import os
import time
from unittest import mock

import pytest

from kiln.auth import ApiKey, AuthError, AuthManager, _KEY_PREFIX


# ---------------------------------------------------------------------------
# 1. AuthManager disabled by default (no env vars)
# ---------------------------------------------------------------------------

class TestAuthManagerDefaults:
    def test_disabled_by_default(self):
        """With no env vars set, auth should be disabled."""
        with mock.patch.dict(os.environ, {}, clear=True):
            mgr = AuthManager()
            assert mgr.enabled is False

    # 2. Enabled via constructor parameter
    def test_enabled_via_constructor(self):
        mgr = AuthManager(enabled=True)
        assert mgr.enabled is True

    def test_disabled_via_constructor(self):
        mgr = AuthManager(enabled=False)
        assert mgr.enabled is False

    # 3. Enabled via env var KILN_AUTH_ENABLED=1
    def test_enabled_via_env_var_1(self):
        with mock.patch.dict(os.environ, {"KILN_AUTH_ENABLED": "1"}, clear=True):
            mgr = AuthManager()
            assert mgr.enabled is True

    def test_enabled_via_env_var_true(self):
        with mock.patch.dict(os.environ, {"KILN_AUTH_ENABLED": "true"}, clear=True):
            mgr = AuthManager()
            assert mgr.enabled is True

    def test_enabled_via_env_var_yes(self):
        with mock.patch.dict(os.environ, {"KILN_AUTH_ENABLED": "yes"}, clear=True):
            mgr = AuthManager()
            assert mgr.enabled is True

    def test_not_enabled_via_env_var_random(self):
        with mock.patch.dict(os.environ, {"KILN_AUTH_ENABLED": "nope"}, clear=True):
            mgr = AuthManager()
            assert mgr.enabled is False

    # 28. enable() and disable() toggle
    def test_enable_toggle(self):
        mgr = AuthManager(enabled=False)
        assert mgr.enabled is False
        mgr.enable()
        assert mgr.enabled is True

    def test_disable_toggle(self):
        mgr = AuthManager(enabled=True)
        assert mgr.enabled is True
        mgr.disable()
        assert mgr.enabled is False


# ---------------------------------------------------------------------------
# 4-5. generate_key()
# ---------------------------------------------------------------------------

class TestGenerateKey:
    # 4. Produces sk_kiln_ prefix
    def test_prefix(self):
        key = AuthManager.generate_key()
        assert key.startswith(_KEY_PREFIX)

    def test_key_length(self):
        key = AuthManager.generate_key()
        # prefix + 48 hex chars (24 bytes)
        assert len(key) == len(_KEY_PREFIX) + 48

    # 5. Produces unique keys
    def test_unique_keys(self):
        keys = {AuthManager.generate_key() for _ in range(100)}
        assert len(keys) == 100


# ---------------------------------------------------------------------------
# 6-8. create_key()
# ---------------------------------------------------------------------------

class TestCreateKey:
    # 6. Returns (ApiKey, raw_key)
    def test_returns_api_key_and_raw_key(self):
        mgr = AuthManager(enabled=True)
        api_key, raw_key = mgr.create_key("test-agent")
        assert isinstance(api_key, ApiKey)
        assert isinstance(raw_key, str)
        assert raw_key.startswith(_KEY_PREFIX)

    def test_api_key_has_id_and_name(self):
        mgr = AuthManager(enabled=True)
        api_key, _ = mgr.create_key("my-agent")
        assert api_key.name == "my-agent"
        assert len(api_key.id) == 12  # secrets.token_hex(6) -> 12 chars

    # 7. Custom scopes
    def test_custom_scopes(self):
        mgr = AuthManager(enabled=True)
        api_key, _ = mgr.create_key("admin-agent", scopes=["read", "admin"])
        assert api_key.scopes == {"read", "admin"}

    # 8. Default scopes
    def test_default_scopes(self):
        mgr = AuthManager(enabled=True)
        api_key, _ = mgr.create_key("default-agent")
        assert api_key.scopes == {"read", "write"}

    def test_key_is_active_by_default(self):
        mgr = AuthManager(enabled=True)
        api_key, _ = mgr.create_key("active-agent")
        assert api_key.active is True

    def test_created_at_is_set(self):
        before = time.time()
        mgr = AuthManager(enabled=True)
        api_key, _ = mgr.create_key("timed-agent")
        after = time.time()
        assert before <= api_key.created_at <= after


# ---------------------------------------------------------------------------
# 9-16. verify()
# ---------------------------------------------------------------------------

class TestVerify:
    # 9. Auth disabled returns permissive stub
    def test_disabled_returns_permissive_stub(self):
        mgr = AuthManager(enabled=False)
        result = mgr.verify("anything")
        assert result.id == "none"
        assert result.name == "auth-disabled"
        assert "admin" in result.scopes

    def test_disabled_ignores_key_content(self):
        mgr = AuthManager(enabled=False)
        result = mgr.verify("")
        assert result.id == "none"

    # 10. Valid env key
    def test_valid_env_key(self):
        env_key = "my-secret-env-key"
        with mock.patch.dict(os.environ, {"KILN_AUTH_KEY": env_key}, clear=True):
            mgr = AuthManager(enabled=True)
            result = mgr.verify(env_key)
            assert result.id == "env"
            assert result.name == "environment-key"
            assert "admin" in result.scopes
            assert "read" in result.scopes
            assert "write" in result.scopes

    # 11. Valid created key
    def test_valid_created_key(self):
        mgr = AuthManager(enabled=True)
        api_key, raw_key = mgr.create_key("agent-x")
        result = mgr.verify(raw_key)
        assert result.id == api_key.id
        assert result.name == "agent-x"

    # 12. Invalid key raises AuthError
    def test_invalid_key_raises(self):
        mgr = AuthManager(enabled=True)
        with pytest.raises(AuthError, match="Invalid API key"):
            mgr.verify("sk_kiln_bogus")

    # 13. Revoked key raises AuthError
    def test_revoked_key_raises(self):
        mgr = AuthManager(enabled=True)
        api_key, raw_key = mgr.create_key("revoke-me")
        mgr.revoke_key(api_key.id)
        with pytest.raises(AuthError, match="revoked"):
            mgr.verify(raw_key)

    # 14. Missing scope raises AuthError
    def test_missing_scope_raises(self):
        mgr = AuthManager(enabled=True)
        _, raw_key = mgr.create_key("read-only", scopes=["read"])
        with pytest.raises(AuthError, match="missing required scope"):
            mgr.verify(raw_key, required_scope="admin")

    def test_valid_scope_passes(self):
        mgr = AuthManager(enabled=True)
        _, raw_key = mgr.create_key("rw-agent", scopes=["read", "write"])
        result = mgr.verify(raw_key, required_scope="read")
        assert "read" in result.scopes

    # 15. Empty key raises AuthError
    def test_empty_key_raises(self):
        mgr = AuthManager(enabled=True)
        with pytest.raises(AuthError, match="API key required"):
            mgr.verify("")

    def test_none_key_raises(self):
        mgr = AuthManager(enabled=True)
        with pytest.raises(AuthError, match="API key required"):
            mgr.verify(None)

    # 16. Updates last_used_at
    def test_updates_last_used_at(self):
        mgr = AuthManager(enabled=True)
        api_key, raw_key = mgr.create_key("timestamp-agent")
        assert api_key.last_used_at is None

        before = time.time()
        mgr.verify(raw_key)
        after = time.time()

        assert api_key.last_used_at is not None
        assert before <= api_key.last_used_at <= after


# ---------------------------------------------------------------------------
# 17-18. revoke_key()
# ---------------------------------------------------------------------------

class TestRevokeKey:
    # 17. Revoke by ID
    def test_revoke_existing_key(self):
        mgr = AuthManager(enabled=True)
        api_key, _ = mgr.create_key("to-revoke")
        assert api_key.active is True
        result = mgr.revoke_key(api_key.id)
        assert result is True
        assert api_key.active is False

    # 18. Revoke nonexistent returns False
    def test_revoke_nonexistent(self):
        mgr = AuthManager(enabled=True)
        result = mgr.revoke_key("does-not-exist")
        assert result is False


# ---------------------------------------------------------------------------
# 19-20. delete_key()
# ---------------------------------------------------------------------------

class TestDeleteKey:
    # 19. Delete by ID
    def test_delete_existing_key(self):
        mgr = AuthManager(enabled=True)
        api_key, raw_key = mgr.create_key("to-delete")
        assert len(mgr.list_keys()) == 1
        result = mgr.delete_key(api_key.id)
        assert result is True
        assert len(mgr.list_keys()) == 0
        # Verify key no longer works
        with pytest.raises(AuthError, match="Invalid API key"):
            mgr.verify(raw_key)

    # 20. Delete nonexistent returns False
    def test_delete_nonexistent(self):
        mgr = AuthManager(enabled=True)
        result = mgr.delete_key("does-not-exist")
        assert result is False


# ---------------------------------------------------------------------------
# 21. list_keys()
# ---------------------------------------------------------------------------

class TestListKeys:
    def test_list_keys_empty(self):
        mgr = AuthManager(enabled=True)
        assert mgr.list_keys() == []

    def test_list_keys_returns_all(self):
        mgr = AuthManager(enabled=True)
        mgr.create_key("key-a")
        mgr.create_key("key-b")
        mgr.create_key("key-c")
        keys = mgr.list_keys()
        assert len(keys) == 3
        names = {k.name for k in keys}
        assert names == {"key-a", "key-b", "key-c"}


# ---------------------------------------------------------------------------
# 22-25. check_request()
# ---------------------------------------------------------------------------

class TestCheckRequest:
    # 22. Auth disabled
    def test_disabled_returns_authenticated(self):
        mgr = AuthManager(enabled=False)
        result = mgr.check_request()
        assert result["authenticated"] is True
        assert result["auth_enabled"] is False

    # 23. Valid key
    def test_valid_key(self):
        mgr = AuthManager(enabled=True)
        _, raw_key = mgr.create_key("check-agent")
        result = mgr.check_request(key=raw_key)
        assert result["authenticated"] is True
        assert result["auth_enabled"] is True
        assert result["key_name"] == "check-agent"
        assert "scopes" in result

    # 24. Invalid key
    def test_invalid_key(self):
        mgr = AuthManager(enabled=True)
        result = mgr.check_request(key="sk_kiln_invalid")
        assert result["authenticated"] is False
        assert result["auth_enabled"] is True
        assert "error" in result

    # 25. No key when auth enabled
    def test_no_key_when_enabled(self):
        mgr = AuthManager(enabled=True)
        result = mgr.check_request()
        assert result["authenticated"] is False
        assert result["auth_enabled"] is True
        assert "API key required" in result["error"]

    def test_check_request_with_scope(self):
        mgr = AuthManager(enabled=True)
        _, raw_key = mgr.create_key("scoped-agent", scopes=["read"])
        result = mgr.check_request(key=raw_key, scope="read")
        assert result["authenticated"] is True

    def test_check_request_with_missing_scope(self):
        mgr = AuthManager(enabled=True)
        _, raw_key = mgr.create_key("scoped-agent", scopes=["read"])
        result = mgr.check_request(key=raw_key, scope="admin")
        assert result["authenticated"] is False
        assert "missing required scope" in result["error"]


# ---------------------------------------------------------------------------
# 26. to_dict() serialization
# ---------------------------------------------------------------------------

class TestToDict:
    def test_serialization(self):
        api_key = ApiKey(
            id="abc123",
            name="test",
            key_hash="deadbeef",
            scopes={"write", "read"},
            active=True,
            created_at=1000.0,
            last_used_at=None,
        )
        data = api_key.to_dict()
        assert data["id"] == "abc123"
        assert data["name"] == "test"
        assert data["key_hash"] == "deadbeef"
        assert data["scopes"] == ["read", "write"]  # sorted
        assert data["active"] is True
        assert data["created_at"] == 1000.0
        assert data["last_used_at"] is None

    def test_scopes_are_sorted(self):
        api_key = ApiKey(
            id="x",
            name="y",
            key_hash="z",
            scopes={"zulu", "alpha", "mike"},
        )
        data = api_key.to_dict()
        assert data["scopes"] == ["alpha", "mike", "zulu"]


# ---------------------------------------------------------------------------
# 27. Timing-safe comparison (env key uses hmac.compare_digest)
# ---------------------------------------------------------------------------

class TestTimingSafe:
    def test_env_key_uses_hmac_compare(self):
        """Verify that env key verification uses hmac.compare_digest."""
        env_key = "timing-safe-test-key"
        with mock.patch.dict(os.environ, {"KILN_AUTH_KEY": env_key}, clear=True):
            mgr = AuthManager(enabled=True)

        with mock.patch("kiln.auth.hmac.compare_digest", return_value=True) as mock_cmp:
            result = mgr.verify(env_key)
            mock_cmp.assert_called_once()
            assert result.id == "env"

    def test_env_key_hmac_rejects_mismatch(self):
        """When hmac.compare_digest returns False, env key path is skipped."""
        env_key = "real-key"
        with mock.patch.dict(os.environ, {"KILN_AUTH_KEY": env_key}, clear=True):
            mgr = AuthManager(enabled=True)

        with mock.patch("kiln.auth.hmac.compare_digest", return_value=False):
            # Since hmac returns False, it falls through to registered keys
            # and finds nothing, so it raises AuthError.
            with pytest.raises(AuthError, match="Invalid API key"):
                mgr.verify(env_key)


# ---------------------------------------------------------------------------
# 29. Key hash is SHA-256
# ---------------------------------------------------------------------------

class TestKeyHash:
    def test_hash_is_sha256(self):
        raw = "test-key-value"
        expected = hashlib.sha256(raw.encode()).hexdigest()
        assert AuthManager._hash_key(raw) == expected

    def test_hash_deterministic(self):
        raw = "deterministic-key"
        assert AuthManager._hash_key(raw) == AuthManager._hash_key(raw)

    def test_hash_different_for_different_keys(self):
        assert AuthManager._hash_key("key-a") != AuthManager._hash_key("key-b")


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_env_key_loaded_on_init(self):
        """KILN_AUTH_KEY is hashed and stored at init time."""
        with mock.patch.dict(os.environ, {"KILN_AUTH_KEY": "init-key"}, clear=True):
            mgr = AuthManager(enabled=True)
            expected_hash = hashlib.sha256(b"init-key").hexdigest()
            assert mgr._env_key_hash == expected_hash

    def test_no_env_key_hash_when_unset(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            mgr = AuthManager(enabled=True)
            assert mgr._env_key_hash is None

    def test_constructor_enabled_overrides_env(self):
        """Constructor `enabled` parameter takes precedence over env var."""
        with mock.patch.dict(os.environ, {"KILN_AUTH_ENABLED": "1"}, clear=True):
            mgr = AuthManager(enabled=False)
            assert mgr.enabled is False

    def test_multiple_keys_independent(self):
        """Creating multiple keys should all be independently verifiable."""
        mgr = AuthManager(enabled=True)
        _, key_a = mgr.create_key("agent-a")
        _, key_b = mgr.create_key("agent-b")
        _, key_c = mgr.create_key("agent-c")

        result_a = mgr.verify(key_a)
        result_b = mgr.verify(key_b)
        result_c = mgr.verify(key_c)

        assert result_a.name == "agent-a"
        assert result_b.name == "agent-b"
        assert result_c.name == "agent-c"

    def test_revoke_does_not_affect_other_keys(self):
        mgr = AuthManager(enabled=True)
        key_a, raw_a = mgr.create_key("keep-me")
        key_b, raw_b = mgr.create_key("revoke-me")

        mgr.revoke_key(key_b.id)

        # key_a still works
        result = mgr.verify(raw_a)
        assert result.name == "keep-me"

        # key_b is revoked
        with pytest.raises(AuthError, match="revoked"):
            mgr.verify(raw_b)

    def test_delete_does_not_affect_other_keys(self):
        mgr = AuthManager(enabled=True)
        key_a, raw_a = mgr.create_key("keep-me")
        key_b, raw_b = mgr.create_key("delete-me")

        mgr.delete_key(key_b.id)

        result = mgr.verify(raw_a)
        assert result.name == "keep-me"
        assert len(mgr.list_keys()) == 1
