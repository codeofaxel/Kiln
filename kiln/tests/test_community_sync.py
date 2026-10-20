"""Tests for kiln.community_sync — the contribution wire and the read gate.

Two halves, deliberately asymmetric:

* **Contribution** goes to the Kiln API anonymously -- no credential of any
  kind -- and stays ungated.  The durable outbox passes a random ``send_id``
  so a crash-replayed contribution folds into a single server row.  The
  server's answer decides the row's fate: a refusal spends a retry, "later"
  (busy, unavailable, offline, a server without the route) does not.
* **Reads** go to the Kiln API with this machine's own bearer and come back as
  computed aggregates.  No database key exists anywhere in this package --
  pinned structurally below, so neither leak can come back by accident.
"""
from __future__ import annotations

import json
import urllib.error
from pathlib import Path
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
    # This file EXERCISES the wire function against a mocked urllib, so it
    # opts into the send path explicitly.  Everywhere else the wire guard
    # (``network_sends_suppressed``) refuses under a test runner by
    # default — the guard's own tests below delete this env again.
    monkeypatch.setenv("KILN_COMMUNITY_TEST_SEND", "1")


class TestWireGuard:
    """The 2026-08-05 leak, pinned: a test runner must be physically unable
    to POST to the production corpus unless it says so by name.

    Seven fixture rows (``sig123``) reached the real ``community_prints``
    table because the outbox's DB guard stands down for a custom
    ``KILN_DB_PATH`` — the standard test isolation — and the sender under
    it had no guard at all.  These tests replay that shape and assert the
    wire now refuses.
    """

    def test_sender_refuses_under_test_runner_by_default(self, monkeypatch):
        from kiln import community_sync

        monkeypatch.delenv("KILN_COMMUNITY_TEST_SEND", raising=False)
        with mock.patch("urllib.request.urlopen") as urlopen:
            ok = community_sync.sync_community_print(
                {"geometric_signature": "sig123", "outcome": "success"}
            )
        assert ok is False
        urlopen.assert_not_called()

    def test_async_sender_refuses_under_test_runner_by_default(self, monkeypatch):
        from kiln import community_sync

        monkeypatch.delenv("KILN_COMMUNITY_TEST_SEND", raising=False)
        with mock.patch("threading.Thread") as thread:
            community_sync.sync_community_print_async(
                {"geometric_signature": "sig123", "outcome": "success"}
            )
        thread.assert_not_called()

    def test_explicit_escape_reaches_the_wire(self):
        # The autouse fixture set the escape; the wire runs (into the mock).
        from kiln import community_sync

        captured, fake = _capture_request()
        with mock.patch("urllib.request.urlopen", side_effect=fake):
            ok = community_sync.sync_community_print(
                {"geometric_signature": "abc", "outcome": "success"}
            )
        assert ok is True
        assert captured["url"].endswith("/api/community/contribute")

    def test_guard_answers_independent_of_db_path(self, monkeypatch, tmp_path):
        """The exact disarm that shipped the leak: a custom KILN_DB_PATH
        stands the OUTBOX guard down (correctly — DB isolation), and must
        not stand the WIRE guard down with it."""
        from kiln import community_sync

        monkeypatch.delenv("KILN_COMMUNITY_TEST_SEND", raising=False)
        monkeypatch.setenv("KILN_DB_PATH", str(tmp_path / "kiln.db"))
        assert community_sync.network_sends_suppressed() is True


def test_no_send_id_sends_none_and_the_server_mints_one():
    from kiln import community_sync

    captured, fake = _capture_request()
    with mock.patch("urllib.request.urlopen", side_effect=fake):
        ok = community_sync.sync_community_print(
            {"geometric_signature": "abc", "outcome": "success"}
        )
    assert ok is True
    assert "send_id" not in captured["data"]
    assert captured["data"]["kind"] == "print"
    assert captured["data"]["record"]["geometric_signature"] == "abc"


def test_send_id_travels_with_the_contribution():
    from kiln import community_sync

    captured, fake = _capture_request()
    with mock.patch("urllib.request.urlopen", side_effect=fake):
        ok = community_sync.sync_community_print(
            {"geometric_signature": "abc", "outcome": "success"},
            send_id="deadbeefcafef00d",
        )
    assert ok is True
    assert captured["data"]["send_id"] == "deadbeefcafef00d"
    assert "send_id" not in captured["data"]["record"]


def test_a_contribution_carries_no_credential():
    """Anonymous by construction: nothing on the request names the sender."""
    from kiln import community_sync

    captured, fake = _capture_request()
    with mock.patch("kiln.auth_session.resolve_api_bearer") as bearer, \
         mock.patch("urllib.request.urlopen", side_effect=fake):
        community_sync.sync_community_print(
            {"geometric_signature": "abc", "outcome": "success"}
        )
    bearer.assert_not_called()
    assert "authorization" not in captured["headers"]
    assert "apikey" not in captured["headers"]


def _answer(code: int):
    def _raise(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, code, "x", {}, None)

    return _raise


@pytest.mark.parametrize("code", [400, 422])
def test_a_refusal_is_about_the_row(code):
    from kiln import community_outbox, community_sync

    with mock.patch("urllib.request.urlopen", side_effect=_answer(code)):
        got = community_sync.sync_community_print({"geometric_signature": "abc"})
    assert got is False and got is not community_outbox.DEFERRED


@pytest.mark.parametrize("code", [404, 429, 500, 502, 503])
def test_busy_or_unavailable_is_a_wait_not_a_failure(code):
    """The 2026-07-28 loss shape: a shut door must not spend the row's retries."""
    from kiln import community_outbox, community_sync

    with mock.patch("urllib.request.urlopen", side_effect=_answer(code)):
        got = community_sync.sync_community_print({"geometric_signature": "abc"})
    assert got is community_outbox.DEFERRED
    assert not got


def test_offline_is_a_wait_not_a_failure():
    from kiln import community_outbox, community_sync

    with mock.patch("urllib.request.urlopen", side_effect=OSError("no route")):
        got = community_sync.sync_community_print({"geometric_signature": "abc"})
    assert got is community_outbox.DEFERRED


# ---------------------------------------------------------------------------
# The structural pin: nothing here talks to the database.
#
# The corpus tables used to be readable with a key hardcoded in this module,
# and until this release contributions were written with it.  Both halves now
# go through the Kiln API; these two tests fail if either path reaches for
# the database again, here or anywhere else in kiln/src.
# ---------------------------------------------------------------------------

_SRC = Path(__file__).resolve().parents[1] / "src" / "kiln"


def test_no_database_key_in_the_community_module():
    text = (_SRC / "community_sync.py").read_text(encoding="utf-8")
    assert "sb_publishable_" not in text
    assert "supabase.co" not in text


def test_no_community_table_read_anywhere_in_src():
    """No module under kiln/src touches a community table directly.

    The corpus is reachable only through the Kiln API, which computes the
    aggregate server-side.  A direct table URL in a GET is the shape of the
    leak this closed, so any reappearance fails here.
    """
    offenders: list[str] = []
    for path in _SRC.rglob("*.py"):
        text = path.read_text(encoding="utf-8", errors="ignore")
        for table in ("community_prints", "community_recoveries"):
            marker = f"/rest/v1/{table}"
            if marker not in text:
                continue
            offenders.append(f"{path.relative_to(_SRC)} -> {marker}")
    assert not offenders, f"direct community-table access: {offenders}"


# ---------------------------------------------------------------------------
# Read side — API-backed, and every empty outcome is the same None.
# ---------------------------------------------------------------------------


@pytest.fixture
def _no_cache(tmp_path, monkeypatch):
    """Point the disk cache at a scratch dir so reads actually hit the wire."""
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


class _JsonResp:
    def __init__(self, payload, status=200):
        self._payload = json.dumps(payload).encode()
        self.status = status

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


def _bearer(monkeypatch, token="tok-live"):
    from kiln import auth_session

    monkeypatch.setattr(
        auth_session,
        "resolve_api_bearer",
        lambda *a, **k: auth_session.ApiBearer(token=token, state="live"),
    )


def test_read_returns_none_when_signed_out(_no_cache, monkeypatch):
    from kiln import auth_session, community_sync

    monkeypatch.setenv("KILN_COMMUNITY_OPT_IN", "true")
    monkeypatch.setattr(
        auth_session,
        "resolve_api_bearer",
        lambda *a, **k: auth_session.ApiBearer(token="", state="signed_out"),
    )
    with mock.patch("urllib.request.urlopen") as urlopen:
        assert community_sync.fetch_community_insights("bambu_x1c", "PLA") is None
        assert (
            community_sync.fetch_community_insight_for_signature("sig-1") is None
        )
    urlopen.assert_not_called()


def test_read_calls_kiln_api_with_bearer(_no_cache, monkeypatch):
    from kiln import community_sync

    monkeypatch.setenv("KILN_COMMUNITY_OPT_IN", "true")
    _bearer(monkeypatch)
    captured: dict = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data.decode())
        captured["headers"] = {k.lower(): v for k, v in req.headers.items()}
        return _JsonResp({
            "has_data": True,
            "failure_breakdown": {"warping": 3},
            "sample_size": 12,
            "success_count": 9,
        })

    with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
        result = community_sync.fetch_community_insights("bambu_x1c", "PLA")

    assert captured["url"].endswith("/api/community/insight")
    assert captured["headers"]["authorization"] == "Bearer tok-live"
    assert captured["body"] == {"printer_model": "bambu_x1c", "material": "PLA"}
    assert "apikey" not in captured["headers"]
    assert result["failure_breakdown"] == {"warping": 3}
    assert result["sample_size"] == 12
    assert result["success_count"] == 9
    assert result["source"] == "community"


def test_read_returns_none_when_server_refuses_by_tier(_no_cache, monkeypatch):
    """A 403 is not an error the caller sees — it's just no community data."""
    from kiln import community_sync

    monkeypatch.setenv("KILN_COMMUNITY_OPT_IN", "true")
    _bearer(monkeypatch)

    def refuse(req, timeout=None):
        raise urllib.error.HTTPError(
            req.full_url, 403, "Forbidden", {}, None,
        )

    with mock.patch("urllib.request.urlopen", side_effect=refuse):
        assert community_sync.fetch_community_insights("bambu_x1c", "PLA") is None
        assert (
            community_sync.fetch_community_insight_for_signature("sig-1") is None
        )


def test_read_returns_none_when_offline(_no_cache, monkeypatch):
    from kiln import community_sync

    monkeypatch.setenv("KILN_COMMUNITY_OPT_IN", "true")
    _bearer(monkeypatch)

    with mock.patch("urllib.request.urlopen", side_effect=OSError("no route")):
        assert community_sync.fetch_community_insights("bambu_x1c", "PLA") is None


def test_signature_read_returns_registry_shape(_no_cache, monkeypatch):
    """The API answer is a drop-in for a locally computed insight."""
    from kiln import community_sync

    monkeypatch.setenv("KILN_COMMUNITY_OPT_IN", "true")
    _bearer(monkeypatch)
    insight = {
        "geometric_signature": "sig-1",
        "total_prints": 40,
        "success_rate": 0.9,
        "top_printer_models": [{"model": "bambu_x1c", "count": 30}],
        "top_materials": [{"material": "PLA", "count": 35}],
        "recommended_settings": {"layer_height": 0.2},
        "common_failures": [{"mode": "warping", "count": 3, "percentage": 7.5}],
        "average_print_time_seconds": 1800,
        "confidence": "high",
        # The hosted aggregate names the design it matched, same as the
        # local one — kiln_pro/_rest/community.py passes it through.
        "geometric_signature_v2": "v2:abc123",
    }

    with mock.patch(
        "urllib.request.urlopen",
        side_effect=lambda req, timeout=None: _JsonResp({
            "has_data": True,
            "insight": insight,
            "top_settings_groups": [{"settings_hash": "abc", "count": 20}],
        }),
    ):
        got = community_sync.fetch_community_insight_for_signature("sig-1")

    from kiln.community_registry import CommunityInsight

    for field in CommunityInsight.__dataclass_fields__:
        assert field in got, f"missing drop-in field {field}"
    assert got["top_settings_groups"] == [{"settings_hash": "abc", "count": 20}]


def test_read_returns_none_when_sharing_disabled(_no_cache, monkeypatch):
    from kiln import community_sync

    monkeypatch.setenv("KILN_COMMUNITY_OPT_IN", "false")
    with mock.patch("urllib.request.urlopen") as urlopen:
        assert community_sync.fetch_community_insights("bambu_x1c", "PLA") is None
        assert (
            community_sync.fetch_community_insight_for_signature("sig-1") is None
        )
    urlopen.assert_not_called()
