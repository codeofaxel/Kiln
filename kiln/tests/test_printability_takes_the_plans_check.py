"""Every printability door on an install gives the check the plan includes.

On an install without kiln-pro, signed in on a plan that includes Kiln Pro,
every door that judges a part through :func:`kiln.printability.analyze_printability`
(the warping check, the validation pipeline, the slice's brim, the
estimates) takes Kiln's servers' reading of the same part, asked once per
part through the served ``analyze_printability`` door.  A free plan is never
asked about and its answer is unchanged.  When the servers' reading does not
come, the check made here stands and the report says why, the verdict in
words once a session.

Driven through the real MCP dispatch; only the network is stood in for (the
served door ``_pro_api_call`` and httpx), answering with a reading the test
writes.
"""

from __future__ import annotations

import asyncio
import json
import sys

import pytest

import kiln.server as srv
from kiln import licensing
from tests._signed_in import sign_in

UPLOADED = "upload0000000000000000000000001"
#: Values no check on this computer gives the test part, so a door that
#: shows them took the servers' reading.
THEIR_RISK = "critical"
THEIR_BRIM = 9


class _Response:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self.content = b""
        self._body = body
        self.headers = {"content-type": "application/json"}

    def json(self):
        return self._body


class _Servers:
    """Kiln's servers as this install sees them: uploads and tool calls."""

    def __init__(self):
        self.uploads: list[str] = []
        self.calls: list[tuple[str, dict]] = []
        self.answer: dict = {}
        self.status = 200
        self.raises: Exception | None = None

    def post(self, url, **kwargs):
        self.uploads.append(url)
        return _Response(body={"status": "success", "file_token": UPLOADED, "format": "stl"})

    def get(self, url, **_kwargs):
        return _Response(status=404, body={"error": "not found"})

    def door(self, tool_name, _timeout=None, _asked_by_user=True, _http=None, **kwargs):
        self.calls.append((tool_name, kwargs))
        if self.raises is not None:
            raise self.raises
        if _http is not None:
            _http["status"] = self.status
        return json.loads(json.dumps(self.answer))


def _plan(monkeypatch, includes_pro: bool) -> None:
    monkeypatch.setattr(
        licensing, "check_tier",
        lambda *_a, **_k: (True, None) if includes_pro else (False, "needs Kiln Pro"),
    )


@pytest.fixture
def servers(monkeypatch, tmp_path):
    """A signed-in install with no kiln-pro, and Kiln's servers behind it."""
    import httpx

    from kiln import _pro_printability_bridge, served_public
    from kiln.mcp_compat import FastMCP

    srv._register_pro_tool_stubs(FastMCP("printability-check-test"))
    monkeypatch.setitem(sys.modules, "kiln_pro", None)
    monkeypatch.setenv("KILN_HOME", str(tmp_path / "kiln-home"))
    sign_in(monkeypatch, tmp_path)
    monkeypatch.setenv("KILN_API_URL", "https://kiln.test")
    monkeypatch.setattr(served_public, "_said", set())
    _pro_printability_bridge._reset_for_tests()
    fake = _Servers()
    monkeypatch.setattr(srv, "_pro_api_call", fake.door)
    monkeypatch.setattr(httpx, "post", fake.post)
    monkeypatch.setattr(httpx, "get", fake.get)
    yield fake
    _pro_printability_bridge._reset_for_tests()
    served_public._reset_for_tests()


@pytest.fixture
def flat_nylon(tmp_path):
    """A wide flat plate: in nylon its corners lift, so it needs a brim."""
    trimesh = pytest.importorskip("trimesh")
    plate = trimesh.creation.box(extents=(200, 200, 3))
    plate.apply_translation((0, 0, 1.5))
    path = tmp_path / "plate.stl"
    plate.export(path)
    return path


def _here(path) -> dict:
    """The report this computer makes alone, as a reply carries it."""
    from kiln.printability import _analyze_printability_here

    return _analyze_printability_here(str(path), material="pa").to_dict()


def _stricter_reading(path) -> dict:
    """The servers' answer for *path*: this computer's report with a
    different warping risk and brim, so a door that takes the servers'
    reading is told apart from one that does not."""
    report = _here(path)
    report["warping"]["risk_level"] = THEIR_RISK if report["warping"]["risk_level"] != THEIR_RISK else "moderate"
    report["adhesion"] = dict(report["adhesion"], brim_width_mm=THEIR_BRIM, slicer_overrides={"brim_width": str(THEIR_BRIM)})
    report["recommendations"] = [f"{THEIR_BRIM}mm brim: the corners of this part lift as it cools."]
    report["printable"] = True
    return {"success": True, "verdict": "ready", "report": report}


def _call(name: str, **arguments):
    raw = asyncio.run(srv.mcp._tool_manager.call_tool(name, arguments, context=None, convert_result=False))
    return raw if isinstance(raw, dict) else json.loads(raw)


def _asks(fake) -> list[dict]:
    return [kw for name, kw in fake.calls if name == "analyze_printability"]


class TestAPlanThatIncludesTheCheck:
    def test_the_warping_door_says_the_servers_risk_and_brim(self, servers, flat_nylon, monkeypatch):
        _plan(monkeypatch, True)
        servers.answer = _stricter_reading(flat_nylon)
        result = _call("analyze_warping_risk", file_path=str(flat_nylon), material="pa")
        assert result["risk_level"] == servers.answer["report"]["warping"]["risk_level"]
        assert result["adhesion"]["brim_width_mm"] == THEIR_BRIM
        assert "plan_check" not in result
        asked = _asks(servers)
        assert len(asked) == 1 and asked[0]["material"] == "pa"
        assert servers.uploads, "the part was not sent"

    def test_the_brim_a_slice_would_write_is_the_servers(self, servers, flat_nylon, monkeypatch):
        from kiln.printability import analyze_printability

        _plan(monkeypatch, True)
        servers.answer = _stricter_reading(flat_nylon)
        report = analyze_printability(str(flat_nylon), material="pa")
        assert report.adhesion.slicer_overrides == {"brim_width": str(THEIR_BRIM)}

    def test_the_same_part_is_sent_once(self, servers, flat_nylon, monkeypatch):
        _plan(monkeypatch, True)
        servers.answer = _stricter_reading(flat_nylon)
        _call("analyze_warping_risk", file_path=str(flat_nylon), material="pa")
        _call("validate_and_prepare", input_path=str(flat_nylon), material="pa")
        assert len(_asks(servers)) == 1

    def test_a_part_that_will_not_print_here_still_does_not(self, servers, tmp_path, monkeypatch):
        from kiln.printability import analyze_printability, verdict_block

        trimesh = pytest.importorskip("trimesh")
        ball = trimesh.creation.icosphere(subdivisions=3, radius=20)
        ball.apply_translation((0, 0, 20))
        path = tmp_path / "ball.stl"
        ball.export(path)
        _plan(monkeypatch, True)
        theirs = _here(path)
        theirs["printable"] = True
        servers.answer = {"success": True, "verdict": "ready", "report": theirs}
        report = analyze_printability(str(path), material="pa")
        assert verdict_block(report)["printability_verdict"] == "wont_print"
        assert report.printable is False


class TestEveryOtherPosture:
    def test_a_free_plan_is_never_asked_about_and_reads_as_before(self, servers, flat_nylon, monkeypatch):
        _plan(monkeypatch, False)
        before = _here(flat_nylon)
        result = _call("analyze_warping_risk", file_path=str(flat_nylon), material="pa")
        assert not _asks(servers)
        assert result["risk_level"] == before["warping"]["risk_level"]
        assert "plan_check" not in result
        assert "Kiln Pro" not in result["printability_verdict_text"]

    def test_kiln_pro_in_the_process_asks_nothing(self, servers, flat_nylon, monkeypatch):
        from kiln import _pro_printability_bridge

        _plan(monkeypatch, True)
        monkeypatch.setitem(sys.modules, "kiln_pro", object())
        assert _pro_printability_bridge.reading_or_miss(str(flat_nylon), {"material": "pa"}) == (None, None)
        assert not servers.calls

    def test_offline_keeps_the_check_made_here_and_says_why_once(self, servers, flat_nylon, monkeypatch):
        from kiln.printability import analyze_printability, verdict_block

        _plan(monkeypatch, True)
        before = _here(flat_nylon)
        servers.raises = OSError(51, "Network is unreachable")
        first = analyze_printability(str(flat_nylon), material="pa")
        assert first.warping.risk_level == before["warping"]["risk_level"]
        assert first.plan_check["applied"] is False and first.plan_check["why"] in ("offline", "unanswered")
        said = verdict_block(first)["printability_verdict_text"]
        assert "Kiln Pro's printability check" in said and "basic check" in said
        assert first.to_dict()["plan_check"]["why"] == first.plan_check["why"]
        # Said once a session; the reason rides on every later report.
        second = analyze_printability(str(flat_nylon), material="pa")
        assert "note" not in second.plan_check and second.plan_check["why"]
        assert "Kiln Pro" not in verdict_block(second)["printability_verdict_text"]

    def test_a_refusal_keeps_the_check_made_here(self, servers, flat_nylon, monkeypatch):
        from kiln.printability import analyze_printability

        _plan(monkeypatch, True)
        servers.status = 403
        servers.answer = {"success": False, "code": "FORBIDDEN", "error": "Not allowed."}
        report = analyze_printability(str(flat_nylon), material="pa")
        assert report.plan_check["applied"] is False
        assert report.plan_check["why"] in ("refused", "unanswered")
