"""Local fulfillment shipping profiles and confirmation tokens."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_PROFILE_ENV = "KILN_SHIPPING_PROFILES_PATH"
_DEFAULT_TTL_SECONDS = 600
_CONFIRMATION_PREFIX = "fc_"
_PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_REQUIRED_ADDRESS_KEYS = (
    "first_name",
    "last_name",
    "email",
    "phone",
    "street",
    "city",
    "postal_code",
)
_OPTIONAL_ADDRESS_KEYS = ("street2", "state", "country", "company", "vat_id")


@dataclass(frozen=True)
class ShippingProfile:
    """Saved local shipping profile."""

    name: str
    shipping_address: dict[str, str]
    created_at: float
    updated_at: float
    is_default: bool = False

    def to_dict(self, *, include_address: bool = False) -> dict[str, Any]:
        data: dict[str, Any] = {
            "name": self.name,
            "is_default": self.is_default,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "summary": summarize_shipping_address(
                self.shipping_address,
                redact_sensitive=not include_address,
            ),
        }
        if include_address:
            data["shipping_address"] = dict(self.shipping_address)
        return data


@dataclass(frozen=True)
class ShippingConfirmationToken:
    """Single-use confirmation for a reviewed fulfillment address."""

    token: str
    digest: str
    issued_at: float
    ttl_seconds: int

    def expired(self, now: float | None = None) -> bool:
        if now is None:
            now = time.time()
        return (now - self.issued_at) > self.ttl_seconds


_confirmation_lock = threading.Lock()
_confirmations: dict[str, ShippingConfirmationToken] = {}


def normalize_shipping_address(address: dict[str, Any] | None) -> dict[str, str]:
    """Normalize and validate a fulfillment shipping contact/address."""
    if not address:
        raise ValueError(
            "Shipping address is required. Provide shipping_address or shipping_profile_name."
        )

    normalized = {
        str(key): str(value).strip()
        for key, value in address.items()
        if value is not None and str(value).strip()
    }
    country = normalized.get("country", "US").upper()
    normalized["country"] = country

    missing = [key for key in _REQUIRED_ADDRESS_KEYS if not normalized.get(key)]
    if country == "US" and not normalized.get("state"):
        missing.append("state")
    if missing:
        raise ValueError(
            "Shipping address is incomplete. Missing: "
            + ", ".join(missing)
            + ". Provide a full shipping contact/address."
        )
    if "@" not in normalized["email"]:
        raise ValueError("Shipping address email must be valid.")

    allowed = set(_REQUIRED_ADDRESS_KEYS) | set(_OPTIONAL_ADDRESS_KEYS)
    return {key: normalized[key] for key in (*_REQUIRED_ADDRESS_KEYS, *_OPTIONAL_ADDRESS_KEYS) if key in normalized and key in allowed}


def _mask_email(email: str) -> str:
    """Return a display-safe email address."""
    if "@" not in email:
        return "[redacted]"
    local, domain = email.split("@", 1)
    if not local:
        return f"[redacted]@{domain}"
    return f"{local[0]}***@{domain}"


def _mask_phone(phone: str) -> str:
    """Return a display-safe phone number."""
    digits = "".join(ch for ch in phone if ch.isdigit())
    if len(digits) < 4:
        return "[redacted]"
    return f"***{digits[-4:]}"


def summarize_shipping_address(
    address: dict[str, str],
    *,
    redact_sensitive: bool = False,
) -> str:
    """Return a concise human-readable shipping address summary."""
    city_line = address.get("city", "")
    state = address.get("state", "")
    postal_code = address.get("postal_code", "")
    if state or postal_code:
        city_line = f"{city_line}, {state} {postal_code}".strip()
    name = " ".join(v for v in (address.get("first_name"), address.get("last_name")) if v)
    if redact_sensitive:
        name_parts = [part[0] + "." for part in name.split() if part]
        parts = [
            " ".join(name_parts),
            address.get("company", ""),
            city_line,
            address.get("country", ""),
            _mask_email(address.get("email", "")) if address.get("email") else "",
            _mask_phone(address.get("phone", "")) if address.get("phone") else "",
        ]
        return " | ".join(part for part in parts if part)

    parts = [
        name,
        address.get("company", ""),
        address.get("street", ""),
        address.get("street2", ""),
        city_line,
        address.get("country", ""),
        address.get("email", ""),
        address.get("phone", ""),
    ]
    return " | ".join(part for part in parts if part)


def _profiles_path() -> Path:
    explicit = os.environ.get(_PROFILE_ENV, "").strip()
    if explicit:
        return Path(explicit).expanduser()
    return Path.home() / ".kiln" / "shipping_profiles.json"


def _empty_store() -> dict[str, Any]:
    return {"_meta": {"version": 1}, "default_profile": "", "profiles": {}}


def _load_store() -> dict[str, Any]:
    path = _profiles_path()
    if not path.exists():
        return _empty_store()
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Shipping profiles file is invalid.")
    profiles = data.get("profiles")
    if not isinstance(profiles, dict):
        data["profiles"] = {}
    data.setdefault("_meta", {"version": 1})
    data.setdefault("default_profile", "")
    return data


def _save_store(data: dict[str, Any]) -> None:
    path = _profiles_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if os.name != "nt":
        tmp.chmod(0o600)
    os.replace(tmp, path)
    if os.name != "nt":
        path.chmod(0o600)


def _validate_profile_name(name: str) -> str:
    normalized = name.strip()
    if not _PROFILE_NAME_RE.fullmatch(normalized):
        raise ValueError(
            "Shipping profile name must be 1-64 characters using letters, numbers, dots, underscores, or hyphens."
        )
    return normalized


def _profile_from_record(name: str, record: dict[str, Any], *, default_name: str) -> ShippingProfile:
    address = normalize_shipping_address(record.get("shipping_address"))
    return ShippingProfile(
        name=name,
        shipping_address=address,
        created_at=float(record.get("created_at") or 0),
        updated_at=float(record.get("updated_at") or 0),
        is_default=name == default_name,
    )


def save_shipping_profile(
    name: str,
    shipping_address: dict[str, Any],
    *,
    overwrite: bool = False,
    set_default: bool = False,
) -> ShippingProfile:
    """Save a local shipping profile after explicit user consent."""
    profile_name = _validate_profile_name(name)
    address = normalize_shipping_address(shipping_address)
    store = _load_store()
    profiles = store.setdefault("profiles", {})
    now = time.time()
    existing = profiles.get(profile_name)
    if existing and not overwrite:
        raise ValueError(
            f"Shipping profile '{profile_name}' already exists. Pass overwrite=True to replace it."
        )
    profiles[profile_name] = {
        "shipping_address": address,
        "created_at": float(existing.get("created_at") if isinstance(existing, dict) else now) if existing else now,
        "updated_at": now,
    }
    if set_default or not store.get("default_profile"):
        store["default_profile"] = profile_name
    _save_store(store)
    return _profile_from_record(
        profile_name,
        profiles[profile_name],
        default_name=str(store.get("default_profile") or ""),
    )


def list_shipping_profiles() -> list[ShippingProfile]:
    """List saved local shipping profiles."""
    store = _load_store()
    default_name = str(store.get("default_profile") or "")
    profiles = store.get("profiles") or {}
    return [
        _profile_from_record(name, record, default_name=default_name)
        for name, record in sorted(profiles.items())
        if isinstance(record, dict)
    ]


def get_shipping_profile(name: str = "") -> ShippingProfile:
    """Return a saved profile by name, or the default profile when omitted."""
    store = _load_store()
    profile_name = name.strip() or str(store.get("default_profile") or "")
    if not profile_name:
        raise ValueError("No shipping profile specified and no default profile is saved.")
    profile_name = _validate_profile_name(profile_name)
    profiles = store.get("profiles") or {}
    record = profiles.get(profile_name)
    if not isinstance(record, dict):
        raise ValueError(f"Shipping profile '{profile_name}' was not found.")
    return _profile_from_record(
        profile_name,
        record,
        default_name=str(store.get("default_profile") or ""),
    )


def delete_shipping_profile(name: str) -> bool:
    """Delete a saved local shipping profile."""
    profile_name = _validate_profile_name(name)
    store = _load_store()
    profiles = store.get("profiles") or {}
    existed = profile_name in profiles
    if existed:
        profiles.pop(profile_name, None)
        if store.get("default_profile") == profile_name:
            store["default_profile"] = sorted(profiles.keys())[0] if profiles else ""
        _save_store(store)
    return existed


def _shipping_confirmation_digest(
    *,
    quote_id: str,
    shipping_option_id: str,
    shipping_address: dict[str, Any],
) -> str:
    payload = {
        "quote_id": quote_id.strip(),
        "shipping_option_id": shipping_option_id.strip(),
        "shipping_address": normalize_shipping_address(shipping_address),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def issue_shipping_confirmation_token(
    *,
    quote_id: str,
    shipping_option_id: str,
    shipping_address: dict[str, Any],
    ttl_seconds: int = _DEFAULT_TTL_SECONDS,
) -> ShippingConfirmationToken:
    """Issue a token after the user confirms the exact shipping details."""
    token = _CONFIRMATION_PREFIX + secrets.token_hex(16)
    confirmation = ShippingConfirmationToken(
        token=token,
        digest=_shipping_confirmation_digest(
            quote_id=quote_id,
            shipping_option_id=shipping_option_id,
            shipping_address=shipping_address,
        ),
        issued_at=time.time(),
        ttl_seconds=ttl_seconds,
    )
    with _confirmation_lock:
        now = time.time()
        _confirmations[token] = confirmation
        expired = [key for key, value in _confirmations.items() if value.expired(now)]
        for key in expired:
            _confirmations.pop(key, None)
    return confirmation


def validate_shipping_confirmation_token(
    token: str,
    *,
    quote_id: str,
    shipping_option_id: str,
    shipping_address: dict[str, Any],
    consume: bool = True,
) -> tuple[bool, str | None]:
    """Validate a shipping confirmation token against order details."""
    if not token or not token.startswith(_CONFIRMATION_PREFIX):
        return False, "invalid_token_format"
    with _confirmation_lock:
        confirmation = _confirmations.get(token)
    if confirmation is None:
        return False, "token_not_found_or_already_used"
    if confirmation.expired():
        return False, "token_expired"
    expected = _shipping_confirmation_digest(
        quote_id=quote_id,
        shipping_option_id=shipping_option_id,
        shipping_address=shipping_address,
    )
    if confirmation.digest != expected:
        return False, "token_shipping_details_mismatch"
    if consume:
        with _confirmation_lock:
            _confirmations.pop(token, None)
    return True, None
