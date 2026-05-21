"""Tests for fulfillment shipping profiles and confirmation tokens."""

from __future__ import annotations

import os

from kiln.fulfillment_profiles import (
    delete_shipping_profile,
    get_shipping_profile,
    issue_shipping_confirmation_token,
    list_shipping_profiles,
    normalize_shipping_address,
    save_shipping_profile,
    summarize_shipping_address,
    validate_shipping_confirmation_token,
)

ADDRESS = {
    "first_name": "Ada",
    "last_name": "Lovelace",
    "email": "ada@example.com",
    "phone": "555-0100",
    "street": "123 Main St",
    "city": "Austin",
    "state": "TX",
    "postal_code": "78701",
    "country": "us",
}


def test_save_list_get_delete_shipping_profile(monkeypatch, tmp_path):
    monkeypatch.setenv("KILN_SHIPPING_PROFILES_PATH", str(tmp_path / "profiles.json"))

    saved = save_shipping_profile(
        "home",
        ADDRESS,
        overwrite=False,
        set_default=True,
    )

    assert saved.name == "home"
    assert saved.shipping_address["country"] == "US"
    assert saved.is_default is True

    profiles = list_shipping_profiles()
    assert [profile.name for profile in profiles] == ["home"]
    assert get_shipping_profile().shipping_address["email"] == "ada@example.com"

    assert delete_shipping_profile("home") is True
    assert list_shipping_profiles() == []


def test_profile_summary_redacts_contact_details_by_default(monkeypatch, tmp_path):
    profile_path = tmp_path / "profiles.json"
    monkeypatch.setenv("KILN_SHIPPING_PROFILES_PATH", str(profile_path))

    save_shipping_profile("home", ADDRESS, set_default=True)

    profile = list_shipping_profiles()[0].to_dict()
    assert "123 Main St" not in profile["summary"]
    assert "ada@example.com" not in profile["summary"]
    assert "555-0100" not in profile["summary"]
    assert "Austin" in profile["summary"]
    # The 0600 mode bits are POSIX-only — fulfillment_profiles skips
    # the chmod on Windows, where st_mode has no group/other concept.
    if os.name != "nt":
        assert profile_path.stat().st_mode & 0o777 == 0o600


def test_shipping_summary_can_show_full_address_for_explicit_confirmation():
    summary = summarize_shipping_address(ADDRESS, redact_sensitive=False)

    assert "123 Main St" in summary
    assert "ada@example.com" in summary
    assert "555-0100" in summary


def test_normalize_shipping_address_rejects_missing_state_for_us():
    address = dict(ADDRESS)
    address.pop("state")

    try:
        normalize_shipping_address(address)
    except ValueError as exc:
        assert "state" in str(exc)
    else:
        raise AssertionError("Expected missing state to be rejected")


def test_shipping_confirmation_token_is_single_use_and_bound_to_address():
    token = issue_shipping_confirmation_token(
        quote_id="quote-1",
        shipping_option_id="ship-1",
        shipping_address=ADDRESS,
    )

    ok, reason = validate_shipping_confirmation_token(
        token.token,
        quote_id="quote-1",
        shipping_option_id="ship-1",
        shipping_address=ADDRESS,
    )
    assert ok, reason

    ok, reason = validate_shipping_confirmation_token(
        token.token,
        quote_id="quote-1",
        shipping_option_id="ship-1",
        shipping_address=ADDRESS,
    )
    assert not ok
    assert reason == "token_not_found_or_already_used"


def test_shipping_confirmation_token_rejects_changed_address():
    token = issue_shipping_confirmation_token(
        quote_id="quote-1",
        shipping_option_id="ship-1",
        shipping_address=ADDRESS,
    )
    changed = dict(ADDRESS)
    changed["postal_code"] = "99999"

    ok, reason = validate_shipping_confirmation_token(
        token.token,
        quote_id="quote-1",
        shipping_option_id="ship-1",
        shipping_address=changed,
    )

    assert not ok
    assert reason == "token_shipping_details_mismatch"
