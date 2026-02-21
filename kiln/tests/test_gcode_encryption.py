"""Tests for kiln.gcode_encryption."""

from __future__ import annotations

import os
from unittest import mock

import pytest

from kiln.gcode_encryption import (
    _HEADER,
    GcodeEncryption,
    GcodeEncryptionError,
    _check_cryptography_installed,
)


class TestGcodeEncryptionNotConfigured:
    def test_not_available_without_key(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            enc = GcodeEncryption()
            assert enc.is_available is False

    def test_encrypt_raises_without_key(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            enc = GcodeEncryption()
            with pytest.raises(GcodeEncryptionError, match="not available"):
                enc.encrypt(b"G28")

    def test_decrypt_passthrough_for_unencrypted(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            enc = GcodeEncryption()
            data = b"G28\nG1 X10 Y10"
            assert enc.decrypt(data) == data

    def test_decrypt_raises_for_encrypted_without_key(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            enc = GcodeEncryption()
            with pytest.raises(GcodeEncryptionError, match="not configured"):
                enc.decrypt(_HEADER + b"some_encrypted_data")

    def test_status_without_key(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            enc = GcodeEncryption()
            s = enc.status()
            assert s["available"] is False
            assert s["key_configured"] is False


class TestGcodeEncryptionConfigured:
    def test_roundtrip(self):
        with mock.patch.dict(os.environ, {"KILN_ENCRYPTION_KEY": "test-secret-key-123"}, clear=True):
            enc = GcodeEncryption()
            if not enc.is_available:
                pytest.skip("cryptography library not installed")
            original = b"G28\nG1 X10 Y10 F3000\nM104 S200"
            encrypted = enc.encrypt(original)
            assert encrypted.startswith(_HEADER)
            assert encrypted != original
            decrypted = enc.decrypt(encrypted)
            assert decrypted == original

    def test_is_encrypted_check(self):
        assert GcodeEncryption.is_encrypted(_HEADER + b"data") is True
        assert GcodeEncryption.is_encrypted(b"G28") is False

    def test_status_with_key(self):
        with mock.patch.dict(os.environ, {"KILN_ENCRYPTION_KEY": "test-key"}, clear=True):
            enc = GcodeEncryption()
            s = enc.status()
            assert s["key_configured"] is True

    def test_key_set_but_salt_unavailable_disables_encryption(self):
        with mock.patch.dict(os.environ, {"KILN_ENCRYPTION_KEY": "test-key"}, clear=True):
            with mock.patch("kiln.gcode_encryption._get_or_create_salt", side_effect=RuntimeError("salt unavailable")):
                enc = GcodeEncryption()
                assert enc.is_available is False


class TestCryptographyCheck:
    def test_returns_bool(self):
        result = _check_cryptography_installed()
        assert isinstance(result, bool)
