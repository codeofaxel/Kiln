"""Tests for kiln.credential_store -- Encrypted credential storage module.

Coverage areas:
- CredentialStore initialization (explicit key, env var, auto-gen, DB dir creation)
- Store + retrieve roundtrip (various plaintext lengths, all CredentialType values)
- Retrieve non-existent credential
- Delete (existing, non-existent)
- list_credentials (empty, multiple, ordering, to_dict exclusions)
- rotate_master_key (0, 1, multiple credentials)
- Encryption determinism (same plaintext + salt + key = same ciphertext)
- Wrong master key decryption failure
- Thread safety (concurrent store/retrieve)
- Auto-generated master key file permissions
- Module-level convenience functions (get_credential_store, store_credential, retrieve_credential)
- to_dict() sensitive field exclusion
"""

from __future__ import annotations

import base64
import os
import secrets
import threading
import time
from unittest import mock

import pytest

from kiln.credential_store import (
    CredentialStore,
    CredentialStoreError,
    CredentialType,
    EncryptedCredential,
    _DEFAULT_MASTER_KEY_PATH,
    get_credential_store,
    retrieve_credential,
    store_credential,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_store(tmp_path, *, master_key="test-master-key-abc123"):
    """Create a CredentialStore backed by a temp directory."""
    db_path = str(tmp_path / "credentials.db")
    return CredentialStore(master_key=master_key, db_path=db_path)


def _insert_legacy_row(
    store: CredentialStore,
    *,
    plaintext: str,
    credential_type: CredentialType = CredentialType.API_KEY,
    label: str = "legacy",
) -> str:
    """Insert a legacy PBKDF2+XOR credential row directly into the DB."""
    credential_id = secrets.token_hex(16)
    salt = os.urandom(32)
    ciphertext = store._encrypt_legacy(plaintext, salt)
    enc_b64 = base64.b64encode(ciphertext).decode("ascii")
    salt_b64 = base64.b64encode(salt).decode("ascii")
    created_at = time.time()
    with store._write_lock:
        store._conn.execute(
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
        store._conn.commit()
    return credential_id


# ---------------------------------------------------------------------------
# 1. Initialization
# ---------------------------------------------------------------------------


class TestCredentialStoreInit:
    """Initialization: explicit key, env var, auto-gen, DB dir creation."""

    def test_explicit_master_key(self, tmp_path):
        store = _make_store(tmp_path, master_key="explicit-key-value")
        # Should be usable — store and retrieve round-trips.
        cred = store.store(CredentialType.API_KEY, "secret")
        assert store.retrieve(cred.credential_id) == "secret"
        store.close()

    def test_env_var_master_key(self, tmp_path):
        db_path = str(tmp_path / "credentials.db")
        with mock.patch.dict(os.environ, {"KILN_MASTER_KEY": "env-key-value"}, clear=False):
            store = CredentialStore(db_path=db_path)
            cred = store.store(CredentialType.API_KEY, "via-env")
            assert store.retrieve(cred.credential_id) == "via-env"
            store.close()

    def test_auto_generated_master_key(self, tmp_path):
        db_path = str(tmp_path / "credentials.db")
        # Clear both env vars and make sure no key file exists at the default path.
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch("kiln.credential_store._DEFAULT_MASTER_KEY_PATH", str(tmp_path / "master.key")):
                with mock.patch("kiln.credential_store._DEFAULT_DB_PATH", db_path):
                    store = CredentialStore(db_path=db_path)
                    # Auto-generated key should still let us round-trip.
                    cred = store.store(CredentialType.API_KEY, "auto-gen-test")
                    assert store.retrieve(cred.credential_id) == "auto-gen-test"
                    store.close()

    def test_auto_generated_key_persisted_to_file(self, tmp_path):
        key_path = str(tmp_path / "master.key")
        db_path = str(tmp_path / "credentials.db")
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch("kiln.credential_store._DEFAULT_MASTER_KEY_PATH", key_path):
                store = CredentialStore(db_path=db_path)
                store.close()
        # The key file should exist and contain a non-empty value.
        assert os.path.isfile(key_path)
        with open(key_path) as fh:
            assert len(fh.read().strip()) > 0

    def test_db_directory_created(self, tmp_path):
        nested = tmp_path / "deep" / "nested" / "dir"
        db_path = str(nested / "credentials.db")
        store = CredentialStore(master_key="key", db_path=db_path)
        assert os.path.isdir(str(nested))
        store.close()

    def test_db_path_from_env_var(self, tmp_path):
        db_path = str(tmp_path / "custom.db")
        with mock.patch.dict(os.environ, {"KILN_CREDENTIAL_DB_PATH": db_path}, clear=False):
            store = CredentialStore(master_key="key")
            assert store._db_path == db_path
            store.close()

    def test_explicit_key_takes_precedence_over_env(self, tmp_path):
        """Constructor master_key beats KILN_MASTER_KEY env var."""
        db_path = str(tmp_path / "credentials.db")
        with mock.patch.dict(os.environ, {"KILN_MASTER_KEY": "env-key"}, clear=False):
            store = CredentialStore(master_key="explicit-key", db_path=db_path)
            # Internally _master_key should be the explicit value.
            assert store._master_key == "explicit-key"
            store.close()

    def test_existing_auto_gen_key_file_reused(self, tmp_path):
        key_path = str(tmp_path / "master.key")
        db_path = str(tmp_path / "credentials.db")
        # Write a known key to the file.
        with open(key_path, "w") as fh:
            fh.write("pre-existing-key-on-disk")
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch("kiln.credential_store._DEFAULT_MASTER_KEY_PATH", key_path):
                store = CredentialStore(db_path=db_path)
                assert store._master_key == "pre-existing-key-on-disk"
                store.close()


# ---------------------------------------------------------------------------
# 2. Store + retrieve roundtrip
# ---------------------------------------------------------------------------


class TestStoreRetrieve:
    """Store and retrieve roundtrip for various plaintext lengths and credential types."""

    @pytest.mark.parametrize("length", [0, 1, 31, 32, 33, 64, 1000])
    def test_roundtrip_various_lengths(self, tmp_path, length):
        store = _make_store(tmp_path)
        plaintext = "x" * length
        cred = store.store(CredentialType.API_KEY, plaintext)
        assert store.retrieve(cred.credential_id) == plaintext
        store.close()

    @pytest.mark.parametrize("ctype", list(CredentialType))
    def test_roundtrip_all_credential_types(self, tmp_path, ctype):
        store = _make_store(tmp_path)
        cred = store.store(ctype, "secret-for-" + ctype.value)
        assert store.retrieve(cred.credential_id) == "secret-for-" + ctype.value
        assert cred.credential_type == ctype
        store.close()

    def test_store_returns_encrypted_credential(self, tmp_path):
        store = _make_store(tmp_path)
        cred = store.store(CredentialType.API_KEY, "val", label="My Key")
        assert isinstance(cred, EncryptedCredential)
        assert cred.credential_type == CredentialType.API_KEY
        assert cred.label == "My Key"
        assert len(cred.credential_id) == 32  # token_hex(16) -> 32 chars
        assert cred.created_at > 0
        assert len(cred.encrypted_value) > 0
        assert len(cred.salt) > 0
        store.close()

    def test_store_with_label(self, tmp_path):
        store = _make_store(tmp_path)
        cred = store.store(CredentialType.STRIPE_KEY, "sk_test", label="Stripe Test")
        assert cred.label == "Stripe Test"
        store.close()

    def test_store_default_empty_label(self, tmp_path):
        store = _make_store(tmp_path)
        cred = store.store(CredentialType.API_KEY, "val")
        assert cred.label == ""
        store.close()

    def test_unicode_plaintext_roundtrip(self, tmp_path):
        store = _make_store(tmp_path)
        plaintext = "unicode: \u00e9\u00e8\u00ea\u00eb \u2603 \U0001f680"
        cred = store.store(CredentialType.API_KEY, plaintext)
        assert store.retrieve(cred.credential_id) == plaintext
        store.close()

    def test_multiple_credentials_independent(self, tmp_path):
        store = _make_store(tmp_path)
        cred_a = store.store(CredentialType.API_KEY, "secret-a", label="A")
        cred_b = store.store(CredentialType.WEBHOOK_SECRET, "secret-b", label="B")
        assert store.retrieve(cred_a.credential_id) == "secret-a"
        assert store.retrieve(cred_b.credential_id) == "secret-b"
        store.close()


# ---------------------------------------------------------------------------
# 3. Retrieve non-existent credential
# ---------------------------------------------------------------------------


class TestRetrieveNonExistent:
    """Retrieve raises CredentialStoreError for missing credentials."""

    def test_retrieve_nonexistent_raises(self, tmp_path):
        store = _make_store(tmp_path)
        with pytest.raises(CredentialStoreError, match="not found"):
            store.retrieve("does-not-exist")
        store.close()

    def test_retrieve_after_delete_raises(self, tmp_path):
        store = _make_store(tmp_path)
        cred = store.store(CredentialType.API_KEY, "ephemeral")
        store.delete(cred.credential_id)
        with pytest.raises(CredentialStoreError, match="not found"):
            store.retrieve(cred.credential_id)
        store.close()


# ---------------------------------------------------------------------------
# 4. Delete
# ---------------------------------------------------------------------------


class TestDelete:
    """Delete existing and non-existent credentials."""

    def test_delete_existing_returns_true(self, tmp_path):
        store = _make_store(tmp_path)
        cred = store.store(CredentialType.API_KEY, "delete-me")
        assert store.delete(cred.credential_id) is True
        store.close()

    def test_delete_nonexistent_returns_false(self, tmp_path):
        store = _make_store(tmp_path)
        assert store.delete("nonexistent-id") is False
        store.close()

    def test_delete_removes_from_list(self, tmp_path):
        store = _make_store(tmp_path)
        cred = store.store(CredentialType.API_KEY, "val")
        assert len(store.list_credentials()) == 1
        store.delete(cred.credential_id)
        assert len(store.list_credentials()) == 0
        store.close()

    def test_delete_does_not_affect_other_credentials(self, tmp_path):
        store = _make_store(tmp_path)
        cred_a = store.store(CredentialType.API_KEY, "keep-a")
        cred_b = store.store(CredentialType.WEBHOOK_SECRET, "delete-b")
        store.delete(cred_b.credential_id)
        assert store.retrieve(cred_a.credential_id) == "keep-a"
        assert len(store.list_credentials()) == 1
        store.close()


# ---------------------------------------------------------------------------
# 5. list_credentials
# ---------------------------------------------------------------------------


class TestListCredentials:
    """list_credentials: empty, multiple, ordering, to_dict exclusions."""

    def test_empty_store(self, tmp_path):
        store = _make_store(tmp_path)
        assert store.list_credentials() == []
        store.close()

    def test_multiple_credentials(self, tmp_path):
        store = _make_store(tmp_path)
        store.store(CredentialType.API_KEY, "a", label="first")
        store.store(CredentialType.WEBHOOK_SECRET, "b", label="second")
        store.store(CredentialType.STRIPE_KEY, "c", label="third")
        creds = store.list_credentials()
        assert len(creds) == 3
        labels = {c.label for c in creds}
        assert labels == {"first", "second", "third"}
        store.close()

    def test_ordering_descending_by_created_at(self, tmp_path):
        store = _make_store(tmp_path)
        # Insert with controlled timestamps via time.sleep to guarantee ordering.
        cred_a = store.store(CredentialType.API_KEY, "a", label="oldest")
        time.sleep(0.02)
        cred_b = store.store(CredentialType.API_KEY, "b", label="middle")
        time.sleep(0.02)
        cred_c = store.store(CredentialType.API_KEY, "c", label="newest")
        creds = store.list_credentials()
        # Descending by created_at: newest first.
        assert creds[0].credential_id == cred_c.credential_id
        assert creds[1].credential_id == cred_b.credential_id
        assert creds[2].credential_id == cred_a.credential_id
        store.close()

    def test_list_returns_encrypted_credential_instances(self, tmp_path):
        store = _make_store(tmp_path)
        store.store(CredentialType.API_KEY, "val")
        creds = store.list_credentials()
        assert isinstance(creds[0], EncryptedCredential)
        store.close()

    def test_to_dict_excludes_encrypted_value_and_salt(self, tmp_path):
        store = _make_store(tmp_path)
        store.store(CredentialType.API_KEY, "val", label="test")
        creds = store.list_credentials()
        d = creds[0].to_dict()
        assert "encrypted_value" not in d
        assert "salt" not in d
        assert "credential_id" in d
        assert "credential_type" in d
        assert "created_at" in d
        assert "label" in d
        store.close()


# ---------------------------------------------------------------------------
# 6. rotate_master_key
# ---------------------------------------------------------------------------


class TestRotateMasterKey:
    """rotate_master_key: 0, 1, and multiple credentials remain retrievable."""

    def test_rotate_zero_credentials(self, tmp_path):
        store = _make_store(tmp_path)
        count = store.rotate_master_key("new-key")
        assert count == 0
        store.close()

    def test_rotate_one_credential(self, tmp_path):
        store = _make_store(tmp_path)
        cred = store.store(CredentialType.API_KEY, "rotate-me")
        count = store.rotate_master_key("rotated-key")
        assert count == 1
        # Must still be retrievable after rotation.
        assert store.retrieve(cred.credential_id) == "rotate-me"
        store.close()

    def test_rotate_multiple_credentials(self, tmp_path):
        store = _make_store(tmp_path)
        cred_a = store.store(CredentialType.API_KEY, "secret-a")
        cred_b = store.store(CredentialType.WEBHOOK_SECRET, "secret-b")
        cred_c = store.store(CredentialType.STRIPE_KEY, "secret-c")
        count = store.rotate_master_key("new-master")
        assert count == 3
        assert store.retrieve(cred_a.credential_id) == "secret-a"
        assert store.retrieve(cred_b.credential_id) == "secret-b"
        assert store.retrieve(cred_c.credential_id) == "secret-c"
        store.close()

    def test_rotate_changes_ciphertext(self, tmp_path):
        store = _make_store(tmp_path)
        cred = store.store(CredentialType.API_KEY, "unchanged-plaintext")
        old_enc = store.list_credentials()[0].encrypted_value
        store.rotate_master_key("different-master-key")
        new_enc = store.list_credentials()[0].encrypted_value
        # Ciphertext should differ after rotation (different key + different salt).
        assert old_enc != new_enc
        store.close()

    def test_rotate_updates_internal_master_key(self, tmp_path):
        store = _make_store(tmp_path)
        store.store(CredentialType.API_KEY, "val")
        store.rotate_master_key("the-new-key")
        assert store._master_key == "the-new-key"
        store.close()

    def test_rotate_migrates_legacy_rows_to_v2(self, tmp_path):
        store = _make_store(tmp_path)
        cred_id = _insert_legacy_row(store, plaintext="legacy-secret")
        count = store.rotate_master_key("new-master")
        assert count == 1
        assert store.retrieve(cred_id) == "legacy-secret"
        with store._write_lock:
            row = store._conn.execute(
                "SELECT encrypted_value FROM credentials WHERE credential_id = ?",
                (cred_id,),
            ).fetchone()
        assert row is not None
        assert row["encrypted_value"].startswith("v2:")
        store.close()


# ---------------------------------------------------------------------------
# 7. Encryption correctness
# ---------------------------------------------------------------------------


class TestEncryption:
    """Encryption determinism and correctness properties."""

    def test_same_plaintext_same_salt_same_key_uses_random_nonce(self, tmp_path):
        store = _make_store(tmp_path)
        salt = b"\x01" * 32
        ct1 = store._encrypt("hello", salt)
        ct2 = store._encrypt("hello", salt)
        # AES-GCM uses a random nonce, so ciphertext should differ.
        assert ct1 != ct2
        assert store._decrypt(ct1, salt) == "hello"
        assert store._decrypt(ct2, salt) == "hello"

    def test_different_salts_produce_different_ciphertext(self, tmp_path):
        store = _make_store(tmp_path)
        salt_a = b"\x01" * 32
        salt_b = b"\x02" * 32
        ct_a = store._encrypt("hello", salt_a)
        ct_b = store._encrypt("hello", salt_b)
        assert ct_a != ct_b

    def test_encrypt_decrypt_roundtrip(self, tmp_path):
        store = _make_store(tmp_path)
        salt = os.urandom(32)
        plaintext = "roundtrip-test-value"
        ciphertext = store._encrypt(plaintext, salt)
        assert store._decrypt(ciphertext, salt) == plaintext

    def test_ciphertext_differs_from_plaintext(self, tmp_path):
        store = _make_store(tmp_path)
        salt = b"\xaa" * 32
        plaintext = "visible-secret"
        ciphertext = store._encrypt(plaintext, salt)
        assert ciphertext != plaintext.encode("utf-8")

    def test_wrong_master_key_fails_to_decrypt(self, tmp_path):
        db_path = str(tmp_path / "credentials.db")
        store_a = CredentialStore(master_key="correct-key", db_path=db_path)
        cred = store_a.store(CredentialType.API_KEY, "protected-secret")
        store_a.close()

        store_b = CredentialStore(master_key="wrong-key", db_path=db_path)
        with pytest.raises(CredentialStoreError, match="wrong master key|corrupted"):
            store_b.retrieve(cred.credential_id)
        store_b.close()

    def test_empty_plaintext_roundtrip(self, tmp_path):
        store = _make_store(tmp_path)
        salt = os.urandom(32)
        ciphertext = store._encrypt("", salt)
        assert ciphertext != b""
        assert store._decrypt(ciphertext, salt) == ""

    def test_key_derivation_deterministic(self, tmp_path):
        store = _make_store(tmp_path)
        salt = b"\xff" * 32
        key1 = store._derive_key(salt)
        key2 = store._derive_key(salt)
        assert key1 == key2
        assert len(key1) == 32

    def test_store_uses_v2_prefix(self, tmp_path):
        store = _make_store(tmp_path)
        cred = store.store(CredentialType.API_KEY, "new-format")
        assert cred.encrypted_value.startswith("v2:")
        store.close()

    def test_retrieve_legacy_row_auto_migrates_to_v2(self, tmp_path):
        store = _make_store(tmp_path)
        cred_id = _insert_legacy_row(store, plaintext="legacy-value")
        assert store.retrieve(cred_id) == "legacy-value"
        with store._write_lock:
            row = store._conn.execute(
                "SELECT encrypted_value FROM credentials WHERE credential_id = ?",
                (cred_id,),
            ).fetchone()
        assert row is not None
        assert row["encrypted_value"].startswith("v2:")
        store.close()


# ---------------------------------------------------------------------------
# 8. Thread safety
# ---------------------------------------------------------------------------


class TestThreadSafety:
    """Concurrent store/retrieve operations don't corrupt data."""

    def test_concurrent_store_and_retrieve(self, tmp_path):
        store = _make_store(tmp_path)
        errors = []
        stored_ids = []
        lock = threading.Lock()

        def store_and_retrieve(idx):
            try:
                value = f"secret-{idx}"
                cred = store.store(CredentialType.API_KEY, value, label=f"thread-{idx}")
                with lock:
                    stored_ids.append(cred.credential_id)
                retrieved = store.retrieve(cred.credential_id)
                assert retrieved == value
            except Exception as exc:
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=store_and_retrieve, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"Thread errors: {errors}"
        assert len(stored_ids) == 20
        # All credentials should be in the store.
        creds = store.list_credentials()
        assert len(creds) == 20
        store.close()


# ---------------------------------------------------------------------------
# 9. File permissions
# ---------------------------------------------------------------------------


class TestFilePermissions:
    """Auto-generated master key file permissions."""

    def test_auto_gen_key_calls_chmod(self, tmp_path):
        key_path = str(tmp_path / "master.key")
        db_path = str(tmp_path / "credentials.db")
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch("kiln.credential_store._DEFAULT_MASTER_KEY_PATH", key_path):
                with mock.patch("kiln.credential_store.sys") as mock_sys:
                    mock_sys.platform = "linux"
                    with mock.patch("kiln.credential_store.os.chmod") as mock_chmod:
                        # Let other os functions work normally.
                        store = CredentialStore(db_path=db_path)
                        # chmod should have been called on the key file with 0o600.
                        chmod_calls = [c for c in mock_chmod.call_args_list if key_path in str(c)]
                        assert len(chmod_calls) >= 1
                        # Verify 0o600 was used for key file.
                        found_key_chmod = False
                        for call in mock_chmod.call_args_list:
                            if call[0][0] == key_path and call[0][1] == 0o600:
                                found_key_chmod = True
                        assert found_key_chmod
                        store.close()

    def test_enforce_permissions_sets_db_and_dir(self, tmp_path):
        db_path = str(tmp_path / "subdir" / "credentials.db")
        store = CredentialStore(master_key="key", db_path=db_path)
        with mock.patch("kiln.credential_store.os.chmod") as mock_chmod:
            with mock.patch("kiln.credential_store.sys") as mock_sys:
                mock_sys.platform = "linux"
                store._enforce_permissions()
                # Should have called chmod for directory (0o700) and file (0o600).
                assert mock_chmod.call_count == 2
                calls = mock_chmod.call_args_list
                dir_call = calls[0]
                file_call = calls[1]
                assert dir_call[0][1] == 0o700
                assert file_call[0][1] == 0o600
        store.close()

    def test_enforce_permissions_skipped_on_windows(self, tmp_path):
        store = _make_store(tmp_path)
        with mock.patch("kiln.credential_store.sys") as mock_sys:
            mock_sys.platform = "win32"
            with mock.patch("kiln.credential_store.os.chmod") as mock_chmod:
                store._enforce_permissions()
                mock_chmod.assert_not_called()
        store.close()


# ---------------------------------------------------------------------------
# 10. Module-level convenience functions
# ---------------------------------------------------------------------------


class TestModuleFunctions:
    """Module-level singleton and convenience functions."""

    def test_get_credential_store_returns_instance(self, tmp_path):
        import kiln.credential_store as mod
        db_path = str(tmp_path / "singleton.db")
        # Reset the singleton.
        mod._store = None
        with mock.patch.dict(os.environ, {"KILN_CREDENTIAL_DB_PATH": db_path}, clear=False):
            with mock.patch.dict(os.environ, {"KILN_MASTER_KEY": "singleton-key"}, clear=False):
                s = get_credential_store()
                assert isinstance(s, CredentialStore)
                # Calling again returns the same instance.
                assert get_credential_store() is s
                s.close()
                mod._store = None  # Clean up.

    def test_store_credential_convenience(self, tmp_path):
        import kiln.credential_store as mod
        db_path = str(tmp_path / "convenience.db")
        mod._store = None
        with mock.patch.dict(os.environ, {"KILN_CREDENTIAL_DB_PATH": db_path, "KILN_MASTER_KEY": "conv-key"}, clear=False):
            cred = store_credential(CredentialType.API_KEY, "conv-secret", label="conv")
            assert isinstance(cred, EncryptedCredential)
            assert cred.label == "conv"
            mod._store.close()
            mod._store = None

    def test_retrieve_credential_convenience(self, tmp_path):
        import kiln.credential_store as mod
        db_path = str(tmp_path / "convenience2.db")
        mod._store = None
        with mock.patch.dict(os.environ, {"KILN_CREDENTIAL_DB_PATH": db_path, "KILN_MASTER_KEY": "conv-key2"}, clear=False):
            cred = store_credential(CredentialType.WEBHOOK_SECRET, "webhook-val")
            plaintext = retrieve_credential(cred.credential_id)
            assert plaintext == "webhook-val"
            mod._store.close()
            mod._store = None


# ---------------------------------------------------------------------------
# 11. to_dict()
# ---------------------------------------------------------------------------


class TestToDict:
    """EncryptedCredential.to_dict() excludes sensitive fields and serializes enums."""

    def test_excludes_encrypted_value(self):
        cred = EncryptedCredential(
            credential_id="abc",
            credential_type=CredentialType.API_KEY,
            encrypted_value="c2VjcmV0",
            salt="c2FsdA==",
            created_at=1000.0,
            label="test",
        )
        d = cred.to_dict()
        assert "encrypted_value" not in d
        assert "salt" not in d

    def test_includes_expected_fields(self):
        cred = EncryptedCredential(
            credential_id="abc123",
            credential_type=CredentialType.STRIPE_KEY,
            encrypted_value="enc",
            salt="slt",
            created_at=1234.5,
            label="My Stripe Key",
        )
        d = cred.to_dict()
        assert d["credential_id"] == "abc123"
        assert d["credential_type"] == "stripe_key"
        assert d["created_at"] == 1234.5
        assert d["label"] == "My Stripe Key"

    def test_credential_type_serialized_as_string(self):
        for ct in CredentialType:
            cred = EncryptedCredential(
                credential_id="x",
                credential_type=ct,
                encrypted_value="e",
                salt="s",
                created_at=0.0,
                label="",
            )
            d = cred.to_dict()
            assert d["credential_type"] == ct.value
            assert isinstance(d["credential_type"], str)
