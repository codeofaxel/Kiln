"""A subscriber on a plain install is on their plan.

Until this module existed, every gate in this package imported
``kiln.licensing``, which only kiln-pro supplied.  On a ``pip install
kiln3d`` the import failed and each gate fell back to Free on its own: a
paying Business customer who had signed in was refused ``fleet_status``,
capped at one printer at a time, and told to connect a machine that was
already connected.

Covers ``kiln.account_plan`` (the plan of the signed-in account, kept
current from ``GET /api/auth/whoami``) through the names the gates import
(``kiln.licensing``) and through a real gated tool.
"""

from __future__ import annotations

import json
import sys
import time
from unittest import mock

import pytest

from kiln import account_plan
from kiln.account_plan import LicenseTier


@pytest.fixture(autouse=True)
def plain_install(monkeypatch, tmp_path):
    """No kiln-pro, a private sign-in file, and no memory of earlier asks."""
    monkeypatch.setenv("KILN_AUTH_HOME", str(tmp_path))
    monkeypatch.delenv("KILN_LICENSE_KEY", raising=False)
    monkeypatch.setattr(account_plan, "_last_ask_at", 0.0)
    monkeypatch.setattr(account_plan, "_last_ask_answered", True)
    yield tmp_path


def _far_future_jwt() -> str:
    """A token whose clock reads valid, so no refresh is attempted."""
    import base64

    def b64(obj) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    return f"{b64({'alg': 'none'})}.{b64({'exp': int(time.time()) + 86400})}.sig"


def _sign_in(home, *, tier="free", checked_ago_s=0.0, **extra) -> None:
    kiln_dir = home / ".kiln"
    kiln_dir.mkdir(exist_ok=True)
    stamp = int(time.time() - checked_ago_s)
    (kiln_dir / "auth_tokens.json").write_text(
        json.dumps(
            {
                "access_token": _far_future_jwt(),
                "refresh_token": "refresh",
                "email": "maker@example.com",
                "tier": tier,
                "signed_in_at": stamp,
                **extra,
            }
        )
    )


def _saved(home) -> dict:
    return json.loads((home / ".kiln" / "auth_tokens.json").read_text())


def _account_says(tier: str | None, status: int = 200):
    """Patch the one network call: what the account answers about its plan."""
    resp = mock.MagicMock()
    resp.status_code = status
    resp.json.return_value = (
        {"success": True, "tier": tier, "has_entitlement": tier != "free"}
        if tier
        else {"success": False}
    )
    return mock.patch("requests.get", return_value=resp)


def _account_unreachable():
    return mock.patch("requests.get", side_effect=OSError("offline"))


class TestThePlan:
    def test_signed_out_is_free_and_asks_nobody(self, plain_install):
        with _account_says("business") as get:
            assert account_plan.get_tier() is LicenseTier.FREE
        get.assert_not_called()

    def test_a_fresh_saved_plan_is_used_without_the_network(self, plain_install):
        _sign_in(plain_install, tier="business")
        with _account_says("free") as get:
            assert account_plan.get_tier() is LicenseTier.BUSINESS
        get.assert_not_called()

    def test_an_old_paid_plan_is_asked_about_and_a_plan_that_ended_stops(
        self, plain_install
    ):
        _sign_in(plain_install, tier="pro", checked_ago_s=25 * 3600)
        with _account_says("free"):
            assert account_plan.get_tier() is LicenseTier.FREE
        assert _saved(plain_install)["tier"] == "free"

    def test_an_upgrade_is_seen_without_signing_in_again(self, plain_install):
        _sign_in(plain_install, tier="free", checked_ago_s=31 * 60)
        with _account_says("pro"):
            assert account_plan.get_tier() is LicenseTier.PRO
        saved = _saved(plain_install)
        assert saved["tier"] == "pro"
        assert saved["plan_checked_at"] >= int(time.time()) - 5

    def test_offline_keeps_the_last_answer_within_the_grace(self, plain_install):
        _sign_in(plain_install, tier="business", checked_ago_s=3 * 24 * 3600)
        with _account_unreachable():
            assert account_plan.get_tier() is LicenseTier.BUSINESS

    def test_offline_past_the_grace_a_paid_plan_reads_free(self, plain_install):
        _sign_in(plain_install, tier="business", checked_ago_s=8 * 24 * 3600)
        with _account_unreachable():
            assert account_plan.get_tier() is LicenseTier.FREE

    def test_a_sign_in_with_no_stamp_is_asked_about_and_not_expired(
        self, plain_install
    ):
        """Its age is unknown: the account is asked, and with no answer the
        saved plan stands."""
        _sign_in(plain_install, tier="pro")
        saved = _saved(plain_install)
        del saved["signed_in_at"]
        (plain_install / ".kiln" / "auth_tokens.json").write_text(json.dumps(saved))
        with _account_unreachable() as get:
            assert account_plan.get_tier() is LicenseTier.PRO
        assert get.call_count == 1

    def test_offline_never_grants_a_plan(self, plain_install):
        _sign_in(plain_install, tier="free", checked_ago_s=31 * 60)
        with _account_unreachable():
            assert account_plan.get_tier() is LicenseTier.FREE

    def test_an_unanswered_ask_is_not_repeated_on_every_gate(self, plain_install):
        _sign_in(plain_install, tier="free", checked_ago_s=31 * 60)
        with _account_unreachable() as get:
            for _ in range(5):
                account_plan.get_tier()
        assert get.call_count == 1

    def test_a_server_that_cannot_read_the_plan_changes_nothing(self, plain_install):
        _sign_in(plain_install, tier="pro", checked_ago_s=25 * 3600)
        with _account_says(None, status=503):
            assert account_plan.get_tier() is LicenseTier.PRO
        assert _saved(plain_install)["tier"] == "pro"

    def test_a_refused_sign_in_grants_no_paid_plan(self, plain_install):
        _sign_in(
            plain_install,
            tier="business",
            refresh_token="",
            refresh_rejected_at=int(time.time()),
        )
        with _account_says("business") as get:
            assert account_plan.get_tier() is LicenseTier.FREE
        get.assert_not_called()

    def test_a_plan_this_release_does_not_know_is_free(self, plain_install):
        _sign_in(plain_install, tier="platinum")
        assert account_plan.get_tier() is LicenseTier.FREE


class TestTheCaps:
    @pytest.mark.parametrize(
        ("tier", "cap"),
        [("free", 1), ("pro", 1), ("business", 50), ("enterprise", None), ("???", 1)],
    )
    def test_printers_at_once(self, tier, cap):
        assert account_plan.max_printers_for_tier(tier) == cap

    def test_the_ladder_orders_the_plans(self):
        assert (
            LicenseTier.FREE
            < LicenseTier.PRO
            < LicenseTier.BUSINESS
            < LicenseTier.ENTERPRISE
        )
        assert LicenseTier.ENTERPRISE >= LicenseTier.BUSINESS


class TestARefusalSaysWhatIsTrue:
    def _gated(self):
        @account_plan.requires_tier(LicenseTier.BUSINESS)
        def fleet_status() -> dict:
            return {"success": True}

        return fleet_status

    def test_signed_out_points_a_subscriber_at_sign_in(self, plain_install):
        out = self._gated()()
        assert out["success"] is False and out["code"] == "TIER_REQUIRED"
        assert out["required_tier"] == "business" and out["tool"] == "fleet_status"
        assert "Already subscribed?" in out["error"]
        assert out["setup_hint"] == "kiln signin"
        assert out["upgrade_url"] == (
            "https://kiln3d.com/pricing?src=agent&tool=fleet_status"
        )

    def test_someone_who_just_upgraded_is_not_refused(self, plain_install):
        """The saved plan says Free; the account, asked before refusing,
        says Business."""
        _sign_in(plain_install, tier="free")
        with _account_says("business") as get:
            assert self._gated()() == {"success": True}
        assert get.call_count == 1
        assert _saved(plain_install)["tier"] == "business"

    def test_signed_in_below_the_plan_names_the_account_and_no_sign_in(
        self, plain_install
    ):
        _sign_in(plain_install, tier="pro")
        with _account_says("pro"):
            out = self._gated()()
        assert out["code"] == "TIER_REQUIRED"
        assert out["current_tier"] == "pro"
        assert "signed in as maker@example.com, on the Pro plan" in out["error"]
        assert "Already subscribed?" not in out["error"]
        # Sending a signed-in person round a sign-in changes nothing.
        assert "setup_hint" not in out and "agent_hint" not in out
        assert out["upgrade_url"].endswith("tool=fleet_status")

    def test_a_refused_sign_in_says_sign_back_in_and_never_upgrade(
        self, plain_install
    ):
        _sign_in(
            plain_install,
            tier="business",
            refresh_token="",
            refresh_rejected_at=int(time.time()),
        )
        out = self._gated()()
        assert out["code"] == "SIGN_IN_AGAIN"
        assert "upgrade_url" not in out
        assert "pricing" not in out["error"]
        assert out["setup_hint"] == "kiln signin"

    def test_an_unconfirmed_paid_plan_is_told_to_get_online(self, plain_install):
        _sign_in(plain_install, tier="business", checked_ago_s=8 * 24 * 3600)
        with _account_unreachable():
            out = self._gated()()
        assert out["code"] == "PLAN_UNCONFIRMED" and out["retryable"] is True
        assert "upgrade_url" not in out
        assert "your Business plan" in out["error"]

    def test_the_recheck_before_a_refusal_is_rate_limited(self, plain_install):
        _sign_in(plain_install, tier="free")
        gated = self._gated()
        with _account_says("free") as get:
            for _ in range(4):
                assert gated()["code"] == "TIER_REQUIRED"
        assert get.call_count == 1

    def test_check_tier_gives_the_same_verdict(self, plain_install):
        ok, message = account_plan.check_tier(LicenseTier.PRO)
        assert ok is False and "needs Kiln Pro" in message
        _sign_in(plain_install, tier="pro")
        assert account_plan.check_tier(LicenseTier.PRO) == (True, None)


class TestEveryGateReadsIt:
    """The names gates import resolve to this module on a plain install."""

    @pytest.fixture
    def licensing(self, monkeypatch):
        # A plain install: kiln-pro cannot be imported.
        monkeypatch.setitem(sys.modules, "kiln_pro", None)
        monkeypatch.setitem(sys.modules, "kiln_pro.enterprise", None)
        monkeypatch.delitem(sys.modules, "kiln.licensing", raising=False)
        import importlib

        module = importlib.import_module("kiln.licensing")
        yield module
        sys.modules.pop("kiln.licensing", None)

    def test_licensing_is_the_account_plan(self, licensing, plain_install):
        assert licensing.get_tier is account_plan.get_tier
        assert licensing.requires_tier is account_plan.requires_tier
        assert licensing.LicenseTier is LicenseTier
        # Names only kiln-pro defines still fail the way callers expect.
        with pytest.raises(ImportError):
            from kiln.licensing import get_license_manager  # noqa: F401

    def test_a_signed_in_business_plan_runs_more_than_one_printer(
        self, licensing, plain_install
    ):
        """What the print gate, the engagement rule and the inventory cap
        all compute."""
        assert licensing.max_printers_for_tier(licensing.get_tier()) == 1
        _sign_in(plain_install, tier="business")
        assert licensing.max_printers_for_tier(licensing.get_tier()) == 50

        from kiln.printers.engagement import _multi_machine_tier

        assert _multi_machine_tier() is True

    def test_always_allow_follows_the_same_plan(self, licensing, plain_install):
        from kiln.consent_windows import always_allow_is_yours

        assert always_allow_is_yours() is False
        _sign_in(plain_install, tier="pro")
        assert always_allow_is_yours() is True

    def test_the_queue_cap_lifts_on_pro(self, licensing, plain_install):
        from kiln.plugins.queue_tools import _is_free_tier

        assert _is_free_tier() is True
        _sign_in(plain_install, tier="pro")
        assert _is_free_tier() is False


class TestWithKilnPro:
    def test_licensing_is_kiln_pros_own_module(self, monkeypatch):
        """Installed kiln-pro keeps deciding, exactly as when it registered
        the name itself."""
        import importlib
        import types

        pro = types.ModuleType("kiln_pro")
        pro.__path__ = []  # a package
        enterprise = types.ModuleType("kiln_pro.enterprise")
        enterprise.__path__ = []
        pro_licensing = types.ModuleType("kiln_pro.enterprise.licensing")
        pro_licensing.get_tier = lambda: "from kiln-pro"
        enterprise.licensing = pro_licensing
        pro.enterprise = enterprise
        monkeypatch.setitem(sys.modules, "kiln_pro", pro)
        monkeypatch.setitem(sys.modules, "kiln_pro.enterprise", enterprise)
        monkeypatch.setitem(sys.modules, "kiln_pro.enterprise.licensing", pro_licensing)
        monkeypatch.delitem(sys.modules, "kiln.licensing", raising=False)
        try:
            module = importlib.import_module("kiln.licensing")
            assert module is pro_licensing
            from kiln.licensing import get_tier

            assert get_tier() == "from kiln-pro"
        finally:
            sys.modules.pop("kiln.licensing", None)


class TestThePlanCommands:
    """``kiln upgrade``, ``kiln license-info`` and ``kiln register`` on an
    install without kiln-pro.  They used to end in a traceback there: the
    licence manager they asked for is kiln-pro's."""

    @pytest.fixture
    def cli(self, monkeypatch, plain_install):
        from click.testing import CliRunner

        from kiln.cli import main as cli_main

        monkeypatch.setattr(cli_main, "_license_manager_or_none", lambda: None)
        runner = CliRunner()
        return lambda *args: runner.invoke(cli_main.cli, list(args))

    def test_signed_out_shows_free_and_both_ways_forward(self, cli):
        for command in ("license-info", "upgrade"):
            result = cli(command)
            assert result.exit_code == 0, result.output
            assert "Plan:    Free" in result.output
            assert "kiln signin" in result.output
            assert "https://kiln3d.com/pricing?src=cli" in result.output

    def test_a_subscriber_sees_their_plan(self, cli, plain_install):
        _sign_in(plain_install, tier="free")
        with _account_says("business"):
            result = cli("license-info", "--json")
        assert result.exit_code == 0, result.output
        data = json.loads(result.output[result.output.index("{"):])
        assert data["tier"] == "business" and data["signed_in"] is True
        assert data["email"] == "maker@example.com"

    def test_a_key_is_answered_with_how_a_plan_applies_here(self, cli):
        result = cli("upgrade", "--key", "kiln_pro_abc")
        assert result.exit_code == 1
        assert "SIGN_IN_INSTEAD" in result.output and "kiln signin" in result.output

    def test_register_points_at_sign_in(self, cli):
        result = cli("register")
        assert result.exit_code == 1 and "kiln signin" in result.output
