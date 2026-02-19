"""Encrypted G-code storage for Enterprise tier.

Provides transparent encryption/decryption of G-code files at rest
using Fernet symmetric encryption. The encryption key is derived from
the ``KILN_ENCRYPTION_KEY`` environment variable via PBKDF2.

When the Enterprise tier is active and an encryption key is configured,
G-code files are encrypted on upload and decrypted transparently on read.

Usage::

    from kiln.gcode_encryption import GcodeEncryption

    enc = GcodeEncryption()
    if enc.is_available:
        encrypted = enc.encrypt(gcode_bytes)
        decrypted = enc.decrypt(encrypted)
"""

from __future__ import annotations

import base64
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

_ENCRYPTION_KEY_ENV = "KILN_ENCRYPTION_KEY"
_SALT_ENV = "KILN_ENCRYPTION_SALT"
_DEFAULT_SALT = b"kiln-gcode-encryption-v1"
_HEADER = b"KILN_ENC_V1:"


class GcodeEncryptionError(Exception):
    """Raised when encryption or decryption fails."""

    pass


class GcodeEncryption:
    """Manages G-code encryption at rest using Fernet.

    Encryption is opt-in: only active when ``KILN_ENCRYPTION_KEY`` is set
    and the ``cryptography`` library is installed.
    """

    def __init__(self) -> None:
        self._fernet: Any = None
        self._available = False
        self._init_encryption()

    def _init_encryption(self) -> None:
        """Derive the Fernet key from the env var."""
        raw_key = os.environ.get(_ENCRYPTION_KEY_ENV, "").strip()
        if not raw_key:
            return

        try:
            from cryptography.fernet import Fernet
            from cryptography.hazmat.primitives import hashes
            from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
        except ImportError:
            logger.warning(
                "KILN_ENCRYPTION_KEY is set but 'cryptography' package is not installed. "
                "Install with: pip install cryptography"
            )
            return

        salt = os.environ.get(_SALT_ENV, "").encode("utf-8") or _DEFAULT_SALT

        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            iterations=600_000,
        )
        derived = kdf.derive(raw_key.encode("utf-8"))
        fernet_key = base64.urlsafe_b64encode(derived)
        self._fernet = Fernet(fernet_key)
        self._available = True
        logger.info("G-code encryption initialized (PBKDF2 + Fernet)")

    @property
    def is_available(self) -> bool:
        """Whether encryption is active (key set + library installed)."""
        return self._available

    def encrypt(self, data: bytes) -> bytes:
        """Encrypt G-code data.

        Args:
            data: Raw G-code bytes.

        Returns:
            Encrypted bytes with a ``KILN_ENC_V1:`` header prefix.

        Raises:
            GcodeEncryptionError: If encryption is not available or fails.
        """
        if not self._available:
            raise GcodeEncryptionError(
                "Encryption not available. Set KILN_ENCRYPTION_KEY and install 'cryptography'."
            )
        try:
            encrypted = self._fernet.encrypt(data)
            return _HEADER + encrypted
        except Exception as exc:
            raise GcodeEncryptionError(f"Encryption failed: {exc}") from exc

    def decrypt(self, data: bytes) -> bytes:
        """Decrypt G-code data.

        If the data doesn't have the encryption header, returns it unchanged
        (passthrough for unencrypted files).

        Args:
            data: Possibly encrypted G-code bytes.

        Returns:
            Decrypted G-code bytes.

        Raises:
            GcodeEncryptionError: If decryption fails on encrypted data.
        """
        if not data.startswith(_HEADER):
            return data  # Not encrypted, passthrough

        if not self._available:
            raise GcodeEncryptionError(
                "Encrypted G-code detected but encryption key not configured. "
                "Set KILN_ENCRYPTION_KEY to decrypt."
            )

        try:
            encrypted_payload = data[len(_HEADER):]
            return self._fernet.decrypt(encrypted_payload)
        except Exception as exc:
            raise GcodeEncryptionError(f"Decryption failed: {exc}") from exc

    @staticmethod
    def is_encrypted(data: bytes) -> bool:
        """Check if data has the Kiln encryption header."""
        return data.startswith(_HEADER)

    def status(self) -> dict[str, Any]:
        """Return encryption status for diagnostics."""
        return {
            "available": self._available,
            "key_configured": bool(os.environ.get(_ENCRYPTION_KEY_ENV, "").strip()),
            "library_installed": _check_cryptography_installed(),
        }


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_instance: GcodeEncryption | None = None


def get_gcode_encryption() -> GcodeEncryption:
    """Return the module-level GcodeEncryption singleton."""
    global _instance  # noqa: PLW0603
    if _instance is None:
        _instance = GcodeEncryption()
    return _instance


def _check_cryptography_installed() -> bool:
    """Check if the cryptography library is importable."""
    try:
        import cryptography  # noqa: F401
        return True
    except ImportError:
        return False
