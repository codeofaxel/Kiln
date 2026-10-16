"""Every refusal of a paid feature tells the agent not to rebuild it.

The session-start instructions carry the tiers-and-terms block, but a refusal
can land tens of thousands of tokens later, which is exactly when an agent
decides whether to build the feature out of free tools instead.  So the
refusal restates the line (``kiln.tiers_and_terms.SUBSTITUTION_LINE``) in its
``agent_guidance`` field.

Until 2026-10-15 the two refusals a plain install meets most carried nothing
of the kind: the sign-in wall in front of every paid tool, and the public
tier gate.  The refusals that did carry it were ones only a kiln-pro install
or the hosted connector ever sees.  The sweep at the bottom is what keeps a
new refusal from being written the old way.
"""

from __future__ import annotations

import ast
import json
import time
from pathlib import Path

import pytest

from kiln import account_plan, tiers_and_terms
from kiln.account_plan import LicenseTier
from kiln.tiers_and_terms import (
    PAYWALL_CODES,
    SUBSTITUTION_LINE,
    is_paywall_refusal,
    paywall_fields,
    with_paywall_guidance,
)

SRC = Path(tiers_and_terms.__file__).resolve().parent


@pytest.fixture
def plain_install(monkeypatch, tmp_path):
    """No kiln-pro, a private sign-in file, and no memory of earlier asks."""
    monkeypatch.setenv("KILN_AUTH_HOME", str(tmp_path))
    monkeypatch.delenv("KILN_LICENSE_KEY", raising=False)
    monkeypatch.setattr(account_plan, "_last_ask_at", 0.0)
    monkeypatch.setattr(account_plan, "_last_ask_answered", True)
    return tmp_path


def _sign_in(home: Path, **fields) -> None:
    import base64

    def b64(obj) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    token = f"{b64({'alg': 'none'})}.{b64({'exp': int(time.time()) + 86400})}.sig"
    (home / ".kiln").mkdir(exist_ok=True)
    (home / ".kiln" / "auth_tokens.json").write_text(json.dumps({
        "access_token": token,
        "refresh_token": "refresh",
        "email": "maker@example.com",
        "signed_in_at": int(time.time()),
        **fields,
    }))


# ---------------------------------------------------------------------------
# The walls a plain install meets
# ---------------------------------------------------------------------------


class TestTheSignInWallInFrontOfAPaidTool:
    """``pip install kiln3d``, never signed in, an agent reaches a paid tool."""

    def _call(self, monkeypatch, tool: str, tier: str) -> dict:
        import kiln.server as srv

        monkeypatch.setitem(srv._PRO_TOOL_TIERS, tool, tier)
        return srv._pro_api_call(tool)

    def test_a_paid_tool_carries_the_line(self, plain_install, monkeypatch):
        out = self._call(monkeypatch, "kilntest_paid_tool", "business")
        assert out["code"] == "KILN_ACCOUNT_NOT_PAIRED"
        assert out["agent_guidance"] == SUBSTITUTION_LINE

    def test_a_free_tool_is_only_a_sign_in(self, plain_install, monkeypatch):
        import kiln.server as srv

        monkeypatch.delitem(srv._PRO_TOOL_TIERS, "kilntest_free_tool", raising=False)
        out = srv._pro_api_call("kilntest_free_tool")
        assert out["code"] == "KILN_ACCOUNT_NOT_PAIRED"
        assert "agent_guidance" not in out


class TestThePublicTierGate:
    """A public tool behind a tier, refused on a plain install."""

    def _gated(self):
        @account_plan.requires_tier(LicenseTier.BUSINESS)
        def fleet_status() -> dict:
            return {"success": True}

        return fleet_status

    def test_signed_out(self, plain_install):
        out = self._gated()()
        assert out["code"] == "TIER_REQUIRED"
        assert out["agent_guidance"] == SUBSTITUTION_LINE

    def test_signed_in_below_the_plan(self, plain_install, monkeypatch):
        from unittest import mock

        _sign_in(plain_install, tier="pro")
        resp = mock.MagicMock(status_code=200)
        resp.json.return_value = {"success": True, "tier": "pro", "has_entitlement": True}
        with mock.patch("requests.get", return_value=resp):
            out = self._gated()()
        assert out["code"] == "TIER_REQUIRED" and out["current_tier"] == "pro"
        assert out["agent_guidance"] == SUBSTITUTION_LINE

    def test_a_lapsed_sign_in_is_never_pitched_an_upgrade(self, plain_install):
        """Its owner may already pay for the tool: sign back in, nothing else."""
        _sign_in(
            plain_install,
            tier="business",
            refresh_token="",
            refresh_rejected_at=int(time.time()),
        )
        out = self._gated()()
        assert out["code"] == "SIGN_IN_AGAIN"
        assert "agent_guidance" not in out


# ---------------------------------------------------------------------------
# The shared definition
# ---------------------------------------------------------------------------


class TestWhatCountsAsAPaywall:
    @pytest.mark.parametrize("code", sorted(PAYWALL_CODES))
    def test_every_paywall_code(self, code):
        assert is_paywall_refusal({"success": False, "code": code})

    @pytest.mark.parametrize(
        "answer",
        [
            {"success": False, "code": "KILN_ACCOUNT_NOT_PAIRED", "required_tier": "free"},
            {"success": False, "code": "KILN_ACCOUNT_NOT_PAIRED"},
            # A lapsed sign-in names the plan it is part of, and is not a wall.
            {"status": "error", "code": "KILN_SESSION_EXPIRED", "required_tier": "pro"},
            {"success": False, "code": "SIGN_IN_AGAIN", "required_tier": "business"},
            {"success": False, "code": "PLAN_UNCONFIRMED"},
            {"success": False, "code": "INVALID_ARGUMENT"},
            {"success": True, "result": 1},
            "not a dict",
        ],
    )
    def test_everything_else_is_left_alone(self, answer):
        assert not is_paywall_refusal(answer)
        assert with_paywall_guidance(answer) == answer

    def test_the_wall_in_front_of_a_paid_tool_counts(self):
        assert is_paywall_refusal(
            {"status": "error", "code": "KILN_ACCOUNT_NOT_PAIRED", "required_tier": "pro"}
        )

    def test_a_refusal_built_elsewhere_gains_the_line(self):
        answer = {"success": False, "code": "BUSINESS_REQUIRED", "error": "x"}
        out = with_paywall_guidance(answer)
        assert out["agent_guidance"] == SUBSTITUTION_LINE
        assert "agent_guidance" not in answer  # a copy, never a mutation

    def test_guidance_already_there_is_kept(self):
        answer = {"success": False, "code": "TIER_REQUIRED", "agent_guidance": "own"}
        assert with_paywall_guidance(answer)["agent_guidance"] == "own"

    def test_the_free_tier_gets_no_field(self):
        assert paywall_fields("free") == {} and paywall_fields("") == {}
        assert paywall_fields("Pro") == {"agent_guidance": SUBSTITUTION_LINE}


# ---------------------------------------------------------------------------
# The sweep: a paywall refusal written anywhere in this package
# ---------------------------------------------------------------------------


def _carries_the_line(node: ast.AST) -> bool:
    """The refusal names agent_guidance itself or splats paywall_fields()."""
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant) and sub.value == "agent_guidance":
            return True
        if isinstance(sub, ast.Call):
            fn = sub.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
            if name in ("paywall_fields", "with_paywall_guidance"):
                return True
    return False


def _paywall_refusals_without_the_line(root: Path) -> list[str]:
    """Every literal refusal under *root* whose code is a paywall code and
    that carries no line: a dict with that ``"code"``, or a call passing it
    as ``code=`` (``_error_dict(..., code="PRO_REQUIRED", extra=...)``)."""
    found: list[str] = []
    for path in sorted(root.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            code = None
            if isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values, strict=True):
                    if isinstance(key, ast.Constant) and key.value == "code":
                        code = value
            elif isinstance(node, ast.Call):
                code = next((kw.value for kw in node.keywords if kw.arg == "code"), None)
            if (
                isinstance(code, ast.Constant)
                and code.value in PAYWALL_CODES
                and not _carries_the_line(node)
            ):
                found.append(f"{path.relative_to(root)}:{node.lineno} {code.value}")
    return found


def test_every_paywall_refusal_in_the_package_carries_the_line():
    missing = _paywall_refusals_without_the_line(SRC)
    assert not missing, (
        "A refusal of a paid feature is built without the line telling the "
        "agent not to rebuild it. Add **paywall_fields(<tier>) to it:\n  "
        + "\n  ".join(missing)
    )


def test_the_sweep_can_fail(tmp_path):
    """A refusal written the old way is found, in both shapes."""
    (tmp_path / "door.py").write_text(
        "def a():\n"
        "    return {'success': False, 'code': 'PRO_REQUIRED'}\n"
        "def b():\n"
        "    return _error_dict('x', code='TIER_REQUIRED', extra={})\n"
        "def c():\n"
        "    return {'code': 'PRO_REQUIRED', **paywall_fields('pro')}\n"
        "def d():\n"
        "    return {'code': 'INVALID_ARGUMENT'}\n"
    )
    assert _paywall_refusals_without_the_line(tmp_path) == [
        "door.py:2 PRO_REQUIRED",
        "door.py:4 TIER_REQUIRED",
    ]
