"""``check_material_environment(requirements=...)`` on an install without
kiln-pro asks Kiln's servers whether the material meets the rules.

The rule record and its reasoning are kiln-pro's; a plain install sends the
material and the requirements named and relays the block it gets back.
These tests pin the door's side of that wire with the server stood in
(public tests cannot import kiln-pro):

* what is sent: the material and the requirements, nothing else;
* the server's block is relayed as it is, at whatever depth the account's
  tier earned, beside the environment report, which is unchanged;
* a request the server could not read is relayed as its own ruling;
* a call that got no answer (offline, signed out, the servers silent) is
  worded in the shared voice, says why, and judges no requirement met;
* with no requirements nothing is asked and the answer is the old one.
"""

from __future__ import annotations

import sys
import urllib.error
from typing import Any

import pytest

from kiln import _pro_material_rules_bridge as bridge


class _FakeMCP:
    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}

    def tool(self, *a, **k):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


@pytest.fixture(autouse=True)
def _plain_install(monkeypatch):
    """No kiln-pro on this machine, whatever the test runner has on its path."""
    real_import = bridge.importlib.import_module

    def no_pro(name, *a, **k):
        if name.startswith("kiln_pro"):
            raise ImportError(f"No module named {name!r}")
        return real_import(name, *a, **k)

    monkeypatch.setattr(bridge.importlib, "import_module", no_pro)
    monkeypatch.delitem(sys.modules, "kiln_pro", raising=False)


@pytest.fixture
def door():
    from kiln.plugins.design_tools import plugin

    mcp = _FakeMCP()
    plugin.register(mcp)
    return mcp.tools["check_material_environment"]


@pytest.fixture
def served(monkeypatch):
    import kiln.server as srv

    calls: list[tuple[str, dict]] = []
    answers: list[Any] = []

    def fake(tool_name, _timeout=None, _asked_by_user=True, **kwargs):
        calls.append((tool_name, kwargs))
        answer = answers.pop(0) if answers else {}
        if isinstance(answer, BaseException):
            raise answer
        return answer

    monkeypatch.setattr(srv, "_pro_api_call", fake)
    return calls, answers


BUSINESS_BLOCK = {
    "success": True, "material": "abs", "requirements": ["food_safe", "rohs"], "known": True,
    "warnings": ["Not food-safe"],
    "verdicts": [
        {"requirement": "food_safe", "label": "food contact", "verdict": "does_not_meet", "reason": "r1"},
        {"requirement": "rohs", "label": "RoHS", "verdict": "meets", "reason": "r2"},
    ],
    "all_met": False,
}
FREE_BLOCK = {
    "success": True, "material": "abs", "requirements": ["food_safe"], "known": True,
    "warnings": ["Not food-safe"], "verdicts_tier": "business",
    "not_judged": "Kiln has not judged food contact for abs.", "upgrade_url": "https://kiln3d.com/pricing",
}


class TestWhatIsSent:
    def test_the_material_and_the_requirements_only(self, door, served):
        calls, answers = served
        answers.append(BUSINESS_BLOCK)
        door(material="abs", environment="kitchen counter", requirements=["food_safe", "rohs"])
        assert calls == [("material_rule_check", {"material": "abs", "requirements": ["food_safe", "rohs"]})]

    def test_no_requirements_asks_nothing(self, door, served):
        calls, _ = served
        out = door(material="abs", environment="kitchen counter")
        assert calls == []
        assert out["success"] is True and "rule_checks" not in out


class TestWhatIsRelayed:
    @pytest.mark.parametrize("block", [BUSINESS_BLOCK, FREE_BLOCK])
    def test_the_servers_block_beside_the_environment_report(self, door, served, block):
        _, answers = served
        answers.append(block)
        out = door(material="abs", environment="outdoor sun", requirements=["food_safe"])
        assert out["success"] is True
        assert out["rule_checks"] == block
        assert "overall_verdict" in out or "per_category_ratings" in out

    def test_a_request_the_server_could_not_read_is_its_own_ruling(self, door, served):
        _, answers = served
        ruling = {"success": False, "code": "INVALID_INPUT", "error": "Unknown requirement 'cheap'."}
        answers.append(ruling)
        out = door(material="abs", environment="indoors", requirements=["cheap"])
        assert out["rule_checks"] == ruling


class TestAMissJudgesNothing:
    def _assert_unjudged(self, block: dict, cause: str) -> None:
        assert block["success"] is False and block["checked"] is False
        assert block["code"] == "RULE_CHECK_UNAVAILABLE"
        assert block["why"] == cause
        assert "verdicts" not in block and "all_met" not in block
        assert "won't call any of them met" in block["error"]
        assert "Kiln can't" in block["error"] and "right now because" in block["error"]

    def test_the_servers_silent(self, door, served):
        _, answers = served
        answers.append(ConnectionRefusedError("refused"))
        out = door(material="abs", environment="indoors", requirements=["rohs"])
        self._assert_unjudged(out["rule_checks"], "unanswered")
        assert out["rule_checks"]["retryable"] is True

    def test_offline(self, door, served):
        _, answers = served
        answers.append(urllib.error.URLError(OSError(101, "Network is unreachable")))
        out = door(material="abs", environment="indoors", requirements=["rohs"])
        self._assert_unjudged(out["rule_checks"], "offline")

    def test_signed_out(self, door, served):
        _, answers = served
        answers.append({"status": "error", "code": "KILN_ACCOUNT_NOT_PAIRED", "error": "Sign in."})
        out = door(material="abs", environment="indoors", requirements=["food_safe"])
        self._assert_unjudged(out["rule_checks"], "signed_out")

    def test_an_answer_with_no_block_in_it(self, door, served):
        _, answers = served
        answers.append({"success": True})
        out = door(material="abs", environment="indoors", requirements=["uv"])
        self._assert_unjudged(out["rule_checks"], "unanswered")
