"""Tests for lockable safety profiles in kiln.safety_profiles."""

from __future__ import annotations

import pytest

from kiln.safety_profiles import (
    _locked_profiles,
    add_community_profile,
    is_profile_locked,
    list_locked_profiles,
    lock_safety_profile,
    unlock_safety_profile,
)


def _valid_profile():
    return {
        "max_hotend_temp": 260.0,
        "max_bed_temp": 110.0,
        "max_feedrate": 150.0,
        "build_volume": [220, 220, 250],
        "display_name": "Test Printer",
    }


class TestLockUnlock:
    def setup_method(self):
        _locked_profiles.clear()

    def test_lock_profile(self):
        assert lock_safety_profile("ender3") is True
        assert is_profile_locked("ender3") is True

    def test_unlock_profile(self):
        lock_safety_profile("ender3")
        assert unlock_safety_profile("ender3") is True
        assert is_profile_locked("ender3") is False

    def test_unlock_not_locked(self):
        assert unlock_safety_profile("ender3") is False

    def test_list_locked(self):
        lock_safety_profile("ender3")
        lock_safety_profile("bambu_x1c")
        locked = list_locked_profiles()
        assert "ender3" in locked
        assert "bambu_x1c" in locked

    def test_normalisation(self):
        lock_safety_profile("Ender-3")
        assert is_profile_locked("ender_3") is True

    def test_double_lock_is_idempotent(self):
        lock_safety_profile("ender3")
        lock_safety_profile("ender3")
        assert list_locked_profiles().count("ender3") == 1


class TestLockedProfileBlocksModification:
    def setup_method(self):
        _locked_profiles.clear()

    def test_locked_profile_rejects_add(self):
        lock_safety_profile("my_printer")
        with pytest.raises(ValueError, match="admin-locked"):
            add_community_profile("my_printer", _valid_profile())

    def test_unlocked_allows_add(self):
        lock_safety_profile("my_printer")
        unlock_safety_profile("my_printer")
        # Should not raise (though it may fail for other reasons in test env)
        try:
            add_community_profile("my_printer", _valid_profile())
        except ValueError as e:
            if "admin-locked" in str(e):
                pytest.fail("Should not be locked after unlock")
