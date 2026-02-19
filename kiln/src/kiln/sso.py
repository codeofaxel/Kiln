"""SSO authentication (OIDC + SAML) for the Enterprise tier.

Provides single sign-on integration with identity providers via OpenID
Connect (primary) and SAML 2.0 (secondary). SSO tokens coexist with
API keys -- the auth layer tries both.

OIDC JWT validation is handled manually using the ``cryptography``
package (no authlib dependency). SAML support is basic; production
deployments requiring full spec compliance should use python-saml2.

SSO configuration is persisted to ``~/.kiln/sso.json``.

Usage::

    from kiln.sso import get_sso_manager

    mgr = get_sso_manager()
    mgr.configure(SSOConfig(
        protocol=SSOProtocol.OIDC,
        issuer_url="https://accounts.google.com",
        client_id="my-client-id",
        redirect_uri="http://localhost:8741/sso/callback",
        allowed_domains=["openmind.ai"],
        role_mapping={"admins": "admin", "engineers": "engineer"},
    ))
    url = mgr.get_oidc_authorize_url(state="random-state")
    user = mgr.exchange_oidc_code(code="auth-code-from-callback")
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zlib
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_SSO_DIR = Path.home() / ".kiln"
_SSO_FILE = _SSO_DIR / "sso.json"

# JWKS cache TTL: 1 hour.
_JWKS_CACHE_TTL: float = 3600


# ---------------------------------------------------------------------------
# Enums & Dataclasses
# ---------------------------------------------------------------------------


class SSOProtocol(str, Enum):
    """Supported SSO protocols."""

    OIDC = "oidc"
    SAML = "saml"


class SSOError(Exception):
    """Raised for SSO authentication or configuration failures."""


@dataclass
class SSOConfig:
    """SSO provider configuration.

    Attributes:
        protocol: The SSO protocol (OIDC or SAML).
        issuer_url: Identity provider issuer URL.
        client_id: OAuth2/OIDC client ID.
        client_secret: OAuth2/OIDC client secret (optional for public clients).
        redirect_uri: Redirect URI after authentication.
        allowed_domains: Email domains allowed to authenticate.
        role_mapping: Maps IdP group/role names to Kiln role names.
        jwks_uri: JWKS endpoint (auto-discovered for OIDC if not set).
        saml_metadata_url: SAML metadata URL (for SAML protocol).
        enabled: Whether SSO is currently active.
    """

    protocol: SSOProtocol
    issuer_url: str
    client_id: str
    client_secret: str | None = None
    redirect_uri: str = "http://localhost:8741/sso/callback"
    allowed_domains: list[str] = field(default_factory=list)
    role_mapping: dict[str, str] = field(default_factory=dict)
    jwks_uri: str | None = None
    saml_metadata_url: str | None = None
    enabled: bool = True

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["protocol"] = self.protocol.value
        return data


@dataclass
class SSOUser:
    """An authenticated SSO user.

    Attributes:
        email: User's email from the IdP.
        name: Display name.
        sub: Subject identifier from the IdP.
        roles: Mapped Kiln role names.
        groups: Raw group/role claims from the IdP.
        provider: Issuer URL of the authenticating IdP.
        authenticated_at: Unix timestamp of authentication.
        token_expires_at: Unix timestamp when the token expires.
    """

    email: str
    name: str
    sub: str
    roles: list[str] = field(default_factory=list)
    groups: list[str] = field(default_factory=list)
    provider: str = ""
    authenticated_at: float = field(default_factory=time.time)
    token_expires_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _base64url_decode(data: str) -> bytes:
    """Decode a base64url-encoded string (no padding required)."""
    # Add padding if needed
    padding = 4 - len(data) % 4
    if padding != 4:
        data += "=" * padding
    return base64.urlsafe_b64decode(data)


def _base64url_encode(data: bytes) -> str:
    """Encode bytes to a base64url string (no padding)."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _http_get_json(url: str, *, timeout: float = 10) -> dict[str, Any]:
    """Fetch JSON from a URL using urllib."""
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        raise SSOError(f"Failed to fetch {url}: {exc}") from exc


def _http_post_form(
    url: str,
    data: dict[str, str],
    *,
    timeout: float = 10,
) -> dict[str, Any]:
    """POST form-encoded data and return JSON response."""
    encoded = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=encoded,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        raise SSOError(f"Failed to POST to {url}: {exc}") from exc


# ---------------------------------------------------------------------------
# SSOManager
# ---------------------------------------------------------------------------


class SSOManager:
    """Manages SSO configuration, OIDC flows, and SAML flows.

    Persists configuration to ``~/.kiln/sso.json``.
    """

    def __init__(self, *, config_path: str | None = None) -> None:
        resolved = config_path or os.environ.get("KILN_SSO_CONFIG_PATH")
        self._config_path = Path(resolved) if resolved else _SSO_FILE
        self._config: SSOConfig | None = None
        self._jwks_cache: dict[str, Any] = {}
        self._jwks_cached_at: float = 0.0
        self._oidc_discovery_cache: dict[str, Any] = {}
        self._load_config()

    # ------------------------------------------------------------------
    # Configuration persistence
    # ------------------------------------------------------------------

    def _load_config(self) -> None:
        """Load SSO config from disk, then overlay env vars."""
        self._config = self._load_config_from_file()
        self._apply_env_overrides()

    def _load_config_from_file(self) -> SSOConfig | None:
        """Read config JSON from disk."""
        if not self._config_path.exists():
            return None
        try:
            data = json.loads(self._config_path.read_text(encoding="utf-8"))
            return SSOConfig(
                protocol=SSOProtocol(data.get("protocol", "oidc")),
                issuer_url=data.get("issuer_url", ""),
                client_id=data.get("client_id", ""),
                client_secret=data.get("client_secret"),
                redirect_uri=data.get("redirect_uri", "http://localhost:8741/sso/callback"),
                allowed_domains=data.get("allowed_domains", []),
                role_mapping=data.get("role_mapping", {}),
                jwks_uri=data.get("jwks_uri"),
                saml_metadata_url=data.get("saml_metadata_url"),
                enabled=data.get("enabled", True),
            )
        except (json.JSONDecodeError, OSError, ValueError) as exc:
            logger.error("Failed to load SSO config from %s: %s", self._config_path, exc)
            return None

    def _apply_env_overrides(self) -> None:
        """Overlay environment variables onto the loaded config.

        If no config file exists but env vars are set, creates a config
        from env vars alone.
        """
        issuer = os.environ.get("KILN_SSO_ISSUER")
        client_id = os.environ.get("KILN_SSO_CLIENT_ID")

        if not issuer and not client_id:
            return

        if self._config is None:
            self._config = SSOConfig(
                protocol=SSOProtocol.OIDC,
                issuer_url=issuer or "",
                client_id=client_id or "",
            )

        if issuer:
            self._config.issuer_url = issuer
        if client_id:
            self._config.client_id = client_id

        secret = os.environ.get("KILN_SSO_CLIENT_SECRET")
        if secret:
            self._config.client_secret = secret

        redirect = os.environ.get("KILN_SSO_REDIRECT_URI")
        if redirect:
            self._config.redirect_uri = redirect

        domains = os.environ.get("KILN_SSO_ALLOWED_DOMAINS")
        if domains:
            self._config.allowed_domains = [d.strip() for d in domains.split(",") if d.strip()]

        mapping = os.environ.get("KILN_SSO_ROLE_MAPPING")
        if mapping:
            try:
                self._config.role_mapping = json.loads(mapping)
            except json.JSONDecodeError:
                logger.warning("KILN_SSO_ROLE_MAPPING is not valid JSON, ignoring")

    def _save_config(self, config: SSOConfig) -> None:
        """Persist config to disk."""
        self._config_path.parent.mkdir(parents=True, exist_ok=True)
        data = config.to_dict()
        data["updated_at"] = time.time()
        self._config_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def configure(self, config: SSOConfig) -> None:
        """Set and persist an SSO configuration.

        Args:
            config: The SSO configuration to save.
        """
        self._config = config
        self._save_config(config)
        # Invalidate caches
        self._jwks_cache = {}
        self._jwks_cached_at = 0.0
        self._oidc_discovery_cache = {}
        logger.info(
            "SSO configured: protocol=%s issuer=%s",
            config.protocol.value,
            config.issuer_url,
        )

    def get_config(self) -> SSOConfig | None:
        """Return the current SSO config, or ``None`` if unconfigured."""
        return self._config

    def remove_config(self) -> bool:
        """Delete SSO configuration from disk.

        Returns:
            ``True`` if config was deleted, ``False`` if it did not exist.
        """
        self._config = None
        self._jwks_cache = {}
        self._jwks_cached_at = 0.0
        self._oidc_discovery_cache = {}
        if self._config_path.exists():
            self._config_path.unlink()
            logger.info("Removed SSO config at %s", self._config_path)
            return True
        return False

    def _require_config(self) -> SSOConfig:
        """Return config or raise if not configured."""
        if self._config is None:
            raise SSOError("SSO is not configured. Call configure() first.")
        if not self._config.enabled:
            raise SSOError("SSO is disabled. Enable it in the SSO configuration.")
        return self._config

    # ------------------------------------------------------------------
    # OIDC Discovery
    # ------------------------------------------------------------------

    def _discover_oidc(self, issuer_url: str) -> dict[str, Any]:
        """Fetch the OpenID Connect discovery document.

        Args:
            issuer_url: The OIDC issuer URL (e.g. ``https://accounts.google.com``).

        Returns:
            The parsed discovery document.
        """
        if issuer_url in self._oidc_discovery_cache:
            return self._oidc_discovery_cache[issuer_url]

        url = issuer_url.rstrip("/") + "/.well-known/openid-configuration"
        doc = _http_get_json(url)
        self._oidc_discovery_cache[issuer_url] = doc
        return doc

    # ------------------------------------------------------------------
    # JWKS Fetching & Caching
    # ------------------------------------------------------------------

    def _fetch_jwks(self, jwks_uri: str) -> dict[str, Any]:
        """Fetch and cache JWKS keys from the IdP.

        Keys are cached for up to 1 hour.

        Args:
            jwks_uri: The JWKS endpoint URL.

        Returns:
            The parsed JWKS document.
        """
        now = time.time()
        if self._jwks_cache and (now - self._jwks_cached_at) < _JWKS_CACHE_TTL:
            return self._jwks_cache

        jwks = _http_get_json(jwks_uri)
        self._jwks_cache = jwks
        self._jwks_cached_at = now
        return jwks

    def _get_jwks_uri(self, config: SSOConfig) -> str:
        """Resolve the JWKS URI, using discovery if needed."""
        if config.jwks_uri:
            return config.jwks_uri
        discovery = self._discover_oidc(config.issuer_url)
        jwks_uri = discovery.get("jwks_uri")
        if not jwks_uri:
            raise SSOError("JWKS URI not found in OIDC discovery document")
        return jwks_uri

    # ------------------------------------------------------------------
    # JWT Validation (RS256 via cryptography)
    # ------------------------------------------------------------------

    def _validate_jwt_signature(self, token: str, jwks: dict[str, Any]) -> dict[str, Any]:
        """Decode and verify an RS256 JWT using JWKS.

        Args:
            token: The raw JWT string (header.payload.signature).
            jwks: The JWKS document containing public keys.

        Returns:
            The decoded JWT payload as a dict.

        Raises:
            SSOError: If the token is malformed, the key is not found,
                or the signature is invalid.
        """
        from cryptography.hazmat.primitives.asymmetric import padding as asym_padding
        from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicNumbers
        from cryptography.hazmat.primitives.hashes import SHA256

        parts = token.split(".")
        if len(parts) != 3:
            raise SSOError("Malformed JWT: expected 3 dot-separated segments")

        header_b64, payload_b64, sig_b64 = parts

        # Decode header to find kid and alg
        try:
            header = json.loads(_base64url_decode(header_b64))
        except (json.JSONDecodeError, ValueError) as exc:
            raise SSOError(f"Failed to decode JWT header: {exc}") from exc

        alg = header.get("alg", "")
        if alg != "RS256":
            raise SSOError(f"Unsupported JWT algorithm: {alg!r}. Only RS256 is supported.")

        kid = header.get("kid")
        if not kid:
            raise SSOError("JWT header missing 'kid' (key ID)")

        # Find matching key in JWKS
        matching_key = None
        for key in jwks.get("keys", []):
            if key.get("kid") == kid and key.get("kty") == "RSA":
                matching_key = key
                break

        if matching_key is None:
            raise SSOError(f"No RSA key found in JWKS with kid={kid!r}")

        # Construct RSA public key from JWK n and e values
        n_bytes = _base64url_decode(matching_key["n"])
        e_bytes = _base64url_decode(matching_key["e"])

        n_int = int.from_bytes(n_bytes, byteorder="big")
        e_int = int.from_bytes(e_bytes, byteorder="big")

        public_numbers = RSAPublicNumbers(e=e_int, n=n_int)
        public_key = public_numbers.public_key()

        # Verify signature
        signature = _base64url_decode(sig_b64)
        signed_content = f"{header_b64}.{payload_b64}".encode("ascii")

        try:
            public_key.verify(
                signature,
                signed_content,
                asym_padding.PKCS1v15(),
                SHA256(),
            )
        except Exception as exc:
            raise SSOError(f"JWT signature verification failed: {exc}") from exc

        # Decode payload
        try:
            payload = json.loads(_base64url_decode(payload_b64))
        except (json.JSONDecodeError, ValueError) as exc:
            raise SSOError(f"Failed to decode JWT payload: {exc}") from exc

        return payload

    def _validate_claims(
        self,
        payload: dict[str, Any],
        config: SSOConfig,
    ) -> None:
        """Validate standard JWT claims (exp, iss, aud).

        Args:
            payload: Decoded JWT payload.
            config: Current SSO configuration.

        Raises:
            SSOError: If any claim validation fails.
        """
        now = time.time()

        # Check expiration
        exp = payload.get("exp")
        if exp is not None and now >= exp:
            raise SSOError("JWT has expired")

        # Check issuer
        iss = payload.get("iss", "")
        expected_issuer = config.issuer_url.rstrip("/")
        if iss.rstrip("/") != expected_issuer:
            raise SSOError(f"JWT issuer mismatch: expected {expected_issuer!r}, got {iss!r}")

        # Check audience
        aud = payload.get("aud")
        if aud is not None:
            # aud can be a string or a list
            audiences = aud if isinstance(aud, list) else [aud]
            if config.client_id not in audiences:
                raise SSOError(
                    f"JWT audience mismatch: {config.client_id!r} not in {audiences!r}"
                )

    def _validate_email_domain(self, email: str, config: SSOConfig) -> None:
        """Verify the email domain is in the allowed list.

        Args:
            email: User's email address.
            config: Current SSO configuration.

        Raises:
            SSOError: If the domain is not allowed.
        """
        if not config.allowed_domains:
            return  # No domain restriction

        domain = email.rsplit("@", 1)[-1].lower() if "@" in email else ""
        allowed = [d.lower() for d in config.allowed_domains]
        if domain not in allowed:
            raise SSOError(
                f"Email domain {domain!r} is not in the allowed domains: {allowed!r}"
            )

    # ------------------------------------------------------------------
    # Role Mapping
    # ------------------------------------------------------------------

    def _map_roles(self, claims: dict[str, Any]) -> list[str]:
        """Map IdP groups/roles to Kiln roles using the role_mapping config.

        Searches for groups in common claim locations: ``groups``,
        ``roles``, ``cognito:groups``, ``realm_access.roles``.

        Args:
            claims: Decoded JWT claims.

        Returns:
            List of mapped Kiln role names.
        """
        config = self._config
        if config is None or not config.role_mapping:
            return []

        # Collect groups from common claim fields
        idp_groups: list[str] = []

        for claim_key in ("groups", "roles", "cognito:groups"):
            value = claims.get(claim_key)
            if isinstance(value, list):
                idp_groups.extend(value)
            elif isinstance(value, str):
                idp_groups.append(value)

        # Keycloak-style nested roles
        realm_access = claims.get("realm_access")
        if isinstance(realm_access, dict):
            realm_roles = realm_access.get("roles", [])
            if isinstance(realm_roles, list):
                idp_groups.extend(realm_roles)

        # Map to Kiln roles
        mapped: list[str] = []
        for group in idp_groups:
            kiln_role = config.role_mapping.get(group)
            if kiln_role and kiln_role not in mapped:
                mapped.append(kiln_role)

        return mapped

    # ------------------------------------------------------------------
    # User Extraction
    # ------------------------------------------------------------------

    def _extract_user(self, claims: dict[str, Any], config: SSOConfig) -> SSOUser:
        """Build an SSOUser from JWT claims.

        Args:
            claims: Decoded and validated JWT claims.
            config: Current SSO configuration.

        Returns:
            Populated :class:`SSOUser`.

        Raises:
            SSOError: If required claims (email, sub) are missing.
        """
        email = claims.get("email", "")
        if not email:
            raise SSOError("JWT is missing the 'email' claim")

        sub = claims.get("sub", "")
        if not sub:
            raise SSOError("JWT is missing the 'sub' claim")

        self._validate_email_domain(email, config)

        name = claims.get("name", "")
        if not name:
            given = claims.get("given_name", "")
            family = claims.get("family_name", "")
            name = f"{given} {family}".strip()

        # Extract raw groups
        groups: list[str] = []
        for claim_key in ("groups", "roles", "cognito:groups"):
            value = claims.get(claim_key)
            if isinstance(value, list):
                groups.extend(value)
            elif isinstance(value, str):
                groups.append(value)

        roles = self._map_roles(claims)

        return SSOUser(
            email=email,
            name=name,
            sub=sub,
            roles=roles,
            groups=groups,
            provider=claims.get("iss", config.issuer_url),
            authenticated_at=time.time(),
            token_expires_at=float(claims.get("exp", 0)),
        )

    # ------------------------------------------------------------------
    # OIDC Flows
    # ------------------------------------------------------------------

    def get_oidc_authorize_url(self, state: str | None = None) -> str:
        """Build the OIDC authorization URL for the IdP.

        Args:
            state: An opaque value for CSRF protection.

        Returns:
            The full authorization URL to redirect the user to.
        """
        config = self._require_config()

        discovery = self._discover_oidc(config.issuer_url)
        auth_endpoint = discovery.get("authorization_endpoint")
        if not auth_endpoint:
            raise SSOError("Authorization endpoint not found in OIDC discovery")

        params: dict[str, str] = {
            "response_type": "code",
            "client_id": config.client_id,
            "redirect_uri": config.redirect_uri,
            "scope": "openid email profile groups",
        }
        if state:
            params["state"] = state

        return f"{auth_endpoint}?{urllib.parse.urlencode(params)}"

    def exchange_oidc_code(self, code: str) -> SSOUser:
        """Exchange an authorization code for tokens and extract user info.

        Args:
            code: The authorization code from the callback.

        Returns:
            The authenticated :class:`SSOUser`.

        Raises:
            SSOError: If the token exchange or validation fails.
        """
        config = self._require_config()

        discovery = self._discover_oidc(config.issuer_url)
        token_endpoint = discovery.get("token_endpoint")
        if not token_endpoint:
            raise SSOError("Token endpoint not found in OIDC discovery")

        # Exchange code for tokens
        token_data: dict[str, str] = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": config.redirect_uri,
            "client_id": config.client_id,
        }
        if config.client_secret:
            token_data["client_secret"] = config.client_secret

        token_response = _http_post_form(token_endpoint, token_data)

        id_token = token_response.get("id_token")
        if not id_token:
            raise SSOError("No id_token in token response")

        return self.validate_oidc_token(id_token)

    def validate_oidc_token(self, id_token: str) -> SSOUser:
        """Validate an OIDC ID token and extract user info.

        Performs full JWT validation: signature verification via JWKS,
        claim validation (exp, iss, aud), and email domain checks.

        Args:
            id_token: The raw JWT ID token string.

        Returns:
            The authenticated :class:`SSOUser`.

        Raises:
            SSOError: If validation fails at any step.
        """
        config = self._require_config()

        # Fetch JWKS
        jwks_uri = self._get_jwks_uri(config)
        jwks = self._fetch_jwks(jwks_uri)

        # Validate signature and decode
        payload = self._validate_jwt_signature(id_token, jwks)

        # Validate standard claims
        self._validate_claims(payload, config)

        # Extract user
        return self._extract_user(payload, config)

    # ------------------------------------------------------------------
    # SAML Flows
    # ------------------------------------------------------------------

    def get_saml_login_url(self) -> str:
        """Build a SAML AuthnRequest redirect URL.

        Creates a base64-encoded, deflated SAML AuthnRequest and
        appends it to the IdP's SSO URL.

        Returns:
            The full SAML login redirect URL.

        Raises:
            SSOError: If SAML metadata URL is not configured.
        """
        config = self._require_config()

        if not config.saml_metadata_url:
            raise SSOError("SAML metadata URL is not configured")

        # Fetch IdP metadata to find SSO URL
        metadata = _http_get_json(config.saml_metadata_url)
        sso_url = metadata.get("sso_url", "")
        if not sso_url:
            # Try fetching XML metadata and parsing
            sso_url = self._parse_saml_metadata_sso_url(config.saml_metadata_url)

        if not sso_url:
            raise SSOError("Could not determine SAML SSO URL from metadata")

        # Build a minimal AuthnRequest
        request_id = f"_kiln_{int(time.time())}_{os.urandom(4).hex()}"
        issue_instant = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        authn_request = (
            f'<samlp:AuthnRequest xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol"'
            f' xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion"'
            f' ID="{request_id}"'
            f' Version="2.0"'
            f' IssueInstant="{issue_instant}"'
            f' AssertionConsumerServiceURL="{config.redirect_uri}"'
            f' ProtocolBinding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST">'
            f"<saml:Issuer>{config.client_id}</saml:Issuer>"
            f"</samlp:AuthnRequest>"
        )

        # Deflate and base64-encode
        deflated = zlib.compress(authn_request.encode("utf-8"))[2:-4]  # raw deflate
        encoded = base64.b64encode(deflated).decode("ascii")

        params = urllib.parse.urlencode({"SAMLRequest": encoded})
        return f"{sso_url}?{params}"

    def _parse_saml_metadata_sso_url(self, metadata_url: str) -> str:
        """Fetch SAML XML metadata and extract the SSO redirect URL.

        Args:
            metadata_url: URL to the SAML metadata XML.

        Returns:
            The SSO redirect URL, or empty string if not found.
        """
        try:
            req = urllib.request.Request(metadata_url)
            with urllib.request.urlopen(req, timeout=10) as resp:
                xml_data = resp.read().decode("utf-8")
        except (urllib.error.URLError, OSError) as exc:
            logger.warning("Failed to fetch SAML metadata XML: %s", exc)
            return ""

        try:
            root = ET.fromstring(xml_data)
            # Look for SingleSignOnService with HTTP-Redirect binding
            ns = {
                "md": "urn:oasis:names:tc:SAML:2.0:metadata",
            }
            for sso_elem in root.iter(f"{{{ns['md']}}}SingleSignOnService"):
                binding = sso_elem.get("Binding", "")
                if "HTTP-Redirect" in binding:
                    return sso_elem.get("Location", "")
            # Fallback: any SingleSignOnService
            for sso_elem in root.iter(f"{{{ns['md']}}}SingleSignOnService"):
                location = sso_elem.get("Location", "")
                if location:
                    return location
        except ET.ParseError as exc:
            logger.warning("Failed to parse SAML metadata XML: %s", exc)

        return ""

    def process_saml_response(self, saml_response: str) -> SSOUser:
        """Parse a SAML response and extract the authenticated user.

        .. warning::
            This is a basic implementation that parses XML and extracts
            NameID and attributes. Production SAML deployments should
            use ``python-saml2`` for full spec compliance including
            XML signature validation and replay protection.

        Args:
            saml_response: Base64-encoded SAML response from the IdP.

        Returns:
            The authenticated :class:`SSOUser`.

        Raises:
            SSOError: If the response cannot be parsed or is invalid.
        """
        config = self._require_config()

        logger.warning(
            "Processing SAML response with basic parser. "
            "Production deployments should use python-saml2 for full "
            "SAML spec compliance (signature validation, replay protection)."
        )

        # Decode the SAML response
        try:
            xml_bytes = base64.b64decode(saml_response)
            xml_str = xml_bytes.decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise SSOError(f"Failed to decode SAML response: {exc}") from exc

        # Parse the XML
        try:
            root = ET.fromstring(xml_str)
        except ET.ParseError as exc:
            raise SSOError(f"Failed to parse SAML response XML: {exc}") from exc

        ns = {
            "saml": "urn:oasis:names:tc:SAML:2.0:assertion",
            "samlp": "urn:oasis:names:tc:SAML:2.0:protocol",
        }

        # Check status
        status_code_elem = root.find(
            ".//samlp:Status/samlp:StatusCode", ns
        )
        if status_code_elem is not None:
            status_value = status_code_elem.get("Value", "")
            if "Success" not in status_value:
                raise SSOError(f"SAML response indicates failure: {status_value}")

        # Extract NameID (subject identifier)
        name_id_elem = root.find(".//saml:Subject/saml:NameID", ns)
        if name_id_elem is None or not name_id_elem.text:
            raise SSOError("SAML response missing NameID")

        name_id = name_id_elem.text.strip()

        # Extract attributes
        attributes: dict[str, list[str]] = {}
        for attr_elem in root.iter(f"{{{ns['saml']}}}Attribute"):
            attr_name = attr_elem.get("Name", "")
            values = []
            for val_elem in attr_elem.iter(f"{{{ns['saml']}}}AttributeValue"):
                if val_elem.text:
                    values.append(val_elem.text.strip())
            if attr_name and values:
                attributes[attr_name] = values

        # Map attributes to user fields
        email = name_id  # NameID is often the email
        # Check common attribute names for email
        for attr_key in (
            "email",
            "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress",
            "urn:oid:0.9.2342.19200300.100.1.3",
        ):
            if attr_key in attributes:
                email = attributes[attr_key][0]
                break

        name = ""
        for attr_key in (
            "displayName",
            "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/name",
            "urn:oid:2.16.840.1.113730.3.1.241",
        ):
            if attr_key in attributes:
                name = attributes[attr_key][0]
                break

        # If no display name, try given + surname
        if not name:
            given = ""
            surname = ""
            for attr_key in (
                "givenName",
                "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/givenname",
                "urn:oid:2.5.4.42",
            ):
                if attr_key in attributes:
                    given = attributes[attr_key][0]
                    break
            for attr_key in (
                "surname",
                "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/surname",
                "urn:oid:2.5.4.4",
            ):
                if attr_key in attributes:
                    surname = attributes[attr_key][0]
                    break
            name = f"{given} {surname}".strip()

        self._validate_email_domain(email, config)

        # Build groups from SAML attributes
        groups: list[str] = []
        for attr_key in (
            "groups",
            "memberOf",
            "http://schemas.xmlsoap.org/claims/Group",
        ):
            if attr_key in attributes:
                groups.extend(attributes[attr_key])

        # Build a claims-like dict for role mapping
        claims: dict[str, Any] = {"groups": groups, "email": email}
        roles = self._map_roles(claims)

        # Determine expiry from conditions
        expires_at = 0.0
        conditions_elem = root.find(".//saml:Conditions", ns)
        if conditions_elem is not None:
            not_on_or_after = conditions_elem.get("NotOnOrAfter", "")
            if not_on_or_after:
                try:
                    # Parse ISO 8601 timestamp
                    import calendar

                    t = time.strptime(not_on_or_after, "%Y-%m-%dT%H:%M:%SZ")
                    expires_at = float(calendar.timegm(t))
                except (ValueError, OverflowError):
                    pass

        return SSOUser(
            email=email,
            name=name,
            sub=name_id,
            roles=roles,
            groups=groups,
            provider=config.issuer_url,
            authenticated_at=time.time(),
            token_expires_at=expires_at,
        )


# ---------------------------------------------------------------------------
# Role Mapping Utility
# ---------------------------------------------------------------------------


def map_sso_user_to_role(user: SSOUser) -> str:
    """Determine the Kiln role for an SSO user.

    Returns the highest-privilege role from the user's mapped roles.
    Falls back to ``"operator"`` if no mapping matches.

    Args:
        user: An authenticated SSO user.

    Returns:
        A Kiln role name: ``"admin"``, ``"engineer"``, or ``"operator"``.
    """
    # Priority order: admin > engineer > operator
    role_priority = ["admin", "engineer", "operator"]
    for role in role_priority:
        if role in user.roles:
            return role
    return "operator"


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_sso_manager: SSOManager | None = None


def get_sso_manager() -> SSOManager:
    """Return the module-level SSOManager singleton."""
    global _sso_manager  # noqa: PLW0603
    if _sso_manager is None:
        _sso_manager = SSOManager()
    return _sso_manager
