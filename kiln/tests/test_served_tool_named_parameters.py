"""A served tool may have a parameter of its own called ``tool_name``.

The forwarders take the served tool's name as their first parameter, also
called ``tool_name``, and a stub hands them the call's own arguments as
keywords.  So a tool whose own parameter has that name raised ``TypeError:
got multiple values for argument 'tool_name'`` on every plain install,
before anything was sent: ``classify_calibration_confidence`` and
``import_third_party_calibration`` name the slicer by it (found 2026-10-17).
"""

from __future__ import annotations

import json


class _FakeResp:
    def __init__(self, body: bytes = b"{}"):
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _capture(monkeypatch) -> list:
    seen: list = []

    def _fake_urlopen(req, timeout=None):
        seen.append(req)
        return _FakeResp(b'{"status": "ok"}')

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
    return seen


def test_a_parameter_called_tool_name_travels_in_the_body(monkeypatch):
    monkeypatch.setenv("KILN_LICENSE_KEY", "lk")
    monkeypatch.setenv("KILN_API_URL", "https://api.test")
    from kiln import server

    seen = _capture(monkeypatch)
    server._pro_api_call("classify_calibration_confidence", tool_name="orcaslicer")
    assert seen, "no request issued"
    assert seen[0].full_url == "https://api.test/api/tools/classify_calibration_confidence"
    assert json.loads(seen[0].data) == {"tool_name": "orcaslicer"}


def test_the_anonymous_forwarder_takes_one_too(monkeypatch):
    monkeypatch.setenv("KILN_API_URL", "https://api.test")
    from kiln import server

    seen = _capture(monkeypatch)
    server._anonymous_api_call("report_issue", tool_name="orcaslicer")
    assert seen, "no request issued"
    assert json.loads(seen[0].data)["tool_name"] == "orcaslicer"
