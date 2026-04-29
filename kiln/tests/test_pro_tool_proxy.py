"""Tests for public Kiln's hosted kiln-pro tool proxy stubs."""

from __future__ import annotations

import json

from kiln.server import _pro_api_call


class _FakeUrlopenResponse:
    def __init__(self, body: bytes = b'{"success": true}') -> None:
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def read(self) -> bytes:
        return self._body


def test_pro_api_call_requires_pairing_when_no_token(tmp_path, monkeypatch):
    monkeypatch.setenv("KILN_AUTH_HOME", str(tmp_path))
    monkeypatch.delenv("KILN_API_URL", raising=False)
    monkeypatch.delenv("KILN_LICENSE_KEY", raising=False)

    result = _pro_api_call("cloud_remote_list")

    assert result["status"] == "error"
    assert result["code"] == "KILN_ACCOUNT_NOT_PAIRED"
    assert "python3 -m kiln pair <code>" in result["error"]


def test_pro_api_call_uses_paired_token_against_hosted_api(tmp_path, monkeypatch):
    auth_dir = tmp_path / ".kiln"
    auth_dir.mkdir()
    (auth_dir / "auth_tokens.json").write_text(
        json.dumps({"access_token": "oauth-token"}), encoding="utf-8",
    )
    monkeypatch.setenv("KILN_AUTH_HOME", str(tmp_path))
    monkeypatch.delenv("KILN_API_URL", raising=False)
    monkeypatch.delenv("KILN_LICENSE_KEY", raising=False)
    captured = {}

    def fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        captured["headers"] = dict(req.headers)
        captured["data"] = req.data
        captured["timeout"] = timeout
        return _FakeUrlopenResponse(b'{"status": "ok"}')

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    result = _pro_api_call("cloud_remote_list", design_id="abc")

    assert result == {"status": "ok"}
    assert captured["url"] == "https://api.kiln3d.com/api/tools/cloud_remote_list"
    assert captured["headers"]["Authorization"] == "Bearer oauth-token"
    assert json.loads(captured["data"]) == {"design_id": "abc"}
    assert captured["timeout"] == 30


def test_pro_api_call_allows_api_and_license_key_overrides(tmp_path, monkeypatch):
    auth_dir = tmp_path / ".kiln"
    auth_dir.mkdir()
    (auth_dir / "auth_tokens.json").write_text(
        json.dumps({"access_token": "oauth-token"}), encoding="utf-8",
    )
    monkeypatch.setenv("KILN_AUTH_HOME", str(tmp_path))
    monkeypatch.setenv("KILN_API_URL", "http://localhost:8742")
    monkeypatch.setenv("KILN_LICENSE_KEY", "license-token")
    captured = {}

    def fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        captured["headers"] = dict(req.headers)
        return _FakeUrlopenResponse()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    _pro_api_call("estimate_plate_part_costs")

    assert captured["url"] == "http://localhost:8742/api/tools/estimate_plate_part_costs"
    assert captured["headers"]["Authorization"] == "Bearer license-token"
