"""Tests for RBAC extensions in kiln.auth."""

from __future__ import annotations

from kiln.auth import ROLE_SCOPES, AuthManager, Role, resolve_role_scopes


class TestRole:
    def test_role_values(self):
        assert Role.ADMIN.value == "admin"
        assert Role.ENGINEER.value == "engineer"
        assert Role.OPERATOR.value == "operator"

    def test_role_scopes_admin(self):
        assert ROLE_SCOPES[Role.ADMIN] == {"read", "write", "admin"}

    def test_role_scopes_engineer(self):
        assert ROLE_SCOPES[Role.ENGINEER] == {"read", "write"}

    def test_role_scopes_operator(self):
        assert ROLE_SCOPES[Role.OPERATOR] == {"read"}

    def test_admin_superset_of_engineer(self):
        assert ROLE_SCOPES[Role.ENGINEER].issubset(ROLE_SCOPES[Role.ADMIN])

    def test_engineer_superset_of_operator(self):
        assert ROLE_SCOPES[Role.OPERATOR].issubset(ROLE_SCOPES[Role.ENGINEER])


class TestResolveRoleScopes:
    def test_valid_role(self):
        assert resolve_role_scopes("admin") == {"read", "write", "admin"}

    def test_invalid_role(self):
        assert resolve_role_scopes("superuser") == set()

    def test_case_sensitive(self):
        assert resolve_role_scopes("Admin") == set()


class TestCreateKeyWithRole:
    def test_admin_key_has_all_scopes(self):
        mgr = AuthManager(enabled=False)
        api_key, raw = mgr.create_key_with_role("test-admin", Role.ADMIN)
        assert api_key.scopes == {"read", "write", "admin"}
        assert api_key.role == "admin"

    def test_engineer_key_has_rw_scopes(self):
        mgr = AuthManager(enabled=False)
        api_key, raw = mgr.create_key_with_role("test-eng", Role.ENGINEER)
        assert api_key.scopes == {"read", "write"}
        assert api_key.role == "engineer"

    def test_operator_key_has_read_only(self):
        mgr = AuthManager(enabled=False)
        api_key, raw = mgr.create_key_with_role("test-op", Role.OPERATOR)
        assert api_key.scopes == {"read"}
        assert api_key.role == "operator"

    def test_key_is_valid(self):
        mgr = AuthManager(enabled=False)
        _, raw = mgr.create_key_with_role("test", Role.ADMIN)
        assert raw.startswith("sk_kiln_")


class TestGetKeyRole:
    def test_returns_role(self):
        mgr = AuthManager(enabled=False)
        _, raw = mgr.create_key_with_role("test", Role.ENGINEER)
        assert mgr.get_key_role(raw) == "engineer"

    def test_returns_none_for_unknown_key(self):
        mgr = AuthManager(enabled=False)
        assert mgr.get_key_role("nonexistent") is None

    def test_returns_none_for_key_without_role(self):
        mgr = AuthManager(enabled=False)
        _, raw = mgr.create_key("no-role")
        assert mgr.get_key_role(raw) is None
