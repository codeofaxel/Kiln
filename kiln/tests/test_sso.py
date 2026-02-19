"""Tests for SSO authentication (OIDC + SAML) in kiln.sso."""

from __future__ import annotations

import base64
import json
import threading
import time
from unittest.mock import patch

import pytest

from kiln.sso import (
    SSOConfig,
    SSOError,
    SSOManager,
    SSOProtocol,
    SSOUser,
    get_sso_manager,
    map_sso_user_to_role,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def sso_config() -> SSOConfig:
    return SSOConfig(
        protocol=SSOProtocol.OIDC,
        issuer_url="https://idp.example.com",
        client_id="test-client-id",
        client_secret="super-secret",
        redirect_uri="http://localhost:8741/sso/callback",
        allowed_domains=["example.com"],
        role_mapping={"admins": "admin", "engineers": "engineer"},
    )


@pytest.fixture()
def manager(tmp_path) -> SSOManager:
    return SSOManager(config_path=str(tmp_path / "sso.json"))


@pytest.fixture()
def configured_manager(tmp_path, sso_config) -> SSOManager:
    mgr = SSOManager(config_path=str(tmp_path / "sso.json"))
    mgr.configure(sso_config)
    return mgr


@pytest.fixture()
def _clear_singleton():
    """Reset the module-level singleton after each test that touches it."""
    import kiln.sso as sso_mod

    original = sso_mod._sso_manager
    yield
    sso_mod._sso_manager = original


def _fake_discovery() -> dict:
    return {
        "authorization_endpoint": "https://idp.example.com/authorize",
        "token_endpoint": "https://idp.example.com/token",
        "jwks_uri": "https://idp.example.com/.well-known/jwks.json",
        "issuer": "https://idp.example.com",
    }


def _build_saml_response(
    name_id: str = "alice@example.com",
    *,
    email: str | None = None,
    display_name: str | None = None,
    not_on_or_after: str | None = None,
    not_before: str | None = None,
    status_value: str = "urn:oasis:names:tc:SAML:2.0:status:Success",
    groups: list[str] | None = None,
) -> str:
    """Build a minimal base64-encoded SAML Response XML for testing."""
    attrs = ""
    if email:
        attrs += (
            '<saml:Attribute Name="email">'
            f'<saml:AttributeValue>{email}</saml:AttributeValue>'
            "</saml:Attribute>"
        )
    if display_name:
        attrs += (
            '<saml:Attribute Name="displayName">'
            f'<saml:AttributeValue>{display_name}</saml:AttributeValue>'
            "</saml:Attribute>"
        )
    if groups:
        group_values = "".join(
            f'<saml:AttributeValue>{g}</saml:AttributeValue>' for g in groups
        )
        attrs += (
            f'<saml:Attribute Name="groups">{group_values}</saml:Attribute>'
        )

    conditions = ""
    if not_on_or_after or not_before:
        cond_attrs = ""
        if not_on_or_after:
            cond_attrs += f' NotOnOrAfter="{not_on_or_after}"'
        if not_before:
            cond_attrs += f' NotBefore="{not_before}"'
        conditions = f"<saml:Conditions{cond_attrs}/>"

    xml = (
        '<samlp:Response xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol"'
        ' xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion">'
        "<samlp:Status>"
        f'<samlp:StatusCode Value="{status_value}"/>'
        "</samlp:Status>"
        "<saml:Assertion>"
        f"{conditions}"
        f"<saml:Subject><saml:NameID>{name_id}</saml:NameID></saml:Subject>"
        f"<saml:AttributeStatement>{attrs}</saml:AttributeStatement>"
        "</saml:Assertion>"
        "</samlp:Response>"
    )
    return base64.b64encode(xml.encode("utf-8")).decode("ascii")


# =========================================================================
# TestSSOConfig
# =========================================================================


class TestSSOConfig:
    def test_config_defaults(self):
        cfg = SSOConfig(
            protocol=SSOProtocol.OIDC,
            issuer_url="https://idp.example.com",
            client_id="cid",
        )
        assert cfg.client_secret is None
        assert cfg.redirect_uri == "http://localhost:8741/sso/callback"
        assert cfg.allowed_domains == []
        assert cfg.role_mapping == {}
        assert cfg.jwks_uri is None
        assert cfg.saml_metadata_url is None
        assert cfg.enabled is True

    def test_config_to_dict_includes_protocol_as_string(self, sso_config):
        data = sso_config.to_dict()
        assert data["protocol"] == "oidc"
        assert isinstance(data["protocol"], str)

    def test_config_from_env_vars_only(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KILN_SSO_ISSUER", "https://env-idp.example.com")
        monkeypatch.setenv("KILN_SSO_CLIENT_ID", "env-client")
        monkeypatch.setenv("KILN_SSO_CLIENT_SECRET", "env-secret")
        monkeypatch.setenv("KILN_SSO_REDIRECT_URI", "http://localhost:9999/cb")
        monkeypatch.setenv("KILN_SSO_ALLOWED_DOMAINS", "foo.com, bar.com")
        monkeypatch.setenv("KILN_SSO_ROLE_MAPPING", '{"devs": "engineer"}')

        mgr = SSOManager(config_path=str(tmp_path / "sso.json"))
        cfg = mgr.get_config()
        assert cfg is not None
        assert cfg.issuer_url == "https://env-idp.example.com"
        assert cfg.client_id == "env-client"
        assert cfg.client_secret == "env-secret"
        assert cfg.redirect_uri == "http://localhost:9999/cb"
        assert cfg.allowed_domains == ["foo.com", "bar.com"]
        assert cfg.role_mapping == {"devs": "engineer"}

    def test_config_from_file_overridden_by_env(self, tmp_path, sso_config, monkeypatch):
        mgr = SSOManager(config_path=str(tmp_path / "sso.json"))
        mgr.configure(sso_config)

        monkeypatch.setenv("KILN_SSO_ISSUER", "https://override.example.com")
        mgr2 = SSOManager(config_path=str(tmp_path / "sso.json"))
        cfg = mgr2.get_config()
        assert cfg is not None
        assert cfg.issuer_url == "https://override.example.com"
        assert cfg.client_id == "test-client-id"

    def test_config_saml_protocol(self):
        cfg = SSOConfig(
            protocol=SSOProtocol.SAML,
            issuer_url="https://idp.example.com",
            client_id="cid",
        )
        assert cfg.protocol == SSOProtocol.SAML
        assert cfg.to_dict()["protocol"] == "saml"


# =========================================================================
# TestSSOManagerConfiguration
# =========================================================================


class TestSSOManagerConfiguration:
    def test_configure_saves_to_disk(self, tmp_path, sso_config):
        mgr = SSOManager(config_path=str(tmp_path / "sso.json"))
        mgr.configure(sso_config)

        config_path = tmp_path / "sso.json"
        assert config_path.exists()
        data = json.loads(config_path.read_text())
        assert data["issuer_url"] == "https://idp.example.com"
        assert data["client_id"] == "test-client-id"

    def test_configure_separates_client_secret(self, tmp_path, sso_config):
        mgr = SSOManager(config_path=str(tmp_path / "sso.json"))
        mgr.configure(sso_config)

        config_data = json.loads((tmp_path / "sso.json").read_text())
        assert "client_secret" not in config_data

        secret_path = tmp_path / "sso_secret"
        assert secret_path.exists()
        assert secret_path.read_text() == "super-secret"

    def test_load_config_from_file(self, tmp_path, sso_config):
        mgr1 = SSOManager(config_path=str(tmp_path / "sso.json"))
        mgr1.configure(sso_config)

        mgr2 = SSOManager(config_path=str(tmp_path / "sso.json"))
        cfg = mgr2.get_config()
        assert cfg is not None
        assert cfg.issuer_url == "https://idp.example.com"
        assert cfg.client_id == "test-client-id"
        assert cfg.protocol == SSOProtocol.OIDC

    def test_load_config_reads_separate_secret_file(self, tmp_path, sso_config):
        mgr1 = SSOManager(config_path=str(tmp_path / "sso.json"))
        mgr1.configure(sso_config)

        mgr2 = SSOManager(config_path=str(tmp_path / "sso.json"))
        cfg = mgr2.get_config()
        assert cfg is not None
        assert cfg.client_secret == "super-secret"

    def test_remove_config_deletes_files(self, tmp_path, sso_config):
        mgr = SSOManager(config_path=str(tmp_path / "sso.json"))
        mgr.configure(sso_config)

        assert (tmp_path / "sso.json").exists()
        assert (tmp_path / "sso_secret").exists()

        result = mgr.remove_config()
        assert result is True
        assert not (tmp_path / "sso.json").exists()
        assert not (tmp_path / "sso_secret").exists()
        assert mgr.get_config() is None

    def test_remove_config_returns_false_when_not_configured(self, manager):
        assert manager.remove_config() is False

    def test_require_config_raises_when_unconfigured(self, manager):
        with pytest.raises(SSOError, match="not configured"):
            manager._require_config()

    def test_require_config_raises_when_disabled(self, configured_manager):
        configured_manager._config.enabled = False
        with pytest.raises(SSOError, match="disabled"):
            configured_manager._require_config()

    def test_env_override_creates_config_from_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KILN_SSO_ISSUER", "https://env.example.com")
        monkeypatch.setenv("KILN_SSO_CLIENT_ID", "env-cid")

        mgr = SSOManager(config_path=str(tmp_path / "nonexistent.json"))
        cfg = mgr.get_config()
        assert cfg is not None
        assert cfg.issuer_url == "https://env.example.com"
        assert cfg.client_id == "env-cid"


# =========================================================================
# TestOIDCDiscovery
# =========================================================================


class TestOIDCDiscovery:
    def test_discover_oidc_fetches_well_known(self, configured_manager):
        with patch("kiln.sso._http_get_json", return_value=_fake_discovery()) as mock_get:
            doc = configured_manager._discover_oidc("https://idp.example.com")
            assert doc["authorization_endpoint"] == "https://idp.example.com/authorize"
            mock_get.assert_called_once_with(
                "https://idp.example.com/.well-known/openid-configuration"
            )

    def test_discover_oidc_caches_result(self, configured_manager):
        with patch("kiln.sso._http_get_json", return_value=_fake_discovery()) as mock_get:
            configured_manager._discover_oidc("https://idp.example.com")
            configured_manager._discover_oidc("https://idp.example.com")
            assert mock_get.call_count == 1

    def test_discover_oidc_rejects_non_https(self, configured_manager):
        with pytest.raises(SSOError, match="HTTPS"):
            configured_manager._discover_oidc("http://insecure.example.com")

    def test_discover_oidc_handles_network_error(self, configured_manager):
        with (
            patch("kiln.sso._http_get_json", side_effect=SSOError("network")),
            pytest.raises(SSOError, match="network"),
        ):
            configured_manager._discover_oidc("https://idp.example.com")

    def test_discover_oidc_cache_expires(self, configured_manager):
        with patch("kiln.sso._http_get_json", return_value=_fake_discovery()) as mock_get:
            configured_manager._discover_oidc("https://idp.example.com")
            assert mock_get.call_count == 1

            # Expire the cache
            configured_manager._oidc_discovery_cached_at["https://idp.example.com"] = (
                time.time() - 7200
            )
            configured_manager._discover_oidc("https://idp.example.com")
            assert mock_get.call_count == 2


# =========================================================================
# TestJWTValidation
# =========================================================================


class TestJWTValidation:
    """Tests for _validate_claims (JWT claim validation without signature)."""

    def _base_payload(self) -> dict:
        now = time.time()
        return {
            "iss": "https://idp.example.com",
            "aud": "test-client-id",
            "exp": now + 3600,
            "iat": now,
            "sub": "user-123",
            "email": "alice@example.com",
        }

    def _base_config(self) -> SSOConfig:
        return SSOConfig(
            protocol=SSOProtocol.OIDC,
            issuer_url="https://idp.example.com",
            client_id="test-client-id",
        )

    def test_validate_claims_requires_exp(self, configured_manager):
        payload = self._base_payload()
        del payload["exp"]
        with pytest.raises(SSOError, match="exp"):
            configured_manager._validate_claims(payload, self._base_config())

    def test_validate_claims_requires_aud(self, configured_manager):
        payload = self._base_payload()
        del payload["aud"]
        with pytest.raises(SSOError, match="aud"):
            configured_manager._validate_claims(payload, self._base_config())

    def test_validate_claims_rejects_expired_token(self, configured_manager):
        payload = self._base_payload()
        payload["exp"] = time.time() - 120
        with pytest.raises(SSOError, match="expired"):
            configured_manager._validate_claims(payload, self._base_config())

    def test_validate_claims_rejects_wrong_issuer(self, configured_manager):
        payload = self._base_payload()
        payload["iss"] = "https://evil.example.com"
        with pytest.raises(SSOError, match="issuer mismatch"):
            configured_manager._validate_claims(payload, self._base_config())

    def test_validate_claims_rejects_wrong_audience(self, configured_manager):
        payload = self._base_payload()
        payload["aud"] = "wrong-client-id"
        with pytest.raises(SSOError, match="audience mismatch"):
            configured_manager._validate_claims(payload, self._base_config())

    def test_validate_claims_allows_clock_skew(self, configured_manager):
        payload = self._base_payload()
        # Token expired 30 seconds ago -- within 60s clock skew
        payload["exp"] = time.time() - 30
        # Should NOT raise
        configured_manager._validate_claims(payload, self._base_config())

    def test_validate_claims_checks_nbf(self, configured_manager):
        payload = self._base_payload()
        payload["nbf"] = time.time() + 300  # 5 min in the future
        with pytest.raises(SSOError, match="not yet valid"):
            configured_manager._validate_claims(payload, self._base_config())

    def test_validate_claims_rejects_nonce_mismatch(self, configured_manager):
        payload = self._base_payload()
        payload["nonce"] = "token-nonce"
        with pytest.raises(SSOError, match="nonce mismatch"):
            configured_manager._validate_claims(
                payload, self._base_config(), expected_nonce="expected-nonce"
            )

    def test_validate_claims_accepts_valid_nonce(self, configured_manager):
        payload = self._base_payload()
        payload["nonce"] = "correct-nonce"
        # Should NOT raise
        configured_manager._validate_claims(
            payload, self._base_config(), expected_nonce="correct-nonce"
        )

    def test_validate_claims_audience_as_list(self, configured_manager):
        payload = self._base_payload()
        payload["aud"] = ["other-client", "test-client-id"]
        # Should NOT raise since test-client-id is in the list
        configured_manager._validate_claims(payload, self._base_config())


# =========================================================================
# TestEmailDomainValidation
# =========================================================================


class TestEmailDomainValidation:
    def test_validate_email_domain_accepts_allowed(self, configured_manager, sso_config):
        # Should not raise
        configured_manager._validate_email_domain("alice@example.com", sso_config)

    def test_validate_email_domain_rejects_unlisted(self, configured_manager, sso_config):
        with pytest.raises(SSOError, match="not in the allowed domains"):
            configured_manager._validate_email_domain("bob@evil.com", sso_config)

    def test_validate_email_domain_case_insensitive(self, configured_manager, sso_config):
        # Should not raise -- domain matching is case-insensitive
        configured_manager._validate_email_domain("alice@EXAMPLE.COM", sso_config)

    def test_validate_email_domain_empty_allows_any(self, configured_manager):
        cfg = SSOConfig(
            protocol=SSOProtocol.OIDC,
            issuer_url="https://idp.example.com",
            client_id="cid",
            allowed_domains=[],
        )
        # Should not raise when allowed_domains is empty
        configured_manager._validate_email_domain("anyone@anywhere.com", cfg)

    def test_validate_email_domain_handles_malformed_email(self, configured_manager, sso_config):
        with pytest.raises(SSOError, match="not in the allowed domains"):
            configured_manager._validate_email_domain("no-at-sign", sso_config)


# =========================================================================
# TestRoleMapping
# =========================================================================


class TestRoleMapping:
    def test_map_roles_from_groups_claim(self, configured_manager):
        claims = {"groups": ["admins", "users"]}
        roles = configured_manager._map_roles(claims)
        assert "admin" in roles

    def test_map_roles_from_cognito_groups(self, configured_manager):
        claims = {"cognito:groups": ["engineers"]}
        roles = configured_manager._map_roles(claims)
        assert "engineer" in roles

    def test_map_roles_from_keycloak_realm_access(self, configured_manager):
        claims = {"realm_access": {"roles": ["admins"]}}
        roles = configured_manager._map_roles(claims)
        assert "admin" in roles

    def test_map_roles_no_mapping_returns_empty(self, tmp_path):
        cfg = SSOConfig(
            protocol=SSOProtocol.OIDC,
            issuer_url="https://idp.example.com",
            client_id="cid",
            role_mapping={},
        )
        mgr = SSOManager(config_path=str(tmp_path / "sso.json"))
        mgr.configure(cfg)
        roles = mgr._map_roles({"groups": ["admins"]})
        assert roles == []

    def test_map_roles_unknown_groups_skipped(self, configured_manager):
        claims = {"groups": ["unknown-group", "random-team"]}
        roles = configured_manager._map_roles(claims)
        assert roles == []


# =========================================================================
# TestOIDCAuthorizeUrl
# =========================================================================


class TestOIDCAuthorizeUrl:
    def test_authorize_url_includes_pkce_params(self, configured_manager):
        with patch("kiln.sso._http_get_json", return_value=_fake_discovery()):
            url = configured_manager.get_oidc_authorize_url()
            assert "code_challenge=" in url
            assert "code_challenge_method=S256" in url

    def test_authorize_url_includes_state_and_nonce(self, configured_manager):
        with patch("kiln.sso._http_get_json", return_value=_fake_discovery()):
            url = configured_manager.get_oidc_authorize_url()
            assert "state=" in url
            assert "nonce=" in url

    def test_authorize_url_uses_custom_state(self, configured_manager):
        with patch("kiln.sso._http_get_json", return_value=_fake_discovery()):
            url = configured_manager.get_oidc_authorize_url(state="my-custom-state")
            assert "state=my-custom-state" in url

    def test_authorize_url_stores_pending_flow(self, configured_manager):
        with patch("kiln.sso._http_get_json", return_value=_fake_discovery()):
            configured_manager.get_oidc_authorize_url(state="tracked-state")
            assert "tracked-state" in configured_manager._pending_flows
            flow = configured_manager._pending_flows["tracked-state"]
            assert "code_verifier" in flow
            assert "nonce" in flow


# =========================================================================
# TestOIDCCodeExchange
# =========================================================================


class TestOIDCCodeExchange:
    def test_exchange_validates_state(self, configured_manager):
        with patch("kiln.sso._http_get_json", return_value=_fake_discovery()):
            configured_manager.get_oidc_authorize_url(state="valid-state")

        with pytest.raises(SSOError, match="Invalid or expired OIDC state"):
            configured_manager.exchange_oidc_code(code="auth-code", state="wrong-state")

    def test_exchange_rejects_invalid_state(self, configured_manager):
        with pytest.raises(SSOError, match="Invalid or expired OIDC state"):
            configured_manager.exchange_oidc_code(code="auth-code", state="never-issued")

    def test_exchange_sends_code_verifier(self, configured_manager):
        with patch("kiln.sso._http_get_json", return_value=_fake_discovery()):
            configured_manager.get_oidc_authorize_url(state="pkce-state")

        stored_verifier = configured_manager._pending_flows["pkce-state"]["code_verifier"]

        def fake_post(url, data, **kwargs):
            assert data["code_verifier"] == stored_verifier
            return {
                "id_token": "fake.jwt.token",
                "access_token": "access-token",
            }

        with (
            patch("kiln.sso._http_get_json", return_value=_fake_discovery()),
            patch("kiln.sso._http_post_form", side_effect=fake_post),
            patch.object(configured_manager, "validate_oidc_token") as mock_validate,
        ):
            mock_validate.return_value = SSOUser(
                email="alice@example.com", name="Alice", sub="123"
            )
            configured_manager.exchange_oidc_code(code="auth-code", state="pkce-state")
            mock_validate.assert_called_once()

    def test_exchange_passes_nonce_to_validation(self, configured_manager):
        with patch("kiln.sso._http_get_json", return_value=_fake_discovery()):
            configured_manager.get_oidc_authorize_url(state="nonce-state")

        stored_nonce = configured_manager._pending_flows["nonce-state"]["nonce"]

        def fake_post(url, data, **kwargs):
            return {"id_token": "fake.jwt.token"}

        with (
            patch("kiln.sso._http_get_json", return_value=_fake_discovery()),
            patch("kiln.sso._http_post_form", side_effect=fake_post),
            patch.object(configured_manager, "validate_oidc_token") as mock_validate,
        ):
            mock_validate.return_value = SSOUser(
                email="alice@example.com", name="Alice", sub="123"
            )
            configured_manager.exchange_oidc_code(code="auth-code", state="nonce-state")
            _, kwargs = mock_validate.call_args
            assert kwargs["expected_nonce"] == stored_nonce

    def test_exchange_raises_on_missing_id_token(self, configured_manager):
        with patch("kiln.sso._http_get_json", return_value=_fake_discovery()):
            configured_manager.get_oidc_authorize_url(state="no-token-state")

        def fake_post(url, data, **kwargs):
            return {"access_token": "at", "token_type": "bearer"}

        with (
            patch("kiln.sso._http_get_json", return_value=_fake_discovery()),
            patch("kiln.sso._http_post_form", side_effect=fake_post),
            pytest.raises(SSOError, match="No id_token"),
        ):
            configured_manager.exchange_oidc_code(
                code="auth-code", state="no-token-state"
            )


# =========================================================================
# TestSAMLProcessing
# =========================================================================


class TestSAMLProcessing:
    def test_saml_blocked_by_default(self, configured_manager, monkeypatch):
        monkeypatch.delenv("KILN_SSO_SAML_ALLOW_UNSIGNED", raising=False)
        saml_resp = _build_saml_response()
        with pytest.raises(SSOError, match="signature validation"):
            configured_manager.process_saml_response(saml_resp)

    def test_saml_extracts_name_id(self, configured_manager, monkeypatch):
        monkeypatch.setenv("KILN_SSO_SAML_ALLOW_UNSIGNED", "1")
        future = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3600)
        )
        saml_resp = _build_saml_response(
            name_id="alice@example.com",
            not_on_or_after=future,
        )
        user = configured_manager.process_saml_response(saml_resp)
        assert user.sub == "alice@example.com"
        assert user.email == "alice@example.com"

    def test_saml_extracts_email_from_attributes(self, configured_manager, monkeypatch):
        monkeypatch.setenv("KILN_SSO_SAML_ALLOW_UNSIGNED", "1")
        future = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3600)
        )
        saml_resp = _build_saml_response(
            name_id="user-123",
            email="alice@example.com",
            display_name="Alice Smith",
            not_on_or_after=future,
        )
        user = configured_manager.process_saml_response(saml_resp)
        assert user.email == "alice@example.com"
        assert user.name == "Alice Smith"

    def test_saml_validates_email_domain(self, configured_manager, monkeypatch):
        monkeypatch.setenv("KILN_SSO_SAML_ALLOW_UNSIGNED", "1")
        future = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3600)
        )
        saml_resp = _build_saml_response(
            name_id="evil@hacker.com",
            not_on_or_after=future,
        )
        with pytest.raises(SSOError, match="not in the allowed domains"):
            configured_manager.process_saml_response(saml_resp)

    def test_saml_enforces_not_on_or_after(self, configured_manager, monkeypatch):
        monkeypatch.setenv("KILN_SSO_SAML_ALLOW_UNSIGNED", "1")
        past = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 3600)
        )
        saml_resp = _build_saml_response(
            name_id="alice@example.com",
            not_on_or_after=past,
        )
        with pytest.raises(SSOError, match="expired.*NotOnOrAfter"):
            configured_manager.process_saml_response(saml_resp)

    def test_saml_enforces_not_before(self, configured_manager, monkeypatch):
        monkeypatch.setenv("KILN_SSO_SAML_ALLOW_UNSIGNED", "1")
        future = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 7200)
        )
        far_future = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 14400)
        )
        saml_resp = _build_saml_response(
            name_id="alice@example.com",
            not_before=future,
            not_on_or_after=far_future,
        )
        with pytest.raises(SSOError, match="not yet valid.*NotBefore"):
            configured_manager.process_saml_response(saml_resp)


# =========================================================================
# TestMapSSOUserToRole
# =========================================================================


class TestMapSSOUserToRole:
    def test_highest_role_wins(self):
        user = SSOUser(
            email="a@example.com",
            name="A",
            sub="1",
            roles=["engineer", "admin", "operator"],
        )
        assert map_sso_user_to_role(user) == "admin"

    def test_defaults_to_operator(self):
        user = SSOUser(
            email="a@example.com",
            name="A",
            sub="1",
            roles=[],
        )
        assert map_sso_user_to_role(user) == "operator"

    def test_single_role_returned(self):
        user = SSOUser(
            email="a@example.com",
            name="A",
            sub="1",
            roles=["engineer"],
        )
        assert map_sso_user_to_role(user) == "engineer"


# =========================================================================
# TestSSOManagerSingleton
# =========================================================================


class TestSSOManagerSingleton:
    @pytest.mark.usefixtures("_clear_singleton")
    def test_get_sso_manager_returns_same_instance(self):
        import kiln.sso as sso_mod

        sso_mod._sso_manager = None

        m1 = get_sso_manager()
        m2 = get_sso_manager()
        assert m1 is m2

    @pytest.mark.usefixtures("_clear_singleton")
    def test_singleton_is_thread_safe(self):
        import kiln.sso as sso_mod

        sso_mod._sso_manager = None

        instances: list[SSOManager] = []
        barrier = threading.Barrier(4)

        def worker():
            barrier.wait()
            instances.append(get_sso_manager())

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(instances) == 4
        assert all(inst is instances[0] for inst in instances)
