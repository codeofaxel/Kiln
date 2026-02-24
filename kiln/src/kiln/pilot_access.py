"""Pilot entitlement helpers backed by Supabase Data API.

This module stores only licensing metadata (no print/job payloads) to keep
privacy boundaries clear while supporting pilot access management.
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from ipaddress import ip_address
from pathlib import Path
from typing import Any

import requests

logger = logging.getLogger(__name__)

_DEFAULT_TABLE = "pilot_entitlements"
_DEFAULT_EVENTS_TABLE = "license_security_events"
_DEFAULT_ENV_FILE = ".env.supabase"
_DEFAULT_CACHE_TTL_SECONDS = 120
_DEFAULT_CACHE_GRACE_SECONDS = 900


def load_env_file(path: str | Path = _DEFAULT_ENV_FILE) -> None:
    """Load ``KEY=VALUE`` pairs into process env if the file exists."""
    if os.environ.get("KILN_SUPABASE_DISABLE_DOTENV", "0") == "1":
        return
    env_path = Path(path)
    if not env_path.is_file():
        return
    try:
        for raw in env_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            key = k.strip()
            val = v.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val
    except OSError as exc:
        logger.debug("Could not read env file %s: %s", env_path, exc)


def hash_license_key(license_key: str) -> str:
    """Hash a full license key for storage without persisting raw secrets."""
    return hashlib.sha256(license_key.encode("utf-8")).hexdigest()


def hash_identifier(value: str) -> str:
    """Hash arbitrary identifier strings (email/device/IP/fingerprint)."""
    return hashlib.sha256(value.strip().encode("utf-8")).hexdigest() if value else ""


def key_hint(license_key: str) -> str:
    """Return a short non-sensitive hint for troubleshooting."""
    return license_key[-8:] if license_key else ""


def normalize_ip_to_coarse_bucket(raw_ip: str) -> str:
    """Return a coarse IP bucket for abuse controls, not precise location tracking."""
    if not raw_ip:
        return ""
    try:
        ip = ip_address(raw_ip.strip())
        if ip.version == 4:
            parts = raw_ip.split(".")
            if len(parts) == 4:
                return ".".join(parts[:3]) + ".0/24"
        # IPv6 coarse bucket: first 4 hextets
        chunks = raw_ip.split(":")
        return ":".join(chunks[:4]) + "::/64"
    except Exception:
        return ""


@dataclass
class PilotGrant:
    jti: str
    email: str
    tier: str
    issued_at: float
    expires_at: float
    key_hash: str
    key_hint: str
    max_activations: int = 3
    notes: str = ""
    status: str = "active"

    def to_insert_payload(self) -> dict[str, Any]:
        return {
            "jti": self.jti,
            "email": self.email,
            "email_hash": hash_identifier(self.email.lower()),
            "tier": self.tier,
            "issued_at": datetime.fromtimestamp(self.issued_at, tz=timezone.utc).isoformat(),
            "expires_at": datetime.fromtimestamp(self.expires_at, tz=timezone.utc).isoformat(),
            "key_hash": self.key_hash,
            "key_hint": self.key_hint,
            "max_activations": self.max_activations,
            "notes": self.notes,
            "status": self.status,
        }


class SupabasePilotStore:
    """Write-only helper for pilot grant metadata in Supabase."""

    def __init__(
        self,
        *,
        url: str,
        service_key: str,
        table: str = _DEFAULT_TABLE,
        events_table: str = _DEFAULT_EVENTS_TABLE,
    ) -> None:
        self._url = url.rstrip("/")
        self._service_key = service_key
        self._table = table
        self._events_table = events_table

    @classmethod
    def from_env(cls) -> SupabasePilotStore | None:
        load_env_file()
        url = os.environ.get("KILN_SUPABASE_URL", "").strip()
        service_key = (
            os.environ.get("KILN_SUPABASE_SERVICE_KEY", "").strip()
            or os.environ.get("KILN_SUPABASE_SERVICE_ROLE_KEY", "").strip()
        )
        table = os.environ.get("KILN_SUPABASE_ENTITLEMENTS_TABLE", _DEFAULT_TABLE).strip() or _DEFAULT_TABLE
        events_table = os.environ.get("KILN_SUPABASE_SECURITY_EVENTS_TABLE", _DEFAULT_EVENTS_TABLE).strip() or _DEFAULT_EVENTS_TABLE
        if not url or not service_key:
            return None
        return cls(url=url, service_key=service_key, table=table, events_table=events_table)

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "apikey": self._service_key,
            "Authorization": f"Bearer {self._service_key}",
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        }

    def record_grant(self, grant: PilotGrant) -> tuple[bool, str | None]:
        endpoint = f"{self._url}/rest/v1/{self._table}"
        try:
            resp = requests.post(
                endpoint,
                headers=self._headers,
                json=grant.to_insert_payload(),
                timeout=15,
            )
            if resp.status_code in (200, 201):
                return True, None
            body = resp.text[:500]
            return False, f"Supabase insert failed ({resp.status_code}): {body}"
        except Exception as exc:
            return False, f"Supabase insert error: {exc}"

    def revoke_grant(self, jti: str, reason: str = "manual revoke") -> tuple[bool, str | None]:
        endpoint = f"{self._url}/rest/v1/{self._table}"
        try:
            resp = requests.patch(
                endpoint,
                headers=self._headers,
                params={"jti": f"eq.{jti}"},
                json={
                    "status": "revoked",
                    "revoked_reason": reason,
                    "revoked_at": datetime.now(timezone.utc).isoformat(),
                },
                timeout=15,
            )
            if resp.status_code in (200, 204):
                if resp.status_code == 204:
                    return True, None
                try:
                    rows = resp.json()
                except Exception:
                    rows = None
                if isinstance(rows, list) and not rows:
                    return False, f"No entitlement found for jti={jti}"
                return True, None
            body = resp.text[:500]
            return False, f"Supabase revoke failed ({resp.status_code}): {body}"
        except Exception as exc:
            return False, f"Supabase revoke error: {exc}"

    def get_grant_detailed(self, jti: str) -> tuple[dict[str, Any] | None, str | None]:
        endpoint = f"{self._url}/rest/v1/{self._table}"
        try:
            resp = requests.get(
                endpoint,
                headers=self._headers,
                params={"jti": f"eq.{jti}", "select": "*", "limit": "1"},
                timeout=15,
            )
            if resp.status_code != 200:
                return None, f"Supabase get_grant failed ({resp.status_code}): {resp.text[:200]}"
            rows = resp.json()
            if isinstance(rows, list) and rows:
                return rows[0], None
            return None, None
        except Exception as exc:
            return None, f"Supabase get_grant error: {exc}"

    def get_grant(self, jti: str) -> dict[str, Any] | None:
        grant, err = self.get_grant_detailed(jti)
        if err:
            logger.debug("%s", err)
        return grant

    def is_revoked(self, jti: str) -> bool:
        grant = self.get_grant(jti)
        if not grant:
            return False
        return str(grant.get("status", "")).lower() == "revoked"

    def count_activations(self, jti: str) -> int:
        endpoint = f"{self._url}/rest/v1/{self._events_table}"
        try:
            resp = requests.get(
                endpoint,
                headers=self._headers,
                params={
                    "jti": f"eq.{jti}",
                    "event_type": "eq.activation",
                    "select": "id",
                },
                timeout=15,
            )
            if resp.status_code != 200:
                return 0
            rows = resp.json()
            return len(rows) if isinstance(rows, list) else 0
        except Exception:
            return 0

    def activation_device_hashes(self, jti: str) -> set[str]:
        endpoint = f"{self._url}/rest/v1/{self._events_table}"
        try:
            resp = requests.get(
                endpoint,
                headers=self._headers,
                params={
                    "jti": f"eq.{jti}",
                    "event_type": "eq.activation",
                    "select": "device_hash",
                },
                timeout=15,
            )
            if resp.status_code != 200:
                return set()
            rows = resp.json()
            if not isinstance(rows, list):
                return set()
            out: set[str] = set()
            for row in rows:
                if isinstance(row, dict):
                    value = str(row.get("device_hash", "")).strip()
                    if value:
                        out.add(value)
            return out
        except Exception:
            return set()

    def list_revocations(self, *, limit: int = 500, since_iso: str | None = None) -> list[dict[str, Any]]:
        endpoint = f"{self._url}/rest/v1/{self._table}"
        params = {
            "status": "eq.revoked",
            "select": "jti,revoked_at,revoked_reason",
            "order": "revoked_at.desc",
            "limit": str(max(1, min(limit, 5000))),
        }
        if since_iso:
            params["revoked_at"] = f"gte.{since_iso}"
        try:
            resp = requests.get(endpoint, headers=self._headers, params=params, timeout=15)
            if resp.status_code != 200:
                logger.debug("Supabase list_revocations failed (%s): %s", resp.status_code, resp.text[:200])
                return []
            rows = resp.json()
            return rows if isinstance(rows, list) else []
        except Exception as exc:
            logger.debug("Supabase list_revocations error: %s", exc)
            return []

    def record_security_event(
        self,
        *,
        jti: str,
        email: str,
        event_type: str,
        device_fingerprint: str = "",
        ip_address_raw: str = "",
        client_version: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> tuple[bool, str | None]:
        endpoint = f"{self._url}/rest/v1/{self._events_table}"
        payload = {
            "jti": jti,
            "email_hash": hash_identifier(email.lower()),
            "event_type": event_type,
            "device_hash": hash_identifier(device_fingerprint),
            "ip_coarse_hash": hash_identifier(normalize_ip_to_coarse_bucket(ip_address_raw)),
            "client_version": client_version or "",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "metadata": metadata or {},
        }
        try:
            resp = requests.post(
                endpoint,
                headers=self._headers,
                json=payload,
                timeout=15,
            )
            if resp.status_code in (200, 201):
                return True, None
            if resp.status_code == 409 and event_type == "activation":
                # Idempotent duplicate activation for same (jti, device_hash).
                return True, None
            return False, f"Supabase event insert failed ({resp.status_code}): {resp.text[:500]}"
        except Exception as exc:
            return False, f"Supabase event insert error: {exc}"


def _parse_supabase_ts(value: Any) -> float | None:
    if not value:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    raw = str(value).strip()
    if not raw:
        return None
    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        return datetime.fromisoformat(raw).timestamp()
    except Exception:
        return None


@dataclass
class EntitlementDecision:
    valid: bool
    tier: str = "free"
    email: str = ""
    jti: str = ""
    reason: str = ""
    source: str = "local"
    activation_count: int = 0
    max_activations: int = 0
    claims: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "tier": self.tier,
            "email": self.email,
            "jti": self.jti,
            "reason": self.reason,
            "source": self.source,
            "activation_count": self.activation_count,
            "max_activations": self.max_activations,
            "claims": self.claims or {},
        }


class EntitlementEnforcer:
    """Server-side entitlement checks with short-lived cache + grace."""

    def __init__(
        self,
        store: SupabasePilotStore | None,
        *,
        cache_ttl_seconds: int = _DEFAULT_CACHE_TTL_SECONDS,
        cache_grace_seconds: int = _DEFAULT_CACHE_GRACE_SECONDS,
        fail_open_on_store_error: bool = False,
        require_ledger_for_v2: bool = True,
    ) -> None:
        self._store = store
        self._cache_ttl_seconds = max(1, int(cache_ttl_seconds))
        self._cache_grace_seconds = max(0, int(cache_grace_seconds))
        self._fail_open_on_store_error = bool(fail_open_on_store_error)
        self._require_ledger_for_v2 = bool(require_ledger_for_v2)
        self._lock = threading.Lock()
        self._grant_cache: dict[str, tuple[float, dict[str, Any] | None]] = {}
        self._activation_locks_lock = threading.Lock()
        self._activation_locks: dict[str, threading.Lock] = {}

    @classmethod
    def from_env(cls) -> EntitlementEnforcer:
        load_env_file()
        store = SupabasePilotStore.from_env()
        ttl = int(os.environ.get("KILN_LICENSE_GRANT_CACHE_TTL_SECONDS", str(_DEFAULT_CACHE_TTL_SECONDS)))
        grace = int(os.environ.get("KILN_LICENSE_GRANT_CACHE_GRACE_SECONDS", str(_DEFAULT_CACHE_GRACE_SECONDS)))
        fail_open = os.environ.get("KILN_LICENSE_FAIL_OPEN_ON_ENTITLEMENT_ERROR", "0") == "1"
        require_ledger = os.environ.get("KILN_LICENSE_REQUIRE_LEDGER_FOR_V2", "1") != "0"
        return cls(
            store=store,
            cache_ttl_seconds=ttl,
            cache_grace_seconds=grace,
            fail_open_on_store_error=fail_open,
            require_ledger_for_v2=require_ledger,
        )

    def _cache_get(self, jti: str) -> tuple[dict[str, Any] | None, float] | None:
        with self._lock:
            row = self._grant_cache.get(jti)
        if row is None:
            return None
        fetched_at, grant = row
        return grant, fetched_at

    def _cache_put(self, jti: str, grant: dict[str, Any] | None) -> None:
        with self._lock:
            self._grant_cache[jti] = (time.time(), grant)

    def _activation_lock(self, jti: str) -> threading.Lock:
        with self._activation_locks_lock:
            lock = self._activation_locks.get(jti)
            if lock is None:
                lock = threading.Lock()
                self._activation_locks[jti] = lock
            return lock

    def _get_grant_with_cache(self, jti: str) -> tuple[dict[str, Any] | None, str | None, str]:
        cached = self._cache_get(jti)
        now = time.time()
        if cached is not None:
            cached_grant, fetched_at = cached
            age = now - fetched_at
            if age <= self._cache_ttl_seconds:
                return cached_grant, None, "cache"

        if self._store is None:
            return None, None, "disabled"

        grant, err = self._store.get_grant_detailed(jti)
        if err:
            if cached is not None:
                cached_grant, fetched_at = cached
                age = now - fetched_at
                if age <= self._cache_ttl_seconds + self._cache_grace_seconds:
                    return cached_grant, err, "stale-cache"
            return None, err, "error"

        self._cache_put(jti, grant)
        return grant, None, "supabase"

    def list_revocations(self, *, limit: int = 500, since_iso: str | None = None) -> list[dict[str, Any]]:
        if self._store is None:
            return []
        return self._store.list_revocations(limit=limit, since_iso=since_iso)

    def is_revoked(self, jti: str) -> bool:
        if not jti or self._store is None:
            return False
        grant, err, _ = self._get_grant_with_cache(jti)
        if err and grant is None:
            return False
        if not grant:
            return False
        return str(grant.get("status", "")).lower() == "revoked"

    def evaluate_license(
        self,
        *,
        license_key: str,
        event_type: str = "validation",
        device_fingerprint: str = "",
        ip_address_raw: str = "",
        client_version: str = "",
        metadata: dict[str, Any] | None = None,
        enforce_activation_cap: bool = False,
        auto_activate_if_needed: bool = False,
        record_event: bool = False,
    ) -> EntitlementDecision:
        from kiln.licensing import LicenseManager, LicenseTier, parse_license_claims

        key = (license_key or "").strip()
        if not key:
            return EntitlementDecision(valid=False, reason="No license key provided")

        try:
            mgr = LicenseManager(license_key=key)
            tier = mgr.get_tier()
            info = mgr.get_info()
        except Exception as exc:
            return EntitlementDecision(valid=False, reason=f"License parse failed: {exc}")

        claims = parse_license_claims(key) or {}
        jti = str(claims.get("jti", "")).strip()
        email = (info.email or str(claims.get("email", ""))).strip()

        if not info.is_valid:
            return EntitlementDecision(
                valid=False,
                tier=tier.value,
                email=email,
                jti=jti,
                reason="License expired or invalid",
                source="local",
                claims=claims,
            )

        decision = EntitlementDecision(
            valid=True,
            tier=tier.value,
            email=email,
            jti=jti,
            source="local",
            claims=claims,
        )

        if not jti:
            return decision

        grant, store_error, source = self._get_grant_with_cache(jti)
        decision.source = source
        if store_error and grant is None:
            if self._fail_open_on_store_error:
                decision.reason = "Entitlement store unavailable (fail-open)"
                return decision
            decision.valid = False
            decision.reason = "Entitlement store unavailable"
            return decision

        is_v2 = int(claims.get("version", 0)) == 2
        if grant is None:
            if is_v2 and tier != LicenseTier.FREE and self._require_ledger_for_v2:
                decision.valid = False
                decision.reason = "Unknown entitlement"
            return decision

        status = str(grant.get("status", "active")).strip().lower()
        if status != "active":
            decision.valid = False
            decision.reason = f"Entitlement {status or 'inactive'}"

        grant_expires = _parse_supabase_ts(grant.get("expires_at"))
        if grant_expires is not None and time.time() >= grant_expires:
            decision.valid = False
            decision.reason = "Entitlement expired"

        if enforce_activation_cap and self._store is not None:
            fingerprint = device_fingerprint.strip()
            if not fingerprint:
                decision.valid = False
                decision.reason = "Device fingerprint required"
                return decision

            with self._activation_lock(jti):
                max_activations = int(grant.get("max_activations", 0) or 0)
                existing_device_hashes = self._store.activation_device_hashes(jti)
                device_hash = hash_identifier(fingerprint)
                already_activated = bool(device_hash and device_hash in existing_device_hashes)
                decision.activation_count = len(existing_device_hashes)
                decision.max_activations = max_activations

                if max_activations > 0 and not already_activated and len(existing_device_hashes) >= max_activations:
                    decision.valid = False
                    decision.reason = "Activation limit reached"

                if decision.valid and auto_activate_if_needed and not already_activated and max_activations > 0:
                    ok, _ = self._store.record_security_event(
                        jti=jti,
                        email=email,
                        event_type="activation",
                        device_fingerprint=fingerprint,
                        ip_address_raw=ip_address_raw,
                        client_version=client_version,
                        metadata={"source": "auto", "tier": tier.value},
                    )
                    if ok:
                        decision.activation_count = len(existing_device_hashes) + 1

        if record_event and self._store is not None:
            event_kind = event_type if event_type in {"activation", "validation", "revocation_check", "refresh"} else "validation"
            self._store.record_security_event(
                jti=jti,
                email=email,
                event_type=event_kind,
                device_fingerprint=device_fingerprint,
                ip_address_raw=ip_address_raw,
                client_version=client_version,
                metadata={
                    "valid": decision.valid,
                    "reason": decision.reason,
                    "tier": decision.tier,
                    **(metadata or {}),
                },
            )

        return decision
