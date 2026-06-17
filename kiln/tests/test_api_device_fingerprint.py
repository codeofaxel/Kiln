"""Coverage gate: public-Kiln license-bearer clients name their device.

When this install calls the hosted API with a *license-key* bearer, the
server's per-license device-activation cap counts the machine by the
``X-Kiln-Device-Fingerprint`` header.  Once the server enforces the cap, a
license-bearer request that omits the header is rejected with a 401 — so the
two public clients that can carry a license key must send it:

  * ``kiln.server._pro_api_call``  → POST /api/tools/<name>  (metered tools)
  * ``kiln.terms._server_request`` → /api/terms/*

(``usage_ledger`` is paired-token/JWT only → /api/me/stats/record is not
cap-gated, so it is intentionally not covered here.)

The public bearer shape is generic (``Bearer {bearer}`` for both license keys
and JWTs), so a blanket regex audit would over-match the JWT-only clients;
the in-scope set is therefore enumerated explicitly and asserted behaviourally.
"""

from __future__ import annotations

import pathlib

import pytest

from kiln.api_device import (
    DEVICE_FINGERPRINT_HEADER,
    device_fingerprint,
    device_fingerprint_headers,
)

_FP = "kiln-device-pubpin"


@pytest.fixture(autouse=True)
def _pin_fingerprint(monkeypatch):
    monkeypatch.setenv("KILN_DEVICE_FINGERPRINT", _FP)


class _FakeResp:
    """Minimal urlopen context-manager stand-in."""

    def __init__(self, body: bytes = b"{}"):
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _header_ci(req, name: str) -> str | None:
    # urllib normalises header case; look it up case-insensitively.
    for key, value in req.header_items():
        if key.lower() == name.lower():
            return value
    return None


# --- the mirror helper --------------------------------------------------------


def test_env_override_and_headers():
    assert device_fingerprint() == _FP
    assert device_fingerprint_headers() == {DEVICE_FINGERPRINT_HEADER: _FP}


def test_persisted_value_is_stable(tmp_path, monkeypatch):
    monkeypatch.delenv("KILN_DEVICE_FINGERPRINT", raising=False)
    monkeypatch.setattr(
        "kiln.api_device._fingerprint_path",
        lambda: tmp_path / ".kiln" / "device_fingerprint",
    )
    first = device_fingerprint()
    assert first.startswith("kiln-device-")
    assert device_fingerprint() == first


# --- behavioural: the two in-scope public clients -----------------------------


def test_pro_api_call_sends_fingerprint(monkeypatch):
    monkeypatch.setenv("KILN_LICENSE_KEY", "lk")
    monkeypatch.setenv("KILN_API_URL", "https://api.test")
    from kiln import server

    seen: list = []

    def _fake_urlopen(req, timeout=None):
        seen.append(req)
        return _FakeResp(b"{}")

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
    server._pro_api_call("generate_coaster", size_mm=80)
    assert seen, "no request issued"
    assert _header_ci(seen[0], DEVICE_FINGERPRINT_HEADER) == _FP


def test_terms_server_request_sends_fingerprint(monkeypatch):
    monkeypatch.setenv("KILN_API_URL", "https://api.test")
    from kiln import terms

    seen: list = []

    def _fake_urlopen(req, timeout=None):
        seen.append(req)
        return _FakeResp(b"{}")

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
    terms._server_request("/api/terms/acceptance", "GET", "lk")
    assert seen, "no request issued"
    assert _header_ci(seen[0], DEVICE_FINGERPRINT_HEADER) == _FP


# --- regression guard: the in-scope files keep wiring the helper --------------


def test_in_scope_files_wire_the_helper():
    src = pathlib.Path(__file__).resolve().parents[1] / "src" / "kiln"
    for name in ("server.py", "terms.py"):
        text = (src / name).read_text(encoding="utf-8")
        assert "device_fingerprint_headers" in text, (
            f"{name} carries a license-key bearer to the Kiln API but no longer "
            "wires kiln.api_device.device_fingerprint_headers"
        )
