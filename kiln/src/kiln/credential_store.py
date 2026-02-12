"""Encrypted credential storage for the Kiln multi-printer system.

Provides at-rest encryption for API keys, webhook secrets, and other
sensitive credentials using PBKDF2 key derivation and XOR stream
encryption.  Only stdlib modules are used (``hashlib``, ``os``,
``base64``, ``sqlite3``, ``threading``).

The master key is sourced from (in order):

1. The ``master_key`` constructor argument.
2. The ``KILN_MASTER_KEY`` environment variable.
3. Auto-generated and persisted to ``~/.kiln/master.key`` (with a
   warning logged on first run).

Example::

    store = get_credential_store()
    cred = store.store(CredentialType.API_KEY, "sk_live_abc123", label="Xometry Key")
    secret = store.retrieve(cred.credential_id)
    store.delete(cred.credential_id)

Key rotation re-encrypts every stored credential under a new master key::

    count = store.rotate_master_key("new-master-key-value")
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import secrets
import sqlite3
import sys
import threading
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PBKDF2_ITERATIONS = 100_000
_SALT_LENGTH = 32
_DEFAULT_DB_DIR = os.path.join(str(Path.home()), ".kiln")
_DEFAULT_DB_PATH = os.path.join(_DEFAULT_DB_DIR, "credentials.db")
_DEFAULT_MASTER_KEY_PATH = os.path.join(_DEFAULT_DB_DIR, "master.key")


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class CredentialType(str, Enum):
    """Types of credentials that can be stored."""

    API_KEY = "api_key"
    WEBHOOK_SECRET = "webhook_secret"
    STRIPE_KEY = "stripe_key"
    CIRCLE_KEY = "circle_key"
    MARKETPLACE_TOKEN = "marketplace_token"
    PRINTER_PASSWORD = "printer_password"


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------


@dataclass
class EncryptedCredential:
    """Metadata for a stored credential.  Never contains the decrypted value.

    :param credential_id: Unique hex identifier.
    :param credential_type: The :class:`CredentialType` enum member.
    :param encrypted_value: Base64-encoded ciphertext (for storage only).
    :param salt: Base64-encoded salt used during encryption.
    :param created_at: Unix timestamp when the credential was stored.
    :param label: Human-readable label (e.g. ``"Xometry API Key"``).
    """

    credential_id: str
    credential_type: CredentialType
    encrypted_value: str
    salt: str
    created_at: float
    label: str

    def to_dict(self) -> Dict[str, Any]:
        """Serialise to a dict suitable for JSON output.

        The encrypted value and salt are intentionally **excluded** to
        prevent accidental leakage of ciphertext.
        """
        return {
            "credential_id": self.credential_id,
            "credential_type": self.credential_type.value,
            "created_at": self.created_at,
            "label": self.label,
        }


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class CredentialStoreError(Exception):
    """Raised when a credential store operation fails."""
    pass


# ---------------------------------------------------------------------------
# CredentialStore
# ---------------------------------------------------------------------------


class CredentialStore:
    """Encrypted credential storage backed by SQLite.

    :param master_key: Encryption master key.  Falls back to
        ``KILN_MASTER_KEY`` env var, then auto-generates and persists
        to ``~/.kiln/master.key``.
    :param db_path: Path to the SQLite database file.  Defaults to
        ``~/.kiln/credentials.db``.
    """

    def __init__(
        self,
        *,
        master_key: Optional[str] = None,
        db_path: Optional[str] = None,
    ) -> None:
        self._db_path = db_path or os.environ.get(
            "KILN_CREDENTIAL_DB_PATH", _DEFAULT_DB_PATH,
        )
        self._master_key = self._resolve_master_key(master_key)
        self._write_lock = threading.Lock()

        # Ensure parent directory exists.
        os.makedirs(os.path.dirname(self._db_path), exist_ok=True)

        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")

        self._init_db()
        self._enforce_permissions()

    # ------------------------------------------------------------------
    # Master key resolution
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_master_key(explicit_key: Optional[str]) -> str:
        """Determine the master key from explicit value, env, or auto-gen.

        :param explicit_key: Key passed directly to the constructor.
        :returns: The resolved master key string.
        """
        if explicit_key:
            return explicit_key

        env_key = os.environ.get("KILN_MASTER_KEY", "")
        if env_key:
            return env_key

        # Auto-generate and persist.
        key_path = _DEFAULT_MASTER_KEY_PATH
        if os.path.isfile(key_path):
            with open(key_path, "r") as fh:
                stored = fh.read().strip()
            if stored:
                return stored

        os.makedirs(os.path.dirname(key_path), exist_ok=True)
        generated = secrets.token_urlsafe(48)
        with open(key_path, "w") as fh:
            fh.write(generated)

        # Restrict file permissions (skip on Windows).
        if sys.platform != "win32":
            try:
                os.chmod(key_path, 0o600)
            except OSError:
                pass

        logger.warning(
            "No master key provided. Auto-generated and saved to %s. "
            "Back up this file — losing it means losing access to all "
            "encrypted credentials.",
            key_path,
        )
        return generated

    # ------------------------------------------------------------------
    # File permissions
    # ------------------------------------------------------------------

    def _enforce_permissions(self) -> None:
        """Set restrictive permissions on the DB file and directory."""
        if sys.platform == "win32":
            return

        db_dir = os.path.dirname(self._db_path)
        try:
            os.chmod(db_dir, 0o700)
        except OSError as exc:
            logger.warning("Unable to set permissions on %s: %s", db_dir, exc)
        try:
            os.chmod(self._db_path, 0o600)
        except OSError as exc:
            logger.warning("Unable to set permissions on %s: %s", self._db_path, exc)

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def _init_db(self) -> None:
        """Create the credentials table if it does not already exist."""
        with self._write_lock:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS credentials (
                    credential_id    TEXT PRIMARY KEY,
                    credential_type  TEXT NOT NULL,
                    encrypted_value  TEXT NOT NULL,
                    salt             TEXT NOT NULL,
                    created_at       REAL NOT NULL,
                    label            TEXT NOT NULL DEFAULT ''
                )
                """
            )
            self._conn.commit()

    # ------------------------------------------------------------------
    # Cryptographic helpers
    # ------------------------------------------------------------------

    def _derive_key(self, salt: bytes) -> bytes:
        """Derive a 32-byte encryption key from the master key and salt.

        Uses PBKDF2-HMAC-SHA256 with 100 000 iterations.

        :param salt: Random salt bytes.
        :returns: 32-byte derived key.
        """
        return hashlib.pbkdf2_hmac(
            "sha256",
            self._master_key.encode("utf-8"),
            salt,
            _PBKDF2_ITERATIONS,
            dklen=32,
        )

    def _encrypt(self, plaintext: str, salt: bytes) -> bytes:
        """Encrypt *plaintext* using XOR with a PBKDF2-derived key stream.

        The derived key is repeated (cycled) to match the plaintext
        length, then XOR'd byte-by-byte with the UTF-8 encoded
        plaintext.

        :param plaintext: The secret value to encrypt.
        :param salt: Salt bytes for key derivation.
        :returns: Ciphertext bytes.
        """
        key = self._derive_key(salt)
        pt_bytes = plaintext.encode("utf-8")
        key_stream = (key * ((len(pt_bytes) // len(key)) + 1))[:len(pt_bytes)]
        return bytes(a ^ b for a, b in zip(pt_bytes, key_stream))

    def _decrypt(self, ciphertext: bytes, salt: bytes) -> str:
        """Decrypt *ciphertext* produced by :meth:`_encrypt`.

        :param ciphertext: Encrypted bytes.
        :param salt: The same salt used during encryption.
        :returns: Decrypted plaintext string.
        :raises CredentialStoreError: If decryption produces invalid UTF-8.
        """
        key = self._derive_key(salt)
        key_stream = (key * ((len(ciphertext) // len(key)) + 1))[:len(ciphertext)]
        pt_bytes = bytes(a ^ b for a, b in zip(ciphertext, key_stream))
        try:
            return pt_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CredentialStoreError(
                "Decryption failed — likely wrong master key"
            ) from exc

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def store(
        self,
        credential_type: CredentialType,
        value: str,
        *,
        label: str = "",
    ) -> EncryptedCredential:
        """Encrypt and store a credential.

        :param credential_type: The kind of credential being stored.
        :param value: The plaintext secret value.
        :param label: Human-readable description (e.g. ``"Xometry API Key"``).
        :returns: An :class:`EncryptedCredential` (metadata only, no plaintext).
        :raises CredentialStoreError: On database or encryption errors.
        """
        credential_id = secrets.token_hex(16)
        salt = os.urandom(_SALT_LENGTH)
        ciphertext = self._encrypt(value, salt)

        enc_b64 = base64.b64encode(ciphertext).decode("ascii")
        salt_b64 = base64.b64encode(salt).decode("ascii")
        created_at = time.time()

        with self._write_lock:
            self._conn.execute(
                """
                INSERT INTO credentials
                    (credential_id, credential_type, encrypted_value,
                     salt, created_at, label)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    credential_id,
                    credential_type.value,
                    enc_b64,
                    salt_b64,
                    created_at,
                    label,
                ),
            )
            self._conn.commit()

        return EncryptedCredential(
            credential_id=credential_id,
            credential_type=credential_type,
            encrypted_value=enc_b64,
            salt=salt_b64,
            created_at=created_at,
            label=label,
        )

    def retrieve(self, credential_id: str) -> str:
        """Decrypt and return the plaintext value for *credential_id*.

        :param credential_id: The ID returned by :meth:`store`.
        :returns: The decrypted plaintext string.
        :raises CredentialStoreError: If the credential is not found or
            decryption fails.
        """
        with self._write_lock:
            row = self._conn.execute(
                "SELECT encrypted_value, salt FROM credentials "
                "WHERE credential_id = ?",
                (credential_id,),
            ).fetchone()
        if row is None:
            raise CredentialStoreError(
                f"Credential {credential_id!r} not found"
            )

        ciphertext = base64.b64decode(row["encrypted_value"])
        salt = base64.b64decode(row["salt"])
        return self._decrypt(ciphertext, salt)

    def delete(self, credential_id: str) -> bool:
        """Delete a credential by ID.

        :param credential_id: The credential to remove.
        :returns: ``True`` if a row was deleted, ``False`` if not found.
        """
        with self._write_lock:
            cur = self._conn.execute(
                "DELETE FROM credentials WHERE credential_id = ?",
                (credential_id,),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def list_credentials(self) -> List[EncryptedCredential]:
        """Return metadata for all stored credentials.

        The returned :class:`EncryptedCredential` objects include the
        encrypted value and salt (for internal use) but
        :meth:`EncryptedCredential.to_dict` intentionally omits them.
        """
        with self._write_lock:
            rows = self._conn.execute(
                "SELECT * FROM credentials ORDER BY created_at DESC"
            ).fetchall()
        results: List[EncryptedCredential] = []
        for row in rows:
            results.append(
                EncryptedCredential(
                    credential_id=row["credential_id"],
                    credential_type=CredentialType(row["credential_type"]),
                    encrypted_value=row["encrypted_value"],
                    salt=row["salt"],
                    created_at=row["created_at"],
                    label=row["label"],
                )
            )
        return results

    def rotate_master_key(self, new_master_key: str) -> int:
        """Re-encrypt all credentials under a new master key.

        :param new_master_key: The replacement master key.
        :returns: Number of credentials re-encrypted.
        :raises CredentialStoreError: If any credential fails to decrypt
            or re-encrypt.
        """
        with self._write_lock:
            rows = self._conn.execute(
                "SELECT credential_id, encrypted_value, salt FROM credentials"
            ).fetchall()

        old_key = self._master_key
        re_encrypted: List[tuple[str, str, str]] = []

        for row in rows:
            ciphertext = base64.b64decode(row["encrypted_value"])
            old_salt = base64.b64decode(row["salt"])

            # Decrypt with old key.
            plaintext = self._decrypt(ciphertext, old_salt)

            # Encrypt with new key.
            new_salt = os.urandom(_SALT_LENGTH)
            # Temporarily swap master key for encryption.
            self._master_key = new_master_key
            new_ciphertext = self._encrypt(plaintext, new_salt)
            self._master_key = old_key  # Restore in case of error.

            re_encrypted.append((
                base64.b64encode(new_ciphertext).decode("ascii"),
                base64.b64encode(new_salt).decode("ascii"),
                row["credential_id"],
            ))

        # Batch update inside the lock.
        with self._write_lock:
            for enc_b64, salt_b64, cred_id in re_encrypted:
                self._conn.execute(
                    "UPDATE credentials "
                    "SET encrypted_value = ?, salt = ? "
                    "WHERE credential_id = ?",
                    (enc_b64, salt_b64, cred_id),
                )
            self._conn.commit()

        # Commit the key change.
        self._master_key = new_master_key
        return len(re_encrypted)

    def close(self) -> None:
        """Close the database connection."""
        self._conn.close()


# ---------------------------------------------------------------------------
# Module-level singleton & convenience functions
# ---------------------------------------------------------------------------

_store: Optional[CredentialStore] = None


def get_credential_store() -> CredentialStore:
    """Return the module-level :class:`CredentialStore` singleton.

    The instance is lazily created on first call.
    """
    global _store
    if _store is None:
        _store = CredentialStore()
    return _store


def store_credential(
    credential_type: CredentialType,
    value: str,
    *,
    label: str = "",
) -> EncryptedCredential:
    """Convenience: encrypt and store a credential via the singleton.

    :param credential_type: The kind of credential being stored.
    :param value: The plaintext secret value.
    :param label: Human-readable description.
    :returns: An :class:`EncryptedCredential` with metadata only.
    """
    return get_credential_store().store(credential_type, value, label=label)


def retrieve_credential(credential_id: str) -> str:
    """Convenience: decrypt and return a credential via the singleton.

    :param credential_id: The credential to retrieve.
    :returns: The decrypted plaintext string.
    """
    return get_credential_store().retrieve(credential_id)
