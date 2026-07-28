"""Tests for kiln.community_sync — the contribution wire and the read gate.

Two halves, deliberately asymmetric:

* **Contribution** keeps the publishable key and stays ungated.  The durable
  outbox passes a random ``send_id`` so a crash-replayed contribution folds
  into a single server row; with a send_id the request must target the
  ``send_id`` conflict index with ignore-duplicates, without one it stays a
  plain insert (the pre-federation-column path).
* **Reads** go to the Kiln API with this machine's own bearer and come back as
  computed aggregates.  The publishable key must not appear on any read path —
  pinned structurally below, so the leak can't come back by accident.
"""
from __future__ import annotations

import ast
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


# ---------------------------------------------------------------------------
# The structural pin: the publishable key is contribution-only.
#
# The corpus tables used to be readable with the key hardcoded in this
# package, so any install could pull rows.  These two tests are the durable
# guard — they fail if a read path ever reaches for that key again, whether
# by a new function here or a new module elsewhere in kiln/src.
# ---------------------------------------------------------------------------

_SRC = Path(__file__).resolve().parents[1] / "src" / "kiln"


def _functions_referencing(module_path: Path, name: str) -> set[str]:
    """Top-level function names whose body mentions ``name``."""
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    hits: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for child in ast.walk(node):
            if isinstance(child, ast.Name) and child.id == name:
                hits.add(node.name)
    return hits


def test_publishable_key_is_contribution_only():
    """Only the insert path may touch the publishable key."""
    module = _SRC / "community_sync.py"
    assert _functions_referencing(module, "_SUPABASE_ANON_KEY") == {
        "sync_community_print"
    }


def test_no_community_table_read_anywhere_in_src():
    """No module under kiln/src reads a community table directly.

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
            # The one legitimate use is the contribution POST in this module.
            if path.name == "community_sync.py" and table == "community_prints":
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
