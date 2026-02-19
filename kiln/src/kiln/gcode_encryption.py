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
import stat
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_ENCRYPTION_KEY_ENV = "KILN_ENCRYPTION_KEY"
_SALT_ENV = "KILN_ENCRYPTION_SALT"
_DEFAULT_SALT = b"kiln-gcode-encryption-v1"
_HEADER = b"KILN_ENC_V1:"
_SALT_FILE = os.path.join(str(Path.home()), ".kiln", "encryption_salt")


def _get_or_create_salt() -> bytes:
    """Load or generate a random PBKDF2 salt, persisted to ``~/.kiln/encryption_salt``.

    On first run, generates 16 random bytes and writes them to disk with
    ``0o600`` permissions.  On subsequent runs, reads the persisted salt.
    Falls back to :data:`_DEFAULT_SALT` only if the file cannot be created
    (e.g. read-only filesystem).
    """
    try:
        if os.path.isfile(_SALT_FILE):
            with open(_SALT_FILE, "rb") as fh:
                salt = fh.read()
            if len(salt) >= 16:
                return salt
            # File exists but is too short — regenerate.

        salt = os.urandom(16)
        salt_dir = os.path.dirname(_SALT_FILE)
        os.makedirs(salt_dir, mode=0o700, exist_ok=True)
        with open(_SALT_FILE, "wb") as fh:
            fh.write(salt)
        os.chmod(_SALT_FILE, stat.S_IRUSR | stat.S_IWUSR)  # 0o600
        logger.info("Generated new PBKDF2 salt at %s", _SALT_FILE)
        return salt
    except OSError as exc:
        logger.warning(
            "Could not read/write salt file %s (%s); falling back to default salt",
            _SALT_FILE,
            exc,
        )
        return _DEFAULT_SALT


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

        env_salt = os.environ.get(_SALT_ENV, "").strip()
        salt = env_salt.encode("utf-8") if env_salt else _get_or_create_salt()

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

    def decrypt(self, data: bytes, *, expect_encrypted: bool = False) -> bytes:
        """Decrypt G-code data.

        If the data doesn't have the encryption header, returns it unchanged
        (passthrough for unencrypted files) unless *expect_encrypted* is set.

        Args:
            data: Possibly encrypted G-code bytes.
            expect_encrypted: When ``True``, raise :class:`GcodeEncryptionError`
                if the data does not carry the encryption header.  Useful when
                the caller knows the data *must* be encrypted and wants to
                reject plaintext that may indicate tampering or misconfiguration.

        Returns:
            Decrypted G-code bytes.

        Raises:
            GcodeEncryptionError: If decryption fails on encrypted data, or
                if *expect_encrypted* is ``True`` and the header is missing.
        """
        if not data.startswith(_HEADER):
            if expect_encrypted:
                raise GcodeEncryptionError(
                    "Expected encrypted data (KILN_ENC_V1 header) but received "
                    "unencrypted payload. The data may have been tampered with "
                    "or encryption was not applied."
                )
            return data  # Not encrypted, passthrough

        if not self._available:
            raise GcodeEncryptionError(
                "Encrypted G-code detected but encryption key not configured. "
                "Set KILN_ENCRYPTION_KEY to decrypt."
            )

        try:
            encrypted_payload = data[len(_HEADER) :]
            return self._fernet.decrypt(encrypted_payload)
        except Exception as exc:
            raise GcodeEncryptionError(f"Decryption failed: {exc}") from exc

    @staticmethod
    def is_encrypted(data: bytes) -> bool:
        """Check if data has the Kiln encryption header."""
        return data.startswith(_HEADER)

    @property
    def supports_rotation(self) -> bool:
        """Whether this encryption backend supports key rotation."""
        return self._available

    def rotate_key(
        self,
        old_passphrase: str,
        new_passphrase: str,
        directory: str,
        *,
        pattern: str = "*.gcode",
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Re-encrypt all G-code files in *directory* from *old_passphrase* to *new_passphrase*.

        Walks *directory* recursively, finds files matching *pattern* that
        carry the ``KILN_ENC_V1:`` header, decrypts with the old key, and
        re-encrypts with the new key.  Unencrypted files are skipped.

        Args:
            old_passphrase: The current ``KILN_ENCRYPTION_KEY`` value.
            new_passphrase: The new passphrase to encrypt with.
            directory: Root directory to scan for encrypted G-code files.
            pattern: Glob pattern for G-code files (default ``"*.gcode"``).
            dry_run: When ``True``, scan and report without modifying files.

        Returns:
            Dict with ``rotated``, ``skipped``, ``failed``, and ``errors`` counts.

        Raises:
            GcodeEncryptionError: If the ``cryptography`` library is unavailable.
        """
        try:
            from cryptography.fernet import Fernet
            from cryptography.hazmat.primitives import hashes
            from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
        except ImportError:
            raise GcodeEncryptionError(
                "cryptography library required for key rotation: pip install cryptography"
            ) from None

        salt = _get_or_create_salt()

        def _derive(passphrase: str) -> Fernet:
            kdf = PBKDF2HMAC(
                algorithm=hashes.SHA256(), length=32, salt=salt, iterations=600_000,
            )
            return Fernet(base64.urlsafe_b64encode(kdf.derive(passphrase.encode("utf-8"))))

        old_fernet = _derive(old_passphrase)
        new_fernet = _derive(new_passphrase)

        rotated = 0
        skipped = 0
        failed = 0
        errors: list[str] = []

        root = Path(directory)
        if not root.is_dir():
            raise GcodeEncryptionError(f"Directory not found: {directory}")

        for filepath in root.rglob(pattern):
            if not filepath.is_file():
                continue
            try:
                data = filepath.read_bytes()
                if not data.startswith(_HEADER):
                    skipped += 1
                    continue

                encrypted_payload = data[len(_HEADER):]
                plaintext = old_fernet.decrypt(encrypted_payload)

                if dry_run:
                    rotated += 1
                    continue

                re_encrypted = new_fernet.encrypt(plaintext)
                filepath.write_bytes(_HEADER + re_encrypted)
                rotated += 1
                logger.info("Rotated encryption key for %s", filepath)
            except Exception as exc:
                failed += 1
                errors.append(f"{filepath}: {exc}")
                logger.warning("Failed to rotate %s: %s", filepath, exc)

        return {
            "rotated": rotated,
            "skipped": skipped,
            "failed": failed,
            "errors": errors,
            "dry_run": dry_run,
            "directory": str(directory),
        }

    def status(self) -> dict[str, Any]:
        """Return encryption status for diagnostics."""
        return {
            "available": self._available,
            "key_configured": bool(os.environ.get(_ENCRYPTION_KEY_ENV, "").strip()),
            "library_installed": _check_cryptography_installed(),
            "supports_rotation": self.supports_rotation,
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
