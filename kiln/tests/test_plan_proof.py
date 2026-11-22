"""A paid plan counts on this computer only as Kiln's servers stated it.

The sign-in file sits on the person's own disk, so its plan can be edited
and a server at ``KILN_API_URL`` can be anyone.  These tests go through the
names the gates use (``account_plan.get_tier``, ``check_tier`` and a
``requires_tier`` tool) with a throwaway signing key standing in for the
servers' own.
"""

from __future__ import annotations

import base64
import json
import time
from unittest import mock

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from kiln import account_plan, plan_proof
from kiln.account_plan import LicenseTier

ACCOUNT = "acct-1111"
OTHER_ACCOUNT = "acct-2222"
MACHINE = "machine-aaaa"
WEEK = 7 * 24 * 3600


def _new_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


def _public_b64(key: Ed25519PrivateKey) -> str:
    raw = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return base64.b64encode(raw).decode("ascii")


SERVER_KEY = _new_key()
STRANGER_KEY = _new_key()


def _proof(
    *,
    key: Ed25519PrivateKey = SERVER_KEY,
    account: str = ACCOUNT,
    tier: str = "business",
    issued_at: float | None = None,
    lifetime: int = WEEK,
    machine: str = "",
) -> dict:
    issued = int(time.time() if issued_at is None else issued_at)
    expires = issued + lifetime
    message = "\n".join(
        ("kiln-plan-proof-v1", account, tier, str(issued), str(expires), machine)
    ).encode("utf-8")
    return {
        "v": 1,
        "account": account,
        "tier": tier,
        "issued_at": issued,
        "expires_at": expires,
        "machine": machine,
        "sig": base64.b64encode(key.sign(message)).decode("ascii"),
    }


def _jwt(sub: str = ACCOUNT) -> str:
    def b64(obj) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    claims = {"exp": int(time.time()) + 86400, "sub": sub}
    return f"{b64({'alg': 'none'})}.{b64(claims)}.sig"


@pytest.fixture(autouse=True)
def plain_install(monkeypatch, tmp_path):
    """A private sign-in file, the throwaway key as the servers' key, no
    memory of earlier asks, and the no-proof window already closed."""
    monkeypatch.setenv("KILN_AUTH_HOME", str(tmp_path))
    monkeypatch.delenv("KILN_LICENSE_KEY", raising=False)
    monkeypatch.delenv("KILN_API_URL", raising=False)
    monkeypatch.setattr(account_plan, "_last_ask_at", 0.0)
    monkeypatch.setattr(account_plan, "_last_ask_answered", True)
    monkeypatch.setattr(plan_proof, "PUBLIC_KEYS", (_public_b64(SERVER_KEY),))
    monkeypatch.setattr(plan_proof, "UNPROVEN_PLAN_HONOURED_UNTIL", 0.0)
    yield tmp_path


def _path(home):
    return home / ".kiln" / "auth_tokens.json"


def _sign_in(home, *, tier="business", checked_ago_s=0.0, sub=ACCOUNT, **extra) -> None:
    (home / ".kiln").mkdir(exist_ok=True)
    stamp = int(time.time() - checked_ago_s)
    _path(home).write_text(
        json.dumps(
            {
                "access_token": _jwt(sub),
                "refresh_token": "refresh",
                "email": "maker@example.com",
                "tier": tier,
                "signed_in_at": stamp,
                **extra,
            }
        )
    )


def _saved(home) -> dict:
    return json.loads(_path(home).read_text())


def _account_answers(body: dict):
    resp = mock.MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"success": True, **body}
    return mock.patch("requests.get", return_value=resp)


def _account_unreachable():
    return mock.patch("requests.get", side_effect=OSError("offline"))


def _gated():
    @account_plan.requires_tier(LicenseTier.BUSINESS)
    def fleet_status() -> dict:
        return {"success": True}

    return fleet_status


def _is(tier: LicenseTier) -> None:
    """Every gate name gives the same verdict."""
    assert account_plan.get_tier() is tier
    covers = tier in (LicenseTier.BUSINESS, LicenseTier.ENTERPRISE)
    assert account_plan.check_tier(LicenseTier.BUSINESS)[0] is covers
    assert (_gated()() == {"success": True}) is covers


class TestAnEditedFile:
    def test_free_edited_to_business_reads_free(self, plain_install):
        future = int(time.time()) + 365 * 24 * 3600
        _sign_in(plain_install, tier="business", plan_checked_at=future)
        with _account_unreachable():
            _is(LicenseTier.FREE)

    def test_a_proof_for_a_lower_plan_does_not_carry_an_edited_plan(self, plain_install):
        future = int(time.time()) + 365 * 24 * 3600
        _sign_in(
            plain_install,
            tier="business",
            plan_checked_at=future,
            plan_proof=_proof(tier="pro", issued_at=time.time() - 3 * 24 * 3600),
        )
        with _account_unreachable():
            _is(LicenseTier.FREE)

    def test_a_record_signed_for_another_account_reads_free(self, plain_install):
        _sign_in(plain_install, plan_proof=_proof(account=OTHER_ACCOUNT))
        with _account_unreachable():
            _is(LicenseTier.FREE)

    def test_the_account_comes_from_the_token_before_the_saved_uid(self, plain_install):
        """A saved ``auth_uid`` edited to match a borrowed proof changes
        nothing while the token names the real account."""
        _sign_in(
            plain_install,
            auth_uid=OTHER_ACCOUNT,
            plan_proof=_proof(account=OTHER_ACCOUNT),
        )
        with _account_unreachable():
            _is(LicenseTier.FREE)


class TestAProvenPlan:
    def test_it_works_offline_within_its_window(self, plain_install):
        _sign_in(
            plain_install,
            checked_ago_s=3 * 24 * 3600,
            plan_proof=_proof(issued_at=time.time() - 3 * 24 * 3600),
        )
        with _account_unreachable():
            _is(LicenseTier.BUSINESS)

    def test_a_fresh_proof_needs_no_network(self, plain_install):
        _sign_in(plain_install, plan_proof=_proof())
        with _account_answers({"tier": "free"}) as get:
            _is(LicenseTier.BUSINESS)
        get.assert_not_called()

    def test_it_reads_free_after_it_runs_out(self, plain_install):
        _sign_in(
            plain_install,
            checked_ago_s=8 * 24 * 3600,
            plan_proof=_proof(issued_at=time.time() - 8 * 24 * 3600),
        )
        with _account_unreachable():
            _is(LicenseTier.FREE)

    def test_it_reads_free_after_expiry_even_with_a_recent_stamp(self, plain_install):
        """The proof's window holds, whatever the file's own stamp says."""
        _sign_in(
            plain_install,
            plan_checked_at=int(time.time()),
            plan_proof=_proof(issued_at=time.time() - 2 * 3600, lifetime=3600),
        )
        with _account_unreachable():
            _is(LicenseTier.FREE)


class TestASignInWithNoProof:
    def test_online_it_is_asked_about_and_the_proof_is_saved(
        self, plain_install, monkeypatch
    ):
        monkeypatch.setattr(
            plan_proof, "UNPROVEN_PLAN_HONOURED_UNTIL", time.time() + 30 * 24 * 3600
        )
        _sign_in(plain_install, checked_ago_s=31 * 60)
        proof = _proof()
        with _account_answers({"tier": "business", "plan_proof": proof}) as get:
            assert account_plan.get_tier() is LicenseTier.BUSINESS
        assert get.call_count == 1
        assert _saved(plain_install)["plan_proof"] == proof

    def test_after_the_cutoff_an_answer_with_a_proof_restores_the_plan(
        self, plain_install
    ):
        _sign_in(plain_install, checked_ago_s=31 * 60)
        with _account_answers({"tier": "business", "plan_proof": _proof()}):
            assert account_plan.get_tier() is LicenseTier.BUSINESS

    def test_offline_before_the_cutoff_it_is_still_honoured(
        self, plain_install, monkeypatch
    ):
        monkeypatch.setattr(
            plan_proof, "UNPROVEN_PLAN_HONOURED_UNTIL", time.time() + 30 * 24 * 3600
        )
        _sign_in(plain_install, checked_ago_s=31 * 60)
        with _account_unreachable() as get:
            _is(LicenseTier.BUSINESS)
        assert get.call_count == 1

    def test_offline_after_the_cutoff_it_reads_free(self, plain_install):
        _sign_in(plain_install, checked_ago_s=31 * 60)
        with _account_unreachable():
            _is(LicenseTier.FREE)


class TestAnAnswerThatProvesNothing:
    @pytest.fixture(autouse=True)
    def local_server(self, monkeypatch):
        monkeypatch.setenv("KILN_API_URL", "http://127.0.0.1:9")

    def test_an_answer_with_no_proof_cannot_raise_the_plan(self, plain_install):
        _sign_in(plain_install, tier="free", checked_ago_s=31 * 60)
        with _account_answers({"tier": "enterprise", "has_entitlement": True}) as get:
            assert account_plan.refresh_plan() is LicenseTier.FREE
            _is(LicenseTier.FREE)
        assert get.call_args.args[0].startswith("http://127.0.0.1:9")

    def test_an_answer_signed_by_another_key_cannot_raise_the_plan(self, plain_install):
        _sign_in(plain_install, tier="free", checked_ago_s=31 * 60)
        forged = _proof(key=STRANGER_KEY, tier="enterprise")
        with _account_answers({"tier": "enterprise", "plan_proof": forged}):
            assert account_plan.refresh_plan() is LicenseTier.FREE
            _is(LicenseTier.FREE)


class TestAMisplacedOrDamagedProof:
    def test_a_proof_issued_to_another_machine_reads_free(self, plain_install):
        _sign_in(
            plain_install,
            machine_id=MACHINE,
            plan_proof=_proof(machine="machine-bbbb"),
        )
        with _account_unreachable():
            _is(LicenseTier.FREE)

    def test_a_proof_issued_to_this_machine_counts(self, plain_install):
        _sign_in(plain_install, machine_id=MACHINE, plan_proof=_proof(machine=MACHINE))
        with _account_unreachable():
            _is(LicenseTier.BUSINESS)

    def test_a_failing_proof_is_free_even_inside_the_no_proof_window(
        self, plain_install, monkeypatch
    ):
        monkeypatch.setattr(
            plan_proof, "UNPROVEN_PLAN_HONOURED_UNTIL", time.time() + 30 * 24 * 3600
        )
        _sign_in(plain_install, plan_proof=_proof(account=OTHER_ACCOUNT))
        with _account_unreachable():
            _is(LicenseTier.FREE)

    def test_a_tampered_signature_reads_free(self, plain_install):
        proof = _proof()
        sig = bytearray(base64.b64decode(proof["sig"]))
        sig[0] ^= 0x01
        proof["sig"] = base64.b64encode(bytes(sig)).decode("ascii")
        _sign_in(plain_install, plan_proof=proof)
        with _account_unreachable():
            _is(LicenseTier.FREE)

    @pytest.mark.parametrize(
        "proof",
        [
            "not a dict",
            {"v": 1},
            {"v": 2, "account": ACCOUNT, "tier": "business"},
            {**_proof(), "sig": "%%%not base64%%%"},
            {**_proof(), "issued_at": "soon"},
            {**_proof(), "sig": base64.b64encode(b"short").decode()},
            [1, 2, 3],
        ],
    )
    def test_a_malformed_proof_never_raises(self, plain_install, proof):
        _sign_in(plain_install, plan_proof=proof)
        assert plan_proof.plan_is_proven(_saved(plain_install), "business", time.time()) is False
        with _account_unreachable():
            _is(LicenseTier.FREE)


class TestHardRules:
    def test_a_garbage_sign_in_file_reads_free_and_does_not_raise(self, plain_install):
        (plain_install / ".kiln").mkdir(exist_ok=True)
        _path(plain_install).write_bytes(b"\x00\xff not json {{{")
        with _account_unreachable():
            _is(LicenseTier.FREE)
        assert _path(plain_install).read_bytes() == b"\x00\xff not json {{{"

    def test_a_failed_proof_never_deletes_or_strips_the_sign_in(self, plain_install):
        _sign_in(plain_install, plan_proof=_proof(account=OTHER_ACCOUNT))
        before = _saved(plain_install)
        with _account_unreachable():
            _is(LicenseTier.FREE)
        after = _saved(plain_install)
        assert after["access_token"] == before["access_token"]
        assert after["refresh_token"] == before["refresh_token"]
        assert after["plan_proof"] == before["plan_proof"]

    def test_a_free_account_behaves_as_before(self, plain_install):
        _sign_in(plain_install, tier="free")
        with _account_answers({"tier": "free"}) as get:
            assert account_plan.get_tier() is LicenseTier.FREE
        get.assert_not_called()
        _sign_in(plain_install, tier="free", checked_ago_s=31 * 60)
        with _account_answers({"tier": "free"}) as get:
            assert account_plan.get_tier() is LicenseTier.FREE
        assert get.call_count == 1
