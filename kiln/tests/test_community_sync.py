"""Tests for kiln.community_sync.sync_community_print's wire format.

The durable outbox passes a random ``send_id`` so a crash-replayed contribution
folds into a single server row.  These pin the upsert wiring: with a send_id the
request must target the ``send_id`` conflict index with ignore-duplicates; without
one it stays a plain insert (the pre-federation-column path).
"""
from __future__ import annotations

import json
from unittest import mock

import pytest


class _FakeResp:
    status = 201

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


def _capture_request():
    captured: dict = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["data"] = json.loads(req.data.decode())
        captured["headers"] = {k.lower(): v for k, v in req.headers.items()}
        return _FakeResp()

    return captured, fake_urlopen


@pytest.fixture(autouse=True)
def _opt_in(monkeypatch):
    monkeypatch.setenv("KILN_COMMUNITY_OPT_IN", "true")


def test_plain_insert_when_no_send_id():
    from kiln import community_sync

    captured, fake = _capture_request()
    with mock.patch("urllib.request.urlopen", side_effect=fake):
        ok = community_sync.sync_community_print(
            {"geometric_signature": "abc", "outcome": "success"}
        )
    assert ok is True
    assert "on_conflict" not in captured["url"]
    assert "ignore-duplicates" not in captured["headers"].get("prefer", "")
    assert "send_id" not in captured["data"]


def test_upsert_when_send_id_present():
    from kiln import community_sync

    captured, fake = _capture_request()
    with mock.patch("urllib.request.urlopen", side_effect=fake):
        ok = community_sync.sync_community_print(
            {"geometric_signature": "abc", "outcome": "success"},
            send_id="deadbeefcafef00d",
        )
    assert ok is True
    assert "on_conflict=send_id" in captured["url"]
    assert "resolution=ignore-duplicates" in captured["headers"].get("prefer", "")
    assert captured["data"]["send_id"] == "deadbeefcafef00d"
