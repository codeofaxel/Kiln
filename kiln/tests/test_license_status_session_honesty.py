"""``license_status`` must not report a live licence off a dead sign-in.

When the tier is resolved from a paired session, the licence IS that
session. Once it can no longer be refreshed every hosted call fails —
so answering "Enterprise, valid" points the user at the wrong problem.
That is not hypothetical: on 2026-07-29 a cloud push failed repeatedly
while this tool reported a valid Enterprise licence, and the real fix
was a single ``kiln signin``.

The fix is deliberately narrow. It changes what a human is TOLD; it
does not touch entitlement (tier decisions route through
``check_tier`` / ``check_pro``), and it leaves an operator-supplied
licence key alone, because a key in the environment or on disk does not
depend on any session.
"""

from __future__ import annotations

import sys
import types

import pytest

from kiln.server import _annotate_session_liveness


#: ``SessionBearer`` guarantees an empty token for exactly these two.
_UNUSABLE = ("needs_signin", "signed_out")


def _install_session(monkeypatch, *, state: str, detail: str = "", raises: bool = False):
    """Stand in for ``kiln.auth_session.resolve_session_bearer``.

    Mirrors the real invariant — empty token iff the session cannot
    authenticate — rather than inventing one, so these tests fail if the
    helper ever goes back to matching on state names.
    """
    mod = types.ModuleType("kiln.auth_session")

    class _Bearer:
        def __init__(self) -> None:
            self.token = "" if state in _UNUSABLE else "x" * 40
            self.state = state
            self.detail = detail

    def resolve_session_bearer(*_a, **_k):
        if raises:
            raise RuntimeError("resolver exploded")
        return _Bearer()

    mod.resolve_session_bearer = resolve_session_bearer
    monkeypatch.setitem(sys.modules, "kiln.auth_session", mod)


EXPIRED = (
    "Your Kiln session for adam@kiln3d.com has expired and could not be "
    "refreshed. Run `python3 -m kiln signin` to sign in again."
)


def test_expired_session_is_not_reported_valid(monkeypatch):
    """The incident, pinned."""
    _install_session(monkeypatch, state="needs_signin", detail=EXPIRED)
    payload = {"source": "oauth", "tier": "enterprise", "is_valid": True}
    _annotate_session_liveness(payload)

    assert payload["is_valid"] is False, (
        "a lapsed sign-in was still reported as a valid licence"
    )
    assert payload["session_state"] == "needs_signin"
    assert payload["action_required"] == EXPIRED
    # The entitlement itself is still worth reporting — it is what the
    # account holds the moment they sign back in.
    assert payload["tier"] == "enterprise"


@pytest.mark.parametrize("state", ["live", "refreshed", "degraded"])
def test_a_working_session_is_never_called_invalid(monkeypatch, state):
    """Every state that still holds a token is a working session.

    ``refreshed`` is a session that just renewed itself and ``degraded``
    is one serving on offline grace — both authenticate fine. The first
    draft of this helper asked ``state == "live"`` and flagged a
    freshly-refreshed Enterprise session as invalid, which is the same
    lie as the bug it was written to fix, pointing the other way. Caught
    by running the real tool, not by any assertion here — hence this.
    """
    _install_session(monkeypatch, state=state)
    payload = {"source": "oauth", "tier": "pro", "is_valid": True}
    _annotate_session_liveness(payload)

    assert payload["is_valid"] is True, f"a {state!r} session was reported invalid"
    assert payload["session_state"] == state
    assert "action_required" not in payload


@pytest.mark.parametrize("source", ["env", "file", "default", None])
def test_an_operator_key_is_never_annotated(monkeypatch, source):
    """A key on disk or in the environment owes nothing to a session."""
    _install_session(monkeypatch, state="needs_signin", detail=EXPIRED)
    payload = {"source": source, "tier": "business", "is_valid": True}
    before = dict(payload)
    _annotate_session_liveness(payload)
    assert payload == before


def test_a_broken_resolver_leaves_the_report_intact(monkeypatch):
    """A diagnostic must never take down the thing it describes."""
    _install_session(monkeypatch, state="needs_signin", raises=True)
    payload = {"source": "oauth", "tier": "pro", "is_valid": True}
    _annotate_session_liveness(payload)
    assert payload["is_valid"] is True
    assert "session_state" not in payload


def test_missing_auth_session_module_degrades_quietly(monkeypatch):
    """Older kiln builds have no auth_session; the report still returns."""
    import builtins

    real_import = builtins.__import__

    def _no_auth_session(name, *a, **k):
        if name == "kiln.auth_session":
            raise ImportError("no auth_session in this build")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", _no_auth_session)
    payload = {"source": "oauth", "tier": "pro", "is_valid": True}
    _annotate_session_liveness(payload)
    assert payload["is_valid"] is True


def test_a_state_with_no_detail_still_says_what_to_do(monkeypatch):
    """Never leave the user with a false report and no next step."""
    _install_session(monkeypatch, state="signed_out", detail="")
    payload = {"source": "oauth", "tier": "pro", "is_valid": True}
    _annotate_session_liveness(payload)
    assert payload["is_valid"] is False
    assert "signin" in payload["action_required"]
